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

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MESSAGE_RATE_LIMIT = float(os.environ.get("MESSAGE_RATE_LIMIT", "1"))


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
    time_limit: int = 3  # minutes
    letters_mode: int = 3
    base_letters: Tuple[str, ...] = field(default_factory=tuple)
    players: Dict[int, Player] = field(default_factory=dict)
    used_words: Set[str] = field(default_factory=set)
    status: str = "config"  # config|waiting|choosing|running|finished
    jobs: Dict[str, object] = field(default_factory=dict)
    combo_choices: List[Tuple[str, ...]] = field(default_factory=list)
    viability_threshold: int = int(os.getenv("VIABILITY_THRESHOLD", "50"))
    player_chats: Dict[int, int] = field(default_factory=dict)

    def game_id(self, chat_id: int, thread_id: Optional[int]) -> Tuple[int, int]:
        return (chat_id, thread_id or 0)


# Mapping ``(chat_id, thread_id) -> GameState``
ACTIVE_GAMES: Dict[Tuple[int, int], GameState] = {}

# Invite join codes -> game key
JOIN_CODES: Dict[str, Tuple[int, int]] = {}

# Finished games stored for quick restart
FINISHED_GAMES: Dict[Tuple[int, int], GameState] = {}


def game_key(chat_id: int, thread_id: Optional[int]) -> Tuple[int, int]:
    return (chat_id, thread_id or 0)


