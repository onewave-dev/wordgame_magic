import os
import logging
from typing import Set, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import compose_word_game.word_game_app as compose_game
import grebeshok_game.grebeshok_app as grebeshok_game


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

APPLICATION: Optional[Application] = None
REGISTERED_GAMES: Set[str] = set()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        join_code = context.args[0]
        if join_code in compose_game.JOIN_CODES:
            if "compose" not in REGISTERED_GAMES:
                compose_game.register_handlers(APPLICATION)
                REGISTERED_GAMES.add("compose")
            await compose_game.start_cmd(update, context)
            return
        if join_code.startswith("join_") or join_code in grebeshok_game.JOIN_CODES:
            if "grebeshok" not in REGISTERED_GAMES:
                grebeshok_game.register_handlers(APPLICATION)
                REGISTERED_GAMES.add("grebeshok")
            await grebeshok_game.start_cmd(update, context)
            return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Составь слово!", callback_data="game_compose"),
                InlineKeyboardButton("Гребешок", callback_data="game_grebeshok"),
            ]
        ]
    )
    if update.message:
        await update.message.reply_text("Выберите игру:", reply_markup=keyboard)


async def choose_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    game = query.data
    if game == "game_compose":
        if "compose" not in REGISTERED_GAMES:
            compose_game.register_handlers(APPLICATION)
            REGISTERED_GAMES.add("compose")
        await compose_game.start_cmd(update, context)
    elif game == "game_grebeshok":
        if "grebeshok" not in REGISTERED_GAMES:
            grebeshok_game.register_handlers(APPLICATION)
            REGISTERED_GAMES.add("grebeshok")
        await grebeshok_game.newgame(update, context)
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass


@app.on_event("startup")
async def on_startup() -> None:
    global APPLICATION
    APPLICATION = Application.builder().token(TOKEN).build()
    bot_username = (await APPLICATION.bot.get_me()).username
    compose_game.BOT_USERNAME = bot_username
    grebeshok_game.BOT_USERNAME = bot_username
    APPLICATION.add_handler(CommandHandler("start", start))
    APPLICATION.add_handler(CallbackQueryHandler(choose_game, pattern="^game_"))
    await APPLICATION.initialize()
    await APPLICATION.start()
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
        info = await APPLICATION.bot.get_webhook_info()
        if info.url != webhook_url:
            await APPLICATION.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query", "chat_member"],
            )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await APPLICATION.stop()
    await APPLICATION.shutdown()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> JSONResponse:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    update = Update.de_json(await request.json(), APPLICATION.bot)
    await APPLICATION.process_update(update)
    return JSONResponse({"ok": True})


@app.get("/set_webhook")
async def set_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query", "chat_member"],
    )
    return JSONResponse({"url": webhook_url})


@app.get("/reset_webhook")
async def reset_webhook() -> JSONResponse:
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    await APPLICATION.bot.delete_webhook(drop_pending_updates=False)
    await APPLICATION.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query", "chat_member"],
    )
    return JSONResponse({"reset_to": webhook_url})


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"message": "Wordgame Magic service. See /healthz for status."})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})

