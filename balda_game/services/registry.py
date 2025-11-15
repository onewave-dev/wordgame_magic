"""In-memory registries used by the Balda game."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Dict, Optional, Tuple

from ..state import GameState

GameKey = Tuple[int, int]


@dataclass
class GameRegistry:
    """Container that groups all shared registries for the Balda game."""

    active_games: Dict[str, GameState]
    chat_games: Dict[GameKey, str]
    join_codes: Dict[str, str]

    def __init__(self) -> None:
        self.active_games = {}
        self.chat_games = {}
        self.join_codes = {}

    def allocate_game(self, host_id: int, chat_id: int, thread_id: Optional[int] = None) -> GameState:
        game_id = token_urlsafe(8)
        state = GameState(game_id=game_id, host_id=host_id, chat_id=chat_id, thread_id=thread_id)
        self.active_games[game_id] = state
        self.chat_games[(chat_id, thread_id or 0)] = game_id
        return state

    def get_game(self, chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
        key = (chat_id, thread_id or 0)
        game_id = self.chat_games.get(key)
        return self.active_games.get(game_id) if game_id else None

    def clear_chat(self, chat_id: int) -> None:
        keys = [key for key in self.chat_games if key[0] == chat_id]
        for key in keys:
            game_id = self.chat_games.pop(key)
            if game_id and game_id in self.active_games:
                self.active_games.pop(game_id)
                stale_codes = [code for code, gid in self.join_codes.items() if gid == game_id]
                for code in stale_codes:
                    self.join_codes.pop(code, None)

    def reset(self) -> None:
        self.active_games.clear()
        self.chat_games.clear()
        self.join_codes.clear()


REGISTRY = GameRegistry()


def clear_chat_state(chat_id: int) -> None:
    """Remove all state bindings associated with the provided chat."""

    REGISTRY.clear_chat(chat_id)


def create_lobby(host_id: int, chat_id: int, thread_id: Optional[int] = None) -> GameState:
    """Create a new lobby bound to the chat and host."""

    return REGISTRY.allocate_game(host_id, chat_id, thread_id)


def get_game(chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
    """Return the stored game for a chat/thread combination, if any."""

    return REGISTRY.get_game(chat_id, thread_id)


def generate_join_code(game_id: str) -> str:
    """Store and return a join code for the provided game id."""

    code = token_urlsafe(4)
    REGISTRY.join_codes[code] = game_id
    return code
