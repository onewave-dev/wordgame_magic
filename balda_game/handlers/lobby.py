"""Lobby handlers and finite-state helpers for Balda."""

from __future__ import annotations

import html
import random
from typing import Dict, List, Optional

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes, filters

from ..rendering import BaldaRenderer
from ..state import GameState, PlayerState
from ..state.manager import STATE_MANAGER
from .gameplay import start_first_turn

MIN_PLAYERS = 2
MAX_PLAYERS = 5
NAME_KEY = "balda_display_name"
PENDING_KEY = "balda_pending"

HELP_TEXT = (
    "<b>Балда — краткие правила</b>\n"
    "1. Создайте лобби командой /newgame или кнопкой в меню игры.\n"
    "2. Представьтесь — это имя увидят другие участники и в итоговой таблице.\n"
    "3. Пригласите друзей через код /join или ссылку с кнопки \"Пригласить игроков\".\n"
    "4. Как только в лобби будет минимум 2 игрока (максимум — 5), жмите \"Старт\".\n"
    "5. Каждый ход игрок добавляет одну букву слева или справа от текущей цепочки\n"
    "   и называет слово, в котором есть новая цепочка.\n"
    "6. Нельзя образовывать готовые слова длиной больше двух букв — тот, кто это\n"
    "   сделал, выбывает. При трёх и более участниках игра продолжается до победителя.\n"
    "7. У вас всегда будет 1 минута на ход. За 15 секунд до конца таймер подскажет.\n"
    "8. Есть кнопка \"Пас\" — ей можно воспользоваться один раз за игру, чтобы пропустить ход.\n"
    "\nКоманды:\n"
    "• /newgame — создать новое лобби.\n"
    "• /join <код> — войти по приглашению.\n"
    "• /score — посмотреть текущих игроков и историю ходов.\n"
    "• /quit — выйти из партии (при старте это будет считаться поражением).\n"
)

AWAITING_NAME_USERS: set[int] = set()
AWAITING_LETTER_USERS: Dict[int, str] = {}
RENDERER = BaldaRenderer()

LETTER_EXCLUDED = {"ъ", "ё", "ы"}
CYRILLIC_ALPHABET = tuple(chr(code) for code in range(ord("а"), ord("я") + 1)) + ("ё",)
RANDOM_LETTERS = tuple(letter for letter in CYRILLIC_ALPHABET if letter not in LETTER_EXCLUDED)


class AwaitingBaldaNameFilter(filters.MessageFilter):
    """Filter that matches replies from users waiting to share a name."""

    name = "balda_awaiting_name"

    def filter(self, message: Message) -> bool:  # type: ignore[override]
        user = getattr(message, "from_user", None)
        return bool(user and user.id in AWAITING_NAME_USERS)


AWAITING_BALDA_NAME_FILTER = AwaitingBaldaNameFilter()


class AwaitingBaldaLetterFilter(filters.MessageFilter):
    """Filter that matches replies with the starting letter."""

    name = "balda_awaiting_letter"

    def filter(self, message: Message) -> bool:  # type: ignore[override]
        user = getattr(message, "from_user", None)
        return bool(user and user.id in AWAITING_LETTER_USERS)


AWAITING_BALDA_LETTER_FILTER = AwaitingBaldaLetterFilter()


def _get_display_name(context: ContextTypes.DEFAULT_TYPE, user: User) -> str:
    stored = context.user_data.get(NAME_KEY)
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return (user.full_name or user.username or "Игрок").strip()


def _mark_pending_name(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, action: str, payload: Optional[dict]
) -> None:
    context.user_data[PENDING_KEY] = {"action": action, "payload": payload or {}}
    AWAITING_NAME_USERS.add(user_id)


def _clear_pending_name(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    context.user_data.pop(PENDING_KEY, None)
    AWAITING_NAME_USERS.discard(user_id)


def release_name_request(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int]) -> None:
    """Reset pending name prompts when switching games."""

    if not user_id:
        return
    AWAITING_NAME_USERS.discard(user_id)
    context.user_data.pop(PENDING_KEY, None)
    if context.application:
        store = context.application.user_data.get(user_id)
        if store is not None:
            store.pop(PENDING_KEY, None)


