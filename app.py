import asyncio
import json
import os
import random
import secrets
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ForceReply,
    KeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButtonRequestUsers,
)
from telegram.ext import (Application, CallbackContext, CallbackQueryHandler,
                          CommandHandler, MessageHandler, ContextTypes,
                          filters)

# --- Utilities --------------------------------------------------------------

DICT_PATH = Path(__file__).with_name("nouns_ru_pymorphy2_yaspeller.jsonl")

logger = logging.getLogger(__name__)

def normalize_word(word: str) -> str:
    """Normalize words: lowercase and replace ё with е."""
    return word.lower().replace("ё", "е")


# Load dictionary at startup
DICT: Set[str] = set()
for line in DICT_PATH.read_text(encoding="utf-8").splitlines():
    try:
        data = json.loads(line)
        DICT.add(normalize_word(data["word"]))
    except Exception:
        continue


def is_cyrillic(word: str) -> bool:
    return all("а" <= ch <= "я" or ch == "ё" for ch in word.lower())


def can_make(word: str, letters: Counter) -> bool:
    c = Counter(word)
    for k, v in c.items():
        if letters.get(k, 0) < v:
            return False
    return True


# --- Data classes ----------------------------------------------------------

@dataclass
class Player:
    user_id: int
    name: str = ""
    words: List[str] = field(default_factory=list)
    points: int = 0


@dataclass
class GameState:
    host_id: int
    time_limit: int = 3
    base_word: str = ""
    letters: Counter = field(default_factory=Counter)
    players: Dict[int, Player] = field(default_factory=dict)
    used_words: Set[str] = field(default_factory=set)
    status: str = "config"  # config | waiting | running | finished
    jobs: Dict[str, any] = field(default_factory=dict)
    base_msg_id: Optional[int] = None
    invited_users: Set[int] = field(default_factory=set)


ACTIVE_GAMES: Dict[int, GameState] = {}
JOIN_CODES: Dict[str, int] = {}


async def refresh_base_button(chat_id: int, context: CallbackContext) -> None:
    """Resend base word button to keep it the last message."""
    game = ACTIVE_GAMES.get(chat_id)
    if not game or game.status != "running" or not game.base_word:
        return
    if game.base_msg_id:
        try:
            await context.bot.delete_message(chat_id, game.base_msg_id)
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id,
        "\u2060",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(game.base_word.upper(), callback_data="noop")]]
        ),
    )
    game.base_msg_id = msg.message_id


async def send_game_message(chat_id: int, context: CallbackContext, text: str, **kwargs):
    msg = await context.bot.send_message(chat_id, text, **kwargs)
    await refresh_base_button(chat_id, context)
    return msg


async def reply_game_message(message, context: CallbackContext, text: str, **kwargs):
    msg = await message.reply_text(text, **kwargs)
    await refresh_base_button(message.chat_id, context)
    return msg


# --- FastAPI & PTB integration ---------------------------------------------

app = FastAPI()
APPLICATION: Optional[Application] = None
BOT_USERNAME: Optional[str] = None

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex())
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = context.args[0] if context.args else None
    if code and code in JOIN_CODES:
        context.user_data["join_chat"] = JOIN_CODES[code]
        await reply_game_message(
            update.message,
            context,
            "Вы можете присоединиться к игре. Зайдите в чат и нажмите кнопку 'Присоединиться'.",
        )
    else:
        await reply_game_message(update.message, context, "Привет! Используйте /newgame в групповом чате.")


async def request_name(user_id: int, chat_id: int, context: CallbackContext) -> None:
    await send_game_message(
        chat_id,
        context,
        "Введите ваше имя",
        reply_markup=ForceReply(selective=True),
    )


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    game = ACTIVE_GAMES.get(chat_id)
    if game and game.status in {"waiting", "running"}:
        await reply_game_message(update.message, context, "Игра уже запущена.")
        return

    game = GameState(host_id=user_id)
    ACTIVE_GAMES[chat_id] = game
    game.players[user_id] = Player(user_id=user_id)

    await request_name(user_id, chat_id, context)


