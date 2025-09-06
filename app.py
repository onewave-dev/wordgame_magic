import asyncio
import json
import os
import random
import secrets
import logging
import html
from time import perf_counter
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, Set, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    KeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButtonRequestUsers,
    User,
)
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# --- Utilities --------------------------------------------------------------

DICT_PATH = Path(__file__).with_name("nouns_ru_pymorphy2_yaspeller.jsonl")
WHITELIST_PATH = Path(__file__).with_name("whitelist.jsonl")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

def normalize_word(word: str) -> str:
    """Normalize words: lowercase and replace ё with е."""
    return word.lower().replace("ё", "е")


def bold_alnum(text: str) -> str:
    """Wrap alphanumeric characters in bold tags for HTML parse mode."""
    return "".join(
        f"<b>{html.escape(ch)}</b>" if ch.isalnum() else html.escape(ch)
        for ch in text
    )


# Load dictionary at startup (main + whitelist)
DICT: Set[str] = set()
for path in (DICT_PATH, WHITELIST_PATH):
    if not path.exists():
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
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
    game_id: str
    time_limit: float = 3
    base_word: str = ""
    letters: Counter = field(default_factory=Counter)
    players: Dict[int, Player] = field(default_factory=dict)
    player_chats: Dict[int, int] = field(default_factory=dict)
    used_words: Set[str] = field(default_factory=set)
    status: str = "config"  # config | waiting | running | finished
    jobs: Dict[str, any] = field(default_factory=dict)
    invited_users: Set[int] = field(default_factory=set)


ACTIVE_GAMES: Dict[str, GameState] = {}
JOIN_CODES: Dict[str, str] = {}
BASE_MSG_IDS: Dict[str, int] = {}
LAST_REFRESH: Dict[Tuple[int, int], float] = {}


def get_game(chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
    """Retrieve a game by chat identifier."""
    for g in ACTIVE_GAMES.values():
        if chat_id in g.player_chats.values():
            return g
    return None


async def broadcast(game_id: str, text: str, reply_markup=None, parse_mode=None) -> None:
    """Send a message to all player chats."""
    if not APPLICATION:
        return
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        return
    sent: Set[int] = set()
    for cid in game.player_chats.values():
        if cid in sent:
            continue
        try:
            await APPLICATION.bot.send_message(
                cid, text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except TelegramError:
            pass
        sent.add(cid)


async def refresh_base_button(chat_id: int, thread_id: int, context: CallbackContext) -> None:
    """Resend base word button to keep it the last message."""
    game = get_game(chat_id, thread_id)
    if not game or game.status != "running" or not game.base_word:
        return
    text = "Собирайте слова из букв базового слова:"
    msg_id = BASE_MSG_IDS.get(game.game_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(game.base_word.upper(), callback_data="noop")]]
        ),
        message_thread_id=thread_id,
    )
    BASE_MSG_IDS[game.game_id] = msg.message_id


def schedule_refresh_base_button(chat_id: int, thread_id: int, context: CallbackContext) -> None:
    """Throttle refresh of the base word button to avoid blocking."""
    now = asyncio.get_event_loop().time()
    key = (chat_id, thread_id or 0)
    last = LAST_REFRESH.get(key, 0)
    if now - last < 1:
        return
    LAST_REFRESH[key] = now
    asyncio.create_task(refresh_base_button(chat_id, thread_id or 0, context))


async def send_game_message(chat_id: int, thread_id: Optional[int], context: CallbackContext, text: str, **kwargs):
    if thread_id is None:
        msg = await context.bot.send_message(chat_id, text, **kwargs)
    else:
        msg = await context.bot.send_message(chat_id, text, message_thread_id=thread_id, **kwargs)
    schedule_refresh_base_button(chat_id, thread_id or 0, context)
    return msg


async def reply_game_message(message, context: CallbackContext, text: str, **kwargs):
    msg = await message.reply_text(text, **kwargs)
    schedule_refresh_base_button(message.chat_id, message.message_thread_id or 0, context)
    return msg


