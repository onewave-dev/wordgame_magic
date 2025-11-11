"""FastAPI app and Telegram bot for the «Гребешок» game.

This module implements a simplified version of the game described in
``grebeshok_game/AGENTS.md``.  The implementation focuses on the core
mechanics required by the task:

* FastAPI application with webhook endpoints and health checks.
* In‑memory game state with ``GameState`` and ``Player`` entities.
* Generation of letter combinations with filtering by heavy letters and
  dictionary viability.
* Commands ``/newgame``, ``/join``, ``/quit``/``/exit``.
* Validation of submitted words against the supplied dictionary and
  scoring with emoji events when a word contains at least six base
  letters.
* Job queue timers for the one‑minute warning and automatic game end.
* Admin test game with a dummy bot sending a valid word every 30 seconds.

The code intentionally keeps the logic compact.  Many features described in
``AGENTS.md`` (deep‑link invitations, rate limiting, etc.) can be added on
top of this foundation.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Message,
    Update,
)
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from llm_utils import describe_word
from shared.choice_timer import ChoiceTimerHandle, send_choice_with_timer
from shared.word_stats import get_zipf


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MESSAGE_RATE_LIMIT = float(os.environ.get("MESSAGE_RATE_LIMIT", "1"))
ALLOWED_UPDATES = ["message", "callback_query", "users_shared"]


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dictionary and letter helpers
# ---------------------------------------------------------------------------

# Working alphabet: Cyrillic letters without ``ъ`` and without a separate ``ё``.
ALPHABET = "абвгдежзийклмнопрстуфхцчшщьыэюя"
HEAVY_LETTERS: Set[str] = set(
    os.getenv(
        "HEAVY_LETTERS",
        "ж,з,й,ф,х,ц,ч,ш,щ,ь,ы,э,ю,я",
    ).replace(" ", "").split(",")
)


def load_dictionary(path: str) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """Load dictionary from JSONL and build a per-letter index."""

    words: Set[str] = set()
    letter_index: Dict[str, Set[str]] = {ch: set() for ch in ALPHABET}
    with open(path, encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            word = data.get("word", "").lower().replace("ё", "е")
            if not re.fullmatch(r"[а-я]+", word):
                continue
            words.add(word)
            for ch in set(word):
                if ch in letter_index:
                    letter_index[ch].add(word)
    logger.info("Loaded %d words", len(words))
    return words, letter_index


DICTIONARY, LETTER_INDEX = load_dictionary("nouns_ru_pymorphy2_yaspeller.jsonl")


# ---------------------------------------------------------------------------
# Core entities and in-memory state
# ---------------------------------------------------------------------------


@dataclass
class Player:
    user_id: int
    name: str = ""
    words: List[str] = field(default_factory=list)
    points: int = 0


@dataclass
class GameState:
    host_id: int
    time_limit: float = 3.0  # minutes
    letters_mode: int = 0
    base_letters: Tuple[str, ...] = field(default_factory=tuple)
    players: Dict[int, Player] = field(default_factory=dict)
    used_words: Set[str] = field(default_factory=set)
    status: str = "config"  # config|waiting|choosing|running|finished
    jobs: Dict[str, object] = field(default_factory=dict)
    combo_choices: List[Tuple[str, ...]] = field(default_factory=list)
    viability_threshold: int = int(os.getenv("VIABILITY_THRESHOLD", "50"))
    player_chats: Dict[int, int] = field(default_factory=dict)
    base_msg_counts: Dict[Tuple[int, int], int] = field(default_factory=dict)
    invite_keyboard_hidden: bool = False
    invited_users: Set[int] = field(default_factory=set)
    word_history: List[Tuple[int, str]] = field(default_factory=list)

    def game_id(self, chat_id: int, thread_id: Optional[int]) -> Tuple[int, int]:
        return (chat_id, thread_id or 0)


def format_player_name(player: Player) -> str:
    """Return player's name with bot emoji if this is the bot."""
    name = player.name
    if player.user_id == 0 or name.lower() in {"bot", "бот"}:
        name = f"🤖 {name}"
    return name


# Mapping ``(chat_id, thread_id) -> GameState``
ACTIVE_GAMES: Dict[Tuple[int, int], GameState] = {}

# Mapping personal chat IDs to running games for quick lookup
CHAT_GAMES: Dict[int, GameState] = {}

# Invite join codes -> game key
JOIN_CODES: Dict[str, Tuple[int, int]] = {}

INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Finished games stored for quick restart
FINISHED_GAMES: Dict[Tuple[int, int], GameState] = {}

# Message IDs for base letters buttons and throttling timestamps
BASE_MSG_IDS: Dict[Tuple[int, int], int] = {}
LAST_REFRESH: Dict[Tuple[int, int], float] = {}
REFRESH_LOCKS: Dict[Tuple[int, int], asyncio.Lock] = {}

# Users currently expected to provide their name
AWAITING_GREBESHOK_NAME_USERS: Set[int] = set()


class AwaitingGrebeshokNameFilter(filters.MessageFilter):
    """Filter that matches messages from players awaiting a name."""

    name = "grebeshok_awaiting_name"

    def filter(self, message: Message) -> bool:
        user = getattr(message, "from_user", None)
        return bool(user and user.id in AWAITING_GREBESHOK_NAME_USERS)


AWAITING_GREBESHOK_NAME_FILTER = AwaitingGrebeshokNameFilter()


def game_key(chat_id: int, thread_id: Optional[int]) -> Tuple[int, int]:
    return (chat_id, thread_id or 0)


