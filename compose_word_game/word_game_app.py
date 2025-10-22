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
from typing import Callable, Dict, Optional, Set, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButtonRequestUsers,
    Message,
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
from telegram.error import BadRequest, Forbidden, TelegramError
from llm_utils import describe_word
from shared.choice_timer import ChoiceTimerHandle, send_choice_with_timer
from shared.word_stats import get_zipf

# --- Utilities --------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DICT_PATH = BASE_DIR / "nouns_ru_pymorphy2_yaspeller.jsonl"
WHITELIST_PATH = BASE_DIR / "whitelist.jsonl"

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
    base_msg_counts: Dict[Tuple[int, int], int] = field(default_factory=dict)
    invite_keyboard_hidden: bool = False
    word_history: List[Tuple[int, str]] = field(default_factory=list)


ACTIVE_GAMES: Dict[str, GameState] = {}
JOIN_CODES: Dict[str, str] = {}
BASE_MSG_IDS: Dict[str, int] = {}
LAST_REFRESH: Dict[Tuple[int, int], float] = {}
# Map player chat (chat_id, thread_id) to game_id for quick lookup
CHAT_GAMES: Dict[Tuple[int, int], str] = {}
# Track users from whom the game currently expects a name
AWAITING_NAME_USERS: Set[int] = set()


class AwaitingComposeNameFilter(filters.MessageFilter):
    """Filter that matches only messages from users awaiting a name."""

    name = "compose_awaiting_name"

    def filter(self, message: Message) -> bool:
        user = getattr(message, "from_user", None)
        return bool(user and user.id in AWAITING_NAME_USERS)


AWAITING_COMPOSE_NAME_FILTER = AwaitingComposeNameFilter()