def get_game(chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
    return ACTIVE_GAMES.get(game_key(chat_id, thread_id))


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


async def broadcast(game: GameState, text: str, context: CallbackContext) -> None:
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            try:
                await context.bot.send_message(chat_id, text)
            except Exception as exc:  # pragma: no cover - network issues
                logger.warning("Broadcast to %s failed: %s", chat_id, exc)


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
        await message.reply_text("Запускать игру нужно в личном чате с ботом.")
        return

    gid = game_key(chat.id, message.message_thread_id)
    if gid in ACTIVE_GAMES:
        await message.reply_text("Игра уже создана.")
        return

    host_id = update.effective_user.id
    game = GameState(host_id=host_id)
    game.players[host_id] = Player(user_id=host_id)
    game.player_chats[host_id] = chat.id
    ACTIVE_GAMES[gid] = game

    code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
    JOIN_CODES[code] = gid
    context.user_data["invite_code"] = code

    context.user_data["awaiting_name"] = True
    await message.reply_text("Игра создана. Введите ваше имя:")


async def invite_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the "Пригласить из контактов" button."""

    query = update.callback_query
    await query.answer()
    code = query.data.split("_", 1)[1]
    context.user_data["invite_code"] = code
    button = KeyboardButton("Отправить контакт", request_contact=True)
    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
    await query.message.reply_text(
        "Поделитесь контактом — бот отправит приглашение",
        reply_markup=markup,
    )


async def send_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send deep-link invitation to the host."""

    query = update.callback_query
    await query.answer()
    code = query.data.split("_", 1)[1]
    link = f"https://t.me/{BOT_USERNAME}?start=join_{code}"
    await query.message.reply_text(f"Ссылка для приглашения:\n{link}")


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an invite link to the shared contact without revealing the code."""

    contact = update.effective_message.contact
    code = context.user_data.get("invite_code")
    if not contact or not code:
        return
    try:
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=join_{code}"
        if contact.user_id:
            await context.bot.send_message(contact.user_id, f"Вас приглашают в игру: {link}")
            await update.message.reply_text("Приглашение отправлено.")
    except Exception as exc:  # pragma: no cover - network issues
        logger.warning("Failed to send contact invite: %s", exc)
        await update.message.reply_text("Не удалось отправить приглашение.")


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /join CODE")
        return
    code = context.args[0].strip()
    gid = JOIN_CODES.get(code)
    if not gid or gid not in ACTIVE_GAMES:
        await update.message.reply_text("Игра не найдена.")
        return
    game = ACTIVE_GAMES[gid]
    user_id = update.effective_user.id
    if user_id in game.players:
        await update.message.reply_text("Вы уже участвуете.")
        return
    if len(game.players) >= 5:
        await update.message.reply_text("Лобби заполнено.")
        return
    game.players[user_id] = Player(user_id=user_id)
    game.player_chats[user_id] = update.effective_chat.id
    context.user_data["awaiting_name"] = True
    await update.message.reply_text("Введите ваше имя:")
    if game.status == "waiting" and len(game.players) >= 2 and game.combo_choices:
        await maybe_show_combos(game, context)


async def quit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    game = next((g for g in ACTIVE_GAMES.values() if user_id in g.players), None)
    if not game:
        await update.message.reply_text("Вы не в игре.")
        return
    player = game.players.get(user_id)
    name = player.name if player and player.name else update.effective_user.first_name
    message = (
        f"Игра прервана участником {name}. Вы можете начать заново, нажав /start"
    )
    for job in game.jobs.values():
        try:
            job.schedule_removal()
        except Exception:
            try:
                job.cancel()
            except Exception:
                pass
    game.jobs.clear()
    await broadcast(game, message, context)
    await update.message.reply_text(message)
    gid = game_key_from_state(game)
    ACTIVE_GAMES.pop(gid, None)


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_name"):
        return
    user_id = update.effective_user.id
    name = update.message.text.strip()
    game = next((g for g in ACTIVE_GAMES.values() if user_id in g.players), None)
    if not game:
        return
    player = game.players[user_id]
    player.name = name
    context.user_data.pop("awaiting_name", None)
    await update.message.reply_text(f"Имя установлено: {name}")
    if game.status == "config" and user_id == game.host_id:
        game.status = "waiting"
        code = context.user_data.get("invite_code")
        if code:
            invite_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Пригласить из контактов", callback_data=f"contact_{code}")],
                [InlineKeyboardButton("Создать ссылку", callback_data=f"link_{code}")],
            ])
            await update.message.reply_text(
                "Пригласите игроков:", reply_markup=invite_keyboard
            )

        buttons = [
            [
                InlineKeyboardButton("3 минуты", callback_data="time_3"),
                InlineKeyboardButton("5 минут", callback_data="time_5"),
            ]
        ]
        if user_id == ADMIN_ID:
            buttons.append([InlineKeyboardButton("[адм.] Тестовая игра", callback_data="adm_test")])
        await update.message.reply_text(
            "Выберите длительность игры:", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await broadcast(game, f"{name} присоединился к игре", context)
        await maybe_show_combos(game, context)
    raise ApplicationHandlerStop


async def time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
    if not game or query.from_user.id != game.host_id:
        return
    if query.data == "adm_test" and query.from_user.id == ADMIN_ID:
        game.time_limit = 1
        game.players[0] = Player(user_id=0, name="Бот")
        game.status = "waiting"
        await query.edit_message_text("Тестовая игра: выберите режим букв")
    elif query.data.startswith("time_"):
        game.time_limit = int(query.data.split("_")[1])
        await query.edit_message_text("Длительность установлена")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("3 буквы", callback_data="letters_3"),
                InlineKeyboardButton("4 буквы", callback_data="letters_4"),
            ]
        ]
    )
    await context.bot.send_message(chat.id, "Выберите режим:", reply_markup=keyboard)


async def letters_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
    if not game or query.from_user.id != game.host_id:
        return
    game.letters_mode = int(query.data.split("_")[1])
    await query.edit_message_text("Режим выбран")
    await maybe_show_combos(game, context)


async def maybe_show_combos(game: GameState, context: CallbackContext) -> None:
    if game.status != "waiting" or len(game.players) < 2:
        return
    game.combo_choices = generate_combinations(game.letters_mode, game.viability_threshold)
    buttons = [
        [InlineKeyboardButton(" • ".join(combo), callback_data=f"combo_{i}")]
        for i, combo in enumerate(game.combo_choices)
    ]
    markup = InlineKeyboardMarkup(buttons)
    messages = []
    for uid, player in game.players.items():
        chat_id = game.player_chats.get(uid)
        if chat_id:
            msg = await context.bot.send_message(
                chat_id,
                "Выберите комбинацию (осталось 5 с):",
                reply_markup=markup,
            )
            messages.append((chat_id, msg.message_id))
    game.status = "choosing"
    task = asyncio.create_task(combo_countdown(game, context, messages, markup))
    game.jobs["combo_countdown"] = task


async def combo_countdown(
    game: GameState,
    context: CallbackContext,
    messages: List[Tuple[int, int]],
    markup: InlineKeyboardMarkup,
) -> None:
    """Update the combo selection messages with a countdown.

    After five seconds, if no combination was chosen, automatically pick one.
    """

    remaining = 5
    try:
        while remaining > 0:
            await asyncio.sleep(1)
            if game.base_letters:
                break
            remaining -= 1
            text = f"Выберите комбинацию (осталось {remaining} с):"
            for chat_id, message_id in messages:
                try:
                    await context.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                    )
                except Exception:
                    pass
        if not game.base_letters:
            await auto_pick_combo(game, context)
    except asyncio.CancelledError:
        pass
    finally:
        game.jobs.pop("combo_countdown", None)


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
    await context.bot.send_message(
        chat_id, "Нажмите «Старт», чтобы начать раунд", reply_markup=keyboard
    )


async def auto_pick_combo(game: GameState, context: CallbackContext) -> None:
    if game.base_letters:
        return
    choice = random.choice(game.combo_choices)
    game.base_letters = tuple(ch.lower() for ch in choice)
    await broadcast(game, f"Случайный выбор: {' • '.join(choice)}", context)
    await send_start_prompt(game, context)


async def combo_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    gid = game_key(chat.id, query.message.message_thread_id)
    game = ACTIVE_GAMES.get(gid)
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
        )
    else:
        await broadcast(
            game,
            f"Буквы выбраны: {' • '.join(game.combo_choices[idx])}",
            context,
        )
    task = game.jobs.pop("combo_countdown", None)
    if task:
        task.cancel()
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
    game.status = "running"
    gid = game_key_from_state(game)
    warn_time = max(game.time_limit * 60 - 60, 0)
    game.jobs["warn"] = context.job_queue.run_once(one_minute_warning, warn_time, data=gid)
    game.jobs["end"] = context.job_queue.run_once(end_game_job, game.time_limit * 60, data=gid)
    if 0 in game.players:  # dummy bot
        game.jobs["dummy"] = context.job_queue.run_repeating(dummy_bot_word, 30, data=gid)
    await broadcast(game, "Игра началась!", context)
    letters = " • ".join(ch.upper() for ch in game.base_letters)
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            try:
                await context.bot.unpin_all_chat_messages(chat_id)
            except Exception:
                pass
            try:
                msg = await context.bot.send_message(chat_id, letters)
                await context.bot.pin_chat_message(chat_id, msg.message_id)
            except Exception as exc:
                logger.warning("Pin to %s failed: %s", chat_id, exc)


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


async def finish_game(game: GameState, context: CallbackContext, reason: str) -> None:
    gid = game_key_from_state(game)
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
    max_points = players_sorted[0].points if players_sorted else 0
    winner_names = ", ".join(
        p.name for p in players_sorted if p.points == max_points
    )
    lines = [
        "**Игра окончена! Результаты**:",
        "",
        f"**Буквы:** {letters}",
        "",
    ]
    for player in players_sorted:
        lines.append(player.name)
        lines.append("Слова:")
        for word in player.words:
            lines.append(f"  {word}")
        lines.append(f"**Итог:** {player.points}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    lines.append(f"🏆 **Победитель:** {winner_names}")
    text = "\n".join(lines)
    await broadcast(game, text, context)

    # Prepare restart keyboard and send to players
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Новая игра", callback_data=f"restart_{gid[0]}_{gid[1]}")]]
    )
    for uid in list(game.players.keys()):
        chat_id = game.player_chats.get(uid)
        if chat_id:
            try:
                await context.bot.send_message(
                    chat_id, "Сыграть ещё раз?", reply_markup=keyboard
                )
            except Exception:
                pass

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

    new_game = GameState(host_id=new_host_id)
    for uid, player in old_game.players.items():
        new_game.players[uid] = Player(user_id=uid, name=player.name)
    new_game.player_chats = old_game.player_chats.copy()
    new_game.player_chats[new_host_id] = new_host_chat.id
    ACTIVE_GAMES[new_gid] = new_game
    FINISHED_GAMES.pop(old_gid, None)

    starter = new_game.players[new_host_id]
    await broadcast(
        new_game, f"{starter.name} начал(а) новую игру", context
    )

    buttons = [
        [
            InlineKeyboardButton("3 минуты", callback_data="time_3"),
            InlineKeyboardButton("5 минут", callback_data="time_5"),
        ]
    ]
    await context.bot.send_message(
        new_host_chat.id,
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
    await broadcast(game, f"Бот: {word}", context)


async def handle_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = asyncio.get_running_loop().time()
    last_time = context.user_data.get("last_message_time")
    if last_time and now - last_time < MESSAGE_RATE_LIMIT:
        await update.message.reply_text("Слишком часто! Подождите немного.")
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
    player = game.players.get(user_id)
    if not player:
        return

    accepted: list[str] = []
    rejected: list[str] = []
    for word in words:
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
        accepted.append(word)
        await broadcast(game, f"{player.name}: {word}", context)
        if sum(word.count(b) for b in game.base_letters) >= 6:
            await broadcast(game, f"🔥 {player.name} прислал мощное слово!", context)

    if accepted:
        await update.message.reply_text("✅")
        await update.message.reply_text("Зачтены: " + ", ".join(accepted))
    if rejected:
        await update.message.reply_text("❌")
        await update.message.reply_text("Отклонены: " + ", ".join(rejected))


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
    application.add_handler(CommandHandler("newgame", newgame))
    application.add_handler(CommandHandler("join", join_cmd))
    application.add_handler(CommandHandler(["quit", "exit"], quit_cmd))
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_name),
        group=0,
    )
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(CallbackQueryHandler(time_selected, pattern="^(time_|adm_test)"))
    application.add_handler(CallbackQueryHandler(letters_selected, pattern="^letters_"))
    application.add_handler(CallbackQueryHandler(combo_chosen, pattern="^combo_"))
    application.add_handler(CallbackQueryHandler(start_round_cb, pattern="^start_round$"))
    application.add_handler(CallbackQueryHandler(invite_contact, pattern="^contact_"))
    application.add_handler(CallbackQueryHandler(send_invite_link, pattern="^link_"))
    application.add_handler(CallbackQueryHandler(restart_game, pattern="^restart_"))
    application.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), handle_word),
        group=1,
    )


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
                allowed_updates=[],
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
            allowed_updates=[],
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
    return JSONResponse({"message": "Grebeshok game service. See /healthz."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


__all__ = [
    "app",
    "register_handlers",
    "start_cmd",
]

