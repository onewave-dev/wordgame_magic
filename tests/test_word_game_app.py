import asyncio
import html
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app as root_app
import balda_game
from balda_game.handlers import lobby as balda_lobby
from compose_word_game import word_game_app as app
from grebeshok_game import grebeshok_app as greb_app
from telegram.ext import Application, ApplicationHandlerStop

import pytest


compose_game = app

class DummyMessage:
    def __init__(self, chat_id: int, user_id: int, text: str = "") -> None:
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_thread_id = None
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.from_user.full_name = ""
        self.from_user.first_name = ""
        self.from_user.username = ""
        self.replies = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=1)


class DummyCallbackQuery:
    def __init__(self, data: str, message: DummyMessage, user_id: int) -> None:
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)
        self.answered = False
        self.deleted = False
        self.edited_texts = []

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def delete_message(self):
        self.deleted = True

    async def edit_message_text(self, text: str, **kwargs):
        self.edited_texts.append((text, kwargs))


@pytest.fixture
def anyio_backend() -> str:
    """Limit AnyIO-powered tests in this module to asyncio."""

    return "asyncio"


def test_start_menu_contains_balda_button():
    async def run():
        user_id = 501
        chat_id = 601
        message = DummyMessage(chat_id, user_id, text="/start")
        update = SimpleNamespace(
            message=message,
            effective_message=message,
            effective_chat=message.chat,
            effective_user=SimpleNamespace(id=user_id),
        )
        context = SimpleNamespace(args=[], user_data={}, bot=None)

        await root_app.start(update, context)

        assert message.replies, "start command should produce a reply"
        _, kwargs = message.replies[-1]
        keyboard = kwargs.get("reply_markup")
        assert keyboard is not None
        assert any(
            button.text == "Балда"
            for row in keyboard.inline_keyboard
            for button in row
        )

    asyncio.run(run())


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


def test_choose_game_routes_to_balda():
    async def run():
        registered_snapshot = root_app.REGISTERED_GAMES.copy()
        application_snapshot = root_app.APPLICATION
        try:
            root_app.REGISTERED_GAMES.clear()
            root_app.APPLICATION = Application.builder().token("123:ABC").build()

            chat_id = 909
            user_id = 808
            start_message = DummyMessage(chat_id, user_id, text="choose")
            query = DummyCallbackQuery("game_balda", start_message, user_id)
            callback_update = SimpleNamespace(
                callback_query=query,
                effective_chat=start_message.chat,
                effective_user=SimpleNamespace(id=user_id),
                message=None,
            )
            context = SimpleNamespace()

            with (
                patch.object(compose_game, "reset_for_chat", AsyncMock()),
                patch.object(greb_app, "reset_for_chat", AsyncMock()),
                patch.object(balda_game, "reset_for_chat", AsyncMock()),
                patch.object(balda_game, "register_handlers", lambda *a, **k: None),
                patch.object(balda_game, "newgame", AsyncMock()) as newgame_mock,
            ):
                await root_app.choose_game(callback_update, context)
                newgame_mock.assert_awaited()
        finally:
            root_app.REGISTERED_GAMES.clear()
            root_app.REGISTERED_GAMES.update(registered_snapshot)
            root_app.APPLICATION = application_snapshot

    asyncio.run(run())


def test_balda_start_button_prompts_letter_choice():
    async def run():
        balda_game.STATE_MANAGER.reset()
        balda_lobby.AWAITING_LETTER_USERS.clear()
        host_id = 901
        guest_id = 902
        chat_id = 5001
        state = balda_game.STATE_MANAGER.create_lobby(host_id, chat_id)
        state.players[host_id] = balda_game.PlayerState(user_id=host_id, name="Хост", is_host=True)
        state.players[guest_id] = balda_game.PlayerState(user_id=guest_id, name="Гость")
        state.players_active = [host_id, guest_id]
        message = DummyMessage(chat_id, host_id, text="start")
        query = DummyCallbackQuery(f"balda:start:{state.game_id}", message, host_id)
        update = SimpleNamespace(callback_query=query)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
        )
        context = SimpleNamespace(bot=bot)

        with patch.object(balda_lobby, "_publish_lobby", AsyncMock()):
            await balda_lobby.start_button_callback(update, context)

        assert bot.send_message.await_count == 1
        call = bot.send_message.await_args
        _, text = call.args[:2]
        keyboard = call.kwargs.get("reply_markup")
        assert text == "Выберите стартовую букву:"
        assert keyboard is not None
        buttons = [btn.text for row in keyboard.inline_keyboard for btn in row]
        assert "Ввести букву" in buttons
        assert "Случайная буква" in buttons

    asyncio.run(run())