async def maybe_show_base_options(chat_id: int, context: CallbackContext) -> None:
    """Send base word options to the host when conditions are met."""
    game = ACTIVE_GAMES.get(chat_id)
    if not game or game.status != "waiting":
        return
    if len(game.players) >= 2 and all(p.name for p in game.players.values()):
        await context.bot.send_message(
            game.host_id,
            "Выберите базовое слово:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Вручную", callback_data="base_manual"),
                        InlineKeyboardButton("Случайное", callback_data="base_random"),
                    ]
                ]
            ),
        )


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    player = game.players.get(user_id)
    if player and not player.name:
        player.name = update.message.text.strip()
        await reply_game_message(update.message, context, f"Имя установлено: {player.name}")
        if user_id == game.host_id and game.status == "config":
            buttons = [
                [
                    InlineKeyboardButton("3 минуты", callback_data="time_3"),
                    InlineKeyboardButton("5 минут", callback_data="time_5"),
                ]
            ]
            if user_id == ADMIN_ID:
                buttons.append([InlineKeyboardButton("[адм.] Тест", callback_data="adm_test")])
            await reply_game_message(
                update.message,
                context,
                "Выберите длительность игры:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        await maybe_show_base_options(chat_id, context)


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or query.from_user.id != game.host_id:
        return
    if query.data.startswith("time_"):
        game.time_limit = int(query.data.split("_")[1])
        game.status = "waiting"
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = chat_id
        await query.edit_message_text("Игра создана. Пригласите участников.")
        buttons = [
            [
                InlineKeyboardButton("Пригласить из контактов", callback_data="invite_contacts"),
                InlineKeyboardButton("Создать ссылку", callback_data="invite_link"),
            ],
            [InlineKeyboardButton("Присоединиться", callback_data="join")],
        ]
        await reply_game_message(
            query.message,
            context,
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif query.data == "adm_test" and query.from_user.id == ADMIN_ID:
        game.time_limit = 3
        game.status = "running"
        game.base_word = random.choice([w for w in DICT if len(w) >= 8])
        game.letters = Counter(game.base_word)
        bot_player = Player(user_id=0, name="Bot")
        game.players[0] = bot_player
        await query.edit_message_text("Тестовая игра началась")
        await start_game(chat_id, context)
        context.job_queue.run_repeating(bot_move, 30, chat_id=chat_id, name=f"bot_{chat_id}")


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    user_id = update.effective_user.id
    if user_id not in game.players:
        game.players[user_id] = Player(user_id=user_id)
        await reply_game_message(
            update.message,
            context,
            "Добро пожаловать! Введите ваше имя:",
            reply_markup=ForceReply(selective=True),
        )
    else:
        await reply_game_message(update.message, context, "Вы уже в игре")


async def join_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    user_id = query.from_user.id
    if user_id not in game.players:
        if len(game.players) < 5:
            game.players[user_id] = Player(user_id=user_id)
            await reply_game_message(
                query.message,
                context,
                "Добро пожаловать! Введите ваше имя:",
                reply_markup=ForceReply(selective=True),
            )
        else:
            await reply_game_message(query.message, context, "Лобби заполнено")
    else:
        await reply_game_message(query.message, context, "Вы уже в игре")


async def invite_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    button = KeyboardButton(
        text="Выбрать из контактов",
        request_users=KeyboardButtonRequestUsers(request_id=1),
    )
    await reply_game_message(
        query.message,
        context,
        "Выберите контакт:",
        reply_markup=ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True),
    )


async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    code = next((c for c, cid in JOIN_CODES.items() if cid == chat_id), None)
    if not code:
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = chat_id
    await reply_game_message(
        query.message,
        context,
        f"Ссылка приглашения: https://t.me/{BOT_USERNAME}?start={code}",
    )


async def users_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.users_shared:
        return
    shared = message.users_shared
    chat_id = update.effective_chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    code = next((c for c, cid in JOIN_CODES.items() if cid == chat_id), None)
    if not code:
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = chat_id
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    for u in shared.users:
        try:
            await context.bot.send_message(u.user_id, f"Приглашение в игру: {link}")
            game.invited_users.add(u.user_id)
        except Exception:
            continue
    await reply_game_message(message, context, "Приглашения отправлены")


async def base_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return

    # Only the host may request random/manual base word options
    if query.data in {"base_manual", "base_random"} and query.from_user.id != game.host_id:
        return

    if query.data == "base_manual":
        await reply_game_message(query.message, context, "Введите базовое слово (>=8 букв):", reply_markup=ForceReply())
    elif query.data == "base_random":
        candidates = [w for w in DICT if len(w) >= 8]
        if game.time_limit >= 5:
            candidates = [w for w in candidates if len(w) >= 10]
        if len(game.players) >= 3:
            candidates = [w for w in candidates if len(w) >= 9]
        words = random.sample(candidates, 3)
        buttons = [[InlineKeyboardButton(w, callback_data=f"pick_{w}")] for w in words]
        await reply_game_message(
            query.message,
            context,
            "Выберите слово:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        game.jobs["rand"] = context.job_queue.run_once(
            finish_random, 5, chat_id=chat_id, data=words, name=f"rand_{chat_id}"
        )
        game.jobs["count"] = context.job_queue.run_repeating(
            countdown,
            1,
            count=5,
            chat_id=chat_id,
            data={"remaining": 5},
            name=f"cnt_{chat_id}",
        )

    elif query.data.startswith("pick_"):
        if game.base_word:
            return
        word = query.data.split("_", 1)[1]
        if "rand" in game.jobs:
            job = game.jobs.pop("rand")
            job.schedule_removal()
        if "count" in game.jobs:
            job = game.jobs.pop("count")
            job.schedule_removal()
        player = game.players.get(query.from_user.id)
        chosen_by = player.name if player and player.name else query.from_user.full_name
        await set_base_word(chat_id, word, context, chosen_by=chosen_by)


async def finish_random(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or game.base_word:
        return
    if "count" in game.jobs:
        job = game.jobs.pop("count")
        job.schedule_removal()
    game.jobs.pop("rand", None)
    word = random.choice(context.job.data)
    await set_base_word(chat_id, word, context)


async def countdown(context: CallbackContext) -> None:
    """Send or edit a message with countdown numbers."""
    chat_id = context.job.chat_id
    data = context.job.data
    remaining = data.get("remaining", 0)
    if remaining <= 0:
        return

    msg_id = data.get("message_id")
    try:
        if msg_id:
            await context.bot.edit_message_text(str(remaining), chat_id, msg_id)
        else:
            msg = await send_game_message(chat_id, context, str(remaining))
            data["message_id"] = msg.message_id
    except Exception:
        # If editing fails (message deleted), send a new one
        msg = await send_game_message(chat_id, context, str(remaining))
        data["message_id"] = msg.message_id

    data["remaining"] = remaining - 1


def schedule_jobs(chat_id: int, context: CallbackContext, game: GameState) -> None:
    warn = context.job_queue.run_once(warn_time, (game.time_limit - 1) * 60, chat_id=chat_id, name=f"warn_{chat_id}")
    end = context.job_queue.run_once(end_game, game.time_limit * 60, chat_id=chat_id, name=f"end_{chat_id}")
    game.jobs["warn"] = warn
    game.jobs["end"] = end


async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or query.from_user.id != game.host_id or not game.base_word:
        return
    await query.edit_message_text("Игра начинается!")
    await start_game(chat_id, context)


async def start_game(chat_id: int, context: CallbackContext) -> None:
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    game.status = "running"
    await send_game_message(chat_id, context, f"Исходное слово: {game.base_word.upper()}")
    schedule_jobs(chat_id, context, game)


async def set_base_word(chat_id: int, word: str, context: CallbackContext, chosen_by: Optional[str] = None) -> None:
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    game.base_word = normalize_word(word)
    game.letters = Counter(game.base_word)
    message = (
        f"{chosen_by} выбрал слово {game.base_word}"
        if chosen_by
        else f"Выбрано слово: {game.base_word}"
    )
    await send_game_message(chat_id, context, message)
    await send_game_message(
        chat_id,
        context,
        "Нажмите Старт, когда будете готовы",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Старт", callback_data="start")]]),
    )


async def warn_time(context: CallbackContext) -> None:
    await send_game_message(context.job.chat_id, context, "Осталась 1 минута!")


async def end_game(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    game.status = "finished"
    if game.base_msg_id:
        try:
            await context.bot.delete_message(chat_id, game.base_msg_id)
        except Exception:
            pass
        game.base_msg_id = None
    scores = [(p.name or str(uid), p.points) for uid, p in game.players.items()]
    scores.sort(key=lambda x: x[1], reverse=True)
    lines = [f"{name}: {pts}" for name, pts in scores]
    await send_game_message(chat_id, context, "Игра окончена!\n" + "\n".join(lines))
    await send_game_message(chat_id, context, "Новая игра с теми же участниками?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Да", callback_data="restart_yes"), InlineKeyboardButton("Нет", callback_data="restart_no")]
    ]))


