"""Persistence-aware CRUD manager for Balda game state."""

from __future__ import annotations

import logging
from secrets import token_urlsafe
from typing import Dict, Optional, Tuple

from .models import GameState
from .storage import DEFAULT_STATE_PATH, StateStorage

GameKey = Tuple[int, int]


class GameStateManager:
    """Utility that stores and retrieves Balda game sessions."""

    def __init__(self, storage: Optional[StateStorage] = None) -> None:
        self._logger = logging.getLogger(__name__)
        self._storage = storage or StateStorage(DEFAULT_STATE_PATH)
        self._active_games: Dict[str, GameState] = {}
        self._chat_index: Dict[GameKey, str] = {}
        self._join_codes: Dict[str, str] = {}
        self._load_from_disk()

    # Creation helpers -------------------------------------------------
    def create_lobby(self, host_id: int, chat_id: int, thread_id: Optional[int] = None) -> GameState:
        """Allocate a new lobby bound to the provided chat."""

        game_id = token_urlsafe(8)
        state = GameState(game_id=game_id, host_id=host_id, chat_id=chat_id, thread_id=thread_id)
        self._active_games[game_id] = state
        self._chat_index[(chat_id, thread_id or 0)] = game_id
        self._persist()
        return state

    def ensure_join_code(self, state: GameState) -> str:
        """Attach a reusable join code to the lobby."""

        if state.join_code and state.join_code in self._join_codes:
            return state.join_code
        code = token_urlsafe(4)
        self._join_codes[code] = state.game_id
        state.join_code = code
        self._persist()
        return code

    # Lookup helpers ---------------------------------------------------
    def get_by_chat(self, chat_id: int, thread_id: Optional[int]) -> Optional[GameState]:
        """Return the lobby/game bound to the chat/thread combination."""

        key = (chat_id, thread_id or 0)
        game_id = self._chat_index.get(key)
        return self._active_games.get(game_id) if game_id else None

    def get_by_id(self, game_id: str) -> Optional[GameState]:
        """Return a state snapshot by its internal identifier."""

        return self._active_games.get(game_id)

    def get_by_join_code(self, join_code: str) -> Optional[GameState]:
        """Resolve and return a lobby via a join code."""

        game_id = self._join_codes.get(join_code)
        return self._active_games.get(game_id) if game_id else None

    def has_join_code(self, join_code: str) -> bool:
        """Check if a join code belongs to the Balda manager."""

        return join_code in self._join_codes

    def find_by_player(self, user_id: int) -> Optional[GameState]:
        """Return the first game that still lists the provided player."""

        for state in self._active_games.values():
            if user_id in state.players:
                return state
        return None

    # Mutation helpers -------------------------------------------------
    def save(self, state: GameState) -> GameState:
        """Persist changes to an existing game state."""

        self._active_games[state.game_id] = state
        self._persist()
        return state

    def reset_chat(self, chat_id: int) -> None:
        """Drop all bindings associated with the provided chat."""

        keys = [key for key in self._chat_index if key[0] == chat_id]
        for key in keys:
            game_id = self._chat_index.pop(key, None)
            if not game_id:
                continue
            state = self._active_games.pop(game_id, None)
            if state:
                state.reset_timer()
            stale_codes = [code for code, gid in self._join_codes.items() if gid == game_id]
            for code in stale_codes:
                self._join_codes.pop(code, None)
        self._persist()

    def drop_game(self, game_id: str) -> None:
        """Remove a single game and its join codes."""

        state = self._active_games.pop(game_id, None)
        if not state:
            return
        state.reset_timer()
        key = (state.chat_id, state.thread_id or 0)
        self._chat_index.pop(key, None)
        stale_codes = [code for code, gid in self._join_codes.items() if gid == game_id]
        for code in stale_codes:
            self._join_codes.pop(code, None)
        self._persist()

    def reset(self) -> None:
        """Clear all stored data (used in tests)."""

        self._active_games.clear()
        self._chat_index.clear()
        self._join_codes.clear()
        self._storage.clear()

    # Internal helpers -------------------------------------------------
    def _persist(self) -> None:
        """Write the in-memory state to disk, logging any failures."""

        try:
            self._storage.dump(self._active_games, self._chat_index, self._join_codes)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to persist Balda state: %s", exc)

    def _load_from_disk(self) -> None:
        """Restore previously saved games when the manager initializes."""

        try:
            games, chat_index, join_codes = self._storage.load()
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("Failed to load Balda state: %s", exc)
            return
        self._active_games = games
        self._chat_index = chat_index
        self._join_codes = join_codes


STATE_MANAGER = GameStateManager()

