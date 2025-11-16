"""Unit tests for the Balda gameplay helpers and state."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from balda_game.handlers import gameplay
from balda_game.handlers import lobby as balda_lobby
from balda_game.services import GameStats
from balda_game.state import GameState, PlayerState, TurnRecord
from balda_game.state.manager import STATE_MANAGER


class _DummyJob:
    def __init__(self) -> None:
        self.removed = False
        self.cancelled = False

    def schedule_removal(self) -> None:
        self.removed = True

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture(autouse=True)
def _reset_state_manager() -> None:
    """Ensure the singleton state manager is clean between tests."""

    STATE_MANAGER.reset()
    gameplay.PENDING_MOVES.clear()
    gameplay.BOARD_FLASH_TASKS.clear()
    yield
    STATE_MANAGER.reset()
    gameplay.PENDING_MOVES.clear()
    gameplay.BOARD_FLASH_TASKS.clear()


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests to run on asyncio only (Telegram handlers use asyncio)."""

    return "asyncio"


def _build_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, reply_text=AsyncMock())


def _build_update(text: str, user_id: int) -> SimpleNamespace:
    message = _build_message(text)
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(effective_message=message, effective_user=user)


def _prepare_state(*, sequence: str = "к") -> GameState:
    state = STATE_MANAGER.create_lobby(host_id=1, chat_id=100)
    player = PlayerState(user_id=1, name="Alice")
    opponent = PlayerState(user_id=2, name="Bob")
    state.players = {1: player, 2: opponent}
    state.players_active = [1, 2]
    state.current_player = 1
    state.sequence = sequence
    STATE_MANAGER.save(state)
    return state


def _build_full_update(
    *,
    user_id: int,
    chat_id: int,
    text: str = "/quit",
    thread_id: int | None = None,
) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id)
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        chat=chat,
        chat_id=chat_id,
        message_thread_id=thread_id,
    )
    user = SimpleNamespace(id=user_id, first_name="User", full_name="User", username="user")
    return SimpleNamespace(
        effective_message=message,
        effective_chat=chat,
        effective_user=user,
    )


def test_game_state_add_turn_and_reset_timer() -> None:
    state = GameState(game_id="g", host_id=1, chat_id=10, sequence="ра")
    reminder_job = _DummyJob()
    timeout_job = _DummyJob()
    state.timer_job = {"reminder": reminder_job, "timeout": timeout_job}

    left_turn = TurnRecord(player_id=1, letter="б", word="бра", direction="left")
    state.add_turn(left_turn)
    assert state.sequence == "бра"

    right_turn = TurnRecord(player_id=2, letter="н", word="бран", direction="right")
    state.add_turn(right_turn)
    assert state.sequence == "бран"
    assert state.direction == "right"
    assert state.words_used[-1] is right_turn

    state.reset_timer()
    assert state.timer_job == {}
    assert reminder_job.removed or reminder_job.cancelled
    assert timeout_job.removed or timeout_job.cancelled


@pytest.mark.anyio
async def test_handle_move_submission_accepts_valid_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state(sequence="к")
    gameplay.PENDING_MOVES[1] = gameplay.PendingMove(game_id=state.game_id, direction="right")
    monkeypatch.setattr(gameplay, "BALDA_DICTIONARY", {"наказ"})
    announce = AsyncMock()
    advance = AsyncMock()
    monkeypatch.setattr(gameplay, "_announce_turn", announce)
    monkeypatch.setattr(gameplay, "_advance_turn", advance)

    update = _build_update("а наказ", user_id=1)
    context = SimpleNamespace()

    await gameplay.handle_move_submission(update, context)

    assert state.sequence == "ка"
    assert len(state.words_used) == 1
    assert state.words_used[0].word == "наказ"
    assert announce.await_count == 1
    assert advance.await_count == 1
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.anyio
async def test_handle_move_submission_rejects_missing_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state(sequence="к")
    gameplay.PENDING_MOVES[1] = gameplay.PendingMove(game_id=state.game_id, direction="left")
    monkeypatch.setattr(gameplay, "BALDA_DICTIONARY", {"лампа"})
    announce = AsyncMock()
    advance = AsyncMock()
    monkeypatch.setattr(gameplay, "_announce_turn", announce)
    monkeypatch.setattr(gameplay, "_advance_turn", advance)

    update = _build_update("л лампа", user_id=1)
    context = SimpleNamespace()

    await gameplay.handle_move_submission(update, context)

    update.effective_message.reply_text.assert_awaited()
    assert "последовательность" in update.effective_message.reply_text.call_args.args[0]
    assert len(state.words_used) == 0
    announce.assert_not_awaited()
    advance.assert_not_awaited()