def reset_game(game: GameState) -> None:
    for p in game.players.values():
        p.words.clear()
        p.points = 0
    game.used_words.clear()
    game.base_word = ""
    game.letters.clear()
    game.status = "config"
    for job in game.jobs.values():
        job.schedule_removal()
    game.jobs.clear()
    game.base_msg_id = None


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game:
        return
    if query.data == "restart_yes":
        reset_game(game)
        game.status = "waiting"
        await query.edit_message_text("Игра перезапущена.")
        await maybe_show_base_options(chat_id, context)
    else:
        del ACTIVE_GAMES[chat_id]
        await query.edit_message_text("Игра завершена. Для новой игры с новыми участниками нажмите /start")


async def word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or game.status != "running":
        return
    player = game.players.get(user_id)
    if not player:
        return
    words = [normalize_word(w) for w in update.message.text.split()]
    responses = []
    for w in words:
        if not is_cyrillic(w) or len(w) < 3:
            continue
        if w in game.used_words:
            continue
        if not can_make(w, game.letters):
            continue
        if w not in DICT:
            continue
        game.used_words.add(w)
        player.words.append(w)
        pts = 2 if len(w) >= 6 else 1
        player.points += pts
        responses.append(f"{w} (+{pts})")
    if responses:
        await reply_game_message(update.message, context, "Зачтено: " + ", ".join(responses))


