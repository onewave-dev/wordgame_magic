"""Rendering helpers for visualising the Balda board."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..state import GameState


@dataclass(slots=True)
class BaldaRenderTheme:
    """Container describing the visual configuration of the board."""

    background: str = "#fdf6e3"
    primary_text: str = "#111"
    accent_text: str = "#d62828"
    font_name: str = "PT Serif"


class BaldaRenderer:
    """Render textual previews of the Balda board.

    The real Telegram bot will eventually use Pillow to render branded images.
    For now we provide a light‑weight textual fallback that is easy to test and
    keeps the module dependency‑free.
    """

    def __init__(self, theme: BaldaRenderTheme | None = None) -> None:
        self.theme = theme or BaldaRenderTheme()

    def render_sequence(self, state: GameState) -> str:
        """Return a string representation of the current letter sequence."""

        sequence = state.sequence or state.base_letter or "—"
        return f"<b>{sequence.upper()}</b>"

    def render_recent_words(self, state: GameState, limit: int = 5) -> str:
        """Return a formatted history of recent turns."""

        turns = state.words_used[-limit:]
        lines: Iterable[str] = (
            f"• {turn.word.upper()} ({'◀' if turn.direction == 'left' else '▶'} {turn.letter.upper()})"
            for turn in turns
        )
        return "\n".join(lines) if turns else "История пуста."
