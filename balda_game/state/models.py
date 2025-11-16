"""Dataclasses describing the Balda lobby and runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass(slots=True)
class PlayerState:
    """Information about a player participating in a Balda match."""

    user_id: int
    name: str
    has_passed: bool = False
    is_eliminated: bool = False
    is_host: bool = False


@dataclass(slots=True)
class TurnRecord:
    """Record of a single action performed by a player."""

    player_id: int
    letter: str
    word: str
    direction: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class GameState:
    """Snapshot of the Balda lobby and running game."""

    game_id: str
    host_id: int
    chat_id: int
    sequence: str = ""
    base_letter: Optional[str] = None
    current_player: Optional[int] = None
    direction: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    thread_id: Optional[int] = None
    players: Dict[int, PlayerState] = field(default_factory=dict)
    players_active: List[int] = field(default_factory=list)
    players_out: List[int] = field(default_factory=list)
    words_used: List[TurnRecord] = field(default_factory=list)
    has_passed: Dict[int, bool] = field(default_factory=dict)
    timer_job: Dict[str, object] = field(default_factory=dict)
    has_started: bool = False
    join_code: Optional[str] = None
    lobby_message_id: Optional[int] = None
    lobby_message_chat_id: Optional[int] = None
    board_message_id: Optional[int] = None
    invite_keyboard_visible: bool = False
    invited_users: set[int] = field(default_factory=set)

    def reset_timer(self) -> None:
        """Cancel and forget scheduled timer jobs for the current player."""

        jobs = list(self.timer_job.values())
        for job in jobs:
            try:
                job.schedule_removal()  # type: ignore[attr-defined]
            except Exception:
                try:
                    job.cancel()  # type: ignore[attr-defined]
                except Exception:
                    pass
        self.timer_job.clear()

    def add_turn(self, turn: TurnRecord) -> None:
        """Append a new turn to the history and update the sequence."""

        self.words_used.append(turn)
        if turn.direction == "left":
            self.sequence = f"{turn.letter}{self.sequence}"
        else:
            self.sequence = f"{self.sequence}{turn.letter}"
        self.direction = turn.direction