def test_quit_command_routes_to_balda():
    async def run():
        balda_game.STATE_MANAGER.reset()
        chat_id = 4242
        user_id = 5151
        message = DummyMessage(chat_id, user_id, text="/quit")
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=message.chat,
            effective_user=SimpleNamespace(id=user_id),
        )
        context = SimpleNamespace()
        state = balda_game.STATE_MANAGER.create_lobby(host_id=user_id, chat_id=chat_id)
        state.players[user_id] = balda_game.PlayerState(user_id=user_id, name="Host", is_host=True)
        state.players_active = [user_id]
        balda_game.STATE_MANAGER.save(state)
        registered_snapshot = root_app.REGISTERED_GAMES.copy()
        root_app.REGISTERED_GAMES.add("balda")
        greb_snapshot = greb_app.ACTIVE_GAMES.copy()
        greb_app.ACTIVE_GAMES.clear()
        compose_get_game = root_app.compose_game.get_game
        root_app.compose_game.get_game = lambda *_: None
        try:
            with patch.object(balda_game, "quit_cmd", AsyncMock()) as quit_mock:
                await root_app.quit_command(update, context)
                quit_mock.assert_awaited_once()
        finally:
            root_app.compose_game.get_game = compose_get_game
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(greb_snapshot)
            root_app.REGISTERED_GAMES.clear()
            root_app.REGISTERED_GAMES.update(registered_snapshot)
            balda_game.STATE_MANAGER.reset()

    asyncio.run(run())


def test_balda_manual_letter_requires_single_cyrillic():
    async def run():
        balda_game.STATE_MANAGER.reset()
        balda_lobby.AWAITING_LETTER_USERS.clear()
        host_id = 1001
        chat_id = 6001
        state = balda_game.STATE_MANAGER.create_lobby(host_id, chat_id)
        state.players[host_id] = balda_game.PlayerState(user_id=host_id, name="Хост", is_host=True)
        balda_lobby.AWAITING_LETTER_USERS[host_id] = state.game_id
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=2))
        )
        message = DummyMessage(chat_id, host_id, text="ab")
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=host_id))
        context = SimpleNamespace(bot=bot)

        await balda_lobby.handle_letter_reply(update, context)
        assert any("Нужна одна кириллическая буква." in reply[0] for reply in message.replies)
        assert balda_lobby.AWAITING_LETTER_USERS.get(host_id) == state.game_id

        message.text = "К"
        await balda_lobby.handle_letter_reply(update, context)
        assert state.base_letter == "к"
        assert host_id not in balda_lobby.AWAITING_LETTER_USERS
        assert any("Стартовая буква установлена" in reply[0] for reply in message.replies)
        assert bot.send_message.await_count == 2
        texts = [call.args[1] for call in bot.send_message.await_args_list]
        assert "<b>К</b>" in texts[0]
        assert texts[1] == "Game started"

    asyncio.run(run())


@pytest.mark.anyio
async def test_balda_join_announces_new_player():
    balda_game.STATE_MANAGER.reset()
    try:
        host_id = 5002
        chat_id = 7002
        state = balda_game.STATE_MANAGER.create_lobby(host_id, chat_id)
        balda_game.STATE_MANAGER.ensure_join_code(state)
        state.players[host_id] = balda_game.PlayerState(user_id=host_id, name="Хост", is_host=True)
        state.players_active = [host_id]
        state.thread_id = 77
        join_code = state.join_code or ""

        message = DummyMessage(chat_id=8001, user_id=9001, text="/start")
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=9001, full_name="Новый Игрок", username=""),
        )
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        context = SimpleNamespace(bot=bot, user_data={})

        with (
            patch.object(balda_lobby, "_publish_lobby", AsyncMock()),
            patch.object(balda_lobby, "_sync_invite_keyboard", AsyncMock()),
        ):
            await balda_lobby._join_lobby(update, context, join_code)

        assert bot.send_message.await_count == 1
        args, kwargs = bot.send_message.await_args_list[0]
        assert args[0] == chat_id
        text = args[1]
        assert "Новый Игрок" in text
        assert "Старт" in text
        assert kwargs["parse_mode"] == "HTML"
        assert kwargs["message_thread_id"] == state.thread_id
    finally:
        balda_game.STATE_MANAGER.reset()


def test_handle_name_uses_application_storage_when_user_data_empty():
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

            user_id = 202
            chat_id = 202
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
                assert (
                    context.application.user_data[user_id]["awaiting_name"] is True
                )

                # Simulate a fresh context where user_data was not preserved
                context.user_data = {}
                message.text = "Борис"
                update.message = message
                update.effective_message = message

                try:
                    await app.handle_name(update, context)
                except ApplicationHandlerStop:
                    pass

            awaiting_entry = context.application.user_data.get(user_id, {})
            assert "awaiting_name" not in awaiting_entry
            assert context.user_data.get("name") == "Борис"
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