def get_game(chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
    """Retrieve a game by chat/thread identifier."""
    key = (chat_id, thread_id or 0)
    game_id = CHAT_GAMES.get(key)
    if game_id:
        return ACTIVE_GAMES.get(game_id)
    for g in ACTIVE_GAMES.values():
        if chat_id in g.player_chats.values():
            return g
    return None


async def broadcast(
    game_id: str,
    text: str,
    reply_markup=None,
    parse_mode=None,
    skip_chat_id: Optional[int] = None,
) -> None:
    """Send a message to all player chats."""
    if not APPLICATION:
        return
    game = ACTIVE_GAMES.get(game_id)
    if not game:
        return
    sent: Set[int] = set()
    for cid in game.player_chats.values():
        if cid == skip_chat_id or cid in sent:
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
    key = (chat_id, thread_id)
    count = game.base_msg_counts.get(key, 0) + 1
    game.base_msg_counts[key] = count
    prefix = ""
    if count >= 5 and (count - 5) % 7 == 0:
        prefix = (
            "Можете отправлять знак ? и слово, чтобы проверить определение любого слова у ИИ.\n"
        )
    text = prefix + "Собирайте слова из букв базового слова:"
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


INVISIBLE_MESSAGE = "\u2063"


async def send_game_message(chat_id: int, thread_id: Optional[int], context: CallbackContext, text: str, **kwargs):
    if thread_id is None:
        msg = await context.bot.send_message(chat_id, text, **kwargs)
    else:
        msg = await context.bot.send_message(chat_id, text, message_thread_id=thread_id, **kwargs)
    schedule_refresh_base_button(chat_id, thread_id or 0, context)
    return msg


async def hide_invite_keyboard(chat_id: int, thread_id: Optional[int], context: CallbackContext) -> None:
    """Remove the invite keyboard without leaving a visible message."""

    msg = await send_game_message(
        chat_id,
        thread_id,
        context,
        INVISIBLE_MESSAGE,
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        await msg.delete()
    except TelegramError:
        logger.debug("Failed to delete invite keyboard removal message", exc_info=True)


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


async def awaiting_name_guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ensure the user provides a name before processing commands."""
    user = update.effective_user
    user_id = user.id if user else None
    awaiting = bool(user_id and user_id in AWAITING_NAME_USERS)
    if not awaiting:
        awaiting = context.user_data.get("awaiting_name", False)
    if not awaiting:
        return
    message = update.effective_message
    if not message:
        return
    text = message.text or ""
    if not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0]
    if cmd in ("/quit", "/exit"):
        return
    await reply_game_message(
        message,
        context,
        "Сначала назовитесь. Введите ваше имя:",
    )
    raise ApplicationHandlerStop


# --- FastAPI & PTB integration ---------------------------------------------

app = FastAPI()
APPLICATION: Optional[Application] = None
BOT_USERNAME: Optional[str] = None

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex())
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
ALLOWED_UPDATES = ["message", "callback_query", "users_shared"]


def mark_awaiting_name(context: CallbackContext, user_id: int) -> None:
    AWAITING_NAME_USERS.add(user_id)
    context.user_data["awaiting_name"] = True
    if context.application:
        storage = getattr(context.application, "_user_data", None)
        if storage is not None:
            user_store = storage.setdefault(user_id, {})
        else:
            user_store = context.application.user_data.setdefault(user_id, {})
        user_store["awaiting_name"] = True


def clear_awaiting_name(context: CallbackContext, user_id: int) -> None:
    AWAITING_NAME_USERS.discard(user_id)
    context.user_data.pop("awaiting_name", None)
    if context.application:
        user_store = context.application.user_data.get(user_id)
        if user_store is not None:
            user_store.pop("awaiting_name", None)
            if not user_store:
                storage = getattr(context.application, "_user_data", None)
                if storage is not None:
                    storage.pop(user_id, None)
                else:
                    context.application.user_data.pop(user_id, None)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = update.effective_chat.id
    code = context.args[0] if context.args else None
    if code and code in JOIN_CODES:
        gid = JOIN_CODES[code]
        game = ACTIVE_GAMES.get(gid)
        if game:
            await add_player_via_invite(update.effective_user, game, context)
        else:
            if message:
                await reply_game_message(message, context, "Игра не найдена")
        return
    user = update.effective_user
    try:
        await reset_for_chat(chat_id, user.id, context)
        logger.debug(
            "Reset state for chat %s and user %s before starting a new game", chat_id, user.id
        )
    except Exception as exc:
        logger.warning(
            "Failed to reset state for chat %s and user %s: %s", chat_id, user.id, exc
        )
    game = create_dm_game(user.id)
    if chat_id != user.id:
        game.player_chats[user.id] = chat_id
        CHAT_GAMES[(chat_id, 0)] = game.game_id
    text = "Игра создана. Введите ваше имя:"
    if message:
        await reply_game_message(message, context, text)
    else:
        await send_game_message(update.effective_chat.id, None, context, text)
    mark_awaiting_name(context, user.id)


async def request_name(user_id: int, chat_id: int, context: CallbackContext) -> None:
    await send_game_message(
        chat_id,
        None,
        context,
        "Введите ваше имя",
    )
    mark_awaiting_name(context, user_id)


def create_dm_game(host_id: int) -> GameState:
    """Create a direct-message game for the host."""
    game_id = secrets.token_urlsafe(8)
    game = GameState(host_id=host_id, game_id=game_id)
    game.players[host_id] = Player(user_id=host_id)
    game.player_chats[host_id] = host_id
    ACTIVE_GAMES[game_id] = game
    CHAT_GAMES[(host_id, 0)] = game_id
    return game


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    game = create_dm_game(user.id)
    chat_id = update.effective_chat.id
    text = "Игра создана. Введите ваше имя:"
    if update.message:
        await reply_game_message(update.message, context, text)
    else:
        await send_game_message(chat_id, None, context, text)
    mark_awaiting_name(context, user.id)


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
        if not game.invite_keyboard_hidden:
            await hide_invite_keyboard(chat_id, thread_id, context)
            game.invite_keyboard_hidden = True
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
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    message = update.message or update.effective_message
    awaiting = user_id in AWAITING_NAME_USERS
    if not awaiting:
        awaiting = context.user_data.get("awaiting_name", False)
        if not awaiting and context.application:
            storage = getattr(context.application, "_user_data", None)
            if storage is not None:
                awaiting = storage.get(user_id, {}).get("awaiting_name", False)
            else:
                awaiting = context.application.user_data.get(user_id, {}).get(
                    "awaiting_name", False
                )
    logger.debug("NAME: entered, awaiting=%s", awaiting)
    if not awaiting:
        return
    if not message:
        return
    chat = message.chat
    chat_id = chat.id
    game = get_game(chat_id, None)
    if not game:
        game = get_game(user_id, None)
        if game:
            game.player_chats[user_id] = chat_id
            CHAT_GAMES[(chat_id, 0)] = game.game_id
    if not game:
        logger.debug(
            "handle_name: game not found for chat_id=%s user_id=%s", chat_id, user_id
        )
        clear_awaiting_name(context, user_id)
        await reply_game_message(
            message, context, "Игра не найдена, начните заново командой /start"
        )
        return
    game.player_chats[user_id] = chat.id
    CHAT_GAMES[(chat.id, 0)] = game.game_id
    player = game.players.get(user_id)
    if player and player.name:
        clear_awaiting_name(context, user_id)
        return
    name = message.text.strip()
    logger.debug(
        "handle_name: processing name '%s' for user_id=%s game_id=%s",
        name,
        user_id,
        game.game_id,
    )
    if not player:
        if len(game.players) >= 5:
            await reply_game_message(message, context, "Лобби заполнено")
            clear_awaiting_name(context, user_id)
            logger.debug("NAME: set '%s' -> stop pipeline", name)
            raise ApplicationHandlerStop
        player = Player(user_id=user_id, name=name)
        game.players[user_id] = player
        context.user_data["name"] = name
        await reply_game_message(message, context, f"Имя установлено: {player.name}")
        await broadcast(
            game.game_id,
            f"{bold_alnum(player.name)} присоединился к игре",
            parse_mode="HTML",
        )
        logger.debug(
            "handle_name: new player set name '%s' in game %s", name, game.game_id
        )
        host_chat = game.player_chats.get(game.host_id)
        if host_chat:
            await maybe_show_base_options(host_chat, None, context, game)
        clear_awaiting_name(context, user_id)
        logger.debug("NAME: set '%s' -> stop pipeline", name)
        raise ApplicationHandlerStop
    elif not player.name:
        player.name = name
        context.user_data["name"] = name
        await reply_game_message(message, context, f"Имя установлено: {player.name}")
        await broadcast(
            game.game_id,
            f"{bold_alnum(player.name)} присоединился к игре",
            parse_mode="HTML",
        )
        logger.debug(
            "handle_name: player %s name set to '%s' in game %s",
            user_id,
            name,
            game.game_id,
        )
        if user_id == game.host_id and game.status == "config":
            buttons = [
                [
                    InlineKeyboardButton("3 минуты", callback_data="time_3"),
                    InlineKeyboardButton("5 минут", callback_data="time_5"),
                ]
            ]
            if user_id == ADMIN_ID:
                buttons.append(
                    [InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")]
                )
            await reply_game_message(
                message,
                context,
                "Выберите длительность игры:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        host_chat = game.player_chats.get(game.host_id)
        if host_chat:
            await maybe_show_base_options(host_chat, None, context, game)
        clear_awaiting_name(context, user_id)
        logger.debug("NAME: set '%s' -> stop pipeline", name)
        raise ApplicationHandlerStop


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    chat_id = chat.id
    thread_id = query.message.message_thread_id
    if query.data == "adm_test" and query.from_user.id == ADMIN_ID:
        game = create_dm_game(query.from_user.id)
        # Remove any previous games tied to this chat to avoid stale state
        for gid, g in list(ACTIVE_GAMES.items()):
            if gid == game.game_id:
                continue
            if chat_id in g.player_chats.values():
                for cid in set(g.player_chats.values()):
                    CHAT_GAMES.pop((cid, 0), None)
                BASE_MSG_IDS.pop(gid, None)
                for code, cg in list(JOIN_CODES.items()):
                    if cg == gid:
                        JOIN_CODES.pop(code, None)
                ACTIVE_GAMES.pop(gid, None)
        game.players[query.from_user.id].name = context.user_data.get("name", "")
        game.time_limit = 1.5
        game.players[0] = Player(user_id=0, name="Бот")
        game.status = "waiting"
        if not game.invite_keyboard_hidden:
            await hide_invite_keyboard(chat_id, thread_id, context)
            game.invite_keyboard_hidden = True
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
        if len(game.players) >= 2 and all(p.name for p in game.players.values()):
            code = next((c for c, gid in JOIN_CODES.items() if gid == game.game_id), None)
            if code:
                JOIN_CODES.pop(code, None)
            if not game.invite_keyboard_hidden:
                await hide_invite_keyboard(chat_id, thread_id, context)
                game.invite_keyboard_hidden = True
            await query.edit_message_text("Длительность установлена")
            await maybe_show_base_options(chat_id, thread_id, context, game)
            return
        code = next((c for c, gid in JOIN_CODES.items() if gid == game.game_id), None)
        if not code:
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
        game.invite_keyboard_hidden = False


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
    CHAT_GAMES[(user_id, 0)] = game.game_id
    await context.bot.send_message(
        user_id,
        "Введите ваше имя",
    )
    mark_awaiting_name(context, user_id)


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
        user = update.effective_user
        if user:
            fallback_game = get_game(user.id, 0)
            if fallback_game:
                fallback_game.player_chats[user.id] = chat_id
                CHAT_GAMES[(chat_id, thread_id or 0)] = fallback_game.game_id
                game = fallback_game
        if not game:
            await reply_game_message(
                message,
                context,
                "Игра не найдена, начните заново командой /start",
            )
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
    delivered: List[str] = []
    permanent_failures: List[Tuple[str, str]] = []
    transient_failures: List[Tuple[str, str]] = []

    def format_shared_user(shared_user: object) -> str:
        first_name = getattr(shared_user, "first_name", "") or ""
        last_name = getattr(shared_user, "last_name", "") or ""
        username = getattr(shared_user, "username", "") or ""
        user_id = getattr(shared_user, "user_id", None)
        name_parts = " ".join(part for part in [first_name.strip(), last_name.strip()] if part)
        if username:
            if name_parts:
                name_parts = f"{name_parts} (@{username})"
            else:
                name_parts = f"@{username}"
        if not name_parts:
            name_parts = f"ID {user_id}" if user_id is not None else "неизвестный пользователь"
        return name_parts

    for u in shared.users:
        user_label = format_shared_user(u)
        try:
            await context.bot.send_message(u.user_id, f"Приглашение в игру: {link}")
            game.invited_users.add(u.user_id)
            delivered.append(user_label)
        except (Forbidden, BadRequest) as exc:
            logger.warning("Failed to deliver invite to %s: %s", user_label, exc)
            permanent_failures.append((user_label, str(exc)))
        except TelegramError as exc:
            logger.warning("Temporary error delivering invite to %s: %s", user_label, exc)
            transient_failures.append((user_label, str(exc)))
        except Exception as exc:  # pragma: no cover - safeguard for unexpected errors
            logger.exception("Unexpected error delivering invite to %s", user_label)
            transient_failures.append((user_label, str(exc)))

    response_lines: List[str] = []
    if delivered:
        response_lines.append("✅ Приглашения доставлены: " + ", ".join(delivered))

    if permanent_failures or transient_failures:
        if permanent_failures:
            failures_text = "; ".join(
                f"{name} — бот не может начать диалог ({reason})"
                for name, reason in permanent_failures
            )
            response_lines.append("❌ Не удалось отправить: " + failures_text)
        if transient_failures:
            failures_text = "; ".join(f"{name} — {reason}" for name, reason in transient_failures)
            response_lines.append("⚠️ Временно не удалось отправить: " + failures_text)
        response_lines.append(
            "Передайте ссылку тем, кто не получил приглашение: "
            f"{link}. Попросите их открыть бота вручную или перешлите ссылку."
        )

    if not response_lines:
        response_lines.append("❌ Не удалось отправить приглашения. Попробуйте поделиться ссылкой вручную: " + link)

    await reply_game_message(message, context, "\n".join(response_lines))

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
    player = game.players.get(user_id)
    name = player.name if player and player.name else update.effective_user.first_name
    choice_handle = game.jobs.pop("base_choice", None)
    if isinstance(choice_handle, ChoiceTimerHandle):
        await choice_handle.complete(final_timer_text=None)
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
    text = f"Игра прервана участником {name}. Вы можете начать заново, нажав /start"
    await reply_game_message(update.message, context, text)
    await broadcast(game.game_id, text, skip_chat_id=chat_id)
    for user_id in list(game.players.keys()):
        clear_awaiting_name(context, user_id)
    BASE_MSG_IDS.pop(game.game_id, None)
    for cid in set(game.player_chats.values()):
        CHAT_GAMES.pop((cid, 0), None)
    ACTIVE_GAMES.pop(game.game_id, None)


async def reset_for_chat(chat_id: int, user_id: int, context: CallbackContext) -> None:
    """Reset and remove any game associated with the provided chat/user."""

    game_ids: Set[str] = set()

    for key, gid in list(CHAT_GAMES.items()):
        if key[0] == chat_id or key[0] == user_id:
            game_ids.add(gid)

    for gid, game in list(ACTIVE_GAMES.items()):
        if gid in game_ids:
            continue
        if chat_id in game.player_chats.values() or user_id in game.players:
            game_ids.add(gid)

    if not game_ids:
        return

    for gid in game_ids:
        game = ACTIVE_GAMES.get(gid)
        if not game:
            continue

        choice_handle = game.jobs.pop("base_choice", None)
        if isinstance(choice_handle, ChoiceTimerHandle):
            await choice_handle.complete(final_timer_text=None)

        for job in list(game.jobs.values()):
            if isinstance(job, ChoiceTimerHandle):
                await job.complete(final_timer_text=None)
            else:
                try:
                    job.schedule_removal()
                except Exception:
                    try:
                        job.cancel()
                    except Exception:
                        pass
        game.jobs.clear()

        for pid in list(game.players.keys()):
            clear_awaiting_name(context, pid)

        BASE_MSG_IDS.pop(gid, None)

        related_keys = set(game.base_msg_counts.keys())
        related_chats = set(game.player_chats.values())
        related_chats.add(chat_id)

        for key in related_keys:
            LAST_REFRESH.pop(key, None)

        for cid in related_chats:
            CHAT_GAMES.pop((cid, 0), None)
            LAST_REFRESH.pop((cid, 0), None)

        for code, stored_gid in list(JOIN_CODES.items()):
            if stored_gid == gid:
                JOIN_CODES.pop(code, None)

        ACTIVE_GAMES.pop(gid, None)


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
        old_handle = game.jobs.pop("base_choice", None)
        if isinstance(old_handle, ChoiceTimerHandle):
            await old_handle.complete(final_timer_text=None)

        targets: List[Tuple[int, Optional[int]]] = []
        for cid in set(game.player_chats.values()):
            thread = thread_id if cid == chat_id else None
            targets.append((cid, thread))

        async def auto_pick(handle: ChoiceTimerHandle) -> None:
            data = handle.data
            chat = data.get("chat_id")
            thread_local = data.get("thread_id")
            choices = data.get("words", [])
            if not chat or not choices:
                return
            game_state = get_game(chat, (thread_local or 0))
            if not game_state or game_state.base_word:
                return
            stored_handle = game_state.jobs.pop("base_choice", None)
            if isinstance(stored_handle, ChoiceTimerHandle) and stored_handle is not handle:
                await stored_handle.complete(final_timer_text=None)
            word_choice = random.choice(choices)
            await set_base_word(chat, thread_local, word_choice, handle.context)

        handle = await send_choice_with_timer(
            context=context,
            targets=targets,
            message_text="Выберите слово:",
            reply_markup=markup,
            send_func=send_game_message,
            on_timeout=auto_pick,
            data={"chat_id": chat_id, "thread_id": thread_id, "words": words},
            timeout_timer_text="Случайный выбор",
        )
        game.jobs["base_choice"] = handle

    elif query.data.startswith("pick_"):
        if game.base_word:
            return
        word = query.data.split("_", 1)[1]
        handle = game.jobs.pop("base_choice", None)
        if isinstance(handle, ChoiceTimerHandle):
            await handle.complete()
        await set_base_word(chat_id, thread_id, word, context, chosen_by=player.name)


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
    await broadcast(
        game.game_id,
        "<b>Новая функция</b>: в игре можно отправлять знак ? и слово, чтобы проверить определение любого слова у ИИ.",
        parse_mode="HTML",
    )
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


def _compose_word_history(game: GameState) -> List[Tuple[int, str]]:
    if game.word_history:
        return list(game.word_history)
    history: List[Tuple[int, str]] = []
    for player in game.players.values():
        for word in player.words:
            history.append((player.user_id, word))
    return history


def build_compose_stats_message(
    game: GameState, format_name: Callable[[Player], str]
) -> str:
    history = _compose_word_history(game)

    long_word_counts: List[Tuple[Player, int]] = []
    for player in game.players.values():
        count = sum(1 for word in player.words if len(word) >= 6)
        if count:
            long_word_counts.append((player, count))
    long_word_counts.sort(
        key=lambda item: (-item[1], item[0].name.casefold())
    )

    longest_length: int = 0
    longest_entries: List[Tuple[int, Player, str]] = []
    rarest_zipf: Optional[float] = None
    rarest_entries: List[Tuple[int, Player, str]] = []
    for index, (player_id, word) in enumerate(history):
        player = game.players.get(player_id)
        if not player:
            continue
        length = len(word)
        if length:
            if length > longest_length:
                longest_length = length
                longest_entries = [(index, player, word)]
            elif length == longest_length and not any(
                entry_player.user_id == player.user_id
                for _, entry_player, _ in longest_entries
            ):
                longest_entries.append((index, player, word))

        zipf = get_zipf(word)
        if zipf is None:
            continue
        if rarest_zipf is None or zipf < rarest_zipf:
            rarest_zipf = zipf
            rarest_entries = [(index, player, word)]
        elif zipf == rarest_zipf and not any(
            entry_player.user_id == player.user_id
            for _, entry_player, _ in rarest_entries
        ):
            rarest_entries.append((index, player, word))

    lines = ["<b>Интересная статистика</b>", ""]

    lines.append("🏅 <b>Лидеры по длинным словам (6 и более букв):</b>")
    if long_word_counts:
        for player, count in long_word_counts:
            lines.append(
                f"• {html.escape(format_name(player))} — {count} шт."
            )
    else:
        lines.append("Нет слов длиной 6+ букв.")
    lines.append("")

    lines.append("🏅 <b>Самое длинное слово</b>")
    if longest_entries:
        for _, player, word in sorted(longest_entries, key=lambda item: item[0]):
            lines.append(
                f"• {html.escape(word)} — {html.escape(format_name(player))}"
                f" ({longest_length} букв)"
            )
    else:
        lines.append("Нет данных о самых длинных словах.")
    lines.append("")

    lines.append("🏅 <b>Самое редкое слово</b>")
    if rarest_entries:
        for _, player, word in sorted(rarest_entries, key=lambda item: item[0]):
            lines.append(
                f"• {html.escape(word)} — {html.escape(format_name(player))}"
            )
    else:
        lines.append("Нет данных о редкости слов.")

    return "\n".join(lines).rstrip()


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
        if not lines or lines[-1] != "":
            lines.append("")
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
    await broadcast(game.game_id, message, parse_mode="HTML")
    stats_message = build_compose_stats_message(game, format_name)
    await broadcast(game.game_id, stats_message, parse_mode="HTML")
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
        game.game_id,
        "Выберите, как продолжить игру:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    choice_handle = game.jobs.pop("base_choice", None)
    if isinstance(choice_handle, ChoiceTimerHandle):
        await choice_handle.complete(final_timer_text=None)
    for job in list(game.jobs.values()):
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.jobs.clear()


async def reset_game(game: GameState) -> None:
    for p in game.players.values():
        p.words.clear()
        p.points = 0
    game.used_words.clear()
    game.base_word = ""
    game.letters.clear()
    game.status = "config"
    game.word_history.clear()
    choice_handle = game.jobs.pop("base_choice", None)
    if isinstance(choice_handle, ChoiceTimerHandle):
        await choice_handle.complete(final_timer_text=None)
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
    final_text = "Игра завершена. Для новой игры с новыми участниками нажмите /start"
    game = get_game(chat_id, thread_id or 0)
    if not game:
        if query.data == "restart_no":
            await query.edit_message_text(final_text)
        return
    if query.data == "restart_yes":
        await reset_game(game)
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
        text = final_text
        await query.edit_message_text(text)
        BASE_MSG_IDS.pop(game.game_id, None)
        sent: Set[int] = set()
        for cid in game.player_chats.values():
            if cid == chat_id or cid in sent:
                continue
            try:
                await context.bot.send_message(cid, text)
            except TelegramError:
                pass
            sent.add(cid)
        for cid in set(game.player_chats.values()):
            CHAT_GAMES.pop((cid, 0), None)
        ACTIVE_GAMES.pop(game.game_id, None)

async def question_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    text = message.text.strip()
    if not text.startswith("?"):
        return
    raw_word = text[1:].strip()
    word = normalize_word(raw_word)
    if not word:
        return
    game = get_game(message.chat_id, message.message_thread_id)
    player_name = ""
    if game:
        player = game.players.get(message.from_user.id) if message.from_user else None
        if player and player.name:
            player_name = player.name
    if not player_name and message.from_user:
        player_name = message.from_user.full_name or message.from_user.first_name or "Игрок"
    if not player_name:
        player_name = "Игрок"
    display_word = raw_word or word
    in_dict = word in DICT
    prefix = "Есть такое слово в словаре." if in_dict else "Этого слова нет в словаре игры"
    llm_text = await describe_word(word)
    response_text = (
        f"<b>{html.escape(player_name)}</b> запросил: <b>{html.escape(display_word)}</b>\n\n"
        f"<b>{html.escape(word)}</b> {prefix}\n\n{llm_text}"
    )
    delivered = False
    if game:
        sent_chats: Set[int] = set()
        for chat_id in game.player_chats.values():
            if chat_id in sent_chats:
                continue
            sent_chats.add(chat_id)
            try:
                await send_game_message(
                    chat_id,
                    None,
                    context,
                    response_text,
                    parse_mode="HTML",
                )
                if chat_id == message.chat_id:
                    delivered = True
            except TelegramError:
                logger.debug("Failed to send question response to chat %s", chat_id)
    if not delivered:
        await reply_game_message(
            message,
            context,
            response_text,
            parse_mode="HTML",
        )
    raise ApplicationHandlerStop


async def word_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_ts = perf_counter()
    # временный INFO-лог для подтверждения запуска обработчика
    try:
        logger.info(
            "word_message START chat_id=%s user=%s text=%r",
            update.effective_chat.id if update.effective_chat else None,
            update.effective_user.id if update.effective_user else None,
            update.effective_message.text if update.effective_message else None,
        )
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
        target = game
        if target and user_id in target.players and not target.players[user_id].name:
            await request_name(user_id, chat_id, context)
        elif not target:
            for g in ACTIVE_GAMES.values():
                p = g.players.get(user_id)
                if p and not p.name:
                    await request_name(user_id, chat_id, context)
                    break
        logger.debug(
            "word_message EXIT: no game or not running; game=%s status=%s chat=%s thread=%s",
            (game.game_id if game else None),
            (game.status if game else None),
            chat_id, thread_id
        )
        return
    game.player_chats[user_id] = chat.id
    CHAT_GAMES[(chat.id, 0)] = game.game_id
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
        emoji = "✅" if text.startswith("Зачтено") else "❌"
        try:
            await context.bot.send_message(user_id, emoji)
            await context.bot.send_message(user_id, text)
        except TelegramError:
            await send_game_message(
                chat_id,
                thread_id,
                context,
                f"{player_name} {emoji}",
            )
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
        game.word_history.append((player.user_id, w))
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
    if (
        not game
        or user_id != game.host_id
        or game.base_word
        or not update.message.reply_to_message
        or update.message.reply_to_message.from_user.id != context.bot.id
    ):
        return
    player = game.players.get(user_id)
    if not player or not player.name:
        await request_name(user_id, chat_id, context)
        return
    word = normalize_word(update.message.text)
    if len(word) < 8 or word not in DICT:
        await reply_game_message(update.message, context, "Неверное слово")
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
        game.word_history.append((bot_player.user_id, word))
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
        game.word_history.append((player.user_id, w))
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
                    allowed_updates=ALLOWED_UPDATES,
                )
                logger.info("Webhook registered: %s", expected_url)
            except Exception as e:
                logger.error("Webhook registration failed: %s", e)
        else:
            logger.warning("PUBLIC_URL not set; cannot register webhook")
    else:
        logger.info("Webhook is up-to-date: %s", info.url)

def register_handlers(application: Application, include_start: bool = False) -> None:
    """Register compose-word-game handlers on the given application."""
    global APPLICATION
    APPLICATION = application
    # 0) «Кран-тик» — логируем все апдейты как можно раньше
    application.add_handler(MessageHandler(filters.ALL, _tap, block=False), group=-2)
    # 1) Guard to require name before other commands
    application.add_handler(
        MessageHandler(filters.COMMAND, awaiting_name_guard), group=-1
    )
    if include_start:
        application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("newgame", newgame))
    application.add_handler(CommandHandler("join", join_cmd))
    application.add_handler(CommandHandler("exit", quit_cmd))
    application.add_handler(CommandHandler("chatid", chat_id_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND) & AWAITING_COMPOSE_NAME_FILTER,
            handle_name,
            block=False,
        ),
        group=-1,
    )
    application.add_handler(CallbackQueryHandler(time_selected, pattern="^(time_|adm_test)"))
    application.add_handler(CallbackQueryHandler(join_button, pattern="^join_"))
    application.add_handler(CallbackQueryHandler(base_choice, pattern="^(base_|pick_)", block=False))
    application.add_handler(CallbackQueryHandler(start_button, pattern="^start$"))
    application.add_handler(CallbackQueryHandler(restart_handler, pattern="^restart_"))
    application.add_handler(MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^Создать ссылку$"),
            invite_link,
        )
    )
    # 2) Поднять основной обработчик слов выше прочих текстовых; он неблокирующий
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Regex(r'^\?'),
            question_word,
            block=False,
        ),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), word_message, block=False),
        group=1,
    )
    # Остальные текстовые — ниже
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), manual_base_word, block=False),
        group=2,
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_submission,
            block=False,
        ),
        group=2,
    )

    def describe_handler(handler: object) -> str:
        callback = getattr(handler, "callback", None)
        if hasattr(callback, "__name__"):
            callback_name = callback.__name__
        elif hasattr(callback, "__class__"):
            callback_name = callback.__class__.__name__
        else:
            callback_name = repr(callback)
        return f"{handler.__class__.__name__}:{callback_name}"

    handler_structure = {
        group: [describe_handler(h) for h in handlers]
        for group, handlers in sorted(application.handlers.items())
    }
    logger.info("Application handlers by group: %s", handler_structure)


@app.on_event("startup")
async def on_startup() -> None:
    global APPLICATION, BOT_USERNAME
    APPLICATION = Application.builder().token(TOKEN).build()
    BOT_USERNAME = (await APPLICATION.bot.get_me()).username
    register_handlers(APPLICATION, include_start=True)

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
                allowed_updates=ALLOWED_UPDATES,
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
        allowed_updates=ALLOWED_UPDATES,
    )
    return JSONResponse({"url": webhook_url})


@app.get("/reset_webhook")
async def reset_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.delete_webhook(drop_pending_updates=False)
    await APPLICATION.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=ALLOWED_UPDATES,
    )
    return JSONResponse({"reset_to": webhook_url})


@app.get("/")
async def root() -> JSONResponse:
    """Base endpoint with brief service info."""
    return JSONResponse({"message": "Wordgame Magic service. See /healthz for status."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})

