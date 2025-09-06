from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder start command for the Grebeshok game."""
    message = update.effective_message
    if message:
        await message.reply_text("Игра «Гребешок» ещё не реализована.")


def register_handlers(application: Application) -> None:
    """Register handlers for the Grebeshok game."""
    application.add_handler(CommandHandler("newgame", start_cmd))