def test_compose_and_grebeshok_name_filters_isolated():
    async def run(compose_first: bool) -> None:
        compose_active = app.ACTIVE_GAMES.copy()
        compose_join = app.JOIN_CODES.copy()
        compose_base_ids = app.BASE_MSG_IDS.copy()
        compose_refresh = app.LAST_REFRESH.copy()
        compose_chat_games = app.CHAT_GAMES.copy()
        compose_awaiting = app.AWAITING_NAME_USERS.copy()
        old_compose_app = app.APPLICATION

        greb_active = greb_app.ACTIVE_GAMES.copy()
        greb_chat_games = greb_app.CHAT_GAMES.copy()
        greb_join = greb_app.JOIN_CODES.copy()
        greb_finished = greb_app.FINISHED_GAMES.copy()
        greb_base_ids = greb_app.BASE_MSG_IDS.copy()
        greb_refresh = greb_app.LAST_REFRESH.copy()
        greb_locks = greb_app.REFRESH_LOCKS.copy()
        greb_awaiting = greb_app.AWAITING_GREBESHOK_NAME_USERS.copy()
        old_greb_app = greb_app.APPLICATION

        try:
            app.ACTIVE_GAMES.clear()
            app.JOIN_CODES.clear()
            app.BASE_MSG_IDS.clear()
            app.LAST_REFRESH.clear()
            app.CHAT_GAMES.clear()
            app.AWAITING_NAME_USERS.clear()

            greb_app.ACTIVE_GAMES.clear()
            greb_app.CHAT_GAMES.clear()
            greb_app.JOIN_CODES.clear()
            greb_app.FINISHED_GAMES.clear()
            greb_app.BASE_MSG_IDS.clear()
            greb_app.LAST_REFRESH.clear()
            greb_app.REFRESH_LOCKS.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()

            application = Application.builder().token("123:ABC").build()

            with patch.object(app, "schedule_refresh_base_button", lambda *a, **kw: None), patch.object(
                greb_app, "schedule_refresh_base_letters", lambda *a, **kw: None
            ):
                if compose_first:
                    app.register_handlers(application)
                    greb_app.register_handlers(application)
                else:
                    greb_app.register_handlers(application)
                    app.register_handlers(application)

                # Avoid network calls in broadcast helpers
                app.APPLICATION = None
                greb_app.APPLICATION = None

                shared_application = SimpleNamespace(user_data={})
                compose_bot = SimpleNamespace(
                    send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
                )
                greb_bot = SimpleNamespace(
                    send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
                )

                compose_context = SimpleNamespace(
                    args=[],
                    user_data={},
                    application=shared_application,
                    bot=compose_bot,
                )
                greb_context = SimpleNamespace(
                    args=[],
                    user_data={},
                    application=shared_application,
                    bot=greb_bot,
                )

                greb_user = 501
                greb_message = DummyMessage(greb_user, greb_user, text="/newgame")
                greb_update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=greb_user),
                    effective_chat=greb_message.chat,
                    effective_message=greb_message,
                    message=greb_message,
                )

                compose_user = 601
                compose_message = DummyMessage(compose_user, compose_user, text="/start")
                compose_update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=compose_user),
                    effective_chat=compose_message.chat,
                    effective_message=compose_message,
                    message=compose_message,
                )

                await greb_app.newgame(greb_update, greb_context)
                assert greb_user in greb_app.AWAITING_GREBESHOK_NAME_USERS
                assert (
                    shared_application.user_data.get(greb_user, {}).get("awaiting_grebeshok_name")
                    is True
                )
                assert compose_user not in greb_app.AWAITING_GREBESHOK_NAME_USERS

                await app.start_cmd(compose_update, compose_context)
                assert compose_user in app.AWAITING_NAME_USERS
                assert (
                    shared_application.user_data.get(compose_user, {}).get("awaiting_name") is True
                )
                assert greb_user not in app.AWAITING_NAME_USERS

                greb_message.text = "Глеб"
                try:
                    await greb_app.handle_name(greb_update, greb_context)
                except ApplicationHandlerStop:
                    pass

                assert greb_user not in greb_app.AWAITING_GREBESHOK_NAME_USERS
                assert "awaiting_grebeshok_name" not in shared_application.user_data.get(
                    greb_user, {}
                )
                assert compose_user in app.AWAITING_NAME_USERS
                assert any("Имя установлено" in reply[0] for reply in greb_message.replies)

                compose_message.text = "Алиса"
                try:
                    await app.handle_name(compose_update, compose_context)
                except ApplicationHandlerStop:
                    pass

                assert compose_user not in app.AWAITING_NAME_USERS
                assert "awaiting_name" not in shared_application.user_data.get(compose_user, {})
                assert greb_user not in greb_app.AWAITING_GREBESHOK_NAME_USERS
                assert any("Имя установлено" in reply[0] for reply in compose_message.replies)
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(compose_active)
            app.JOIN_CODES.clear()
            app.JOIN_CODES.update(compose_join)
            app.BASE_MSG_IDS.clear()
            app.BASE_MSG_IDS.update(compose_base_ids)
            app.LAST_REFRESH.clear()
            app.LAST_REFRESH.update(compose_refresh)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(compose_chat_games)
            app.AWAITING_NAME_USERS.clear()
            app.AWAITING_NAME_USERS.update(compose_awaiting)
            app.APPLICATION = old_compose_app

            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(greb_active)
            greb_app.CHAT_GAMES.clear()
            greb_app.CHAT_GAMES.update(greb_chat_games)
            greb_app.JOIN_CODES.clear()
            greb_app.JOIN_CODES.update(greb_join)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(greb_finished)
            greb_app.BASE_MSG_IDS.clear()
            greb_app.BASE_MSG_IDS.update(greb_base_ids)
            greb_app.LAST_REFRESH.clear()
            greb_app.LAST_REFRESH.update(greb_refresh)
            greb_app.REFRESH_LOCKS.clear()
            greb_app.REFRESH_LOCKS.update(greb_locks)
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.update(greb_awaiting)
            greb_app.APPLICATION = old_greb_app

    asyncio.run(run(True))
    asyncio.run(run(False))


