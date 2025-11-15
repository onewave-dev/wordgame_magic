"""Registration helpers for Balda handlers."""

from __future__ import annotations

from typing import Optional

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..state.manager import STATE_MANAGER
from .lobby import (
    AWAITING_BALDA_NAME_FILTER,
    awaiting_name_guard,
    handle_name_reply,
    help_cmd,
    invite_callback,
    join_cmd,
    newgame,
    release_name_request,
    score_cmd,
    start_button_callback,
    start_cmd,
)


async def reset_for_chat(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop temporary state for the provided chat."""

    STATE_MANAGER.reset_chat(chat_id)
    release_name_request(context, user_id)


def register_handlers(application: Optional[Application]) -> None:
    """Attach Balda specific command handlers to the shared application."""

    if not application:
        return

    application.add_handler(CommandHandler("balda", start_cmd))
    application.add_handler(CommandHandler("newgame", newgame))
    application.add_handler(CommandHandler("join", join_cmd))
    application.add_handler(CommandHandler("help", help_cmd, block=False))
    application.add_handler(CommandHandler("score", score_cmd, block=False))
    application.add_handler(MessageHandler(filters.COMMAND, awaiting_name_guard), group=-1)
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND) & AWAITING_BALDA_NAME_FILTER,
            handle_name_reply,
            block=False,
        ),
        group=-1,
    )
    application.add_handler(CallbackQueryHandler(invite_callback, pattern="^balda:invite:"))
    application.add_handler(CallbackQueryHandler(start_button_callback, pattern="^balda:start:"))
