import asyncio
import json
import os
import random
import secrets
import logging
from time import perf_counter
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Set, List, Tuple

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
from telegram.error import TelegramError
from telegram.constants import ChatMemberStatus

# --- Utilities --------------------------------------------------------------

DICT_PATH = Path(__file__).with_name("nouns_ru_pymorphy2_yaspeller.jsonl")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
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
    game_id: str
    time_limit: int = 3
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
CHAT_GAMES: Dict[Tuple[int, int], str] = {}
GAME_CHATS: Dict[str, Tuple[int, int]] = {}
BASE_MSG_IDS: Dict[str, int] = {}
LAST_REFRESH: Dict[Tuple[int, int], float] = {}


def get_game(chat_id: int, thread_id: int) -> Optional[GameState]:
    """Retrieve game by chat and thread."""
    game_id = CHAT_GAMES.get((chat_id, thread_id))
    if game_id:
        return ACTIVE_GAMES.get(game_id)
    return None


def get_chat(game_id: str) -> Optional[Tuple[int, int]]:
    """Retrieve chat and thread by game id."""
    return GAME_CHATS.get(game_id)


async def broadcast(game_id: str, text: str, reply_markup=None) -> None:
    """Send a message to all player chats for the given game."""
    if not APPLICATION:
        return
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        return
    sent: Set[int] = set()
    for chat_id in game.player_chats.values():
        if chat_id in sent:
            continue
        try:
            await APPLICATION.bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramError:
            pass
        sent.add(chat_id)


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
    key = (chat_id, thread_id)
    last = LAST_REFRESH.get(key, 0)
    if now - last < 1:
        return
    LAST_REFRESH[key] = now
    asyncio.create_task(refresh_base_button(chat_id, thread_id, context))


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


# --- FastAPI & PTB integration ---------------------------------------------

