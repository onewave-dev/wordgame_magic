import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from compose_word_game import word_game_app as app
from grebeshok_game import grebeshok_app as greb_app
from telegram.ext import Application, ApplicationHandlerStop


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

            assert broadcast_mock.await_count == 2
            stats_call = broadcast_mock.await_args_list[1]
            _, stats_text = stats_call.args[:2]
            assert "📊 <b>Длинные слова</b>" in stats_text
            assert "Алиса" in stats_text and "2" in stats_text
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

            assert broadcast_mock.await_count == 2
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
            assert "Нет данных по базовым буквам" in stats_text
            assert "Нет данных о редкости слов" in stats_text
        finally:
            greb_app.ACTIVE_GAMES.clear()
            greb_app.ACTIVE_GAMES.update(old_active)
            greb_app.FINISHED_GAMES.clear()
            greb_app.FINISHED_GAMES.update(old_finished)

    asyncio.run(run())