def test_question_word_escapes_llm_text():
    async def run():
        old_chat_games = app.CHAT_GAMES.copy()
        try:
            app.CHAT_GAMES.clear()

            user_id = 303
            chat_id = 404
            message = DummyMessage(chat_id, user_id, text="?Кот&dog")
            message.from_user.full_name = "Игрок <&>"
            update = SimpleNamespace(message=message)
            context = SimpleNamespace(bot=None)

            llm_text = '<b>"опасно"&</b>'

            with patch.object(app, "DICT", set()), patch.object(
                app,
                "describe_word",
                AsyncMock(return_value=llm_text),
            ), patch.object(app, "reply_game_message", AsyncMock()) as mock_reply, patch.object(
                app, "send_game_message", AsyncMock()
            ):
                try:
                    await app.question_word(update, context)
                except ApplicationHandlerStop:
                    pass

            assert mock_reply.await_count == 1
            call = mock_reply.await_args
            response_text = call.args[2]
            assert call.kwargs.get("parse_mode") == "HTML"
            assert "&lt;b&gt;&quot;опасно&quot;&amp;&lt;/b&gt;" in response_text
            assert "<b>Игрок &lt;&amp;&gt;</b>" in response_text
            assert "<b>Кот&amp;dog</b>" in response_text
        finally:
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())


def test_question_word_escapes_llm_response_html():
    async def run():
        message = DummyMessage(chat_id=404, user_id=404, text="?Тест")
        message.from_user.full_name = "Игрок <Тест>"
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(bot=SimpleNamespace())

        llm_text = "<описание> & \"кавычки\""
        normalized_word = app.normalize_word("Тест")
        expected_text = (
            f"<b>{html.escape(message.from_user.full_name)}</b> запросил: "
            f"<b>{html.escape('Тест')}</b>\n\n"
            f"<b>{html.escape(normalized_word)}</b> Этого слова нет в словаре игры\n\n"
            f"{html.escape(llm_text)}"
        )

        with patch.object(app, "describe_word", AsyncMock(return_value=llm_text)):
            with patch.object(app, "schedule_refresh_base_button", lambda *a, **kw: None):
                with patch.object(app, "DICT", set()):
                    try:
                        await app.question_word(update, context)
                    except ApplicationHandlerStop:
                        pass

        assert len(message.replies) == 1
        reply_text, kwargs = message.replies[0]
        assert reply_text == expected_text
        assert kwargs.get("parse_mode") == "HTML"

    asyncio.run(run())


def test_greb_question_word_escapes_llm_text():
    async def run():
        message = DummyMessage(chat_id=505, user_id=606, text="?Слово")
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(bot=None)
        llm_text = "<ответ & определение>"

        with patch.object(
            greb_app, "describe_word", AsyncMock(return_value=llm_text)
        ), patch.object(
            greb_app, "schedule_refresh_base_letters", lambda *a, **kw: None
        ), patch.object(greb_app, "DICTIONARY", set()):
            try:
                await greb_app.question_word(update, context)
            except ApplicationHandlerStop:
                pass

        assert len(message.replies) == 1
        reply_text, kwargs = message.replies[0]
        assert html.escape(llm_text) in reply_text
        assert llm_text not in reply_text
        assert kwargs.get("parse_mode") == "HTML"

    asyncio.run(run())


def test_base_random_handles_insufficient_candidates():
    async def run():
        old_active_games = app.ACTIVE_GAMES.copy()
        old_join_codes = app.JOIN_CODES.copy()
        old_base_msg_ids = app.BASE_MSG_IDS.copy()
        old_last_refresh = app.LAST_REFRESH.copy()
        old_chat_games = app.CHAT_GAMES.copy()
        old_dict = set(app.DICT)
        try:
            app.ACTIVE_GAMES.clear()
            app.JOIN_CODES.clear()
            app.BASE_MSG_IDS.clear()
            app.LAST_REFRESH.clear()
            app.CHAT_GAMES.clear()
            app.DICT.clear()

            host_id = 303
            chat_id = 303
            game_id = "gid"
            game = app.GameState(host_id=host_id, game_id=game_id)
            game.players[host_id] = app.Player(user_id=host_id, name="Ведущий")
            game.player_chats[host_id] = chat_id
            app.ACTIVE_GAMES[game_id] = game
            app.CHAT_GAMES[(chat_id, 0)] = game_id

            message = DummyMessage(chat_id, host_id)
            query = DummyCallbackQuery("base_random", message, host_id)
            update = SimpleNamespace(callback_query=query)
            context = SimpleNamespace(bot=None)

            with patch.object(app, "schedule_refresh_base_button", lambda *a, **kw: None):
                with patch(
                    "compose_word_game.word_game_app.random.sample",
                    side_effect=AssertionError("random.sample should not be called"),
                ):
                    await app.base_choice(update, context)

            assert message.replies, "Expected an informative reply"
            assert "Не удалось найти достаточно подходящих слов" in message.replies[0][0]
        finally:
            app.DICT.clear()
            app.DICT.update(old_dict)
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