@pytest.mark.anyio
async def test_pass_turn_marks_player_and_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state()
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    advance = AsyncMock()
    monkeypatch.setattr(gameplay, "_advance_turn", advance)
    query = SimpleNamespace(
        data=f"balda:pass:{state.game_id}",
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)

    await gameplay.pass_turn_callback(update, context)

    assert state.players[1].has_passed is True
    assert state.has_passed[1] is True
    advance.assert_awaited()
    query.answer.assert_awaited()
    bot.send_message.assert_awaited()


@pytest.mark.anyio
async def test_finish_game_announces_winner_and_drops_state(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state(sequence="рака")
    stats = GameStats(
        total_turns=3,
        unique_words=3,
        duration_seconds=60,
        duration_text="1м00с",
        final_sequence="РАКА",
        elimination_names=["Bob"],
    )
    monkeypatch.setattr(gameplay, "collect_game_stats", lambda *_: stats)
    monkeypatch.setattr(gameplay, "format_stats_message", lambda stats, winner_name=None: "stats")
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    winner = state.players[1]

    await gameplay.finish_game(state, context, winner)

    assert bot.send_message.await_count == 2
    assert "Победитель" in bot.send_message.await_args_list[0].args[1]
    assert bot.send_message.await_args_list[1].args[1] == "stats"
    assert STATE_MANAGER.get_by_id(state.game_id) is None


def test_state_manager_finds_game_by_player() -> None:
    state = _prepare_state(sequence="ла")
    found = STATE_MANAGER.find_by_player(2)
    missing = STATE_MANAGER.find_by_player(99)

    assert found is state
    assert missing is None


@pytest.mark.anyio
async def test_resign_player_announces_and_eliminates(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state(sequence="ра")
    state.chat_id = 777
    state.has_started = True
    state.current_player = 1
    gameplay.PENDING_MOVES[1] = gameplay.PendingMove(game_id=state.game_id, direction="left")
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    eliminated = AsyncMock()
    monkeypatch.setattr(gameplay, "eliminate_player", eliminated)

    await gameplay.resign_player(state, context, 1)

    assert bot.send_message.await_count == 1
    assert "покинул игру" in bot.send_message.await_args_list[0].args[1]
    eliminated.assert_awaited()
    assert 1 not in gameplay.PENDING_MOVES


@pytest.mark.anyio
async def test_quit_cmd_removes_host_from_lobby(monkeypatch: pytest.MonkeyPatch) -> None:
    state = STATE_MANAGER.create_lobby(host_id=2, chat_id=500)
    host = PlayerState(user_id=2, name="Host", is_host=True)
    guest = PlayerState(user_id=3, name="Guest")
    state.players = {2: host, 3: guest}
    state.players_active = [2, 3]
    state.has_started = False
    publish = AsyncMock()
    sync_keyboard = AsyncMock()
    announce = AsyncMock()
    monkeypatch.setattr(balda_lobby, "_publish_lobby", publish)
    monkeypatch.setattr(balda_lobby, "_sync_invite_keyboard", sync_keyboard)
    monkeypatch.setattr(balda_lobby, "_announce_lobby_departure", announce)
    update = _build_full_update(user_id=2, chat_id=999)
    context = SimpleNamespace(bot=None)

    await balda_lobby.quit_cmd(update, context)

    assert 2 not in state.players
    assert state.host_id == 3
    assert state.players_active == [3]
    assert state.players[3].is_host is True
    publish.assert_awaited()
    sync_keyboard.assert_awaited()
    announce.assert_awaited()
    assert "покинули" in update.effective_message.reply_text.await_args_list[0].args[0]


@pytest.mark.anyio
async def test_quit_cmd_resigns_active_player(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _prepare_state(sequence="ра")
    state.has_started = True
    state.chat_id = 100
    state.thread_id = None
    state.current_player = 1
    resign = AsyncMock()
    monkeypatch.setattr(balda_lobby, "resign_player", resign)
    update = _build_full_update(user_id=1, chat_id=state.chat_id)
    context = SimpleNamespace(bot=None)

    await balda_lobby.quit_cmd(update, context)

    resign.assert_awaited_once()
    reply_text = update.effective_message.reply_text.await_args_list[-1].args[0]
    assert "поражение" in reply_text


@pytest.mark.anyio
async def test_quit_cmd_drops_empty_lobby(monkeypatch: pytest.MonkeyPatch) -> None:
    state = STATE_MANAGER.create_lobby(host_id=5, chat_id=600)
    player = PlayerState(user_id=5, name="Solo", is_host=True)
    state.players = {5: player}
    state.players_active = [5]
    state.has_started = False
    notify_closed = AsyncMock()
    monkeypatch.setattr(balda_lobby, "_notify_lobby_closed", notify_closed)
    update = _build_full_update(user_id=5, chat_id=state.chat_id)
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)

    await balda_lobby.quit_cmd(update, context)

    notify_closed.assert_awaited()
    assert STATE_MANAGER.get_by_id(state.game_id) is None
    assert "покинули" in update.effective_message.reply_text.await_args_list[0].args[0]