def release_letter_request(user_id: Optional[int]) -> None:
    """Clear the pending letter marker for the provided user."""

    if not user_id:
        return
    AWAITING_LETTER_USERS.pop(user_id, None)


async def _ensure_player_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    action: str,
    payload: Optional[dict] = None,
) -> bool:
    user = update.effective_user
    if not user:
        return False
    if context.user_data.get(NAME_KEY):
        return True
    message = update.effective_message
    if not message:
        return False
    _mark_pending_name(context, user.id, action, payload)
    await message.reply_text(
        "Как тебя представить другим игрокам?\nОтправь имя или ник одной строкой.",
    )
    return False


async def handle_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.reply_text("Имя должно содержать хотя бы 2 символа. Попробуйте ещё раз.")
        return
    if len(text) > 32:
        await message.reply_text("Сократите имя до 32 символов.")
        return
    pending = context.user_data.get(PENDING_KEY)
    context.user_data[NAME_KEY] = text
    _clear_pending_name(context, user.id)
    await message.reply_text(f"Отлично, записал: {html.escape(text)}", parse_mode="HTML")
    if not pending:
        return
    action = pending.get("action")
    payload = pending.get("payload") or {}
    context.user_data.pop(PENDING_KEY, None)
    if action == "host_lobby":
        await newgame(update, context)
    elif action == "join_lobby":
        await _join_lobby(update, context, payload.get("code", ""))


async def awaiting_name_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id not in AWAITING_NAME_USERS:
        return
    message = update.effective_message
    if not message:
        return
    text = message.text or ""
    if not text.startswith("/"):
        return
    if text.split()[0] in ("/quit", "/exit"):
        return
    await message.reply_text("Сначала назовитесь — отправьте имя одной строкой.")
    raise ApplicationHandlerStop


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await join_cmd(update, context)
        return
    await newgame(update, context)


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not all([message, chat, user]):
        return
    if not await _ensure_player_name(update, context, action="host_lobby", payload=None):
        return
    thread_id = message.message_thread_id or None
    existing = STATE_MANAGER.get_by_chat(chat.id, thread_id)
    if existing and existing.has_started:
        await message.reply_text("Игра уже запущена в этом чате. Дождитесь завершения или используйте /quit.")
        return
    STATE_MANAGER.reset_chat(chat.id)
    state = STATE_MANAGER.create_lobby(user.id, chat.id, thread_id)
    STATE_MANAGER.ensure_join_code(state)
    host_name = _get_display_name(context, user)
    state.players[user.id] = PlayerState(user_id=user.id, name=host_name, is_host=True)
    state.players_active = [user.id]
    state.has_started = False
    await _publish_lobby(update, context, state, fresh_start=True)


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    args = context.args or []
    if not args:
        await message.reply_text("Укажите код приглашения после команды: /join <код>.")
        return
    join_code = args[0]
    if not await _ensure_player_name(
        update,
        context,
        action="join_lobby",
        payload={"code": join_code},
    ):
        return
    await _join_lobby(update, context, join_code)


async def _join_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE, join_code: str) -> None:
    message = update.effective_message
    user = update.effective_user
    if not all([message, user, join_code]):
        return
    state = STATE_MANAGER.get_by_join_code(join_code)
    if not state:
        await message.reply_text("Лобби уже закрыто. Попросите хоста создать новое.")
        return
    if state.has_started:
        await message.reply_text("Игра уже началась. Дождитесь следующей партии.")
        return
    if user.id in state.players:
        await message.reply_text("Вы уже в этом лобби — ожидаем старт.")
        return
    if len(state.players) >= MAX_PLAYERS:
        await message.reply_text("Лобби заполнено: максимум 5 игроков.")
        return
    state.players[user.id] = PlayerState(user_id=user.id, name=_get_display_name(context, user))
    state.players_active.append(user.id)
    await message.reply_text(
        "Вы присоединились к лобби «Балда». Дождитесь команды старта от хоста.",
    )
    await _publish_lobby(update, context, state)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)