app = FastAPI()
APPLICATION: Optional[Application] = None
BOT_USERNAME: Optional[str] = None

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex())
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WORD_CONFIRM_IN_CHAT = os.environ.get("WORD_CONFIRM_IN_CHAT") == "1"
SUPERGROUP_ID = int(os.getenv("SUPERGROUP_ID", "0"))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = context.args[0] if context.args else None
    if code == "home":
        user = update.effective_user
        game_id, thread_id = await create_game(user.id, context)
        context.user_data["join_chat"] = SUPERGROUP_ID
        context.user_data["join_thread"] = thread_id
        game = get_game(SUPERGROUP_ID, thread_id)
        if game:
            game.players[user.id] = Player(user_id=user.id, name=user.first_name or "")
            game.player_chats[user.id] = update.effective_chat.id
        buttons = [
            [
                InlineKeyboardButton("3 минуты", callback_data="time_3"),
                InlineKeyboardButton("5 минут", callback_data="time_5"),
            ]
        ]
        if user.id == ADMIN_ID:
            buttons.append([InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")])
        await reply_game_message(
            update.message,
            context,
            f"Игра #{game_id} создана. Выберите длительность:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if code and code.startswith("create_"):
        key = code.split("create_", 1)[1]
        chat = update.effective_chat
        user_id = update.effective_user.id
        if chat.type in {"group", "supergroup"} and key in JOIN_CODES:
            JOIN_CODES.pop(key, None)
            game = get_game(chat.id, 0)
            if game and game.status in {"waiting", "running"}:
                await reply_game_message(update.message, context, "Игра уже запущена.")
                return
            gid = secrets.token_urlsafe(8)
            game = GameState(host_id=user_id, game_id=gid)
            ACTIVE_GAMES[gid] = game
            CHAT_GAMES[(chat.id, 0)] = gid
            GAME_CHATS[gid] = (chat.id, 0)
            game.players[user_id] = Player(user_id=user_id)
            context.user_data["join_chat"] = chat.id
            context.user_data["join_thread"] = 0
            await request_name(user_id, chat.id, context)
            try:
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status != ChatMemberStatus.ADMINISTRATOR:
                    await context.bot.send_message(
                        user_id,
                        "Чтобы бот видел слова игроков, отключите режим приватности у @BotFather или повысьте бота до администратора в этом чате",
                    )
            except TelegramError:
                pass
        else:
            JOIN_CODES[key] = ""
            await reply_game_message(
                update.message,
                context,
                "Группа создана. Используйте /newgame для старта.",
            )
        return
    if code and code in JOIN_CODES:
        gid = JOIN_CODES[code]
        chat_info = get_chat(gid)
        if chat_info:
            jc, jt = chat_info
            context.user_data["join_chat"] = jc
            context.user_data["join_thread"] = jt
            await reply_game_message(
                update.message,
                context,
                "Вы можете присоединиться к игре. Зайдите в чат и нажмите кнопку 'Присоединиться'.",
            )
    else:
        buttons = [
            [
                InlineKeyboardButton("3 мин", callback_data="time_3"),
                InlineKeyboardButton("5 мин", callback_data="time_5"),
            ]
        ]
        user = update.effective_user
        if user and user.id == ADMIN_ID:
            buttons.append([
                InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")
            ])
        await reply_game_message(
            update.message,
            context,
            "Выберите длительность игры:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def clear_home_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg_id = context.user_data.pop("home_link_msg_id", None)
    if msg_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, msg_id)
        except TelegramError:
            pass


async def request_name(user_id: int, chat_id: int, context: CallbackContext) -> None:
    await send_game_message(
        chat_id,
        None,
        context,
        "Введите ваше имя",
        reply_markup=ForceReply(selective=True),
    )


async def create_game(host_id: int, context: CallbackContext) -> Tuple[str, int]:
    game_id = secrets.token_urlsafe(8)
    topic = await context.bot.create_forum_topic(
        chat_id=SUPERGROUP_ID,
        name=f"Игра #{game_id} • 10 минут",
    )
    thread_id = topic.message_thread_id
    invite = await context.bot.create_chat_invite_link(SUPERGROUP_ID)
    topic_url = f"https://t.me/c/{str(SUPERGROUP_ID)[4:]}/{thread_id}"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Пригласить игроков", url=invite.invite_link),
                InlineKeyboardButton("Зайти в комнату", url=topic_url),
            ]
        ]
    )
    await context.bot.send_message(
        SUPERGROUP_ID,
        f"Игра #{game_id} создана",
        message_thread_id=thread_id,
        reply_markup=keyboard,
    )
    context.application.chat_data.setdefault(SUPERGROUP_ID, {})[thread_id] = game_id
    game = GameState(host_id=host_id, game_id=game_id)
    ACTIVE_GAMES[game_id] = game
    CHAT_GAMES[(SUPERGROUP_ID, thread_id)] = game_id
    GAME_CHATS[game_id] = (SUPERGROUP_ID, thread_id)
    return game_id, thread_id


def create_dm_game(host_id: int) -> GameState:
    """Create a direct-message game for the host."""
    game_id = secrets.token_urlsafe(8)
    game = GameState(host_id=host_id, game_id=game_id)
    game.players[host_id] = Player(user_id=host_id)
    game.player_chats[host_id] = host_id
    ACTIVE_GAMES[game_id] = game
    CHAT_GAMES[(host_id, None)] = game_id
    GAME_CHATS[game_id] = (host_id, None)
    return game


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game_id, _ = await create_game(update.effective_user.id, context)
    if update.message:
        await reply_game_message(update.message, context, f"Игра #{game_id} создана")


