"""Data structures describing the Balda game state."""

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
    """Record of an action performed by a player during the game."""

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
    turn_direction: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    players: Dict[int, PlayerState] = field(default_factory=dict)
    player_order: List[int] = field(default_factory=list)
    eliminated_players: List[int] = field(default_factory=list)
    turns: List[TurnRecord] = field(default_factory=list)
    timer_job: Optional[object] = None
    has_started: bool = False

    def reset_timer(self) -> None:
        """Forget the currently scheduled timer job."""

        self.timer_job = None

    def add_turn(self, turn: TurnRecord) -> None:
        """Append a new turn to the history and advance bookkeeping."""

        self.turns.append(turn)
        self.sequence = (
            f"{turn.letter}{self.sequence}" if turn.direction == "left" else f"{self.sequence}{turn.letter}"
        )
        self.turn_direction = turn.direction
