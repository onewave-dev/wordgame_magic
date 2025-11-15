"""Utility helpers that summarize Balda matches."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

from ..state import GameState


@dataclass(slots=True)
class GameStats:
    """High-level snapshot of a Balda game used for announcements."""

    total_turns: int
    unique_words: int
    duration_seconds: int
    duration_text: str
    final_sequence: str
    elimination_names: List[str]


def _format_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}ч")
    if minutes or hours:
        parts.append(f"{minutes:02d}м" if hours else f"{minutes}м")
    parts.append(f"{seconds:02d}с" if minutes or hours else f"{seconds}с")
    return "".join(parts)


def _resolve_sequence(state: GameState) -> str:
    if state.sequence:
        return state.sequence.upper()
    if state.base_letter:
        return state.base_letter.upper()
    return "—"


def _collect_elimination_names(state: GameState) -> List[str]:
    ordered: List[str] = []
    for player_id in state.players_out:
        player = state.players.get(player_id)
        if player and player.name:
            ordered.append(player.name)
    return ordered


def collect_game_stats(state: GameState, *, now: datetime | None = None) -> GameStats:
    """Aggregate metrics used for scoreboards and the final summary."""

    moment = now or datetime.utcnow()
    duration_seconds = int(max((moment - state.created_at).total_seconds(), 0))
    total_turns = len(state.words_used)
    unique_words = len({turn.word for turn in state.words_used})
    final_sequence = _resolve_sequence(state)
    elimination_names = _collect_elimination_names(state)
    return GameStats(
        total_turns=total_turns,
        unique_words=unique_words,
        duration_seconds=duration_seconds,
        duration_text=_format_duration(duration_seconds),
        final_sequence=final_sequence,
        elimination_names=elimination_names,
    )


def _format_elimination_summary(names: Sequence[str], winner_name: str | None) -> str:
    parts = [html.escape(name) for name in names if name]
    if winner_name:
        parts.append(f"Winner {html.escape(winner_name)}")
    if not parts:
        return "—"
    return " → ".join(parts)


def format_stats_message(stats: GameStats, *, winner_name: str | None = None) -> str:
    """Render the AGENT-style scoreboard shown at the end of the match."""

    lines = [
        "📈 <b>Game Stats</b>",
        f"🧩 Total turns: {stats.total_turns}",
        f"🕐 Duration: {stats.duration_text}",
        f"🔠 Unique words: {stats.unique_words}",
        f"💬 Final sequence: <b>{html.escape(stats.final_sequence)}</b>",
        f"👥 Eliminations: {_format_elimination_summary(stats.elimination_names, winner_name)}",
    ]
    return "\n".join(lines)


__all__ = ["GameStats", "collect_game_stats", "format_stats_message"]