def ensure_invite_code(
    game: GameState, context: Optional[CallbackContext] = None
) -> str:
    """Return an invite code for the game, generating one if necessary."""

    try:
        gid = game_key_from_state(game)
    except KeyError:
        host_chat_id = game.player_chats.get(game.host_id)
        gid = game.game_id(host_chat_id or 0, None) if host_chat_id else None
    code: Optional[str] = None
    if gid is not None:
        code = next((c for c, stored in JOIN_CODES.items() if stored == gid), None)
    if not code:
        code = "".join(random.choices(INVITE_CODE_ALPHABET, k=6))
        if gid is not None:
            JOIN_CODES[code] = gid
    if context is not None:
        context.user_data["invite_code"] = code
    return code


def get_game(chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
    game = CHAT_GAMES.get(chat_id)
    if game:
        return game
    gid = game_key(chat_id, thread_id)
    game = ACTIVE_GAMES.get(gid)
    if game:
        return game
    for g in ACTIVE_GAMES.values():
        if chat_id in g.player_chats.values():
            return g
    return None


# ---------------------------------------------------------------------------
# Letter combinations and helper functions
# ---------------------------------------------------------------------------


def viable_words(letters: Tuple[str, ...]) -> Set[str]:
    sets = [LETTER_INDEX.get(ch, set()) for ch in letters]
    if not sets:
        return set()
    words = set.intersection(*sets)
    return words


def generate_combinations(mode: int, viability_threshold: int) -> List[Tuple[str, ...]]:
    """Generate three viable combinations of base letters."""

    combos: List[Tuple[str, ...]] = []
    letters = list(ALPHABET)
    while len(combos) < 3:
        combo = tuple(random.sample(letters, mode))
        if sum(1 for c in combo if c in HEAVY_LETTERS) > 1:
            continue
        words = viable_words(combo)
        if len(words) < viability_threshold:
            continue
        combos.append(tuple(ch.upper() for ch in combo))
    logger.debug("Generated combos: %s", combos)
    return combos


async def broadcast(
    game: GameState,
    text: str,
    context: CallbackContext,
    reply_markup=None,
    parse_mode: Optional[str] = None,
    refresh: bool = True,
    skip_chat_id: Optional[int] = None,
) -> None:
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            if chat_id == skip_chat_id:
                continue
            try:
                await context.bot.send_message(
                    chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
                )
                if refresh:
                    schedule_refresh_base_letters(chat_id, 0, context)
            except Exception as exc:  # pragma: no cover - network issues
                logger.warning("Broadcast to %s failed: %s", chat_id, exc)


async def refresh_base_letters_button(
    chat_id: int, thread_id: int, context: CallbackContext
) -> None:
    """Resend base letters button to keep it the last message."""

    game = get_game(chat_id, thread_id)
    if (
        not game
        or game.status != "running"
        or not game.base_letters
    ):
        return
    key = (chat_id, thread_id)
    lock = REFRESH_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        msg_id = BASE_MSG_IDS.get(key)
        if msg_id:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        letters = " • ".join(ch.upper() for ch in game.base_letters)
        count = game.base_msg_counts.get(key, 0) + 1
        game.base_msg_counts[key] = count
        prefix = ""
        if count >= 5 and (count - 5) % 7 == 0:
            prefix = (
                "Можете отправлять знак ? и слово, чтобы проверить определение любого слова у ИИ.\n"
            )
        msg = await context.bot.send_message(
            chat_id,
            prefix + "Используйте буквы:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(letters, callback_data="noop")]]
            ),
            message_thread_id=thread_id or None,
        )
        BASE_MSG_IDS[key] = msg.message_id
        LAST_REFRESH[key] = asyncio.get_event_loop().time()


def schedule_refresh_base_letters(
    chat_id: int, thread_id: int, context: CallbackContext
) -> None:
    """Throttle refresh of the base letters button."""

    now = asyncio.get_event_loop().time()
    key = (chat_id, thread_id)
    last = LAST_REFRESH.get(key, 0)
    if now - last < 1:
        return
    LAST_REFRESH[key] = now
    asyncio.create_task(refresh_base_letters_button(chat_id, thread_id, context))


INVISIBLE_MESSAGE = "\u2063"


async def send_game_message(
    chat_id: int,
    thread_id: Optional[int],
    context: CallbackContext,
    text: str,
    refresh: bool = True,
    **kwargs,
):
    """Wrapper for ``send_message`` that schedules base letters refresh."""

    if thread_id is None:
        msg = await context.bot.send_message(chat_id, text, **kwargs)
    else:
        msg = await context.bot.send_message(
            chat_id, text, message_thread_id=thread_id, **kwargs
        )
    if refresh:
        schedule_refresh_base_letters(chat_id, thread_id or 0, context)
    return msg


