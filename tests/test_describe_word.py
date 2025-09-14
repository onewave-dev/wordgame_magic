import asyncio
import json
import logging
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

import llm_utils


def test_describe_word_wiktionary_noun(caplog):
    definition = "женское начало"
    with patch("llm_utils.lookup_wiktionary", return_value=(True, True, definition)), \
         patch.object(llm_utils._chain, "apredict", new_callable=AsyncMock) as mock_llm:
        with caplog.at_level(logging.INFO):
            message = asyncio.run(llm_utils.describe_word("инь"))
    mock_llm.assert_not_called()
    assert message == f"Определение: {definition}"
    assert any("category: noun" in r.message for r in caplog.records)


def test_describe_word_llm_noun(caplog):
    result = json.dumps({"exists": True, "is_noun": True, "definition": "материал"})
    with patch("llm_utils.lookup_wiktionary", return_value=None), \
         patch.object(llm_utils._chain, "apredict", AsyncMock(return_value=result)) as mock_llm:
        with caplog.at_level(logging.INFO):
            message = asyncio.run(llm_utils.describe_word("толь"))
    mock_llm.assert_called_once()
    assert message == "Определение: материал"
    assert any("category: noun" in r.message for r in caplog.records)