async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    thread_id = message.message_thread_id or None
    state = STATE_MANAGER.get_by_chat(chat.id, thread_id)
    if not state:
        await message.reply_text("Для этого чата нет активного лобби «Балда». Используйте /newgame.")
        return
    await message.reply_text(_format_score(state), parse_mode="HTML")


async def invite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    _, _, game_id = data.partition(":invite:")
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        return
    code = STATE_MANAGER.ensure_join_code(state)
    bot_username = context.bot.username if context.bot else "wordgamesbot"
    link = f"https://t.me/{bot_username}?start={code}"
    text = (
        "Приглашение в лобби «Балда»:\n"
        f"• Код: <code>{html.escape(code)}</code>\n"
        f"• Ссылка: {html.escape(link)}\n\n"
        "Отправьте ссылку друзьям или поделитесь кодом для команды /join."
    )
    await query.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def start_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    _, _, game_id = data.partition(":start:")
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        return
    user = query.from_user
    if not user:
        return
    if user.id != state.host_id:
        await query.answer("Запустить игру может только создатель лобби.", show_alert=True)
        return
    if len(state.players) < MIN_PLAYERS:
        await query.answer("Нужно минимум 2 игрока для старта.", show_alert=True)
        return
    if len(state.players) > MAX_PLAYERS:
        await query.answer("Сократите состав до 5 игроков.", show_alert=True)
        return
    state.has_started = True
    await _publish_lobby(update, context, state)
    await _send_letter_choice_prompt(state, context)


async def letter_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    _, _, payload = data.partition(":letter:")
    action, _, game_id = payload.partition(":")
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        return
    user = query.from_user
    if not user or user.id != state.host_id:
        await query.answer("Букву выбирает только хост лобби.", show_alert=True)
        return
    if action == "manual":
        AWAITING_LETTER_USERS[user.id] = state.game_id
        await query.edit_message_text("Введите стартовую букву вручную.")
        if context.bot:
            await context.bot.send_message(
                state.chat_id,
                "Введите одну кириллическую букву.",
                reply_markup=ForceReply(selective=True),
                message_thread_id=state.thread_id,
            )
        return
    if action == "random":
        if not RANDOM_LETTERS:
            await query.answer("Нет доступных букв.", show_alert=True)
            return
        letter = random.choice(RANDOM_LETTERS)
        await query.edit_message_text(f"Случайно выбрана буква: {letter.upper()}")
        await _finalize_initial_letter(state, letter, context)


async def handle_letter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    game_id = AWAITING_LETTER_USERS.get(user.id)
    if not game_id:
        return
    state = STATE_MANAGER.get_by_id(game_id)
    if not state:
        release_letter_request(user.id)
        await message.reply_text("Лобби не найдено, попробуйте снова.")
        return
    text = (message.text or "").strip().lower()
    if len(text) != 1 or text not in CYRILLIC_ALPHABET:
        await message.reply_text("Нужна одна кириллическая буква.")
        return
    release_letter_request(user.id)
    await message.reply_text(f"Стартовая буква установлена: {text.upper()}")
    await _finalize_initial_letter(state, text, context)


def _format_score(state: GameState) -> str:
    lines: List[str] = ["<b>Статистика «Балды»</b>"]
    status = "матч запущен" if state.has_started else "лобби собирается"
    lines.append(f"Сейчас {status}. Игроков: {len(state.players)}/{MAX_PLAYERS}.")
    if state.join_code:
        lines.append(f"Код приглашения: <code>{html.escape(state.join_code)}</code>")
    if state.sequence:
        lines.append(f"Текущее слово: <b>{html.escape(state.sequence)}</b>")
    if state.words_used:
        lines.append(f"Сделано ходов: {len(state.words_used)}")
    else:
        lines.append('Ходы ещё не начинались — жмите "Старт", чтобы перейти к игре.')
    if state.players_active:
        lines.append("\n<em>Список игроков:</em>")
        for idx, player_id in enumerate(state.players_active, start=1):
            player = state.players.get(player_id)
            if not player:
                continue
            marker = "👑 " if player.is_host else ""
            status_icon = "✖️" if player.is_eliminated else "✅"
            lines.append(
                f"{status_icon} {idx}. {marker}{html.escape(player.name)}"
            )
    if state.players_out:
        cleaned = [
            html.escape(state.players[pid].name)
            for pid in state.players_out
            if pid in state.players and state.players[pid].name
        ]
        if cleaned:
            lines.append("\nВыбыли: " + ", ".join(cleaned))
    return "\n".join(lines)