def test_grebeshok_invite_flow_from_newgame():
    async def run():
        old_active = greb_app.ACTIVE_GAMES.copy()
        old_join = greb_app.JOIN_CODES.copy()
        old_chat_games = greb_app.CHAT_GAMES.copy()
        old_awaiting = greb_app.AWAITING_GREBESHOK_NAME_USERS.copy()
        try:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.JOIN_CODES.clear()
            greb_app.CHAT_GAMES.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()

            host_id = 9001
            chat_id = 9101
            start_message = DummyMessage(chat_id, host_id, text="/start")
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=host_id),
                effective_chat=start_message.chat,
                effective_message=start_message,
                message=start_message,
            )
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace()))
            application_ns = SimpleNamespace(user_data={})
            context = SimpleNamespace(
                args=[],
                user_data={},
                application=application_ns,
                bot=bot,
            )

            with patch.object(greb_app, "schedule_refresh_base_letters", lambda *a, **k: None):
                await greb_app.newgame(update, context)

                gid = greb_app.game_key(chat_id, None)
                assert gid in greb_app.ACTIVE_GAMES
                first_code = context.user_data.get("invite_code")
                assert first_code in greb_app.JOIN_CODES
                assert greb_app.JOIN_CODES[first_code] == gid

                start_message.text = "Ведущий"
                try:
                    await greb_app.handle_name(update, context)
                except ApplicationHandlerStop:
                    pass

                invite_keyboard_msg = DummyMessage(chat_id, host_id)
                time_query = DummyCallbackQuery("greb_time_3", invite_keyboard_msg, host_id)
                await greb_app.time_selected(
                    SimpleNamespace(callback_query=time_query),
                    context,
                )

                assert any(
                    "Игра создана. Пригласите участников" in text
                    for text, _ in time_query.edited_texts
                )
                assert any(
                    "Выберите способ приглашения" in text
                    for text, _ in invite_keyboard_msg.replies
                )

                invite_message = DummyMessage(chat_id, host_id, text="Создать ссылку")
                invite_update = SimpleNamespace(
                    message=invite_message,
                    effective_message=invite_message,
                    effective_chat=invite_message.chat,
                    effective_user=SimpleNamespace(id=host_id),
                )
                await greb_app.invite_link(invite_update, context)

                assert any("https://t.me" in text for text, _ in invite_message.replies)
                assert context.user_data.get("invite_code") == first_code
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.JOIN_CODES.clear()
            greb_app.JOIN_CODES.update(old_join)
            greb_app.CHAT_GAMES.clear()
            greb_app.CHAT_GAMES.update(old_chat_games)
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.update(old_awaiting)

    asyncio.run(run())


def test_grebeshok_invite_flow_after_restart():
    async def run():
        old_active = greb_app.ACTIVE_GAMES.copy()
        old_finished = greb_app.FINISHED_GAMES.copy()
        old_join = greb_app.JOIN_CODES.copy()
        old_chat_games = greb_app.CHAT_GAMES.copy()
        try:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.FINISHED_GAMES.clear()
            greb_app.JOIN_CODES.clear()
            greb_app.CHAT_GAMES.clear()

            host_id = 9102
            guest_id = 9103
            host_chat = 9202
            guest_chat = 9203
            game = greb_app.GameState(host_id=host_id)
            game.time_limit = 3
            game.base_letters = ("к", "о", "т")
            game.players = {
                host_id: greb_app.Player(user_id=host_id, name="Хост"),
                guest_id: greb_app.Player(user_id=guest_id, name="Гость"),
            }
            game.player_chats = {host_id: host_chat, guest_id: guest_chat}
            gid = greb_app.game_key(host_chat, None)
            greb_app.ACTIVE_GAMES[gid] = game

            with (
                patch.object(greb_app, "schedule_refresh_base_letters", lambda *a, **k: None),
                patch.object(greb_app, "broadcast", new=AsyncMock()),
            ):
                await greb_app.finish_game(
                    game,
                    SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock())),
                    "Время вышло",
                )

            assert gid not in greb_app.ACTIVE_GAMES
            assert gid in greb_app.FINISHED_GAMES

            restart_message = DummyMessage(host_chat, host_id)
            restart_query = DummyCallbackQuery(f"restart_{host_chat}_0", restart_message, host_id)
            send_message_ns = SimpleNamespace(delete=AsyncMock())
            context = SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(send_message=AsyncMock(return_value=send_message_ns)),
            )

            with (
                patch.object(greb_app, "schedule_refresh_base_letters", lambda *a, **k: None),
                patch.object(greb_app, "broadcast", new=AsyncMock()),
                patch.object(greb_app, "send_game_message", new=AsyncMock(return_value=send_message_ns)),
            ):
                await greb_app.restart_game(SimpleNamespace(callback_query=restart_query), context)

            new_gid = greb_app.game_key(host_chat, None)
            assert new_gid in greb_app.ACTIVE_GAMES
            new_game = greb_app.ACTIVE_GAMES[new_gid]
            assert guest_id in new_game.players

            time_message = DummyMessage(host_chat, host_id)
            time_query = DummyCallbackQuery("greb_time_5", time_message, host_id)
            with (
                patch.object(greb_app, "schedule_refresh_base_letters", lambda *a, **k: None),
                patch.object(greb_app, "send_game_message", new=AsyncMock(return_value=send_message_ns)),
            ):
                await greb_app.time_selected(SimpleNamespace(callback_query=time_query), context)

            invite_message = DummyMessage(host_chat, host_id, text="Создать ссылку")
            invite_update = SimpleNamespace(
                message=invite_message,
                effective_message=invite_message,
                effective_chat=invite_message.chat,
                effective_user=SimpleNamespace(id=host_id),
            )
            await greb_app.invite_link(invite_update, context)

            assert any("https://t.me" in text for text, _ in invite_message.replies)
            assert context.user_data.get("invite_code") in greb_app.JOIN_CODES
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(old_finished)
            greb_app.JOIN_CODES.clear()
            greb_app.JOIN_CODES.update(old_join)
            greb_app.CHAT_GAMES.clear()
            greb_app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())