async def maybe_show_base_options(
    chat_id: int,
    thread_id: Optional[int],
    context: CallbackContext,
    game: Optional[GameState] = None,
) -> None:
    """Send base word options to the host when conditions are met."""
    if game is None:
        game = get_game(chat_id, thread_id)
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
    thread_id = update.effective_message.message_thread_id
    game = get_game(chat_id, thread_id)
    if not game and chat.type == "private":
        join_chat = context.user_data.get("join_chat")
        join_thread = context.user_data.get("join_thread")
        if join_chat is not None and join_thread is not None:
            game = get_game(join_chat, join_thread)
            chat_id, thread_id = join_chat, join_thread
    if not game:
        return
    game.player_chats[user_id] = chat.id
    player = game.players.get(user_id)
    name = update.message.text.strip()
    if not player:
        if len(game.players) >= 5:
            await reply_game_message(update.message, context, "Лобби заполнено")
            return
        player = Player(user_id=user_id, name=name)
        game.players[user_id] = player
        context.user_data["join_chat"] = chat_id
        context.user_data["join_thread"] = thread_id
        context.user_data["name"] = name
        await reply_game_message(update.message, context, f"Имя установлено: {player.name}")
        host_chat = game.player_chats.get(game.host_id)
        if host_chat:
            await maybe_show_base_options(host_chat, None, context, game)
        return
    if not player.name:
        player.name = name
        context.user_data["name"] = name
        await reply_game_message(update.message, context, f"Имя установлено: {player.name}")
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


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    chat_id = chat.id
    thread_id = query.message.message_thread_id
    if query.data == "adm_test" and query.from_user.id == ADMIN_ID:
        game = create_dm_game(query.from_user.id)
        game.players[query.from_user.id].name = query.from_user.first_name or ""
        context.user_data["join_chat"] = chat_id
        context.user_data["join_thread"] = thread_id
        game.time_limit = 3
        game.players[0] = Player(user_id=0, name="Bot")
        game.status = "waiting"
        await query.edit_message_text("Тестовая игра создана")
        await maybe_show_base_options(chat_id, thread_id, context, game)
        return

    game = get_game(chat_id, thread_id)
    if not game and chat.type == "private":
        game = create_dm_game(query.from_user.id)
        game.players[query.from_user.id].name = query.from_user.first_name or ""
        context.user_data["join_chat"] = chat_id
        context.user_data["join_thread"] = thread_id
    if not game:
        jc = context.user_data.get("join_chat")
        jt = context.user_data.get("join_thread")
        if jc is not None and jt is not None:
            game = get_game(jc, jt)
            chat_id, thread_id = jc, jt
    if not game or query.from_user.id != game.host_id:
        return
    if query.data.startswith("time_"):
        game.time_limit = int(query.data.split("_")[1])
        game.status = "waiting"
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = game.game_id
        invite_buttons = [
            [
                InlineKeyboardButton("Пригласить из контактов", callback_data="invite_contacts"),
                InlineKeyboardButton("Создать ссылку", callback_data="invite_link"),
            ]
        ]
        await query.edit_message_text(
            "Игра создана. Пригласите участников.",
            reply_markup=InlineKeyboardMarkup(invite_buttons),
        )
        if chat.type != "private":
            await reply_game_message(
                query.message,
                context,
                "Нажмите, чтобы присоединиться:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Присоединиться", callback_data=f"join_{code}")]]
                ),
            )
            await reply_game_message(
                query.message,
                context,
                "Когда все участники присоединились, перейдите к выбору базового слова",
            )


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
    chat_info = get_chat(game_id)
    if not chat_info:
        await reply_game_message(update.message, context, "Игра не найдена")
        return
    chat_id, thread_id = chat_info
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        await reply_game_message(update.message, context, "Игра не найдена")
        return
    user_id = update.effective_user.id
    if user_id in game.players:
        await reply_game_message(update.message, context, "Вы уже в игре")
        return
    if len(game.players) >= 5:
        await reply_game_message(update.message, context, "Лобби заполнено")
        return
    game.players[user_id] = Player(user_id=user_id)
    game.player_chats[user_id] = update.effective_chat.id
    context.user_data['join_chat'] = chat_id
    context.user_data['join_thread'] = thread_id
    await reply_game_message(
        update.message,
        context,
        "Добро пожаловать! Введите ваше имя:",
        reply_markup=ForceReply(selective=True),
    )