# --- Tap logger -------------------------------------------------------------

async def _tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lightweight logger for every update that reaches PTB."""
    msg = getattr(update, "message", None)
    if msg:
        logger.debug(
            "TAP message: chat_id=%s type=%s thread=%s user=%s text=%r",
            msg.chat.id, msg.chat.type, msg.message_thread_id,
            (msg.from_user.id if msg.from_user else None),
            (msg.text if msg.text else None),
        )
    cq = getattr(update, "callback_query", None)
    if cq and cq.message:
        logger.debug(
            "TAP callback: chat_id=%s thread=%s from=%s data=%r",
            cq.message.chat.id, cq.message.message_thread_id,
            (cq.from_user.id if cq.from_user else None), cq.data,
        )


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
        gid = JOIN_CODES[code]
        game = ACTIVE_GAMES.get(gid)
        if game:
            await add_player_via_invite(update.effective_user, game, context)
        else:
            await reply_game_message(update.message, context, "Игра не найдена")
        return
    user = update.effective_user
    game = create_dm_game(user.id)
    if update.message:
        await reply_game_message(update.message, context, f"Игра #{game.game_id} создана")
    await request_name(user.id, update.effective_chat.id, context)


async def request_name(user_id: int, chat_id: int, context: CallbackContext) -> None:
    await send_game_message(
        chat_id,
        None,
        context,
        "Введите ваше имя",
    )


def create_dm_game(host_id: int) -> GameState:
    """Create a direct-message game for the host."""
    game_id = secrets.token_urlsafe(8)
    game = GameState(host_id=host_id, game_id=game_id)
    game.players[host_id] = Player(user_id=host_id)
    game.player_chats[host_id] = host_id
    ACTIVE_GAMES[game_id] = game
    return game


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    game = create_dm_game(user.id)
    chat_id = update.effective_chat.id
    if update.message:
        await reply_game_message(update.message, context, f"Игра #{game.game_id} создана")
    await request_name(user.id, chat_id, context)


async def maybe_show_base_options(
    chat_id: int,
    thread_id: Optional[int],
    context: CallbackContext,
    game: Optional[GameState] = None,
) -> None:
    """Send base word options to the host when conditions are met."""
    if game is None:
        game = get_game(chat_id, thread_id or 0)
    if not game or game.status != "waiting":
        return
    if len(game.players) >= 2 and all(p.name for p in game.players.values()):
        await send_game_message(
            chat_id,
            thread_id,
            context,
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
    chat = update.effective_chat
    chat_id = chat.id
    user_id = update.effective_user.id
    game = get_game(chat_id, None)
    if not game:
        return
    game.player_chats[user_id] = chat.id
    player = game.players.get(user_id)
    if player and player.name:
        return
    name = update.message.text.strip()
    if not player:
        if len(game.players) >= 5:
            await reply_game_message(update.message, context, "Лобби заполнено")
            raise ApplicationHandlerStop
        player = Player(user_id=user_id, name=name)
        game.players[user_id] = player
        context.user_data["name"] = name
        await reply_game_message(update.message, context, f"Имя установлено: {player.name}")
        await broadcast(
            game.game_id,
            f"{bold_alnum(player.name)} присоединился к игре",
            parse_mode="HTML",
        )
        host_chat = game.player_chats.get(game.host_id)
        if host_chat:
            await maybe_show_base_options(host_chat, None, context, game)
        raise ApplicationHandlerStop
    elif not player.name:
        player.name = name
        context.user_data["name"] = name
        await reply_game_message(update.message, context, f"Имя установлено: {player.name}")
        await broadcast(
            game.game_id,
            f"{bold_alnum(player.name)} присоединился к игре",
            parse_mode="HTML",
        )
        if user_id == game.host_id and game.status == "config":
            buttons = [
                [
                    InlineKeyboardButton("3 минуты", callback_data="time_3"),
                    InlineKeyboardButton("5 минут", callback_data="time_5"),
                ]
            ]
            if user_id == ADMIN_ID:
                buttons.append([InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")])
            await reply_game_message(
                update.message,
                context,
                "Выберите длительность игры:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        host_chat = game.player_chats.get(game.host_id)
        if host_chat:
            await maybe_show_base_options(host_chat, None, context, game)
        raise ApplicationHandlerStop


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    chat_id = chat.id
    thread_id = query.message.message_thread_id
    if query.data == "adm_test" and query.from_user.id == ADMIN_ID:
        game = create_dm_game(query.from_user.id)
        game.players[query.from_user.id].name = context.user_data.get("name", "")
        game.time_limit = 1.5
        game.players[0] = Player(user_id=0, name="Бот")
        game.status = "waiting"
        await query.edit_message_text("Тестовая игра создана")
        await maybe_show_base_options(chat_id, thread_id, context, game)
        return

    game = get_game(chat_id, thread_id or 0)
    if not game and chat.type == "private":
        game = create_dm_game(query.from_user.id)
        await request_name(query.from_user.id, chat_id, context)
        return
    if not game or query.from_user.id != game.host_id:
        return
    player = game.players.get(query.from_user.id)
    if not player or not player.name:
        await request_name(query.from_user.id, chat_id, context)
        return
    if query.data.startswith("time_"):
        game.time_limit = int(query.data.split("_")[1])
        game.status = "waiting"
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = game.game_id
        await query.edit_message_text("Игра создана. Пригласите участников.")
        keyboard = ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(
                        text="Пригласить из контактов",
                        request_users=KeyboardButtonRequestUsers(request_id=1),
                    ),
                    KeyboardButton(text="Создать ссылку"),
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await reply_game_message(
            query.message,
            context,
            "Выберите способ приглашения:",
            reply_markup=keyboard,
        )


async def add_player_via_invite(
    user: User,
    game: GameState,
    context: CallbackContext,
) -> None:
    """Helper to add a player to a game via an invite link or code."""
    user_id = user.id
    if user_id in game.players:
        await context.bot.send_message(user_id, "Вы уже в игре")
        return
    if len(game.players) >= 5:
        await context.bot.send_message(user_id, "Лобби заполнено")
        return
    game.players[user_id] = Player(user_id=user_id)
    game.player_chats[user_id] = user_id
    await context.bot.send_message(user_id, "Введите ваше имя")


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await reply_game_message(update.message, context, "Укажите код приглашения")
        return
    join_code = args[0]
    game_id = JOIN_CODES.get(join_code)
    if not game_id:
        await reply_game_message(update.message, context, "Неверный код")
        return
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        await reply_game_message(update.message, context, "Игра не найдена")
        return
    await add_player_via_invite(update.effective_user, game, context)


async def join_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    join_code = data.split("_", 1)[1] if "_" in data else ""
    game_id = JOIN_CODES.get(join_code)
    if not game_id:
        return
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        return
    await add_player_via_invite(query.from_user, game, context)


async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    code = next((c for c, gid in JOIN_CODES.items() if gid == game.game_id), None)
    if not code:
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = game.game_id
    await reply_game_message(
        message,
        context,
        f"Ссылка приглашения: https://t.me/{BOT_USERNAME}?start={code}",
    )


async def users_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.users_shared:
        return
    shared = message.users_shared
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    code = next((c for c, gid in JOIN_CODES.items() if gid == game.game_id), None)
    if not code:
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = game.game_id
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    for u in shared.users:
        try:
            await context.bot.send_message(u.user_id, f"Приглашение в игру: {link}")
            game.invited_users.add(u.user_id)
        except Exception:
            continue
    await reply_game_message(message, context, "Приглашения отправлены")

async def chat_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"ℹ️ Chat ID: `{chat.id}`\n"
        f"Название: {chat.title if chat.title else '—'}\n"
        f"Тип: {chat.type}"
    )
    
async def quit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    game = get_game(chat_id, None)
    if not game:
        await reply_game_message(update.message, context, "Игра не запущена")
        return
    user_id = update.effective_user.id
    if user_id != game.host_id:
        await reply_game_message(update.message, context, "Только инициатор может прервать игру")
        return
    for job in list(game.jobs.values()):
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.jobs.clear()
    msg_id = BASE_MSG_IDS.get(game.game_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    await reply_game_message(update.message, context, "Игра прервана")
    await broadcast(game.game_id, "Игра прервана")
    BASE_MSG_IDS.pop(game.game_id, None)
    ACTIVE_GAMES.pop(game.game_id, None)


async def base_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    player = game.players.get(query.from_user.id)
    if not player or not player.name:
        await request_name(query.from_user.id, chat_id, context)
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
        markup = InlineKeyboardMarkup(buttons)

        sent_messages: List[Tuple[int, int]] = []
        count_jobs: List[Any] = []
        for cid in set(game.player_chats.values()):
            thread = thread_id if cid == chat_id else None
            msg = await send_game_message(
                cid,
                thread,
                context,
                "Выберите слово:",
                reply_markup=markup,
            )
            sent_messages.append((cid, msg.message_id))
            job = context.job_queue.run_repeating(
                countdown,
                interval=1,
                chat_id=cid,
                data={
                    "thread_id": thread,
                    "remaining": 5,
                    "message_id": msg.message_id,
                    "reply_markup": markup,
                },
                name=f"cnt_{cid}_{thread or 0}",
            )
            count_jobs.append(job)

        game.jobs["rand_msgs"] = sent_messages
        game.jobs["count"] = count_jobs
        game.jobs["rand"] = context.job_queue.run_once(
            finish_random,
            5,
            chat_id=chat_id,
            data={"thread_id": thread_id, "words": words},
            name=f"rand_{chat_id}_{thread_id}",
        )

    elif query.data.startswith("pick_"):
        if game.base_word:
            return
        word = query.data.split("_", 1)[1]
        if "rand" in game.jobs:
            job = game.jobs.pop("rand")
            job.schedule_removal()
        for job in game.jobs.pop("count", []):
            try:
                job.schedule_removal()
            except Exception:
                pass
        await set_base_word(chat_id, thread_id, word, context, chosen_by=player.name)


async def finish_random(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id or 0)
    if not game or game.base_word:
        return
    for job in game.jobs.pop("count", []):
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.jobs.pop("rand", None)
    word = random.choice(data.get("words", []))
    await set_base_word(chat_id, thread_id, word, context)


async def countdown(context: CallbackContext) -> None:
    """Send or edit a message with countdown numbers."""
    chat_id = context.job.chat_id
    data = context.job.data
    thread_id = data.get("thread_id")
    remaining = data.get("remaining", 0)
    if remaining <= 0:
        context.job.schedule_removal()
        return

    msg_id = data.get("message_id")
    markup = data.get("reply_markup")
    text = f"⏱️ <b>{remaining}</b>"
    try:
        if msg_id:
            await context.bot.edit_message_text(
                text,
                chat_id,
                msg_id,
                message_thread_id=thread_id,
                reply_markup=markup,
                parse_mode="HTML",
            )
            data["message_id"] = msg_id
        else:
            msg = await send_game_message(
                chat_id,
                thread_id,
                context,
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            data["message_id"] = msg.message_id
    except TelegramError as e:
        if "message to edit not found" in str(e).lower():
            msg = await send_game_message(
                chat_id,
                thread_id,
                context,
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            data["message_id"] = msg.message_id
        else:
            raise

    data["remaining"] = remaining - 1


def schedule_jobs(chat_id: int, thread_id: int, context: CallbackContext, game: GameState) -> None:
    warn = context.job_queue.run_once(
        warn_time,
        (game.time_limit - 1) * 60,
        chat_id=chat_id,
        data={"thread_id": thread_id},
        name=f"warn_{chat_id}_{thread_id}",
    )
    end = context.job_queue.run_once(
        end_game,
        game.time_limit * 60,
        chat_id=chat_id,
        data={"thread_id": thread_id},
        name=f"end_{chat_id}_{thread_id}",
    )
    game.jobs["warn"] = warn
    game.jobs["end"] = end


async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game or query.from_user.id != game.host_id or not game.base_word:
        return
    await query.edit_message_text("Игра начинается!")
    await start_game(chat_id, thread_id, context)


async def start_game(chat_id: int, thread_id: int, context: CallbackContext) -> None:
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    game.status = "running"
    await broadcast(game.game_id, f"Исходное слово: {game.base_word.upper()}")
    schedule_refresh_base_button(chat_id, thread_id, context)
    schedule_jobs(chat_id, thread_id, context, game)
    if 0 in game.players:
        game.jobs["bot"] = context.job_queue.run_repeating(
            bot_move,
            30,
            chat_id=chat_id,
            data={"thread_id": thread_id},
            name=f"bot_{chat_id}_{thread_id}",
        )


async def set_base_word(chat_id: int, thread_id: int, word: str, context: CallbackContext, chosen_by: Optional[str] = None) -> None:
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    game.base_word = normalize_word(word)
    game.letters = Counter(game.base_word)
    message = (
        f"{bold_alnum(chosen_by)} выбрал слово {html.escape(game.base_word)}"
        if chosen_by
        else f"Выбрано слово: {html.escape(game.base_word)}"
    )
    await broadcast(game.game_id, message, parse_mode="HTML")
    await broadcast(
        game.game_id,
        "Нажмите Старт, когда будете готовы",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Старт", callback_data="start")]]),
    )


async def warn_time(context: CallbackContext) -> None:
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(context.job.chat_id, thread_id or 0)
    if game:
        await broadcast(game.game_id, "Осталась 1 минута!")


async def end_game(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    if any(not p.name for p in game.players.values()):
        for p in game.players.values():
            if not p.name:
                chat = game.player_chats.get(p.user_id, chat_id)
                await request_name(p.user_id, chat, context)
        return
    game.status = "finished"
    msg_id = BASE_MSG_IDS.get(game.game_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    players_sorted = sorted(game.players.values(), key=lambda p: p.points, reverse=True)

    def format_name(player: Player) -> str:
        name = player.name
        if player.user_id == 0 or name.lower() in {"bot", "бот"}:
            name = f"🤖 {name}"
        return name

    max_score = players_sorted[0].points if players_sorted else 0
    winners = [p for p in players_sorted if p.points == max_score]

    lines = [
        "<b>Игра окончена!</b>",
        "<b>Результаты:</b>",
        "",
        f"<b>Слово:</b> {html.escape(game.base_word.upper())}",
        "",
    ]
    for p in players_sorted:
        lines.append(html.escape(format_name(p)))
        for i, w in enumerate(p.words, 1):
            pts = 2 if len(w) >= 6 else 1
            lines.append(f"{i}. {html.escape(w)} — {pts}")
        lines.append(f"<b>Результат:</b> {p.points}")
        lines.append("")

    if winners:
        if len(winners) == 1:
            lines.append(
                f"🏆 <b>Победитель:</b> {html.escape(format_name(winners[0]))}"
            )
        else:
            lines.append(
                "🏆 <b>Победители:</b> "
                + ", ".join(html.escape(format_name(p)) for p in winners)
            )
    message = "\n".join(lines).rstrip()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Новая игра с теми же участниками", callback_data="restart_yes"
                )
            ],
            [
                InlineKeyboardButton(
                    "Новая игра с другими участниками", callback_data="restart_no"
                )
            ],
        ]
    )
    await broadcast(
        game.game_id, message, reply_markup=keyboard, parse_mode="HTML"
    )
    for job in list(game.jobs.values()):
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.jobs.clear()


def reset_game(game: GameState) -> None:
    for p in game.players.values():
        p.words.clear()
        p.points = 0
    game.used_words.clear()
    game.base_word = ""
    game.letters.clear()
    game.status = "config"
    for job in list(game.jobs.values()):
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.jobs.clear()
    BASE_MSG_IDS.pop(game.game_id, None)


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    if query.data == "restart_yes":
        reset_game(game)
        BASE_MSG_IDS.pop(game.game_id, None)
        await query.edit_message_text("Игра перезапущена.")
        buttons = [
            [
                InlineKeyboardButton("3 минуты", callback_data="time_3"),
                InlineKeyboardButton("5 минут", callback_data="time_5"),
            ]
        ]
        if query.from_user.id == ADMIN_ID:
            buttons.append([InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")])
        await reply_game_message(
            query.message,
            context,
            "Выберите длительность игры:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await broadcast(
            game.game_id,
            "Игра завершена. Для новой игры с новыми участниками нажмите /start",
        )
        BASE_MSG_IDS.pop(game.game_id, None)
        ACTIVE_GAMES.pop(game.game_id, None)
        await query.edit_message_text(
            "Игра завершена. Для новой игры с новыми участниками нажмите /start"
        )


async def word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_ts = perf_counter()
    # подробный входной лог
    try:
        logger.debug(
            "word_message ENTER chat_id=%s type=%s thread=%s user=%s text=%r",
            update.effective_chat.id if update.effective_chat else None,
            update.effective_chat.type if update.effective_chat else None,
            update.effective_message.message_thread_id if update.effective_message else None,
            update.effective_user.id if update.effective_user else None,
            update.effective_message.text if update.effective_message else None,
        )
    except Exception:
        pass
    chat = update.effective_chat
    chat_id = chat.id
    thread_id = update.effective_message.message_thread_id
    user_id = update.effective_user.id
    tokens = update.message.text.split()
    words_tokens = tokens
    game = get_game(chat_id, thread_id)
    logger.debug(
        "word_message after get_game: game=%s status=%s (chat=%s,thread=%s)",
        (game.game_id if game else None),
        (game.status if game else None),
        chat_id, thread_id
    )
    if not game and chat.type == "private":
        if tokens:
            gid = tokens[0]
            potential = ACTIVE_GAMES.get(gid)
            if potential and user_id in potential.players:
                game = potential
                words_tokens = tokens[1:]
        if not game:
            for g in ACTIVE_GAMES.values():
                if g.player_chats.get(user_id) == chat_id:
                    game = g
                    break
    if not game or game.status != "running":
        logger.debug(
            "word_message EXIT: no game or not running; game=%s status=%s chat=%s thread=%s",
            (game.game_id if game else None),
            (game.status if game else None),
            chat_id, thread_id
        )
        return
    game.player_chats[user_id] = chat.id
    player = game.players.get(user_id)
    if not player:
        saved_name = context.user_data.get("name")
        if saved_name and len(game.players) < 5:
            player = Player(user_id=user_id, name=saved_name)
            game.players[user_id] = player
            await broadcast(
                game.game_id,
                f"{bold_alnum(saved_name)} присоединился к игре",
                parse_mode="HTML",
            )
        else:
            await reply_game_message(update.message, context, "Чтобы участвовать, используйте /join")
            logger.debug("player not registered")
            return
    if not player.name:
        await request_name(user_id, chat_id, context)
        return
    words = [normalize_word(w) for w in words_tokens]
    player_name = player.name
    tasks: List = []

    async def send_to_user(text: str) -> None:
        logger.debug("send_to_user start %.6f", perf_counter() - start_ts)
        try:
            await context.bot.send_message(user_id, text)
        except TelegramError:
            await send_game_message(
                chat_id,
                thread_id,
                context,
                f"{player_name} {text}",
            )
            if not context.user_data.get("dm_warned"):
                context.user_data["dm_warned"] = True
                await send_game_message(
                    chat_id,
                    thread_id,
                    context,
                    f"{player_name} напишите мне в личные сообщения (/start), чтобы получать мгновенную обратную связь.",
                )
        logger.debug("send_to_user end %.6f", perf_counter() - start_ts)

    for w in words:
        if not is_cyrillic(w) or len(w) < 3:
            tasks.append(send_to_user(f"Отклонено: {w} (принимаются слова из 3 букв и длиннее)"))
            continue
        if w in player.words:
            tasks.append(send_to_user(f"Отклонено: {w} (вы уже использовали это слово)"))
            continue
        if w in game.used_words:
            tasks.append(send_to_user(f"Отклонено: {w} (уже использовано другим игроком)"))
            continue
        if w not in DICT:
            tasks.append(send_to_user(f"Отклонено: {w} (такого слова нет в словаре)"))
            continue
        if not can_make(w, game.letters):
            tasks.append(send_to_user(f"Отклонено: {w} (нет таких букв)"))
            continue
        game.used_words.add(w)
        player.words.append(w)
        pts = 2 if len(w) >= 6 else 1
        player.points += pts
        message = f"Зачтено: {w}"
        if len(w) >= 6:
            message += "\nБраво! Вы получили 2 очка за это слово. 🤩"
        tasks.append(send_to_user(message))
        if len(w) >= 6:
            name = player_name
            length = len(w)
            phrases = [
                f"🔥 {name} жжёт! Прилетело слово из {length} букв.",
                f"{name} выдает красоту ✨: слово из {length} букв!",
                f"🥊 {name} в ударе! Словечко на {length} букв.",
                f"💣 Да это ж бомба! Слово из {length} букв от игрока {name}.",
                f"😎 Лови стиль: {name} выкатывает слово на {length} букв.",
                f"Ход короля! 👑 {name} выкладывает слово из {length} букв.",
            ]
            tasks.append(broadcast(game.game_id, random.choice(phrases)))

    if tasks:
        logger.debug("before asyncio.gather %.6f", perf_counter() - start_ts)
        await asyncio.gather(*tasks)
        logger.debug("after asyncio.gather %.6f", perf_counter() - start_ts)
    schedule_refresh_base_button(chat_id, thread_id, context)
    logger.debug("word_message end %.6f", perf_counter() - start_ts)

async def manual_base_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    user_id = update.effective_user.id
    game = get_game(chat_id, thread_id or 0)
    if not game or user_id != game.host_id or game.base_word:
        return
    word = normalize_word(update.message.text)
    if len(word) < 8 or word not in DICT:
        await reply_game_message(update.message, context, "Неверное слово")
        return
    player = game.players.get(user_id)
    if not player or not player.name:
        await request_name(user_id, chat_id, context)
        return
    await set_base_word(chat_id, thread_id, word, context, chosen_by=player.name)


async def bot_move(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id or 0)
    if not game or game.status != "running":
        return
    available = [w for w in DICT if len(w) >= 3 and can_make(w, game.letters) and w not in game.used_words]
    if not available:
        return
    word = random.choice(available)
    bot_player = game.players.get(0)
    if bot_player:
        bot_player.words.append(word)
        pts = 2 if len(word) >= 6 else 1
        bot_player.points += pts
        game.used_words.add(word)
        await broadcast(game.game_id, f"🤖 {bot_player.name}: {word}")
        schedule_refresh_base_button(chat_id, thread_id, context)


async def handle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin word submissions in private chat."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    message = update.message
    if not message or not message.text:
        return
    chat_id = message.chat_id
    thread_id = message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game or game.status != "running":
        return
    player = game.players.get(user.id)
    if not player:
        return
    if not player.name:
        await request_name(user.id, chat_id, context)
        return
    words = [normalize_word(w) for w in message.text.split()]
    handled = False
    for w in words:
        if not is_cyrillic(w) or len(w) < 3:
            await message.reply_text(
                f"Отклонено: {w} (принимаются слова из 3 букв и длиннее)"
            )
            continue
        if w in player.words:
            await message.reply_text(
                f"Отклонено: {w} (вы уже использовали это слово)"
            )
            continue
        if w in game.used_words:
            await message.reply_text(
                f"Отклонено: {w} (уже использовано другим игроком)"
            )
            continue
        if w not in DICT:
            await message.reply_text(
                f"Отклонено: {w} (такого слова нет в словаре)"
            )
            continue
        if not can_make(w, game.letters):
            await message.reply_text(
                f"Отклонено: {w} (нет таких букв)"
            )
            continue
        game.used_words.add(w)
        player.words.append(w)
        pts = 2 if len(w) >= 6 else 1
        player.points += pts
        msg = f"Зачтено: {w}"
        if len(w) >= 6:
            msg += "\nБраво! Вы получили 2 очка за это слово. 🤩"
        await message.reply_text(msg)
        handled = True
    schedule_refresh_base_button(chat_id, thread_id, context)
    if handled:
        raise ApplicationHandlerStop


async def webhook_check(context: CallbackContext) -> None:
    info = await context.bot.get_webhook_info()
    expected_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}" if PUBLIC_URL else None
    if not info.url or info.url != expected_url:
        if expected_url:
            try:
                await context.bot.set_webhook(
                    url=expected_url,
                    secret_token=WEBHOOK_SECRET,
                    allowed_updates=[],
                )
                logger.info("Webhook registered: %s", expected_url)
            except Exception as e:
                logger.error("Webhook registration failed: %s", e)
        else:
            logger.warning("PUBLIC_URL not set; cannot register webhook")
    else:
        logger.info("Webhook is up-to-date: %s", info.url)

@app.on_event("startup")
async def on_startup() -> None:
    global APPLICATION, BOT_USERNAME
    APPLICATION = Application.builder().token(TOKEN).build()
    BOT_USERNAME = (await APPLICATION.bot.get_me()).username
    # 0) «Кран-тик» — логируем все апдейты как можно раньше
    APPLICATION.add_handler(MessageHandler(filters.ALL, _tap, block=False), group=-2)
    APPLICATION.add_handler(CommandHandler("start", start_cmd))
    APPLICATION.add_handler(CommandHandler("newgame", newgame))
    APPLICATION.add_handler(CommandHandler("join", join_cmd))
    APPLICATION.add_handler(CommandHandler(["quit", "exit"], quit_cmd))
    APPLICATION.add_handler(CommandHandler("chatid", chat_id_handler))
    APPLICATION.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_name, block=False),
        group=0,
    )
    APPLICATION.add_handler(CallbackQueryHandler(time_selected, pattern="^(time_|adm_test)"))
    APPLICATION.add_handler(CallbackQueryHandler(join_button, pattern="^join_"))
    APPLICATION.add_handler(CallbackQueryHandler(base_choice, pattern="^(base_|pick_)", block=False))
    APPLICATION.add_handler(CallbackQueryHandler(start_button, pattern="^start$"))
    APPLICATION.add_handler(CallbackQueryHandler(restart_handler, pattern="^restart_"))
    APPLICATION.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared_handler))
    APPLICATION.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^Создать ссылку$"),
            invite_link,
        )
    )
    # 2) Поднять основной обработчик слов выше прочих текстовых; он неблокирующий
    APPLICATION.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), word_message, block=False),
        group=0
    )
    # Остальные текстовые — ниже
    APPLICATION.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), manual_base_word, block=False)
    )
    APPLICATION.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_submission,
            block=False,
        )
    )
    # (раньше мы регистрировали word_message здесь; перенесено выше)
    
    
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
            await APPLICATION.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=[],
            )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await APPLICATION.stop()
    await APPLICATION.shutdown()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> JSONResponse:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    data = await request.json()
    logger.debug("Webhook update keys: %s", list(data.keys()))
    update = Update.de_json(data, APPLICATION.bot)
    await APPLICATION.process_update(update)
    return JSONResponse({"ok": True})


@app.get("/set_webhook")
async def set_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[],
    )
    return JSONResponse({"url": webhook_url})


@app.get("/reset_webhook")
async def reset_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.delete_webhook(drop_pending_updates=False)
    await APPLICATION.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[],
    )
    return JSONResponse({"reset_to": webhook_url})


@app.get("/")
async def root() -> JSONResponse:
    """Base endpoint with brief service info."""
    return JSONResponse({"message": "Wordgame Magic service. See /healthz for status."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})

