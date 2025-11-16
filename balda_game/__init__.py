"""Balda game scaffolding package."""

from .handlers import newgame, quit_cmd, register_handlers, reset_for_chat, start_cmd
from .state import GameState, PlayerState, TurnRecord
from .state.manager import STATE_MANAGER


def get_game(chat_id: int, thread_id: int | None = None) -> GameState | None:
    """Public helper that proxies to the shared state manager."""

    return STATE_MANAGER.get_by_chat(chat_id, thread_id)


def find_game_for_player(user_id: int) -> GameState | None:
    """Resolve the active game that involves the provided user."""

    return STATE_MANAGER.find_by_player(user_id)

BOT_USERNAME = ""

__all__ = [
    "BOT_USERNAME",
    "GameState",
    "PlayerState",
    "TurnRecord",
    "register_handlers",
    "start_cmd",
    "newgame",
    "reset_for_chat",
    "get_game",
    "find_game_for_player",
    "STATE_MANAGER",
    "quit_cmd",
]