async def _publish_lobby(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: GameState,
    *,
    fresh_start: bool = False,
) -> None:
    message = update.effective_message
    if not message:
        return
    text = _format_lobby(state, fresh_start=fresh_start)
    keyboard = _build_keyboard(state)
    chat_id = state.lobby_message_chat_id or message.chat_id
    message_id = state.lobby_message_id
    if chat_id and message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            state.lobby_message_id = None
    if state.chat_id:
        sent = await context.bot.send_message(
            state.chat_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
            message_thread_id=state.thread_id,
        )
    else:
        sent = await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    state.lobby_message_id = sent.message_id
    state.lobby_message_chat_id = sent.chat_id


def _format_lobby(state: GameState, *, fresh_start: bool) -> str:
    code = state.join_code or "—"
    header = "Создано новое лобби «Балда»" if fresh_start else "Лобби «Балда» обновлено"
    lines = [f"<b>{header}</b>", f"Код для /join: <code>{html.escape(code)}</code>"]
    if state.thread_id:
        lines.append("Это лобби закреплено в текущей ветке чата.")
    slots_line = f"Игроки ({len(state.players)}/{MAX_PLAYERS}):"
    lines.append(slots_line)
    for idx, player_id in enumerate(state.players_active, start=1):
        player = state.players.get(player_id)
        if not player:
            continue
        marker = "👑 " if player.is_host else ""
        status = " (выбыл)" if player.is_eliminated else ""
        lines.append(f"{idx}. {marker}{html.escape(player.name)}{status}")
    active_count = sum(
        1
        for pid in state.players_active
        if (player := state.players.get(pid)) and not player.is_eliminated
    )
    if active_count < MIN_PLAYERS:
        need = MIN_PLAYERS - active_count
        lines.append(f"Нужно ещё {need} игрок(а) для старта.")
    elif len(state.players) >= MAX_PLAYERS:
        lines.append('Лобби заполнено — можно сразу жать "Старт".')
    else:
        lines.append("Можно начать игру, как только все готовы.")
    lines.append("\nИспользуйте кнопки ниже, чтобы пригласить друзей или начать матч.")
    return "\n".join(lines)


def _build_keyboard(state: GameState) -> Optional[InlineKeyboardMarkup]:
    buttons: List[List[InlineKeyboardButton]] = []
    buttons.append(
        [InlineKeyboardButton("📨 Пригласить игроков", callback_data=f"balda:invite:{state.game_id}")]
    )
    if not state.has_started and len(state.players) >= MIN_PLAYERS:
        buttons.append(
            [InlineKeyboardButton("🚀 Старт", callback_data=f"balda:start:{state.game_id}")]
        )
    if not buttons:
        return None
    return InlineKeyboardMarkup(buttons)


async def _send_letter_choice_prompt(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot:
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ввести букву", callback_data=f"balda:letter:manual:{state.game_id}"
                ),
                InlineKeyboardButton(
                    "Случайная буква", callback_data=f"balda:letter:random:{state.game_id}"
                ),
            ]
        ]
    )
    await context.bot.send_message(
        state.chat_id,
        "Выберите стартовую букву:",
        reply_markup=keyboard,
        message_thread_id=state.thread_id,
    )


async def _finalize_initial_letter(
    state: GameState, letter: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state.base_letter = letter
    state.sequence = letter
    STATE_MANAGER.save(state)
    if context.bot:
        preview = RENDERER.render_sequence(state)
        await context.bot.send_message(
            state.chat_id,
            f"🖼️ {preview}",
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
        await context.bot.send_message(
            state.chat_id,
            "Игра началась — ждём первый ход.",
            message_thread_id=state.thread_id,
        )
    await start_first_turn(state, context)
