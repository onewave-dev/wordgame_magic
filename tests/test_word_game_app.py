import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from compose_word_game import word_game_app as app
from telegram.ext import ApplicationHandlerStop


class DummyMessage:
    def __init__(self, chat_id: int, user_id: int, text: str = "") -> None:
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_thread_id = None
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=1)


def test_start_then_handle_name_clears_flag():
    async def run():
        old_active_games = app.ACTIVE_GAMES.copy()
        old_join_codes = app.JOIN_CODES.copy()
        old_base_msg_ids = app.BASE_MSG_IDS.copy()
        old_last_refresh = app.LAST_REFRESH.copy()
        old_chat_games = app.CHAT_GAMES.copy()
        try:
            app.ACTIVE_GAMES.clear()
            app.JOIN_CODES.clear()
            app.BASE_MSG_IDS.clear()
            app.LAST_REFRESH.clear()
            app.CHAT_GAMES.clear()

            user_id = 101
            chat_id = 101
            message = DummyMessage(chat_id, user_id, text="/start")
            user = SimpleNamespace(id=user_id)
            update = SimpleNamespace(
                effective_user=user,
                effective_chat=message.chat,
                effective_message=message,
                message=message,
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=2))
            )
            application_ns = SimpleNamespace(user_data={})
            context = SimpleNamespace(
                args=[],
                user_data={},
                application=application_ns,
                bot=bot,
            )

            with patch.object(app, "schedule_refresh_base_button", lambda *a, **kw: None):
                await app.start_cmd(update, context)
                assert context.application.user_data[user_id]["awaiting_name"] is True
                assert context.user_data["awaiting_name"] is True

                message.text = "Алиса"
                update.message = message
                update.effective_message = message

                try:
                    await app.handle_name(update, context)
                except ApplicationHandlerStop:
                    pass

            awaiting_entry = context.application.user_data.get(user_id, {})
            assert "awaiting_name" not in awaiting_entry
            assert "awaiting_name" not in context.user_data
            assert any("Имя установлено" in reply[0] for reply in message.replies)
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(old_active_games)
            app.JOIN_CODES.clear()
            app.JOIN_CODES.update(old_join_codes)
            app.BASE_MSG_IDS.clear()
            app.BASE_MSG_IDS.update(old_base_msg_ids)
            app.LAST_REFRESH.clear()
            app.LAST_REFRESH.update(old_last_refresh)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())