def test_handle_name_handler_is_non_blocking():
    async def run():
        original_application = app.APPLICATION
        try:
            application = Application.builder().token("123:ABC").build()
            app.register_handlers(application)
            handlers = application.handlers.get(-1, [])
            name_handlers = [
                handler
                for handler in handlers
                if getattr(handler.callback, "__name__", "") == "handle_name"
            ]
            assert name_handlers, "handle_name handler not registered"
            assert name_handlers[0].block is False
        finally:
            app.APPLICATION = original_application

    asyncio.run(run())


def test_choose_grebeshok_then_set_name_replies_with_confirmation():
    async def run():
        compose_active = app.ACTIVE_GAMES.copy()
        compose_join = app.JOIN_CODES.copy()
        compose_base_ids = app.BASE_MSG_IDS.copy()
        compose_refresh = app.LAST_REFRESH.copy()
        compose_chat_games = app.CHAT_GAMES.copy()
        compose_awaiting = app.AWAITING_NAME_USERS.copy()
        old_compose_app = app.APPLICATION

        greb_active = greb_app.ACTIVE_GAMES.copy()
        greb_chat_games = greb_app.CHAT_GAMES.copy()
        greb_join = greb_app.JOIN_CODES.copy()
        greb_finished = greb_app.FINISHED_GAMES.copy()
        greb_base_ids = greb_app.BASE_MSG_IDS.copy()
        greb_refresh = greb_app.LAST_REFRESH.copy()
        greb_locks = greb_app.REFRESH_LOCKS.copy()
        greb_awaiting = greb_app.AWAITING_GREBESHOK_NAME_USERS.copy()
        old_greb_app = greb_app.APPLICATION

        registered_snapshot = root_app.REGISTERED_GAMES.copy()
        application_snapshot = root_app.APPLICATION

        try:
            app.ACTIVE_GAMES.clear()
            app.JOIN_CODES.clear()
            app.BASE_MSG_IDS.clear()
            app.LAST_REFRESH.clear()
            app.CHAT_GAMES.clear()
            app.AWAITING_NAME_USERS.clear()

            greb_app.ACTIVE_GAMES.clear()
            greb_app.CHAT_GAMES.clear()
            greb_app.JOIN_CODES.clear()
            greb_app.FINISHED_GAMES.clear()
            greb_app.BASE_MSG_IDS.clear()
            greb_app.LAST_REFRESH.clear()
            greb_app.REFRESH_LOCKS.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()

            root_app.REGISTERED_GAMES.clear()
            root_app.APPLICATION = Application.builder().token("123:ABC").build()

            shared_application = SimpleNamespace(user_data={})
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))
            )
            context = SimpleNamespace(
                args=[],
                user_data={},
                application=shared_application,
                bot=bot,
            )

            user_id = 707
            chat_id = 808
            start_message = DummyMessage(chat_id, user_id, text="/start")
            start_update = SimpleNamespace(
                message=start_message,
                effective_message=start_message,
                effective_chat=start_message.chat,
                effective_user=SimpleNamespace(id=user_id),
            )

            await root_app.start(start_update, context)

            query = DummyCallbackQuery("game_grebeshok", start_message, user_id)
            callback_update = SimpleNamespace(
                callback_query=query,
                effective_chat=start_message.chat,
                effective_user=SimpleNamespace(id=user_id),
                effective_message=start_message,
                message=None,
            )

            name_message = DummyMessage(chat_id, user_id, text="Глеб")

            with (
                patch.object(app, "schedule_refresh_base_button", lambda *a, **kw: None),
                patch.object(
                    greb_app, "schedule_refresh_base_letters", lambda *a, **kw: None
                ),
            ):
                await root_app.choose_game(callback_update, context)

                assert user_id in greb_app.AWAITING_GREBESHOK_NAME_USERS
                assert context.user_data.get("awaiting_grebeshok_name") is True

                name_update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=user_id),
                    effective_chat=name_message.chat,
                    effective_message=name_message,
                    message=name_message,
                )

                try:
                    await greb_app.handle_name(name_update, context)
                except ApplicationHandlerStop:
                    pass

            assert any(
                "Имя установлено" in reply[0] for reply in name_message.replies
            )
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(compose_active)
            app.JOIN_CODES.clear()
            app.JOIN_CODES.update(compose_join)
            app.BASE_MSG_IDS.clear()
            app.BASE_MSG_IDS.update(compose_base_ids)
            app.LAST_REFRESH.clear()
            app.LAST_REFRESH.update(compose_refresh)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(compose_chat_games)
            app.AWAITING_NAME_USERS.clear()
            app.AWAITING_NAME_USERS.update(compose_awaiting)
            app.APPLICATION = old_compose_app

            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(greb_active)
            greb_app.CHAT_GAMES.clear()
            greb_app.CHAT_GAMES.update(greb_chat_games)
            greb_app.JOIN_CODES.clear()
            greb_app.JOIN_CODES.update(greb_join)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(greb_finished)
            greb_app.BASE_MSG_IDS.clear()
            greb_app.BASE_MSG_IDS.update(greb_base_ids)
            greb_app.LAST_REFRESH.clear()
            greb_app.LAST_REFRESH.update(greb_refresh)
            greb_app.REFRESH_LOCKS.clear()
            greb_app.REFRESH_LOCKS.update(greb_locks)
            greb_app.AWAITING_GREBESHOK_NAME_USERS.clear()
            greb_app.AWAITING_GREBESHOK_NAME_USERS.update(greb_awaiting)
            greb_app.APPLICATION = old_greb_app

            root_app.REGISTERED_GAMES.clear()
            root_app.REGISTERED_GAMES.update(registered_snapshot)
            root_app.APPLICATION = application_snapshot

    asyncio.run(run())


