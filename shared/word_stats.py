"""Utilities for working with Zipf frequencies from the shared dictionary."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional


def _normalize(word: str) -> str:
    return word.lower().replace("ё", "е")


def _parse_zipf(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            matches = re.findall(r"[-+]?\d+[\.,]?\d*", value)
            for match in matches:
                try:
                    return float(match.replace(",", "."))
                except ValueError:
                    continue
    return None


def _load_zipf_map() -> Dict[str, Optional[float]]:
    base_dir = Path(__file__).resolve().parent.parent
    path = base_dir / "nouns_ru_pymorphy2_yaspeller.jsonl"
    mapping: Dict[str, Optional[float]] = {}
    if not path.exists():
        return mapping
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            word = data.get("word")
            if not word:
                continue
            norm = _normalize(str(word))
            if norm in mapping:
                continue
            zipf = _parse_zipf(data.get("zipf_form"))
            if zipf is None:
                zipf = _parse_zipf(data.get("freq_check"))
            mapping[norm] = zipf
    return mapping


_ZIPF_BY_WORD = _load_zipf_map()


@lru_cache(maxsize=None)
def get_zipf(word: str) -> Optional[float]:
    """Return the Zipf frequency for ``word`` if known."""

    return _ZIPF_BY_WORD.get(_normalize(word))


__all__ = ["get_zipf"]
