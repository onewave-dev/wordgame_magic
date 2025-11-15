"""Command handlers for the Balda game."""

from __future__ import annotations

from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..rendering import BaldaRenderer
from ..services import clear_chat_state

INFO_MESSAGE = (
    "Игра «Балда» готовится к запуску.\n"
    "Сейчас мы собираем инфраструктуру (хранилище, рендер и обработчики),\n"
    "чтобы поддержать пошаговый режим с приглашениями до 5 игроков.\n\n"
    "Вы можете подписаться на обновления: как только тестовая версия будет\n"
    "готова, бот предложит создать лобби прямо здесь."
)

_renderer = BaldaRenderer()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initial entry point for Balda specific /start requests."""

    message = update.effective_message
    if not message:
        return
    preview = _renderer.render_sequence(state=_renderer_theme_state_placeholder())
    text = f"{INFO_MESSAGE}\n\nТекущее слово: {preview}"
    await message.reply_text(text, parse_mode="HTML")


def _renderer_theme_state_placeholder():
    from ..state import GameState

    return GameState(game_id="placeholder", host_id=0, chat_id=0, base_letter="б")


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder for the /newgame command while the logic is in development."""

    await start_cmd(update, context)


async def reset_for_chat(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop temporary state for the provided chat."""

    clear_chat_state(chat_id)


def register_handlers(application: Optional[Application]) -> None:
    """Attach Balda specific command handlers to the shared application."""

    if not application:
        return
    application.add_handler(CommandHandler("balda", start_cmd))
