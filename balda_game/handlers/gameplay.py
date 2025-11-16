"""Runtime turn handling for the Balda gameplay loop."""

from __future__ import annotations

import asyncio
import html
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import CallbackContext, ContextTypes, filters

from ..rendering import BaldaRenderer
from ..services import collect_game_stats, format_stats_message
from ..state import GameState, PlayerState, TurnRecord
from ..state.manager import STATE_MANAGER


BASE_DIR = Path(__file__).resolve().parents[2]
DICT_PATH = BASE_DIR / "nouns_ru_pymorphy2_yaspeller.jsonl"
WHITELIST_PATH = BASE_DIR / "whitelist.jsonl"


def _normalize_word(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _load_dictionary() -> set[str]:
    words: set[str] = set()
    for path in (DICT_PATH, WHITELIST_PATH):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            word = payload.get("word")
            if not isinstance(word, str):
                continue
            words.add(_normalize_word(word))
    return words


BALDA_DICTIONARY = _load_dictionary()
RENDERER = BaldaRenderer()


@dataclass(slots=True)
class PendingMove:
    game_id: str
    direction: str


PENDING_MOVES: Dict[int, PendingMove] = {}
BOARD_FLASH_TASKS: Dict[str, asyncio.Task[None]] = {}


def _cancel_flash_task(game_id: str) -> None:
    task = BOARD_FLASH_TASKS.pop(game_id, None)
    if task:
        task.cancel()


async def update_board_image(
    state: GameState,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    helper_word: str | None = None,
    flash_seconds: int | None = None,
) -> None:
    """Send a brand new board or update the existing image message."""

    bot = context.bot
    if not bot:
        return
    send_photo = getattr(bot, "send_photo", None)
    edit_media = getattr(bot, "edit_message_media", None)
    if not callable(send_photo):
        send_text = getattr(bot, "send_message", None)
        if callable(send_text):
            await send_text(
                state.chat_id,
                "Game started",
                message_thread_id=state.thread_id,
            )
        return
    game_id = state.game_id
    buffer = RENDERER.render_board_image(state, helper_word=helper_word)
    payload = buffer.getvalue()

    def _build_file() -> InputFile:
        return InputFile(BytesIO(payload), filename="balda_board.png")

    current_task = asyncio.current_task()
    active_task = BOARD_FLASH_TASKS.get(game_id)
    if not helper_word and active_task and active_task is not current_task:
        _cancel_flash_task(game_id)

    if state.board_message_id and callable(edit_media):
        try:
            await edit_media(
                chat_id=state.chat_id,
                message_id=state.board_message_id,
                media=InputMediaPhoto(media=_build_file()),
            )
        except TelegramError:
            state.board_message_id = None
    elif state.board_message_id and not callable(edit_media):
        state.board_message_id = None
    if not state.board_message_id:
        sent = await send_photo(
            state.chat_id,
            photo=_build_file(),
            message_thread_id=state.thread_id,
        )
        state.board_message_id = sent.message_id
        STATE_MANAGER.save(state)

    if helper_word and flash_seconds:
        _cancel_flash_task(game_id)

        async def _reset_helper() -> None:
            await asyncio.sleep(flash_seconds)
            await update_board_image(state, context)

        coroutine = _reset_helper()
        if context.application:
            task = context.application.create_task(coroutine)
        else:
            task = asyncio.create_task(coroutine)

        def _cleanup(completed: asyncio.Task) -> None:
            if BOARD_FLASH_TASKS.get(game_id) is completed:
                BOARD_FLASH_TASKS.pop(game_id, None)

        task.add_done_callback(_cleanup)
        BOARD_FLASH_TASKS[game_id] = task


class AwaitingBaldaMoveFilter(filters.MessageFilter):
    """Match messages from players who are about to enter a move."""

    name = "balda_awaiting_move"

    def filter(self, message: Message) -> bool:  # type: ignore[override]
        user = getattr(message, "from_user", None)
        return bool(user and user.id in PENDING_MOVES)


AWAITING_BALDA_MOVE_FILTER = AwaitingBaldaMoveFilter()


def _is_cyrillic(text: str) -> bool:
    return all("а" <= ch <= "я" for ch in text if ch.isalpha())


def _clear_pending_move(user_id: int) -> None:
    PENDING_MOVES.pop(user_id, None)


def _alive_players(state: GameState) -> list[PlayerState]:
    alive: list[PlayerState] = []
    for player_id in state.players_active:
        player = state.players.get(player_id)
        if player and not player.is_eliminated:
            alive.append(player)
    return alive


def _cancel_turn_jobs(state: GameState) -> None:
    """Cancel any pending reminder/timeout jobs for the active player."""

    state.reset_timer()


def _schedule_turn_jobs(
    state: GameState, player: PlayerState, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Schedule reminder and timeout jobs for the current player's move."""

    if not context.job_queue:
        return
    data = {"game_id": state.game_id, "player_id": player.user_id}
    _cancel_turn_jobs(state)
    reminder = context.job_queue.run_once(
        turn_warning_job,
        45,
        data=dict(data),
        name=f"balda_warn_{state.game_id}_{player.user_id}",
    )
    timeout = context.job_queue.run_once(
        turn_timeout_job,
        60,
        data=dict(data),
        name=f"balda_timeout_{state.game_id}_{player.user_id}",
    )
    state.timer_job["reminder"] = reminder
    state.timer_job["timeout"] = timeout


def _pick_next_player(state: GameState, *, advance: bool) -> Optional[PlayerState]:
    alive = _alive_players(state)
    if not alive:
        return None
    if not advance and state.current_player:
        current = state.players.get(state.current_player)
        if current and not current.is_eliminated:
            return current
    start_index = 0
    if advance and state.current_player in state.players_active:
        try:
            start_index = state.players_active.index(state.current_player) + 1
        except ValueError:
            start_index = 0
    total = len(state.players_active)
    for offset in range(total):
        idx = (start_index + offset) % total
        player_id = state.players_active[idx]
        player = state.players.get(player_id)
        if player and not player.is_eliminated:
            return player
    return None


async def start_first_turn(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Select the first active player and prompt them to choose a side."""

    player = _pick_next_player(state, advance=False)
    if not player:
        return
    state.current_player = player.user_id
    state.direction = None
    STATE_MANAGER.save(state)
    await _prompt_direction_choice(state, player, context)


async def _prompt_direction_choice(
    state: GameState, player: PlayerState, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not context.bot:
        return
    pass_used = bool(player.has_passed or state.has_passed.get(player.user_id))
    pass_label = "✖️ Pass" if pass_used else "↩️ Pass"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "◀️ Left", callback_data=f"balda:turn:left:{state.game_id}"
                ),
                InlineKeyboardButton(
                    "Right ▶️", callback_data=f"balda:turn:right:{state.game_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    pass_label, callback_data=f"balda:pass:{state.game_id}"
                )
            ],
        ]
    )
    await context.bot.send_message(
        state.chat_id,
        f"Ход игрока <b>{html.escape(player.name)}</b>. Выберите сторону для новой буквы.",
        parse_mode="HTML",
        reply_markup=keyboard,
        message_thread_id=state.thread_id,
    )
    _schedule_turn_jobs(state, player, context)


async def direction_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks for the left/right choice."""

    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    _, _, payload = data.partition(":turn:")
    direction, _, game_id = payload.partition(":")
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        return
    user = query.from_user
    if not user:
        return
    if user.id != state.current_player:
        await query.answer("Сейчас ход другого игрока.", show_alert=True)
        return
    if direction not in {"left", "right"}:
        await query.answer("Неверная сторона.", show_alert=True)
        return
    PENDING_MOVES[user.id] = PendingMove(game_id=game_id, direction=direction)
    await query.edit_message_text(
        f"Добавляем букву {'слева' if direction == 'left' else 'справа'}."
    )
    if context.bot:
        await context.bot.send_message(
            state.chat_id,
            (
                "Введите ход в формате: <code>буква слово</code>.\n"
                "Например: <code>л плакат</code>."
            ),
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True),
            message_thread_id=state.thread_id,
        )


async def pass_turn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow the active player to skip their turn once per game."""

    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    _, _, game_id = data.partition(":pass:")
    if not game_id:
        await query.answer()
        return
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        await query.answer("Игра не найдена.", show_alert=True)
        return
    user = query.from_user
    if not user:
        await query.answer()
        return
    if user.id != state.current_player:
        await query.answer("Сейчас ход другого игрока.", show_alert=True)
        return
    player = state.players.get(user.id)
    if not player or player.is_eliminated:
        await query.answer("Игрок не найден.", show_alert=True)
        return
    if player.has_passed or state.has_passed.get(user.id):
        await query.answer("Пас уже использован — попыток больше нет.", show_alert=True)
        return
    player.has_passed = True
    state.has_passed[user.id] = True
    STATE_MANAGER.save(state)
    _clear_pending_move(user.id)
    _cancel_turn_jobs(state)
    await query.answer("Ход пропущен.")
    try:
        await query.edit_message_text("Вы пропустили ход — передаём очередь дальше.")
    except TelegramError:
        pass
    if context.bot:
        name = html.escape(player.name)
        await context.bot.send_message(
            state.chat_id,
            f"🔁 {name} воспользовался пасом — ход переходит к следующему игроку.",
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
    await _advance_turn(state, context)


async def handle_move_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse and validate the player's textual turn."""

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    pending = PENDING_MOVES.get(user.id)
    if not pending:
        return
    state = STATE_MANAGER.get_by_id(pending.game_id)
    if not state:
        _clear_pending_move(user.id)
        await message.reply_text("Игра не найдена. Попробуйте снова.")
        return
    if user.id != state.current_player:
        _clear_pending_move(user.id)
        await message.reply_text("Сейчас ход другого игрока.")
        return
    text = (message.text or "").strip()
    parts = text.split()
    if len(parts) != 2:
        await message.reply_text("Нужен формат: одна буква и слово через пробел.")
        return
    letter_raw, word_raw = parts
    normalized_letter = _normalize_word(letter_raw)
    if len(normalized_letter) != 1 or not _is_cyrillic(normalized_letter):
        await message.reply_text("Первая часть должна быть одной кириллической буквой.")
        return
    normalized_word = _normalize_word(word_raw)
    if not normalized_word.isalpha() or not _is_cyrillic(normalized_word):
        await message.reply_text("В слове используйте только кириллические буквы.")
        return
    if normalized_word not in BALDA_DICTIONARY:
        await message.reply_text("❌ Слово не найдено в словаре. Попробуйте другое.")
        return
    if any(
        turn.word == normalized_word and turn.player_id != user.id
        for turn in state.words_used
    ):
        await message.reply_text("Это слово уже использовал другой игрок.")
        return
    new_sequence = (
        normalized_letter + _normalize_word(state.sequence)
        if pending.direction == "left"
        else _normalize_word(state.sequence) + normalized_letter
    )
    if new_sequence not in normalized_word:
        await message.reply_text(
            "Слово должно содержать новую последовательность букв целиком."
        )
        return
    if len(new_sequence) > 2 and new_sequence in BALDA_DICTIONARY:
        await _handle_loss(state, user.id, new_sequence, context)
        _clear_pending_move(user.id)
        return
    turn = TurnRecord(
        player_id=user.id,
        letter=normalized_letter,
        word=normalized_word,
        direction=pending.direction,
    )
    state.add_turn(turn)
    STATE_MANAGER.save(state)
    _clear_pending_move(user.id)
    _cancel_turn_jobs(state)
    await _announce_turn(state, turn, context)
    await _advance_turn(state, context)


async def _announce_turn(
    state: GameState, turn: TurnRecord, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not context.bot:
        return
    player = state.players.get(turn.player_id)
    preview = RENDERER.render_sequence(state)
    word_display = turn.word.upper()
    letter_display = turn.letter.upper()
    direction_text = "слева" if turn.direction == "left" else "справа"
    text = (
        f"💡 {html.escape(player.name if player else 'Игрок')} добавил {direction_text} букву"
        f" <b>{letter_display}</b> (слово: <b>{word_display}</b>).\n"
        f"Текущая последовательность: {preview}"
    )
    await context.bot.send_message(
        state.chat_id,
        text,
        parse_mode="HTML",
        message_thread_id=state.thread_id,
    )
    await update_board_image(state, context, helper_word=turn.word, flash_seconds=5)


async def _advance_turn(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    next_player = _pick_next_player(state, advance=True)
    if not next_player:
        return
    state.current_player = next_player.user_id
    state.direction = None
    STATE_MANAGER.save(state)
    await _prompt_direction_choice(state, next_player, context)


async def resign_player(
    state: GameState, context: ContextTypes.DEFAULT_TYPE, player_id: int
) -> None:
    """Handle a voluntary exit from an active game (counts as a loss)."""

    player = state.players.get(player_id)
    if not player or player.is_eliminated:
        return
    if state.current_player == player_id:
        _cancel_turn_jobs(state)
    _clear_pending_move(player_id)
    if context.bot:
        name = html.escape(player.name)
        await context.bot.send_message(
            state.chat_id,
            f"🚪 {name} покинул игру — засчитано поражение.",
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
    await eliminate_player(state, context, player_id)


async def finish_game(
    state: GameState, context: ContextTypes.DEFAULT_TYPE, winner: PlayerState
) -> None:
    """Announce the winner, share the final stats and clean up the state."""

    _cancel_turn_jobs(state)
    _cancel_flash_task(state.game_id)
    stats = collect_game_stats(state)
    winner_name = html.escape(winner.name)
    sequence_display = html.escape(stats.final_sequence)
    stats_message = format_stats_message(stats, winner_name=winner.name)

    if context.bot:
        await context.bot.send_message(
            state.chat_id,
            (
                f"🏆 Победитель: <b>{winner_name}</b>!\n"
                f"Финальная последовательность: <b>{sequence_display}</b>"
            ),
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
        await context.bot.send_message(
            state.chat_id,
            stats_message,
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )

    STATE_MANAGER.drop_game(state.game_id)


async def eliminate_player(
    state: GameState,
    context: ContextTypes.DEFAULT_TYPE,
    player_id: int,
) -> None:
    """Mark the player as eliminated and either advance or finish the match."""

    player = state.players.get(player_id)
    if not player or player.is_eliminated:
        return

    player.is_eliminated = True
    if player_id not in state.players_out:
        state.players_out.append(player_id)
    STATE_MANAGER.save(state)
    _clear_pending_move(player_id)

    alive = _alive_players(state)
    if len(alive) == 1:
        await finish_game(state, context, winner=alive[0])
        return
    if alive:
        await _advance_turn(state, context)
    else:
        STATE_MANAGER.drop_game(state.game_id)


async def _handle_loss(
    state: GameState, player_id: int, sequence: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    _cancel_turn_jobs(state)
    player = state.players.get(player_id)
    if context.bot:
        name = html.escape(player.name) if player else "Игрок"
        await context.bot.send_message(
            state.chat_id,
            (
                f"❌ {name} образовал существующее слово <b>{sequence.upper()}</b> и выбывает."
            ),
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
    await eliminate_player(state, context, player_id)


async def turn_warning_job(context: CallbackContext) -> None:
    """Send a reminder to the current player and the shared chat."""

    job = context.job
    data = job.data if job else None
    game_id = data.get("game_id") if isinstance(data, dict) else None
    player_id = data.get("player_id") if isinstance(data, dict) else None
    if not game_id or not player_id or not context.bot:
        return
    state = STATE_MANAGER.get_by_id(game_id)
    if not state or state.current_player != player_id:
        return
    player = state.players.get(player_id)
    text = "15 seconds left"
    try:
        await context.bot.send_message(player_id, text)
    except TelegramError:
        pass
    chat_text = text
    parse_mode = None
    if player and player.name:
        chat_text = f"15 seconds left — ход игрока <b>{html.escape(player.name)}</b>."
        parse_mode = "HTML"
    try:
        await context.bot.send_message(
            state.chat_id,
            chat_text,
            parse_mode=parse_mode,
            message_thread_id=state.thread_id,
        )
    except TelegramError:
        pass


async def turn_timeout_job(context: CallbackContext) -> None:
    """Handle player elimination when the per-turn timer expires."""

    job = context.job
    data = job.data if job else None
    game_id = data.get("game_id") if isinstance(data, dict) else None
    player_id = data.get("player_id") if isinstance(data, dict) else None
    if not game_id or not player_id:
        return
    state = STATE_MANAGER.get_by_id(game_id)
    if not state or state.current_player != player_id:
        return
    _cancel_turn_jobs(state)
    player = state.players.get(player_id)
    _clear_pending_move(player_id)
    if context.bot:
        name = html.escape(player.name) if player else "Игрок"
        try:
            await context.bot.send_message(
                state.chat_id,
                (
                    f"❌ {name} не успел(а) сделать ход вовремя и теперь наблюдает за игрой."
                ),
                parse_mode="HTML",
                message_thread_id=state.thread_id,
            )
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                player_id,
                "Ваш ход пропущен по тайм-ауту — вы переведены в наблюдатели.",
            )
        except TelegramError:
            pass
    await asyncio.sleep(3)
    await eliminate_player(state, context, player_id)


__all__ = [
    "AWAITING_BALDA_MOVE_FILTER",
    "direction_choice_callback",
    "handle_move_submission",
    "pass_turn_callback",
    "resign_player",
    "start_first_turn",
    "update_board_image",
]