async def manual_base_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or user_id != game.host_id or game.base_word:
        return
    word = normalize_word(update.message.text)
    if len(word) < 8 or word not in DICT:
        await reply_game_message(update.message, context, "Неверное слово")
        return
    player = game.players.get(user_id)
    chosen_by = player.name if player and player.name else update.effective_user.full_name
    await set_base_word(chat_id, word, context, chosen_by=chosen_by)


async def bot_move(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    game = ACTIVE_GAMES.get(chat_id)
    if not game or game.status != "running":
        return
    available = [w for w in DICT if len(w) >= 3 and can_make(w, game.letters) and w not in game.used_words]
    if not available:
        return
    word = random.choice(available)
    update = Update(update_id=0, message=None)
    bot_player = game.players.get(0)
    if bot_player:
        bot_player.words.append(word)
        pts = 2 if len(word) >= 6 else 1
        bot_player.points += pts
        game.used_words.add(word)
        await send_game_message(chat_id, context, f"Bot: {word}")


async def webhook_check(context: CallbackContext) -> None:
    info = await context.bot.get_webhook_info()
    if not info.url:
        print("Webhook missing")


@app.on_event("startup")
async def on_startup() -> None:
    global APPLICATION, BOT_USERNAME
    APPLICATION = Application.builder().token(TOKEN).build()
    BOT_USERNAME = (await APPLICATION.bot.get_me()).username
    APPLICATION.add_handler(CommandHandler("start", start_cmd))
    APPLICATION.add_handler(CommandHandler("newgame", newgame))
    APPLICATION.add_handler(CommandHandler("join", join_cmd))
    APPLICATION.add_handler(MessageHandler(filters.REPLY & filters.TEXT & (~filters.COMMAND), handle_name))
    APPLICATION.add_handler(CallbackQueryHandler(time_selected, pattern="^(time_|adm_test)"))
    APPLICATION.add_handler(CallbackQueryHandler(join_button, pattern="^join$"))
    APPLICATION.add_handler(CallbackQueryHandler(invite_contacts, pattern="^invite_contacts$"))
    APPLICATION.add_handler(CallbackQueryHandler(invite_link, pattern="^invite_link$"))
    APPLICATION.add_handler(CallbackQueryHandler(base_choice, pattern="^(base_|pick_)", block=False))
    APPLICATION.add_handler(CallbackQueryHandler(start_button, pattern="^start$"))
    APPLICATION.add_handler(CallbackQueryHandler(restart_handler, pattern="^restart_"))
    APPLICATION.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared_handler))
    APPLICATION.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), manual_base_word))
    APPLICATION.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), word_message))
    await APPLICATION.initialize()
    await APPLICATION.start()
    if APPLICATION.job_queue:
        APPLICATION.job_queue.run_repeating(webhook_check, 600, name="webhook_check")
    else:
        logger.warning("Job queue is disabled; periodic webhook check will not run")

    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
        info = await APPLICATION.bot.get_webhook_info()
        if info.url != webhook_url:
            await APPLICATION.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await APPLICATION.stop()
    await APPLICATION.shutdown()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> JSONResponse:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    data = await request.json()
    update = Update.de_json(data, APPLICATION.bot)
    await APPLICATION.process_update(update)
    return JSONResponse({"ok": True})


@app.get("/set_webhook")
async def set_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
    return JSONResponse({"url": webhook_url})


@app.get("/")
async def root() -> JSONResponse:
    """Base endpoint with brief service info."""
    return JSONResponse({"message": "Wordgame Magic service. See /healthz for status."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})