def test_compose_end_game_sends_stats_message():
    async def run():
        old_active = app.ACTIVE_GAMES.copy()
        old_base_ids = app.BASE_MSG_IDS.copy()
        old_chat_games = app.CHAT_GAMES.copy()
        try:
            app.ACTIVE_GAMES.clear()
            app.BASE_MSG_IDS.clear()
            app.CHAT_GAMES.clear()

            game = app.GameState(host_id=1, game_id="gid")
            player_a = app.Player(user_id=1, name="Алиса", words=["молоко", "самовар"])
            player_a.points = 3
            player_b = app.Player(user_id=2, name="Боб", words=["тест", "самолет"])
            player_b.points = 2
            game.players = {1: player_a, 2: player_b}
            game.base_word = "пример"
            game.word_history = [(1, "молоко"), (2, "самолет"), (1, "самовар")]
            game.player_chats = {1: 42, 2: 43}
            app.ACTIVE_GAMES["gid"] = game
            app.BASE_MSG_IDS["gid"] = 10
            app.CHAT_GAMES[(42, 0)] = "gid"

            context = SimpleNamespace(
                job=SimpleNamespace(chat_id=42, data={"thread_id": None}),
                bot=SimpleNamespace(delete_message=AsyncMock()),
            )

            zipf_map = {"молоко": 3.2, "самолет": 3.0, "самовар": 2.5}

            with (
                patch.object(app, "broadcast", new=AsyncMock()) as broadcast_mock,
                patch.object(app, "get_zipf", side_effect=lambda w: zipf_map.get(w)),
            ):
                await app.end_game(context)

            assert broadcast_mock.await_count == 3
            stats_call = broadcast_mock.await_args_list[1]
            _, stats_text = stats_call.args[:2]
            assert "🏅 <b>Лидеры по длинным словам (6 и более букв):</b>" in stats_text
            assert "Алиса" in stats_text and "2 шт." in stats_text
            assert "самолет" in stats_text
            assert "самовар" in stats_text
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(old_active)
            app.BASE_MSG_IDS.clear()
            app.BASE_MSG_IDS.update(old_base_ids)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())


def test_compose_stats_handle_empty_data():
    async def run():
        old_active = app.ACTIVE_GAMES.copy()
        old_base_ids = app.BASE_MSG_IDS.copy()
        old_chat_games = app.CHAT_GAMES.copy()
        try:
            app.ACTIVE_GAMES.clear()
            app.BASE_MSG_IDS.clear()
            app.CHAT_GAMES.clear()

            game = app.GameState(host_id=1, game_id="gid2")
            player = app.Player(user_id=1, name="Алиса", words=[])
            game.players = {1: player}
            game.base_word = "пример"
            app.ACTIVE_GAMES["gid2"] = game
            app.CHAT_GAMES[(99, 0)] = "gid2"

            context = SimpleNamespace(
                job=SimpleNamespace(chat_id=99, data={"thread_id": None}),
                bot=SimpleNamespace(delete_message=AsyncMock()),
            )

            with (
                patch.object(app, "broadcast", new=AsyncMock()) as broadcast_mock,
                patch.object(app, "get_zipf", return_value=None),
            ):
                await app.end_game(context)

            assert broadcast_mock.await_count == 3
            stats_text = broadcast_mock.await_args_list[1].args[1]
            assert "Нет слов длиной 6+ букв" in stats_text
            assert "Нет данных о самых длинных словах" in stats_text
            assert "Нет данных о редкости слов" in stats_text
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(old_active)
            app.BASE_MSG_IDS.clear()
            app.BASE_MSG_IDS.update(old_base_ids)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())


def test_restart_handler_handles_repeated_restart_no_callbacks():
    async def run():
        old_active = app.ACTIVE_GAMES.copy()
        old_chat_games = app.CHAT_GAMES.copy()
        try:
            app.ACTIVE_GAMES.clear()
            app.CHAT_GAMES.clear()

            host_id = 501
            host_chat_id = 601
            other_chat_id = 602
            game = app.GameState(host_id=host_id, game_id="restart_game")
            game.player_chats = {host_id: host_chat_id, 999: other_chat_id}
            app.ACTIVE_GAMES["restart_game"] = game
            app.CHAT_GAMES[(host_chat_id, 0)] = "restart_game"

            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(bot=bot)

            message = DummyMessage(host_chat_id, host_id)
            query = DummyCallbackQuery("restart_no", message, host_id)
            update = SimpleNamespace(callback_query=query)

            await app.restart_handler(update, context)

            expected_text = (
                "Игра завершена. Для новой игры с новыми участниками нажмите /start"
            )
            assert query.edited_texts and query.edited_texts[-1][0] == expected_text
            assert bot.send_message.await_count == 1
            assert "restart_game" not in app.ACTIVE_GAMES
            assert (host_chat_id, 0) not in app.CHAT_GAMES

            second_query = DummyCallbackQuery("restart_no", message, host_id)
            second_update = SimpleNamespace(callback_query=second_query)

            await app.restart_handler(second_update, context)

            assert second_query.edited_texts and second_query.edited_texts[-1][0] == expected_text
            assert bot.send_message.await_count == 1
        finally:
            app.ACTIVE_GAMES.clear()
            app.ACTIVE_GAMES.update(old_active)
            app.CHAT_GAMES.clear()
            app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())


