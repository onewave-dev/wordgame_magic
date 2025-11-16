"""Utilities for serializing Balda game state to a local JSON file."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Tuple

from .models import GameState, PlayerState, TurnRecord

LOGGER = logging.getLogger(__name__)
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / ".balda_state.json"

GameKey = Tuple[int, int]


def _serialize_player(player: PlayerState) -> Dict[str, object]:
    return {
        "user_id": player.user_id,
        "name": player.name,
        "has_passed": player.has_passed,
        "is_eliminated": player.is_eliminated,
        "is_host": player.is_host,
    }


def _serialize_turn(turn: TurnRecord) -> Dict[str, object]:
    return {
        "player_id": turn.player_id,
        "letter": turn.letter,
        "word": turn.word,
        "direction": turn.direction,
        "timestamp": turn.timestamp.isoformat(),
    }


def _serialize_state(state: GameState) -> Dict[str, object]:
    return {
        "game_id": state.game_id,
        "host_id": state.host_id,
        "chat_id": state.chat_id,
        "sequence": state.sequence,
        "base_letter": state.base_letter,
        "current_player": state.current_player,
        "direction": state.direction,
        "created_at": state.created_at.isoformat(),
        "thread_id": state.thread_id,
        "players": {str(uid): _serialize_player(player) for uid, player in state.players.items()},
        "players_active": state.players_active,
        "players_out": state.players_out,
        "words_used": [_serialize_turn(turn) for turn in state.words_used],
        "has_passed": {str(uid): flag for uid, flag in state.has_passed.items()},
        "has_started": state.has_started,
        "join_code": state.join_code,
        "lobby_message_id": state.lobby_message_id,
        "lobby_message_chat_id": state.lobby_message_chat_id,
        "board_message_id": state.board_message_id,
        "invite_keyboard_visible": state.invite_keyboard_visible,
        "invited_users": sorted(state.invited_users),
    }


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        LOGGER.warning("Invalid datetime value %s in persisted Balda state", value)
        return datetime.utcnow()


def _deserialize_player(payload: Dict[str, object]) -> PlayerState:
    return PlayerState(
        user_id=int(payload["user_id"]),
        name=str(payload["name"]),
        has_passed=bool(payload.get("has_passed", False)),
        is_eliminated=bool(payload.get("is_eliminated", False)),
        is_host=bool(payload.get("is_host", False)),
    )


def _deserialize_turn(payload: Dict[str, object]) -> TurnRecord:
    timestamp_raw = payload.get("timestamp")
    return TurnRecord(
        player_id=int(payload["player_id"]),
        letter=str(payload["letter"]),
        word=str(payload["word"]),
        direction=str(payload["direction"]),
        timestamp=_parse_datetime(timestamp_raw if isinstance(timestamp_raw, str) else None),
    )


def _deserialize_state(payload: Dict[str, object]) -> GameState:
    players_payload = payload.get("players", {})
    players = {int(uid): _deserialize_player(data) for uid, data in players_payload.items()}
    words = [_deserialize_turn(entry) for entry in payload.get("words_used", [])]
    has_passed_payload = payload.get("has_passed", {})
    has_passed = {int(uid): bool(flag) for uid, flag in has_passed_payload.items()}
    invited_users_payload = payload.get("invited_users", [])
    invited_users = {int(uid) for uid in invited_users_payload}
    return GameState(
        game_id=str(payload["game_id"]),
        host_id=int(payload["host_id"]),
        chat_id=int(payload["chat_id"]),
        sequence=str(payload.get("sequence", "")),
        base_letter=payload.get("base_letter"),
        current_player=payload.get("current_player"),
        direction=payload.get("direction"),
        created_at=_parse_datetime(payload.get("created_at")),
        thread_id=payload.get("thread_id"),
        players=players,
        players_active=[int(player_id) for player_id in payload.get("players_active", [])],
        players_out=[int(player_id) for player_id in payload.get("players_out", [])],
        words_used=words,
        has_passed=has_passed,
        timer_job={},
        has_started=bool(payload.get("has_started", False)),
        join_code=payload.get("join_code"),
        lobby_message_id=payload.get("lobby_message_id"),
        lobby_message_chat_id=payload.get("lobby_message_chat_id"),
        board_message_id=payload.get("board_message_id"),
        invite_keyboard_visible=bool(payload.get("invite_keyboard_visible", False)),
        invited_users=invited_users,
    )


class StateStorage:
    """Read and write Balda state snapshots to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Tuple[Dict[str, GameState], Dict[GameKey, str], Dict[str, str]]:
        """Load the serialized state from disk."""

        if not self._path.exists():
            return {}, {}, {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.error("Failed to read Balda state from %s: %s", self._path, exc)
            return {}, {}, {}
        games_payload = payload.get("games", {})
        games: Dict[str, GameState] = {}
        for game_id, data in games_payload.items():
            try:
                games[game_id] = _deserialize_state(data)
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.error("Failed to deserialize Balda game %s: %s", game_id, exc)
        chat_index_payload: Iterable[Dict[str, object]] = payload.get("chat_index", [])
        chat_index: Dict[GameKey, str] = {}
        for entry in chat_index_payload:
            try:
                chat_id = int(entry["chat_id"])
                thread_id = int(entry.get("thread_id", 0))
                game_id = str(entry["game_id"])
            except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - defensive logging
                LOGGER.error("Invalid chat index entry %s: %s", entry, exc)
                continue
            chat_index[(chat_id, thread_id)] = game_id
        join_codes_payload = payload.get("join_codes", {})
        join_codes: Dict[str, str] = {str(code): str(game_id) for code, game_id in join_codes_payload.items()}
        return games, chat_index, join_codes

    def dump(
        self,
        games: Dict[str, GameState],
        chat_index: Dict[GameKey, str],
        join_codes: Dict[str, str],
    ) -> None:
        """Write the in-memory state to disk."""

        payload = {
            "games": {game_id: _serialize_state(state) for game_id, state in games.items()},
            "chat_index": [
                {"chat_id": chat_id, "thread_id": thread_id, "game_id": game_id}
                for (chat_id, thread_id), game_id in chat_index.items()
            ],
            "join_codes": join_codes,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError as exc:
            LOGGER.error("Failed to persist Balda state to %s: %s", self._path, exc)

    def clear(self) -> None:
        """Remove the persisted state file entirely."""

        try:
            if self._path.exists():
                self._path.unlink()
        except OSError as exc:
            LOGGER.error("Failed to delete Balda state file %s: %s", self._path, exc)


__all__ = ["StateStorage", "DEFAULT_STATE_PATH"]