async def hide_invite_keyboard(
    chat_id: int,
    thread_id: Optional[int],
    context: CallbackContext,
    *,
    refresh: bool = True,
) -> None:
    """Remove the invite keyboard without leaving a visible message."""

    msg = await send_game_message(
        chat_id,
        thread_id,
        context,
        INVISIBLE_MESSAGE,
        refresh=refresh,
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        await msg.delete()
    except TelegramError:
        logger.debug("Failed to delete invite keyboard removal message", exc_info=True)


async def reply_game_message(
    message, context: CallbackContext, text: str, refresh: bool = True, **kwargs
):
    msg = await message.reply_text(text, **kwargs)
    if refresh:
        schedule_refresh_base_letters(
            message.chat_id, message.message_thread_id or 0, context
        )
    return msg


async def awaiting_grebeshok_name_guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Ensure the user supplies a name before processing commands."""
    user = update.effective_user
    user_id = user.id if user else None
    awaiting = bool(user_id and user_id in AWAITING_GREBESHOK_NAME_USERS)
    if not awaiting:
        awaiting = context.user_data.get("awaiting_grebeshok_name", False)
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


def mark_awaiting_grebeshok_name(context: CallbackContext, user_id: int) -> None:
    AWAITING_GREBESHOK_NAME_USERS.add(user_id)
    context.user_data["awaiting_grebeshok_name"] = True
    if context.application:
        storage = getattr(context.application, "_user_data", None)
        if storage is not None:
            user_store = storage.setdefault(user_id, {})
        else:
            user_store = context.application.user_data.setdefault(user_id, {})
        user_store["awaiting_grebeshok_name"] = True


def clear_awaiting_grebeshok_name(
    context: CallbackContext, user_id: int
) -> None:
    """Remove awaiting name flags for a player from context storages."""
    AWAITING_GREBESHOK_NAME_USERS.discard(user_id)
    context.user_data.pop("awaiting_grebeshok_name", None)
    if context.application:
        storage = getattr(context.application, "_user_data", None)
        if storage is not None:
            user_store = storage.get(user_id)
            if user_store is not None:
                user_store.pop("awaiting_grebeshok_name", None)
                if not user_store:
                    storage.pop(user_id, None)
        else:
            user_store = context.application.user_data.get(user_id)
            if user_store is not None:
                user_store.pop("awaiting_grebeshok_name", None)
                if not user_store:
                    context.application.user_data.pop(user_id, None)


# ---------------------------------------------------------------------------
# Command and callback handlers
# ---------------------------------------------------------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for ``/start`` command."""

    if context.args and context.args[0].startswith("join_"):
        code = context.args[0][5:]
        context.args = [code]
        await join_cmd(update, context)
        return

    await newgame(update, context)


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Host starts a new game."""

    chat = update.effective_chat
    message = update.effective_message
    if chat.type != "private":
        await reply_game_message(message, context, "Запускать игру нужно в личном чате с ботом.")
        return

    gid = game_key(chat.id, message.message_thread_id)
    if gid in ACTIVE_GAMES:
        await reply_game_message(message, context, "Игра уже создана. Введите ваше имя:")
        return

    host_id = update.effective_user.id
    game = GameState(host_id=host_id)
    CHAT_GAMES[chat.id] = game
    game.players[host_id] = Player(user_id=host_id)
    game.player_chats[host_id] = chat.id
    ACTIVE_GAMES[gid] = game

    ensure_invite_code(game, context)

    mark_awaiting_grebeshok_name(context, host_id)
    await reply_game_message(message, context, "Игра создана. Введите ваше имя:")


async def invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send deep-link invitation to the host on text button press."""

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
                CHAT_GAMES[chat_id] = fallback_game
                game = fallback_game
        if not game:
            await reply_game_message(
                message,
                context,
                "Игра не найдена, начните заново командой /start",
            )
            return
    code = ensure_invite_code(game, context)
    link = f"https://t.me/{BOT_USERNAME}?start=join_{code}"
    await reply_game_message(
        message,
        context,
        f"Ссылка приглашения: {link}",
    )


async def users_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.users_shared:
        return
    shared = message.users_shared
    chat_id = update.effective_chat.id
    thread_id = message.message_thread_id
    game = get_game(chat_id, thread_id or 0)
    if not game:
        return
    code = ensure_invite_code(game, context)
    link = f"https://t.me/{BOT_USERNAME}?start=join_{code}"

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
            failures_text = "; ".join(
                f"{name} — {reason}" for name, reason in transient_failures
            )
            response_lines.append("⚠️ Временно не удалось отправить: " + failures_text)
        response_lines.append(
            "Передайте ссылку тем, кто не получил приглашение: "
            f"{link}. Попросите их открыть бота вручную или перешлите ссылку."
        )

    if not response_lines:
        response_lines.append(
            "❌ Не удалось отправить приглашения. Попробуйте поделиться ссылкой вручную: "
            + link
        )

    await reply_game_message(message, context, "\n".join(response_lines))


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply_game_message(update.message, context, "Использование: /join CODE")
        return
    code = context.args[0].strip()
    gid = JOIN_CODES.get(code)
    if not gid or gid not in ACTIVE_GAMES:
        await reply_game_message(update.message, context, "Игра не найдена.")
        return
    game = ACTIVE_GAMES[gid]
    user_id = update.effective_user.id
    if user_id in game.players:
        await reply_game_message(update.message, context, "Вы уже участвуете.")
        return
    if len(game.players) >= 5:
        await reply_game_message(update.message, context, "Лобби заполнено.")
        return
    game.players[user_id] = Player(user_id=user_id)
    chat_id = update.effective_chat.id
    game.player_chats[user_id] = chat_id
    CHAT_GAMES[chat_id] = game
    mark_awaiting_grebeshok_name(context, user_id)
    await reply_game_message(update.message, context, "Введите ваше имя:")


async def quit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    game = next((g for g in ACTIVE_GAMES.values() if user_id in g.players), None)
    if not game:
        await reply_game_message(update.message, context, "Вы не в игре.")
        return
    player = game.players.get(user_id)
    name = (
        format_player_name(player)
        if player and player.name
        else update.effective_user.first_name
    )
    message = (
        f"Игра прервана участником {name}. Вы можете начать заново, нажав /start"
    )
    handle = game.jobs.pop("combo_choice", None)
    if isinstance(handle, ChoiceTimerHandle):
        await handle.complete(final_timer_text=None)
    for job in game.jobs.values():
        try:
            job.schedule_removal()
        except Exception:
            try:
                job.cancel()
            except Exception:
                pass
    game.jobs.clear()
    for member_id in list(game.players.keys()):
        clear_awaiting_grebeshok_name(context, member_id)
    await broadcast(game, message, context, skip_chat_id=chat_id)
    await reply_game_message(update.message, context, message)
    for cid in list(game.player_chats.values()):
        CHAT_GAMES.pop(cid, None)
    gid = game_key_from_state(game)
    ACTIVE_GAMES.pop(gid, None)


async def reset_for_chat(chat_id: int, user_id: int, context: CallbackContext) -> None:
    """Stop jobs and remove any Grebeshok game bound to the chat or user."""

    active_keys: Set[Tuple[int, int]] = set()

    for key, game in list(ACTIVE_GAMES.items()):
        if key[0] == chat_id or chat_id in game.player_chats.values() or user_id in game.players:
            active_keys.add(key)

    for candidate in (chat_id, user_id):
        game = CHAT_GAMES.get(candidate)
        if not game:
            continue
        try:
            key = game_key_from_state(game)
        except KeyError:
            continue
        active_keys.add(key)

    finished_keys: Set[Tuple[int, int]] = set()
    for key, game in list(FINISHED_GAMES.items()):
        if key[0] == chat_id or chat_id in game.player_chats.values() or user_id in game.players:
            finished_keys.add(key)

    players_to_clear: Set[int] = set()
    for key in active_keys:
        game = ACTIVE_GAMES.get(key)
        if game:
            players_to_clear.update(game.players.keys())
    for key in finished_keys:
        game = FINISHED_GAMES.get(key)
        if game:
            players_to_clear.update(game.players.keys())

    for pid in players_to_clear:
        clear_awaiting_grebeshok_name(context, pid)

    for key in list(active_keys):
        game = ACTIVE_GAMES.get(key)
        if not game:
            continue

        handle = game.jobs.pop("combo_choice", None)
        if isinstance(handle, ChoiceTimerHandle):
            await handle.complete(final_timer_text=None)
            await cleanup_choice_messages(handle)

        for job in list(game.jobs.values()):
            if isinstance(job, ChoiceTimerHandle):
                await job.complete(final_timer_text=None)
                await cleanup_choice_messages(job)
            else:
                try:
                    job.schedule_removal()
                except Exception:
                    try:
                        job.cancel()
                    except Exception:
                        pass
        game.jobs.clear()

        related_chats = set(game.player_chats.values())
        related_keys = set(game.base_msg_counts.keys())
        related_keys.add(key)

        for base_key in list(related_keys):
            BASE_MSG_IDS.pop(base_key, None)
            LAST_REFRESH.pop(base_key, None)
            REFRESH_LOCKS.pop(base_key, None)

        for cid in related_chats:
            CHAT_GAMES.pop(cid, None)
            base_key = (cid, 0)
            BASE_MSG_IDS.pop(base_key, None)
            LAST_REFRESH.pop(base_key, None)
            REFRESH_LOCKS.pop(base_key, None)

        for code, stored_key in list(JOIN_CODES.items()):
            if stored_key == key:
                JOIN_CODES.pop(code, None)

        ACTIVE_GAMES.pop(key, None)
        FINISHED_GAMES.pop(key, None)

    for key in list(finished_keys):
        game = FINISHED_GAMES.pop(key, None)
        if not game:
            continue

        related_chats = set(game.player_chats.values())
        related_keys = set(game.base_msg_counts.keys())
        related_keys.add(key)

        for base_key in list(related_keys):
            BASE_MSG_IDS.pop(base_key, None)
            LAST_REFRESH.pop(base_key, None)
            REFRESH_LOCKS.pop(base_key, None)

        for cid in related_chats:
            CHAT_GAMES.pop(cid, None)
            base_key = (cid, 0)
            BASE_MSG_IDS.pop(base_key, None)
            LAST_REFRESH.pop(base_key, None)
            REFRESH_LOCKS.pop(base_key, None)

        for code, stored_key in list(JOIN_CODES.items()):
            if stored_key == key:
                JOIN_CODES.pop(code, None)


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id if user else None
    awaiting = bool(user_id and user_id in AWAITING_GREBESHOK_NAME_USERS)
    if not awaiting:
        awaiting = context.user_data.get("awaiting_grebeshok_name", False)
    logger.debug("NAME: entered, awaiting=%s", awaiting)
    if not awaiting or user_id is None:
        return
    name = update.message.text.strip()
    game = next((g for g in ACTIVE_GAMES.values() if user_id in g.players), None)
    if not game:
        return
    player = game.players[user_id]
    player.name = name
    clear_awaiting_grebeshok_name(context, user_id)
    formatted_name = format_player_name(player)
    await reply_game_message(
        update.message, context, f"Имя установлено: {formatted_name}"
    )
    if game.status == "config" and user_id == game.host_id:
        buttons = [
            [
                InlineKeyboardButton("3 минуты", callback_data="greb_time_3"),
                InlineKeyboardButton("5 минут", callback_data="greb_time_5"),
            ]
        ]
        if user_id == ADMIN_ID:
            buttons.append([
                InlineKeyboardButton(
                    "[адм.] Тестовая игра", callback_data="greb_adm_test"
                )
            ])
        await reply_game_message(
            update.message,
            context,
            "Выберите длительность игры:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await broadcast(game, f"{formatted_name} присоединился к игре", context)
        if len(game.players) >= 2:
            if not game.letters_mode:
                await prompt_letters_selection(game, context)
            elif not game.combo_choices:
                await send_combo_choices(game, context)
    logger.debug("NAME: set '%s' -> stop pipeline", name)
    raise ApplicationHandlerStop


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    thread_id = query.message.message_thread_id
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
    if not game or query.from_user.id != game.host_id:
        return
    if query.data == "greb_adm_test" and query.from_user.id == ADMIN_ID:
        game.time_limit = 1.5
        game.players[0] = Player(user_id=0, name="Бот")
        game.status = "waiting"
        if not game.invite_keyboard_hidden:
            await hide_invite_keyboard(chat.id, thread_id, context)
            game.invite_keyboard_hidden = True
        await query.edit_message_text("Тестовая игра: выберите режим букв")
    elif query.data.startswith("greb_time_"):
        try:
            game.time_limit = int(query.data.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            logger.warning("Unexpected greb_time callback data: %s", query.data)
            return
        game.status = "waiting"
        ready_to_start = len(game.players) >= 2 and all(
            p.name for p in game.players.values()
        )
        if ready_to_start:
            await query.edit_message_text("Длительность установлена")
            if not game.invite_keyboard_hidden:
                await hide_invite_keyboard(chat.id, thread_id, context)
                game.invite_keyboard_hidden = True
        else:
            await query.edit_message_text("Игра создана. Пригласите участников.")
            ensure_invite_code(game, context)
            keyboard = ReplyKeyboardMarkup(
                [
                    [
                        KeyboardButton(
                            "Пригласить из контактов",
                            request_users=KeyboardButtonRequestUsers(request_id=1),
                        ),
                        KeyboardButton("Создать ссылку"),
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

    await prompt_letters_selection(game, context)


async def prompt_letters_selection(game: GameState, context: CallbackContext) -> None:
    if len(game.players) < 2 or game.letters_mode:
        return
    chat_id = game.player_chats.get(game.host_id)
    if not chat_id:
        return
    if not game.invite_keyboard_hidden:
        await hide_invite_keyboard(chat_id, None, context)
        game.invite_keyboard_hidden = True
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("3 буквы", callback_data="letters_3"),
                InlineKeyboardButton("4 буквы", callback_data="letters_4"),
            ]
        ]
    )
    await send_game_message(chat_id, None, context, "Выберите режим:", reply_markup=keyboard)


async def letters_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
    if (
        not game
        or query.from_user.id != game.host_id
        or game.letters_mode
        or len(game.players) < 2
    ):
        return
    game.letters_mode = int(query.data.split("_")[1])
    try:
        await query.delete_message()
    except Exception:
        pass
    await send_combo_choices(game, context)


async def send_combo_choices(game: GameState, context: CallbackContext) -> None:
    if game.status != "waiting" or len(game.players) < 2 or not game.letters_mode:
        return
    game.combo_choices = generate_combinations(
        game.letters_mode, game.viability_threshold
    )
    buttons = [
        [InlineKeyboardButton(" • ".join(combo), callback_data=f"combo_{i}")]
        for i, combo in enumerate(game.combo_choices)
    ]
    markup = InlineKeyboardMarkup(buttons)

    targets: List[Tuple[int, Optional[int]]] = []
    for uid in game.players:
        chat_id = game.player_chats.get(uid)
        if chat_id:
            targets.append((chat_id, None))
    if not targets:
        return

    gid = game_key_from_state(game)

    async def auto_pick(handle: ChoiceTimerHandle) -> None:
        key = handle.data.get("game_key")
        game_state = ACTIVE_GAMES.get(key) if key else None
        if not game_state or game_state.base_letters:
            return
        await auto_pick_combo(game_state, handle.context, handle)

    old_handle = game.jobs.pop("combo_choice", None)
    if isinstance(old_handle, ChoiceTimerHandle):
        await old_handle.complete(final_timer_text=None)
        await cleanup_choice_messages(old_handle)

    handle = await send_choice_with_timer(
        context=context,
        targets=targets,
        message_text="Выберите комбинацию:",
        reply_markup=markup,
        send_func=send_game_message,
        on_timeout=auto_pick,
        data={"game_key": gid},
        final_timer_text=None,
        timeout_timer_text="Случайный выбор",
    )
    game.status = "choosing"
    game.jobs["combo_choice"] = handle


async def cleanup_choice_messages(handle: ChoiceTimerHandle) -> None:
    """Delete choice and timer messages associated with a handle."""

    bot = handle.context.bot
    for attr in ("messages", "timer_messages"):
        entries = getattr(handle, attr, [])
        for chat_id, thread_id, message_id in list(entries):
            try:
                kwargs = {"chat_id": chat_id, "message_id": message_id}
                if thread_id is not None:
                    kwargs["message_thread_id"] = thread_id
                await bot.delete_message(**kwargs)
            except Exception:
                continue
        try:
            entries.clear()
        except Exception:
            pass


def game_key_from_state(game: GameState) -> Tuple[int, int]:
    # Reverse lookup in ACTIVE_GAMES
    for key, g in ACTIVE_GAMES.items():
        if g is game:
            return key
    raise KeyError("Game not found in ACTIVE_GAMES")


async def send_start_prompt(game: GameState, context: CallbackContext) -> None:
    """Send a start button to the game initiator."""

    chat_id = game.player_chats.get(game.host_id)
    if not chat_id:
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Старт", callback_data="start_round")]]
    )
    await send_game_message(
        chat_id,
        None,
        context,
        "Нажмите «Старт», чтобы начать раунд",
        reply_markup=keyboard,
    )


async def auto_pick_combo(
    game: GameState,
    context: CallbackContext,
    handle: Optional[ChoiceTimerHandle] = None,
) -> None:
    if game.base_letters:
        return

    job_handle = game.jobs.pop("combo_choice", None)
    primary_handle: Optional[ChoiceTimerHandle]
    if isinstance(handle, ChoiceTimerHandle):
        primary_handle = handle
    elif isinstance(job_handle, ChoiceTimerHandle):
        primary_handle = job_handle
    else:
        primary_handle = None

    if isinstance(job_handle, ChoiceTimerHandle) and job_handle is not primary_handle:
        await job_handle.complete(final_timer_text=None)
        await cleanup_choice_messages(job_handle)

    if isinstance(primary_handle, ChoiceTimerHandle):
        await primary_handle.complete(final_timer_text=None)
        await cleanup_choice_messages(primary_handle)

    choice = random.choice(game.combo_choices)
    game.base_letters = tuple(ch.lower() for ch in choice)
    await broadcast(
        game, f"Случайный выбор: {' • '.join(choice)}", context, refresh=False
    )
    await send_start_prompt(game, context)


async def combo_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()
    message = query.message
    chat = message.chat
    game = get_game(chat.id, message.message_thread_id)
    if not game or game.base_letters:
        return
    if not query.data.startswith("combo_"):
        return
    idx = int(query.data.split("_")[1])
    if idx >= len(game.combo_choices):
        return
    game.base_letters = tuple(ch.lower() for ch in game.combo_choices[idx])
    chooser = game.players.get(query.from_user.id)
    if chooser:
        await broadcast(
            game,
            f"{chooser.name} выбрал(а) буквы: {' • '.join(game.combo_choices[idx])}",
            context,
            refresh=False,
        )
    else:
        await broadcast(
            game,
            f"Буквы выбраны: {' • '.join(game.combo_choices[idx])}",
            context,
            refresh=False,
        )
    handle = game.jobs.pop("combo_choice", None)
    if isinstance(handle, ChoiceTimerHandle):
        await handle.complete(final_timer_text=None)
        await cleanup_choice_messages(handle)
    await send_start_prompt(game, context)


async def start_round_cb(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
    if (
        not game
        or game.status != "choosing"
        or not game.base_letters
        or query.from_user.id != game.host_id
    ):
        return
    await query.edit_message_reply_markup(None)
    await start_round(game, context)


async def start_round(game: GameState, context: CallbackContext) -> None:
    game.word_history.clear()
    game.status = "running"
    gid = game_key_from_state(game)
    warn_time = game.time_limit * 60 - 60
    if warn_time > 0:
        game.jobs["warn"] = context.job_queue.run_once(
            one_minute_warning, warn_time, data=gid
        )
        start_message = "Игра началась!"
    else:
        game.jobs.pop("warn", None)
        start_message = "Игра началась!\nОсталась 1 минута!"
    game.jobs["end"] = context.job_queue.run_once(end_game_job, game.time_limit * 60, data=gid)
    if 0 in game.players:  # dummy bot
        game.jobs["dummy"] = context.job_queue.run_repeating(dummy_bot_word, 30, data=gid)
    await broadcast(game, start_message, context, refresh=False)
    await broadcast(
        game,
        "<b>Новая функция</b>: в игре можно отправлять знак ? и слово, чтобы проверить определение любого слова у ИИ.",
        context,
        parse_mode="HTML",
        refresh=False,
    )
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            await context.bot.unpin_all_chat_messages(chat_id)
            schedule_refresh_base_letters(chat_id, 0, context)


async def one_minute_warning(context: CallbackContext) -> None:
    gid = context.job.data
    game = ACTIVE_GAMES.get(gid)
    if game:
        await broadcast(game, "Осталась 1 минута!", context)


async def end_game_job(context: CallbackContext) -> None:
    gid = context.job.data
    game = ACTIVE_GAMES.get(gid)
    if game:
        await finish_game(game, context, "Время вышло")


def _grebeshok_history(game: GameState) -> List[Tuple[int, str]]:
    if game.word_history:
        return list(game.word_history)
    history: List[Tuple[int, str]] = []
    for player in game.players.values():
        for word in player.words:
            history.append((player.user_id, word))
    return history


def build_grebeshok_stats_message(game: GameState) -> str:
    history = _grebeshok_history(game)
    base_letters = tuple(ch.lower().replace("ё", "е") for ch in game.base_letters)

    longest_length: int = 0
    longest_entries: List[Tuple[int, Player, str]] = []
    richest_base: Dict[int, Tuple[int, int, str]] = {}
    max_base_count = 0
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

        base_count = sum(1 for ch in word.replace("ё", "е") if ch in base_letters)
        if base_count:
            best = richest_base.get(player_id)
            if best is None or base_count > best[0] or (
                base_count == best[0] and index < best[1]
            ):
                richest_base[player_id] = (base_count, index, word)
            if base_count > max_base_count:
                max_base_count = base_count

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

    lines.append("🏅 <b>Самое длинное слово</b>")
    if longest_entries:
        for _, player, word in sorted(longest_entries, key=lambda item: item[0]):
            lines.append(
                f"• {html.escape(word)} — {html.escape(format_player_name(player))}"
                f" ({longest_length} букв)"
            )
    else:
        lines.append("Нет данных о самых длинных словах.")
    lines.append("")

    lines.append("🧩 <b>Слово с наибольшим количеством базовых букв</b>")
    base_threshold = len(base_letters)
    if max_base_count > base_threshold:
        winners: List[Tuple[int, Player, str, int]] = []
        for player_id, (count, idx, word) in richest_base.items():
            if count == max_base_count:
                player = game.players.get(player_id)
                if not player:
                    continue
                winners.append((idx, player, word, count))
        winners.sort(key=lambda item: (item[0], item[1].name.casefold()))
        for _, player, word, count in winners:
            lines.append(
                f"• {html.escape(word)} — {html.escape(format_player_name(player))}"
                f" ({count} букв из базовых)"
            )
    else:
        lines.append(
            "Никто не составил слова с более чем "
            f"{base_threshold} базовыми буквами."
        )
    lines.append("")

    lines.append("🏅 <b>Самое редкое слово</b>")
    if rarest_entries:
        for _, player, word in sorted(rarest_entries, key=lambda item: item[0]):
            lines.append(
                f"• {html.escape(word)} — {html.escape(format_player_name(player))}"
            )
    else:
        lines.append("Нет данных о редкости слов.")

    return "\n".join(lines).rstrip()


async def finish_game(game: GameState, context: CallbackContext, reason: str) -> None:
    gid = game_key_from_state(game)
    handle = game.jobs.pop("combo_choice", None)
    if isinstance(handle, ChoiceTimerHandle):
        await handle.complete(final_timer_text=None)
    for job in game.jobs.values():
        try:
            job.schedule_removal()
        except Exception:
            pass
    game.status = "finished"
    letters = " • ".join(ch.upper() for ch in game.base_letters)
    players_sorted = sorted(
        game.players.values(), key=lambda p: p.points, reverse=True
    )

    lines = [
        "<b>Игра окончена!</b>",
        "<b>Результаты:</b>",
        "",
        f"<b>Буквы:</b> {letters}",
        "",
    ]
    for p in players_sorted:
        lines.append(html.escape(format_player_name(p)))
        for i, w in enumerate(p.words, 1):
            lines.append(f"{i}. {html.escape(w)}")
        lines.append(f"<b>Итог:</b> {p.points}")
        lines.append("")
    max_points = players_sorted[0].points if players_sorted else 0
    winners = [p for p in players_sorted if p.points == max_points]
    if winners:
        if not lines or lines[-1] != "":
            lines.append("")
        if len(winners) == 1:
            lines.append(
                f"🏆 <b>Победитель:</b> {html.escape(format_player_name(winners[0]))}"
            )
        else:
            lines.append(
                "🏆 <b>Победители:</b> "
                + ", ".join(html.escape(format_player_name(p)) for p in winners)
            )
    elif lines and lines[-1] == "":
        lines.pop()

    text = "\n".join(lines).rstrip()
    await broadcast(game, text, context, parse_mode="HTML")

    stats_message = build_grebeshok_stats_message(game)
    await broadcast(game, stats_message, context, parse_mode="HTML")

    # Prepare restart keyboard and send to players
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Новая игра", callback_data=f"restart_{gid[0]}_{gid[1]}")]]
    )
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            try:
                await send_game_message(
                    chat_id,
                    None,
                    context,
                    "Новая игра с теми же участниками?",
                    reply_markup=keyboard,
                )
                await send_game_message(
                    chat_id,
                    None,
                    context,
                    "Либо нажмите /start для запуска новой сессии игры",
                )
            except Exception:
                pass
    for cid in list(game.player_chats.values()):
        CHAT_GAMES.pop(cid, None)

    # Move game to finished store for possible restart
    ACTIVE_GAMES.pop(gid, None)
    FINISHED_GAMES[gid] = game


async def restart_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the «Новая игра» button."""

    query = update.callback_query
    await query.answer()
    try:
        _, chat_id, thread_id = query.data.split("_")
    except ValueError:
        return
    old_gid = (int(chat_id), int(thread_id))
    old_game = FINISHED_GAMES.get(old_gid)
    if not old_game or query.from_user.id not in old_game.players:
        return

    new_host_chat = query.message.chat
    new_gid = game_key(new_host_chat.id, query.message.message_thread_id)
    new_host_id = query.from_user.id

    # Remove any leftover invite codes so no further invitations are suggested
    context.user_data.pop("invite_code", None)

    new_game = GameState(host_id=new_host_id)
    for uid, player in old_game.players.items():
        new_game.players[uid] = Player(user_id=uid, name=player.name)
    new_game.player_chats = old_game.player_chats.copy()
    new_game.player_chats[new_host_id] = new_host_chat.id

    is_admin_test = 0 in old_game.players or math.isclose(old_game.time_limit, 1.5)
    if is_admin_test:
        new_game.time_limit = 1.5
        new_game.status = "waiting"

    ACTIVE_GAMES[new_gid] = new_game
    FINISHED_GAMES.pop(old_gid, None)

    starter = new_game.players[new_host_id]
    await broadcast(
        new_game, f"{starter.name} начал(а) новую игру", context
    )
    if is_admin_test:
        await send_game_message(
            new_host_chat.id,
            None,
            context,
            "Тестовая игра: выберите режим букв",
        )
        await prompt_letters_selection(new_game, context)
    else:
        buttons = [
            [
                InlineKeyboardButton("3 минуты", callback_data="greb_time_3"),
                InlineKeyboardButton("5 минут", callback_data="greb_time_5"),
            ]
        ]
        await send_game_message(
            new_host_chat.id,
            None,
            context,
            "Выберите длительность игры:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def dummy_bot_word(context: CallbackContext) -> None:
    gid = context.job.data
    game = ACTIVE_GAMES.get(gid)
    if not game or not game.base_letters:
        return
    words = list(viable_words(tuple(game.base_letters)))
    if not words:
        return
    word = random.choice(words)
    player = game.players[0]
    if word in game.used_words:
        return
    if any(word.count(b) < 1 for b in game.base_letters):
        return
    player.words.append(word)
    player.points += 1
    game.used_words.add(word)
    game.word_history.append((player.user_id, word))
    await broadcast(game, f"{format_player_name(player)}: {word}", context)


async def question_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    text = message.text.strip()
    if not text.startswith("?"):
        return
    raw_word = text[1:].strip()
    if not raw_word:
        return
    display_word = raw_word or ""
    word_token = raw_word.split()[0]
    word = word_token.lower().replace("ё", "е")
    if not word:
        return

    game = get_game(message.chat_id, message.message_thread_id)
    player_name = ""
    user = message.from_user
    if game and user:
        game.player_chats[user.id] = message.chat_id
        player = game.players.get(user.id)
        if player and player.name:
            player_name = player.name
    if not player_name and user:
        player_name = (
            user.full_name
            or user.first_name
            or user.username
            or "Игрок"
        )
    if not player_name:
        player_name = "Игрок"

    prefix = (
        "Есть такое слово в словаре."
        if word in DICTIONARY
        else "Этого слова нет в словаре игры"
    )
    llm_text = await describe_word(word)
    escaped_llm_text = html.escape(llm_text or "")
    response_lines = [
        (
            f"<b>{html.escape(player_name)}</b> запросил: "
            f"<b>{html.escape(display_word or word)}</b>"
        ),
        "",
        f"<b>{html.escape(word)}</b> {prefix}",
    ]
    if escaped_llm_text:
        response_lines.extend(["", escaped_llm_text])
    response_text = "\n".join(response_lines)

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
                logger.debug(
                    "Failed to send question response to chat %s", chat_id
                )
    if not delivered:
        await reply_game_message(
            message,
            context,
            response_text,
            parse_mode="HTML",
        )
    raise ApplicationHandlerStop


async def handle_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = asyncio.get_running_loop().time()
    last_time = context.user_data.get("last_message_time")
    if last_time and now - last_time < MESSAGE_RATE_LIMIT:
        await reply_game_message(
            update.message, context, "Слишком часто! Подождите немного."
        )
        context.user_data["last_message_time"] = now
        logger.debug("Rate limit hit for user %s", update.effective_user.id)
        return
    context.user_data["last_message_time"] = now

    text = update.message.text.lower().replace("ё", "е")
    words = text.split()
    if not words:
        return
    chat = update.effective_chat
    gid = game_key(chat.id, update.message.message_thread_id)
    game = get_game(chat.id, update.message.message_thread_id)
    user_id = update.effective_user.id
    if not game:
        game = next((g for g in ACTIVE_GAMES.values() if user_id in g.players), None)
        gid = game_key_from_state(game) if game else None
    if not game or game.status != "running":
        return
    game.player_chats[user_id] = chat.id
    CHAT_GAMES[chat.id] = game
    player = game.players.get(user_id)
    if not player:
        return

    accepted: list[str] = []
    rejected: list[str] = []
    for word in words:
        if game.status != "running":
            logger.debug(
                "Game %s status switched to %s, skipping remaining words",
                gid,
                game.status,
            )
            break
        if not re.fullmatch(r"[а-я]+", word):
            rejected.append(f"{word} (недопустимые символы)")
            continue
        if word not in DICTIONARY:
            rejected.append(f"{word} (такого слова нет в словаре)")
            continue
        if any(word.count(b) < 1 for b in game.base_letters):
            rejected.append(f"{word} (слово не содержит все буквы)")
            continue
        if word in player.words:
            rejected.append(f"{word} (вы уже использовали это слово)")
            continue
        if word in game.used_words:
            rejected.append(f"{word} (уже использовано другим игроком)")
            continue
        player.words.append(word)
        player.points += 1
        game.used_words.add(word)
        game.word_history.append((player.user_id, word))
        accepted.append(word)
        await broadcast(
            game,
            f"{format_player_name(player)}: {word}",
            context,
            skip_chat_id=chat.id,
        )
        if sum(word.count(b) for b in game.base_letters) >= 6:
            await broadcast(
                game,
                f"🔥 {format_player_name(player)} прислал мощное слово!",
                context,
                skip_chat_id=chat.id,
            )

    if accepted:
        await reply_game_message(update.message, context, "✅", refresh=False)
        await reply_game_message(
            update.message,
            context,
            "Зачтены: " + ", ".join(accepted),
            refresh=False,
        )
    if rejected:
        await reply_game_message(update.message, context, "❌", refresh=False)
        await reply_game_message(
            update.message,
            context,
            "Отклонены: " + ", ".join(rejected),
            refresh=False,
        )

    schedule_refresh_base_letters(
        chat.id, update.message.message_thread_id or 0, context
    )


# ---------------------------------------------------------------------------
# Handler registration and FastAPI setup
# ---------------------------------------------------------------------------


APPLICATION: Optional[Application] = None
BOT_USERNAME: str = ""


def register_handlers(application: Application, include_start: bool = False) -> None:
    global APPLICATION
    APPLICATION = application
    if include_start:
        application.add_handler(CommandHandler("start", start_cmd))
    # Guard to require players to introduce themselves first
    application.add_handler(
        MessageHandler(filters.COMMAND, awaiting_grebeshok_name_guard), group=-1
    )
    application.add_handler(CommandHandler("newgame", newgame))
    application.add_handler(CommandHandler("join", join_cmd))
    application.add_handler(CommandHandler("exit", quit_cmd))
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND) & AWAITING_GREBESHOK_NAME_FILTER,
            handle_name,
            block=True,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.Regex("^Создать ссылку$"), invite_link),
        group=0,
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.USERS_SHARED, users_shared_handler)
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Regex(r'^\?'),
            question_word,
            block=False,
        ),
        group=1,
    )
    application.add_handler(
        CallbackQueryHandler(time_selected, pattern="^(greb_time_|greb_adm_test)")
    )
    application.add_handler(CallbackQueryHandler(letters_selected, pattern="^letters_"))
    application.add_handler(CallbackQueryHandler(combo_chosen, pattern="^combo_"))
    application.add_handler(CallbackQueryHandler(start_round_cb, pattern="^start_round$"))
    application.add_handler(CallbackQueryHandler(restart_game, pattern="^restart_"))
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_word, block=False),
        group=1,
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


# FastAPI application -------------------------------------------------------

app = FastAPI()


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


async def webhook_check(context: CallbackContext) -> None:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}" if PUBLIC_URL else ""
    info = await APPLICATION.bot.get_webhook_info()
    if webhook_url and info.url != webhook_url:
        logger.warning("Webhook desynced; resetting")
        await APPLICATION.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=ALLOWED_UPDATES,
        )


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
    return JSONResponse({"message": "Grebeshok game service. See /healthz."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


__all__ = [
    "app",
    "register_handlers",
    "start_cmd",
]