def test_grebeshok_finish_game_stats_message():
    async def run():
        old_active = greb_app.ACTIVE_GAMES.copy()
        old_finished = greb_app.FINISHED_GAMES.copy()
        try:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.FINISHED_GAMES.clear()

            game = greb_app.GameState(host_id=1)
            game.base_letters = ("к", "о", "т")
            player_a = greb_app.Player(user_id=1, name="Глеб", words=["котята"])
            player_a.points = 1
            player_b = greb_app.Player(user_id=2, name="Оля", words=["котофей", "тотем"])
            player_b.points = 2
            game.players = {1: player_a, 2: player_b}
            game.word_history = [(1, "котята"), (2, "котофей"), (2, "тотем")]
            key = (200, 0)
            greb_app.ACTIVE_GAMES[key] = game

            context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

            zipf_map = {"котята": 3.0, "котофей": 3.2, "тотем": 2.4}

            with (
                patch.object(greb_app, "broadcast", new=AsyncMock()) as broadcast_mock,
                patch.object(greb_app, "send_game_message", new=AsyncMock()),
                patch.object(
                    greb_app, "get_zipf", side_effect=lambda w: zipf_map.get(w)
                ),
            ):
                await greb_app.finish_game(game, context, "Время вышло")

            assert broadcast_mock.await_count >= 2
            stats_text = broadcast_mock.await_args_list[1].args[1]
            assert "Самое длинное слово" in stats_text
            assert "котофей" in stats_text
            assert "котята" in stats_text
            assert "тотем" in stats_text
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(old_finished)

    asyncio.run(run())


def test_grebeshok_stats_handle_empty_data():
    async def run():
        old_active = greb_app.ACTIVE_GAMES.copy()
        old_finished = greb_app.FINISHED_GAMES.copy()
        try:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.FINISHED_GAMES.clear()

            game = greb_app.GameState(host_id=1)
            game.base_letters = ("к", "о", "т")
            player = greb_app.Player(user_id=1, name="Глеб", words=[])
            game.players = {1: player}
            key = (300, 0)
            greb_app.ACTIVE_GAMES[key] = game

            context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

            with (
                patch.object(greb_app, "broadcast", new=AsyncMock()) as broadcast_mock,
                patch.object(greb_app, "send_game_message", new=AsyncMock()),
                patch.object(greb_app, "get_zipf", return_value=None),
            ):
                await greb_app.finish_game(game, context, "Время вышло")

            assert broadcast_mock.await_count >= 2
            stats_text = broadcast_mock.await_args_list[1].args[1]
            assert "Нет данных о самых длинных словах" in stats_text
            assert (
                "Никто не составил слова с более чем 3 базовыми буквами." in stats_text
            )
            assert "Нет данных о редкости слов" in stats_text
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(old_finished)

    asyncio.run(run())


def test_grebeshok_handle_word_stops_after_status_change():
    async def run():
        old_active = greb_app.ACTIVE_GAMES.copy()
        old_chat_games = greb_app.CHAT_GAMES.copy()
        try:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.CHAT_GAMES.clear()

            host_id = 1
            player_id = 42
            chat_id = 420
            game = greb_app.GameState(host_id=host_id)
            game.status = "running"
            game.base_letters = ("к", "о", "т")
            player = greb_app.Player(user_id=player_id, name="Игрок")
            game.players = {player_id: player}
            game.player_chats = {player_id: chat_id}

            key = (chat_id, 0)
            greb_app.ACTIVE_GAMES[key] = game
            greb_app.CHAT_GAMES[chat_id] = game

            message = DummyMessage(chat_id, player_id, text="котик котомка")
            update = SimpleNamespace(
                message=message,
                effective_chat=message.chat,
                effective_user=message.from_user,
            )
            context = SimpleNamespace(user_data={}, bot=SimpleNamespace())

            async def broadcast_side_effect(*args, **kwargs):
                text = args[1] if len(args) > 1 else kwargs.get("text", "")
                if "котик" in text:
                    game.status = "finished"

            with (
                patch.object(greb_app, "DICTIONARY", {"котик", "котомка"}),
                patch.object(
                    greb_app,
                    "broadcast",
                    new=AsyncMock(side_effect=broadcast_side_effect),
                ) as broadcast_mock,
                patch.object(greb_app, "reply_game_message", new=AsyncMock()) as reply_mock,
                patch.object(greb_app, "schedule_refresh_base_letters", lambda *a, **kw: None),
            ):
                await greb_app.handle_word(update, context)

            assert player.words == ["котик"]
            assert game.word_history == [(player_id, "котик")]
            assert game.used_words == {"котик"}
            assert broadcast_mock.await_count == 1
            texts = [call.args[2] for call in reply_mock.await_args_list]
            assert all("котомка" not in text for text in texts)
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.CHAT_GAMES.clear()
            greb_app.CHAT_GAMES.update(old_chat_games)

    asyncio.run(run())
