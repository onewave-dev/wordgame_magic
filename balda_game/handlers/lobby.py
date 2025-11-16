"""Lobby handlers and finite-state helpers for Balda."""

from __future__ import annotations

import html
import random
from typing import Dict, List, Optional, Tuple

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
)
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes, filters

from ..services import collect_game_stats
from ..state import GameState, PlayerState
from ..state.manager import STATE_MANAGER
from .gameplay import eliminate_player, start_first_turn, update_board_image

MIN_PLAYERS = 2
MAX_PLAYERS = 5
NAME_KEY = "balda_display_name"
PENDING_KEY = "balda_pending"

HELP_TEXT = (
    "<b>Балда — краткие правила</b>\n"
    "1. Создайте лобби командой /newgame или кнопкой в меню игры.\n"
    "2. Представьтесь — это имя увидят другие участники и в итоговой таблице.\n"
    "3. Пригласите друзей кнопками «Пригласить из контактов» или «Создать ссылку».\n"
    "   Код для команды /join всегда указан в сообщении лобби.\n"
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
INVISIBLE_MESSAGE = "\u2063"

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


async def _show_invite_keyboard(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the shared invite keyboard in the host chat."""

    if state.invite_keyboard_visible or not context.bot or not state.chat_id:
        return
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
    await context.bot.send_message(
        state.chat_id,
        "Игра создана. Пригласите участников.",
        message_thread_id=state.thread_id,
    )
    await context.bot.send_message(
        state.chat_id,
        "Выберите способ приглашения:",
        reply_markup=keyboard,
        message_thread_id=state.thread_id,
    )
    state.invite_keyboard_visible = True


async def _hide_invite_keyboard(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the invite keyboard without leaving a message behind."""

    if not state.invite_keyboard_visible or not context.bot or not state.chat_id:
        return
    msg = await context.bot.send_message(
        state.chat_id,
        INVISIBLE_MESSAGE,
        reply_markup=ReplyKeyboardRemove(),
        message_thread_id=state.thread_id,
    )
    try:
        await msg.delete()
    except TelegramError:
        pass
    state.invite_keyboard_visible = False


async def _sync_invite_keyboard(state: GameState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or hide the invite keyboard based on lobby readiness."""

    if state.has_started or len(state.players) >= MIN_PLAYERS:
        await _hide_invite_keyboard(state, context)
    else:
        await _show_invite_keyboard(state, context)


async def _announce_player_join(
    state: GameState,
    context: ContextTypes.DEFAULT_TYPE,
    player: PlayerState,
) -> None:
    """Notify the lobby chat that a new participant has joined."""

    bot = context.bot
    if not bot or not state.chat_id:
        return

    active_count = sum(
        1
        for pid in state.players_active
        if (participant := state.players.get(pid)) and not participant.is_eliminated
    )
    player_name = html.escape(player.name)
    lines = [
        f"👋 <b>{player_name}</b> присоединился к лобби.",
        f"Игроков сейчас: {active_count}/{MAX_PLAYERS}.",
    ]
    if active_count >= MAX_PLAYERS:
        lines.append('Лобби заполнено — можно сразу жать «Старт».')
    elif active_count >= MIN_PLAYERS:
        lines.append('Можно нажать «🚀 Старт», как только все готовы.')
    else:
        need = MIN_PLAYERS - active_count
        lines.append(f"Нужно ещё {need} игрок(а) для старта.")

    await bot.send_message(
        state.chat_id,
        "\n".join(lines),
        parse_mode="HTML",
        message_thread_id=state.thread_id,
    )


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
    await _sync_invite_keyboard(state, context)


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
    player = state.players[user.id]
    await _publish_lobby(update, context, state)
    await _sync_invite_keyboard(state, context)
    await _announce_player_join(state, context, player)


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


def _assign_new_host(state: GameState, *, departing_id: int) -> Optional[PlayerState]:
    """Choose a replacement host when the current one leaves the lobby."""

    if state.host_id != departing_id:
        return None
    candidate_id: Optional[int] = None
    for player_id in state.players_active:
        if player_id == departing_id:
            continue
        player = state.players.get(player_id)
        if player and not player.is_eliminated:
            candidate_id = player_id
            break
    if candidate_id is None:
        return None
    for player in state.players.values():
        player.is_host = False
    state.host_id = candidate_id
    player = state.players.get(candidate_id)
    if player:
        player.is_host = True
    return player


async def _announce_departure(
    state: GameState, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    if not context.bot or not state.chat_id:
        return
    await context.bot.send_message(
        state.chat_id,
        text,
        parse_mode="HTML",
        message_thread_id=state.thread_id,
    )


async def _handle_lobby_departure(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: GameState,
    player: PlayerState,
) -> None:
    message = update.effective_message
    if not message:
        return
    user_id = player.user_id
    state.players.pop(user_id, None)
    if user_id in state.players_active:
        state.players_active.remove(user_id)
    state.has_passed.pop(user_id, None)
    if user_id in state.players_out:
        state.players_out.remove(user_id)
    await message.reply_text("Вы покинули лобби «Балда».")
    if not state.players_active:
        await _hide_invite_keyboard(state, context)
        await _announce_departure(
            state,
            context,
            f"🚪 {html.escape(player.name)} закрыл(а) лобби «Балда».",
        )
        STATE_MANAGER.drop_game(state.game_id)
        return
    new_host = _assign_new_host(state, departing_id=user_id)
    STATE_MANAGER.save(state)
    host_note = ""
    if new_host:
        host_note = f" Новый хост — <b>{html.escape(new_host.name)}</b>."
    await _announce_departure(
        state,
        context,
        f"🚪 {html.escape(player.name)} покинул(а) лобби «Балда».{host_note}",
    )
    await _publish_lobby(update, context, state)
    await _sync_invite_keyboard(state, context)


async def _handle_active_forfeit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: GameState,
    player: PlayerState,
) -> None:
    message = update.effective_message
    if not message:
        return
    user_id = player.user_id
    if state.current_player == user_id:
        state.reset_timer()
    if state.host_id == user_id and not state.base_letter:
        new_host = _assign_new_host(state, departing_id=user_id)
        if new_host:
            STATE_MANAGER.save(state)
    await message.reply_text("Вы покинули игру «Балда». Это засчитано как поражение.")
    await _announce_departure(
        state,
        context,
        f"❌ {html.escape(player.name)} покинул(а) игру и считается проигравшим.",
    )
    await eliminate_player(state, context, user_id)
    if (not state.base_letter) and STATE_MANAGER.get_by_id(state.game_id):
        await _send_letter_choice_prompt(state, context)


async def quit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user:
        return
    user_id = user.id
    if user_id in AWAITING_NAME_USERS:
        release_name_request(context, user_id)
        await message.reply_text(
            "Заявка на участие в «Балде» отменена. Можно начать заново командой /newgame."
        )
        return
    release_letter_request(user_id)
    thread_id = message.message_thread_id or None
    state: Optional[GameState] = None
    if chat:
        state = STATE_MANAGER.get_by_chat(chat.id, thread_id)
        if state and user_id not in state.players:
            state = None
    if not state:
        state = STATE_MANAGER.find_by_player(user_id)
    if not state:
        await message.reply_text("Вы не участвуете в игре «Балда». Используйте /newgame, чтобы начать.")
        return
    player = state.players.get(user_id)
    if not player:
        await message.reply_text("Вы не участвуете в игре «Балда». Используйте /join, чтобы присоединиться.")
        return
    if not state.has_started:
        await _handle_lobby_departure(update, context, state, player)
        return
    if player.is_eliminated:
        await message.reply_text("Вы уже наблюдаете за текущей партией.")
        return
    await _handle_active_forfeit(update, context, state, player)


async def invite_link_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    thread_id = message.message_thread_id or None
    state = STATE_MANAGER.get_by_chat(chat.id, thread_id)
    if not state:
        await message.reply_text("Игра не найдена, начните заново командой /start")
        return
    code = STATE_MANAGER.ensure_join_code(state)
    bot = context.bot
    bot_username = (getattr(bot, "username", None) or "wordgamesbot").lstrip("@")
    link = f"https://t.me/{bot_username}?start={code}"
    await message.reply_text(f"Ссылка приглашения: {link}")


async def users_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.users_shared:
        return
    thread_id = message.message_thread_id or None
    state = STATE_MANAGER.get_by_chat(chat.id, thread_id)
    if not state:
        return
    bot = context.bot
    if not bot:
        return
    code = STATE_MANAGER.ensure_join_code(state)
    bot_username = (getattr(bot, "username", None) or "wordgamesbot").lstrip("@")
    link = f"https://t.me/{bot_username}?start={code}"

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

    for shared_user in message.users_shared.users:
        user_label = format_shared_user(shared_user)
        user_id = getattr(shared_user, "user_id", None)
        if not user_id:
            reason = "Telegram не передал ID пользователя — он ещё не открывал этого бота."
            permanent_failures.append((user_label, reason))
            continue
        try:
            await bot.send_message(user_id, f"Приглашение в игру: {link}")
            state.invited_users.add(user_id)
            delivered.append(user_label)
        except (Forbidden, BadRequest) as exc:
            reason = str(exc)
            if isinstance(exc, Forbidden) and "initiate conversation" in reason:
                reason = (
                    "Telegram запрещает боту писать первым. Попросите игрока открыть бота по ссылке."
                )
            permanent_failures.append((user_label, reason))
        except TelegramError as exc:
            transient_failures.append((user_label, str(exc)))
        except Exception as exc:  # pragma: no cover - safeguard for unexpected errors
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

    await message.reply_text("\n".join(response_lines))


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
    await _sync_invite_keyboard(state, context)
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
    stats = collect_game_stats(state)
    lines: List[str] = ["<b>Статистика «Балды»</b>"]
    status = "матч запущен" if state.has_started else "лобби собирается"
    lines.append(f"Сейчас {status}. Игроков: {len(state.players)}/{MAX_PLAYERS}.")
    if state.join_code:
        lines.append(f"Код приглашения: <code>{html.escape(state.join_code)}</code>")
    lines.append(f"🧩 Сделано ходов: {stats.total_turns}")
    lines.append(f"🕐 Время с создания лобби: {stats.duration_text}")
    lines.append(f"🔠 Уникальных слов: {stats.unique_words}")
    if state.sequence:
        lines.append(
            f"💬 Текущая последовательность: <b>{html.escape(state.sequence.upper())}</b>"
        )
    else:
        lines.append("💬 Текущая последовательность ещё не выбрана.")
    if state.players_active:
        lines.append("\n<em>Список игроков:</em>")
        for idx, player_id in enumerate(state.players_active, start=1):
            player = state.players.get(player_id)
            if not player:
                continue
            marker = "👑 " if player.is_host else ""
            status_icon = "✖️" if player.is_eliminated else "✅"
            lines.append(f"{status_icon} {idx}. {marker}{html.escape(player.name)}")
    if state.words_used:
        lines.append("\n<em>История слов:</em>")
        for idx, turn in enumerate(state.words_used, start=1):
            player = state.players.get(turn.player_id)
            player_name = html.escape(player.name) if player else "Игрок"
            direction_icon = "◀️" if turn.direction == "left" else "▶️"
            letter_display = turn.letter.upper()
            word_display = turn.word.upper()
            lines.append(
                f"{idx}. {player_name} — <b>{word_display}</b> "
                f"({direction_icon} +{letter_display})"
            )
    else:
        lines.append('\nИстория ходов пока пуста — жмите "Старт", чтобы начать игру.')
    lines.append("\n<em>Выбывшие:</em>")
    eliminated = [
        html.escape(state.players[player_id].name)
        for player_id in state.players_out
        if player_id in state.players and state.players[player_id].name
    ]
    if eliminated:
        for name in eliminated:
            lines.append(f"• {name}")
    else:
        lines.append("• Пока никто не выбывал.")
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
    lines.append(
        "\nКнопки «Пригласить из контактов» и «Создать ссылку» доступны под полем ввода."
    )
    lines.append("Кнопки ниже помогают управлять лобби и запускать игру.")
    return "\n".join(lines)


def _build_keyboard(state: GameState) -> Optional[InlineKeyboardMarkup]:
    buttons: List[List[InlineKeyboardButton]] = []
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
        await context.bot.send_message(
            state.chat_id,
            (
                "Игра началась — стартовая буква: "
                f"<b>{html.escape(letter.upper())}</b>. Ждём первый ход."
            ),
            parse_mode="HTML",
            message_thread_id=state.thread_id,
        )
    await update_board_image(state, context)
    await start_first_turn(state, context)
