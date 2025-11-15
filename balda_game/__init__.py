"""Balda game scaffolding package."""

from .handlers import newgame, register_handlers, reset_for_chat, start_cmd
from .services import REGISTRY, get_game
from .state import GameState, PlayerState, TurnRecord

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
    "REGISTRY",
]
