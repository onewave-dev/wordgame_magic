import logging
import os
import socket
from typing import Optional, Set
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.error import TelegramError

import balda_game as balda_game
import compose_word_game.word_game_app as compose_game
import grebeshok_game.grebeshok_app as grebeshok_game

from shared.logging_utils import configure_logging


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")

configure_logging(extra_values=[TOKEN, WEBHOOK_SECRET])
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
        if balda_game.STATE_MANAGER.has_join_code(join_code):
            if "balda" not in REGISTERED_GAMES:
                balda_game.register_handlers(APPLICATION)
                REGISTERED_GAMES.add("balda")
            await balda_game.start_cmd(update, context)
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
                InlineKeyboardButton("Балда", callback_data="game_balda"),
            ]
        ]
    )
    if update.message:
        await update.message.reply_text("Выберите игру:", reply_markup=keyboard)


async def choose_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    game = query.data
    chat = query.message.chat if query.message else None
    chat_id = chat.id if chat else (query.from_user.id if query.from_user else None)
    user_id = query.from_user.id if query.from_user else None
    if chat_id is not None and user_id is not None:
        await compose_game.reset_for_chat(chat_id, user_id, context)
        await grebeshok_game.reset_for_chat(chat_id, user_id, context)
        await balda_game.reset_for_chat(chat_id, user_id, context)
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
    elif game == "game_balda":
        if "balda" not in REGISTERED_GAMES:
            balda_game.register_handlers(APPLICATION)
            REGISTERED_GAMES.add("balda")
        await balda_game.newgame(update, context)
    try:
        await query.delete_message()
    except Exception:
        pass


async def quit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    thread_id = message.message_thread_id if message else None

    if chat_id is not None:
        compose_game_state = compose_game.get_game(chat_id, thread_id)
        if compose_game_state:
            await compose_game.quit_cmd(update, context)
            return

    user = update.effective_user
    user_id = user.id if user else None
    if user_id is not None:
        for game in grebeshok_game.ACTIVE_GAMES.values():
            if user_id in game.players:
                await grebeshok_game.quit_cmd(update, context)
                return

    await message.reply_text("Игра не запущена")


def _can_resolve_webhook_host(webhook_url: str) -> bool:
    parsed = urlparse(webhook_url)
    host = parsed.hostname
    if not host:
        logger.error("Webhook URL %s does not contain a hostname", webhook_url)
        return False
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        logger.warning(
            "Skipping webhook registration for %s: failed to resolve host %s (%s)",
            webhook_url,
            host,
            exc,
        )
        return False
    return True


@app.on_event("startup")
async def on_startup() -> None:
    global APPLICATION
    APPLICATION = Application.builder().token(TOKEN).build()
    bot_username = (await APPLICATION.bot.get_me()).username
    compose_game.BOT_USERNAME = bot_username
    grebeshok_game.BOT_USERNAME = bot_username
    balda_game.BOT_USERNAME = bot_username
    APPLICATION.add_handler(CommandHandler("start", start))
    APPLICATION.add_handler(CommandHandler("quit", quit_command, block=False))
    APPLICATION.add_handler(CallbackQueryHandler(choose_game, pattern="^game_"))
    await APPLICATION.initialize()
    await APPLICATION.start()
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
        if _can_resolve_webhook_host(webhook_url):
            try:
                info = await APPLICATION.bot.get_webhook_info()
                webhook_is_different = info.url != webhook_url
            except TelegramError as exc:
                logger.warning("Failed to fetch current webhook info: %s", exc)
                webhook_is_different = True
            if webhook_is_different:
                try:
                    await APPLICATION.bot.set_webhook(
                        url=webhook_url,
                        secret_token=WEBHOOK_SECRET,
                        allowed_updates=[
                            "message",
                            "callback_query",
                            "chat_member",
                            "users_shared",
                        ],
                    )
                except TelegramError as exc:
                    logger.error("Failed to set webhook to %s: %s", webhook_url, exc)
        else:
            logger.warning("Telegram webhook will not be configured without a resolvable host")


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
    if not PUBLIC_URL:
        raise HTTPException(status_code=400, detail="PUBLIC_URL is not configured")
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    if not _can_resolve_webhook_host(webhook_url):
        raise HTTPException(status_code=503, detail="Webhook host cannot be resolved")
    try:
        await APPLICATION.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query", "chat_member", "users_shared"],
        )
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to set webhook: {exc}") from exc
    return JSONResponse({"url": webhook_url})


@app.get("/reset_webhook")
async def reset_webhook() -> JSONResponse:
    if not PUBLIC_URL:
        raise HTTPException(status_code=400, detail="PUBLIC_URL is not configured")
    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    if not _can_resolve_webhook_host(webhook_url):
        raise HTTPException(status_code=503, detail="Webhook host cannot be resolved")
    try:
        await APPLICATION.bot.delete_webhook(drop_pending_updates=False)
        await APPLICATION.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query", "chat_member", "users_shared"],
        )
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reset webhook: {exc}") from exc
    return JSONResponse({"reset_to": webhook_url})


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"message": "Wordgame Magic service. See /healthz for status."})


@app.get("/healthz")
async def healthz_get():
    return {"status": "ok"}

@app.head("/healthz", include_in_schema=False)
async def healthz_head():
    # Для HEAD тело не нужно
    return Response(status_code=200)
