"""Rendering helpers for visualising the Balda board."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

from ..state import GameState


@dataclass(slots=True)
class BaldaRenderTheme:
    """Container describing the visual configuration of the board."""

    background: str = "#f9f2e4"
    primary_text: str = "#3f2f1d"
    accent_text: str = "#d62828"
    font_name: str = "PT Serif"


class BaldaRenderer:
    """Render both textual fallbacks and Pillow images for Balda."""

    BOARD_SIZE = (1024, 576)
    REGULAR_FONTS = (
        "/usr/share/fonts/truetype/ptserif/PTSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    BOLD_FONTS = (
        "/usr/share/fonts/truetype/ptserif/PTSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )

    def __init__(self, theme: BaldaRenderTheme | None = None) -> None:
        self.theme = theme or BaldaRenderTheme()
        self._font_cache: dict[tuple[int, bool], ImageFont.ImageFont] = {}

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

    def render_board_image(
        self, state: GameState, *, helper_word: str | None = None
    ) -> io.BytesIO:
        """Render the Balda board as a PNG stored in an in-memory buffer."""

        sequence = (state.sequence or state.base_letter or "—").strip() or "—"
        sequence = sequence.upper()
        highlight_index = self._resolve_highlight_index(state, sequence)
        helper_text = helper_word.upper() if helper_word else None
        lines = self._split_sequence(sequence)
        image = Image.new("RGB", self.BOARD_SIZE, color=self.theme.background)
        draw = ImageDraw.Draw(image)
        self._draw_background(draw)
        self._draw_grid(draw)
        self._draw_sequence(draw, lines, highlight_index)
        if helper_text:
            self._draw_helper_word(draw, helper_text)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    def _split_sequence(self, sequence: str) -> list[str]:
        if len(sequence) >= 10:
            midpoint = math.ceil(len(sequence) / 2)
            return [sequence[:midpoint], sequence[midpoint:]]
        return [sequence]

    def _resolve_highlight_index(
        self, state: GameState, sequence: str
    ) -> int | None:
        if not sequence:
            return None
        if state.words_used:
            last_turn = state.words_used[-1]
            return 0 if last_turn.direction == "left" else len(sequence) - 1
        return len(sequence) - 1

    def _draw_background(self, draw: ImageDraw.ImageDraw) -> None:
        width, height = self.BOARD_SIZE
        margin = 24
        panel_radius = 48
        inner_rect = (margin, margin, width - margin, height - margin)
        draw.rounded_rectangle(inner_rect, radius=panel_radius, fill="#fffdf7")
        draw.rounded_rectangle(
            inner_rect,
            radius=panel_radius,
            outline="#d8c7a0",
            width=6,
        )
        header_height = 110
        draw.rectangle((margin, margin, width - margin, margin + header_height), fill="#563321")
        title = "БАЛДА"
        title_font = self._get_font(56, bold=True)
        title_width = draw.textlength(title, font=title_font)
        title_height = self._font_height(title_font)
        title_x = (width - title_width) / 2
        title_y = margin + (header_height - title_height) / 2
        draw.text((title_x, title_y), title, fill="#fff7e8", font=title_font)

    def _draw_grid(self, draw: ImageDraw.ImageDraw) -> None:
        width, height = self.BOARD_SIZE
        grid_left = 110
        grid_right = width - 110
        grid_top = 160
        grid_bottom = height - 190
        columns = 8
        rows = 3
        col_width = (grid_right - grid_left) / columns
        row_height = (grid_bottom - grid_top) / rows
        grid_color = "#e4d4b5"
        for idx in range(columns + 1):
            x = grid_left + idx * col_width
            draw.line((x, grid_top, x, grid_bottom), fill=grid_color, width=2)
        for idx in range(rows + 1):
            y = grid_top + idx * row_height
            draw.line((grid_left, y, grid_right, y), fill=grid_color, width=2)

    def _draw_sequence(
        self, draw: ImageDraw.ImageDraw, lines: Sequence[str], highlight_index: int | None
    ) -> None:
        width, height = self.BOARD_SIZE
        line_gap = 40
        top_margin = 150
        bottom_margin = 170
        available_height = height - top_margin - bottom_margin
        fonts: list[tuple[int, ImageFont.ImageFont, int]] = []
        for line in lines:
            font_size = self._font_size(len(line), len(lines))
            font = self._get_font(font_size)
            fonts.append((font_size, font, self._font_height(font)))
        total_text_height = sum(height for _, _, height in fonts) + line_gap * (len(lines) - 1)
        start_y = top_margin + max((available_height - total_text_height) / 2, 0)
        global_index = 0
        for line_index, line in enumerate(lines):
            font_size, base_font, font_height = fonts[line_index]
            bold_font = self._get_font(font_size + 12, bold=True)
            char_widths: list[float] = []
            for offset, char in enumerate(line):
                absolute_index = global_index + offset
                font = bold_font if absolute_index == highlight_index else base_font
                char_widths.append(draw.textlength(char, font=font))
            spacing = 14
            line_width = sum(char_widths) + spacing * max(len(line) - 1, 0)
            x = (width - line_width) / 2
            y = start_y + max((font_size - font_height) / 2, 0)
            for offset, char in enumerate(line):
                absolute_index = global_index + offset
                font = bold_font if absolute_index == highlight_index else base_font
                fill = "#000000" if absolute_index == highlight_index else self.theme.primary_text
                draw.text((x, y), char, font=font, fill=fill)
                x += char_widths[offset] + spacing
            start_y += font_height + line_gap
            global_index += len(line)

    def _draw_helper_word(self, draw: ImageDraw.ImageDraw, helper_word: str) -> None:
        width, height = self.BOARD_SIZE
        label = "Слово хода"
        label_font = self._get_font(34)
        helper_font = self._get_font(64, bold=True)
        label_width = draw.textlength(label, font=label_font)
        helper_width = draw.textlength(helper_word, font=helper_font)
        label_y = height - 150
        helper_y = label_y + 42
        draw.text(
            ((width - label_width) / 2, label_y),
            label,
            font=label_font,
            fill=self.theme.primary_text,
        )
        draw.text(
            ((width - helper_width) / 2, helper_y),
            helper_word,
            font=helper_font,
            fill=self.theme.accent_text,
        )

    def _font_size(self, line_length: int, total_lines: int) -> int:
        if total_lines == 1:
            if line_length <= 3:
                return 260
            if line_length <= 6:
                return 210
            if line_length <= 9:
                return 170
            return 140
        if line_length <= 5:
            return 180
        if line_length <= 8:
            return 150
        return 120

    def _get_font(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        key = (size, bold)
        cached = self._font_cache.get(key)
        if cached:
            return cached
        candidates = self.BOLD_FONTS if bold else self.REGULAR_FONTS
        for path in candidates:
            try:
                font = ImageFont.truetype(path, size=size)
                self._font_cache[key] = font
                return font
            except OSError:
                continue
        fallback = ImageFont.load_default()
        self._font_cache[key] = fallback
        return fallback

    def _font_height(self, font: ImageFont.ImageFont) -> int:
        try:
            bbox = font.getbbox("Б")
            return int(bbox[3] - bbox[1])
        except AttributeError:
            return font.getsize("Б")[1]