async def join_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    join_code = data.split("_", 1)[1] if "_" in data else ""
    game_id = JOIN_CODES.get(join_code)
    if not game_id:
        return
    chat_info = get_chat(game_id)
    if not chat_info:
        return
    chat_id, thread_id = chat_info
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        return
    user_id = query.from_user.id
    if user_id in game.players:
        await context.bot.send_message(user_id, "Вы уже в игре")
        return
    if len(game.players) >= 5:
        await context.bot.send_message(user_id, "Лобби заполнено")
        return
    game.players[user_id] = Player(user_id=user_id)
    game.player_chats[user_id] = user_id
    context.user_data['join_chat'] = chat_id
    context.user_data['join_thread'] = thread_id
    await context.bot.send_message(
        user_id,
        "Добро пожаловать! Введите ваше имя:",
        reply_markup=ForceReply(selective=True),
    )


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
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id)
    if not game:
        return
    code = next((c for c, gid in JOIN_CODES.items() if gid == game.game_id), None)
    if not code:
        code = secrets.token_urlsafe(8)
        JOIN_CODES[code] = game.game_id
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
    thread_id = update.effective_message.message_thread_id
    game = get_game(chat_id, thread_id)
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
    thread_id = update.effective_message.message_thread_id
    game = get_game(chat_id, thread_id)
    if not game:
        await reply_game_message(update.message, context, "Игра не запущена")
        return
    user_id = update.effective_user.id
    if user_id != game.host_id:
        await reply_game_message(update.message, context, "Только инициатор может прервать игру")
        return
    for job in game.jobs.values():
        job.schedule_removal()
    game.jobs.clear()
    msg_id = BASE_MSG_IDS.get(game.game_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    await reply_game_message(update.message, context, "Игра прервана")
    try:
        await context.bot.close_forum_topic(chat_id, thread_id)
    except TelegramError:
        pass
    gid = CHAT_GAMES.pop((chat_id, thread_id), None)
    if gid:
        ACTIVE_GAMES.pop(gid, None)
        GAME_CHATS.pop(gid, None)
        BASE_MSG_IDS.pop(gid, None)
    game_id = context.application.chat_data.get(chat_id, {}).pop(thread_id, None)
    summary = (
        f"Игра #{game_id} прервана и тема закрыта."
        if game_id
        else "Игра прервана и тема закрыта."
    )
    try:
        await broadcast(game.game_id, summary)
    except TelegramError:
        pass


async def base_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id)
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
        await send_game_message(
            chat_id,
            thread_id,
            context,
            "Выберите слово:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        game.jobs["rand"] = context.job_queue.run_once(
            finish_random,
            5,
            chat_id=chat_id,
            data={"thread_id": thread_id, "words": words},
            name=f"rand_{chat_id}_{thread_id}",
        )
        game.jobs["count"] = context.job_queue.run_repeating(
            countdown,
            1,
            count=5,
            chat_id=chat_id,
            data={"thread_id": thread_id, "remaining": 5},
            name=f"cnt_{chat_id}_{thread_id}",
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
        await set_base_word(chat_id, thread_id, word, context, chosen_by=chosen_by)


async def finish_random(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id)
    if not game or game.base_word:
        return
    if "count" in game.jobs:
        job = game.jobs.pop("count")
        job.schedule_removal()
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
        return

    msg_id = data.get("message_id")
    try:
        if msg_id:
            await context.bot.edit_message_text(str(remaining), chat_id, msg_id, message_thread_id=thread_id)
        else:
            msg = await send_game_message(chat_id, thread_id, context, str(remaining))
            data["message_id"] = msg.message_id
    except Exception:
        # If editing fails (message deleted), send a new one
        msg = await send_game_message(chat_id, thread_id, context, str(remaining))
        data["message_id"] = msg.message_id

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
    game = get_game(chat_id, thread_id)
    if not game or query.from_user.id != game.host_id or not game.base_word:
        return
    await query.edit_message_text("Игра начинается!")
    await start_game(chat_id, thread_id, context)


async def start_game(chat_id: int, thread_id: int, context: CallbackContext) -> None:
    game = get_game(chat_id, thread_id)
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
    game = get_game(chat_id, thread_id)
    if not game:
        return
    game.base_word = normalize_word(word)
    game.letters = Counter(game.base_word)
    message = (
        f"{chosen_by} выбрал слово {game.base_word}"
        if chosen_by
        else f"Выбрано слово: {game.base_word}"
    )
    await broadcast(game.game_id, message)
    await broadcast(
        game.game_id,
        "Нажмите Старт, когда будете готовы",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Старт", callback_data="start")]]),
    )


async def warn_time(context: CallbackContext) -> None:
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(context.job.chat_id, thread_id)
    if game:
        await broadcast(game.game_id, "Осталась 1 минута!")


async def end_game(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id)
    if not game:
        return
    game.status = "finished"
    msg_id = BASE_MSG_IDS.pop(game.game_id, None)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    players_sorted = sorted(game.players.values(), key=lambda p: p.points, reverse=True)
    scores = [(p.name or str(uid), p.points) for uid, p in game.players.items()]
    scores.sort(key=lambda x: x[1], reverse=True)
    max_score = scores[0][1] if scores else 0
    winners = [name for name, pts in scores if pts == max_score]

    lines = ["Игра окончена! Результаты:", "", f"Слово: {game.base_word.upper()}", ""]
    for p in players_sorted:
        name = p.name or str(p.user_id)
        lines.append(name)
        for i, w in enumerate(p.words, 1):
            pts = 2 if len(w) >= 6 else 1
            lines.append(f"{i}. {w} — {pts}")
        lines.append(f"Результат: {p.points}")
        lines.append("")

    if winners:
        if len(winners) == 1:
            lines.append(f"🏆 Победитель: {winners[0]}")
        else:
            lines.append("🏆 Победители: " + ", ".join(winners))
    message = "\n".join(lines).rstrip()
    await broadcast(game.game_id, message)
    try:
        await context.bot.close_forum_topic(chat_id, thread_id)
    except TelegramError:
        pass
    for job in game.jobs.values():
        job.schedule_removal()
    game.jobs.clear()
    gid = CHAT_GAMES.pop((chat_id, thread_id), None)
    if gid:
        ACTIVE_GAMES.pop(gid, None)
        GAME_CHATS.pop(gid, None)
        BASE_MSG_IDS.pop(gid, None)
    game_id = context.application.chat_data.get(chat_id, {}).pop(thread_id, None)
    summary = (
        f"Игра #{game_id} завершена, тема закрыта."
        if game_id
        else "Игра завершена, тема закрыта."
    )
    try:
        await broadcast(game.game_id, summary)
    except TelegramError:
        pass


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
    BASE_MSG_IDS.pop(game.game_id, None)


async def restart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id
    game = get_game(chat_id, thread_id)
    if not game:
        return
    if query.data == "restart_yes":
        reset_game(game)
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
        gid = CHAT_GAMES.pop((chat_id, thread_id), None)
        if gid:
            ACTIVE_GAMES.pop(gid, None)
            GAME_CHATS.pop(gid, None)
            BASE_MSG_IDS.pop(gid, None)
        await query.edit_message_text("Игра завершена. Для новой игры с новыми участниками нажмите /start")


async def word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_ts = perf_counter()
    logger.debug("word_message start %.6f", start_ts)
    chat = update.effective_chat
    chat_id = chat.id
    thread_id = update.effective_message.message_thread_id
    user_id = update.effective_user.id
    tokens = update.message.text.split()
    words_tokens = tokens
    game = get_game(chat_id, thread_id)
    if not game and chat.type == "private":
        if tokens:
            gid = tokens[0]
            potential = ACTIVE_GAMES.get(gid)
            if potential and user_id in potential.players:
                game = potential
                chat_info = get_chat(gid)
                if chat_info:
                    chat_id, thread_id = chat_info
                words_tokens = tokens[1:]
        if not game:
            for g in ACTIVE_GAMES.values():
                if g.player_chats.get(user_id) == chat_id:
                    game = g
                    chat_info = get_chat(g.game_id)
                    if chat_info:
                        chat_id, thread_id = chat_info
                    break
    if not game or game.status != "running":
        logger.debug("game not running or not found")
        return
    game.player_chats[user_id] = chat.id
    player = game.players.get(user_id)
    if not player:
        saved_name = context.user_data.get("name")
        if saved_name and len(game.players) < 5:
            player = Player(user_id=user_id, name=saved_name)
            game.players[user_id] = player
            await broadcast(game.game_id, f"{saved_name} присоединился к игре")
        else:
            await reply_game_message(update.message, context, "Чтобы участвовать, используйте /join")
            logger.debug("player not registered")
            return
    words = [normalize_word(w) for w in words_tokens]
    mention = update.effective_user.mention_html()
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
                f"{mention} {text}",
                parse_mode="HTML",
            )
            if not context.user_data.get("dm_warned"):
                context.user_data["dm_warned"] = True
                await send_game_message(
                    chat_id,
                    thread_id,
                    context,
                    f"{mention} напишите мне в личные сообщения (/start), чтобы получать мгновенную обратную связь.",
                    parse_mode="HTML",
                )
        else:
            if WORD_CONFIRM_IN_CHAT:
                await send_game_message(
                    chat_id,
                    thread_id,
                    context,
                    f"{mention} {text}",
                    parse_mode="HTML",
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
            name = player.name if player.name else update.effective_user.full_name
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
    game = get_game(chat_id, thread_id)
    if not game or user_id != game.host_id or game.base_word:
        return
    word = normalize_word(update.message.text)
    if len(word) < 8 or word not in DICT:
        await reply_game_message(update.message, context, "Неверное слово")
        return
    player = game.players.get(user_id)
    chosen_by = player.name if player and player.name else update.effective_user.full_name
    await set_base_word(chat_id, thread_id, word, context, chosen_by=chosen_by)


async def bot_move(context: CallbackContext) -> None:
    chat_id = context.job.chat_id
    data = context.job.data or {}
    thread_id = data.get("thread_id")
    game = get_game(chat_id, thread_id)
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
        await broadcast(game.game_id, f"Bot: {word}")
        schedule_refresh_base_button(chat_id, thread_id, context)


async def webhook_check(context: CallbackContext) -> None:
    info = await context.bot.get_webhook_info()
    expected_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}" if PUBLIC_URL else None
    if not info.url or info.url != expected_url:
        if expected_url:
            try:
                await context.bot.set_webhook(url=expected_url, secret_token=WEBHOOK_SECRET)
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
    APPLICATION.add_handler(MessageHandler(filters.ALL, clear_home_link_message, block=False), group=-1)
    APPLICATION.add_handler(CommandHandler("start", start_cmd))
    APPLICATION.add_handler(CommandHandler("newgame", newgame))
    APPLICATION.add_handler(CommandHandler("join", join_cmd))
    APPLICATION.add_handler(CommandHandler(["quit", "exit"], quit_cmd))
    APPLICATION.add_handler(CommandHandler("chatid", chat_id_handler))
    APPLICATION.add_handler(MessageHandler(filters.REPLY & filters.TEXT & (~filters.COMMAND), handle_name))
    APPLICATION.add_handler(CallbackQueryHandler(time_selected, pattern="^(time_|adm_test)"))
    APPLICATION.add_handler(CallbackQueryHandler(join_button, pattern="^join_"))
    APPLICATION.add_handler(CallbackQueryHandler(invite_contacts, pattern="^invite_contacts$"))
    APPLICATION.add_handler(CallbackQueryHandler(invite_link, pattern="^invite_link$"))
    APPLICATION.add_handler(CallbackQueryHandler(base_choice, pattern="^(base_|pick_)", block=False))
    APPLICATION.add_handler(CallbackQueryHandler(start_button, pattern="^start$"))
    APPLICATION.add_handler(CallbackQueryHandler(restart_handler, pattern="^restart_"))
    APPLICATION.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared_handler))
    APPLICATION.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), manual_base_word, block=False))
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

