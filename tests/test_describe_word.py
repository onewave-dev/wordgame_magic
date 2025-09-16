import asyncio
import json
import logging
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

import llm_utils


def test_describe_word_llm_noun(caplog):
    result = json.dumps({"exists": True, "is_noun": True, "definition": "материал"})
    with patch.object(llm_utils._chain, "apredict", AsyncMock(return_value=result)) as mock_llm:
        llm_utils._cache.clear()
        with caplog.at_level(logging.INFO):
            message = asyncio.run(llm_utils.describe_word("толь"))
    mock_llm.assert_called_once()
    assert message == "Определение: материал"
    assert any("category: noun" in r.message for r in caplog.records)


def test_describe_word_llm_failure():
    with patch.object(llm_utils._chain, "apredict", AsyncMock(side_effect=RuntimeError("boom"))) as mock_llm:
        llm_utils._cache.clear()
        message = asyncio.run(llm_utils.describe_word("инь"))
    mock_llm.assert_called_once()
    assert message == "Сервис определения слов временно недоступен."


def test_describe_word_cache():
    result = json.dumps({"exists": True, "is_noun": True, "definition": "женское начало"})
    with patch.object(llm_utils._chain, "apredict", AsyncMock(return_value=result)) as mock_llm:
        llm_utils._cache.clear()
        message1 = asyncio.run(llm_utils.describe_word("инь"))
        message2 = asyncio.run(llm_utils.describe_word("инь"))
    assert message1 == message2 == "Определение: женское начало"
    mock_llm.assert_called_once()


def test_describe_word_wiktionary_definition():
    result = json.dumps({"exists": False, "is_noun": False, "definition": ""})
    with patch.object(llm_utils._chain, "apredict", AsyncMock(return_value=result)) as mock_llm:
        with patch("llm_utils.lookup_wiktionary_meaning", return_value="краткое толкование") as mock_lookup:
            llm_utils._cache.clear()
            message = asyncio.run(llm_utils.describe_word("фуп"))
    mock_llm.assert_called_once()
    mock_lookup.assert_called_once_with("фуп")
    assert message == "Определение: краткое толкование"


def test_describe_word_wiktionary_missing():
    result = json.dumps({"exists": False, "is_noun": False, "definition": ""})
    with patch.object(llm_utils._chain, "apredict", AsyncMock(return_value=result)) as mock_llm:
        with patch("llm_utils.lookup_wiktionary_meaning", return_value=None) as mock_lookup:
            llm_utils._cache.clear()
            message = asyncio.run(llm_utils.describe_word("фуп"))
    mock_llm.assert_called_once()
    mock_lookup.assert_called_once_with("фуп")
    assert message == "Такого слова не существует."
