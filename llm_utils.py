"""Utilities for querying LLM about Russian words using LangChain."""

import json
import logging
import os
from typing import Dict, Optional

from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

from shared.logging_utils import configure_logging
from wiktionary_utils import lookup_wiktionary_meaning

configure_logging(extra_values=[os.environ.get("OPENAI_API_KEY")])
logger = logging.getLogger(__name__)


_prompt = PromptTemplate(
    input_variables=["word"],
    template=(
        "Ты лингвист. Проанализируй русское слово '{word}'. "
        "Слово может быть редким или заимствованным. "
        "Считай его существующим, если оно встречается в словарях или в составе известных выражений. "
        "Если у слова несколько значений, выбери самое распространённое значение существительного. "
        "Если существует существительное значение, установи is_noun=true. "
        "Сохрани JSON-поля 'exists', 'is_noun' и 'definition'. "
        "Ответь строго в формате JSON с полями 'exists', 'is_noun', 'definition'. "
        "Примеры ответов: {{\"exists\": false, \"is_noun\": false, \"definition\": \"\"}} "
        "или {{\"exists\": true, \"is_noun\": true, \"definition\": \"краткое определение\"}}. "
        "Например, слово 'инь' -> {{\"exists\": true, \"is_noun\": true, \"definition\": \"женское начало в китайской философии\"}}. "
        "Если слово не существует, установи exists=false и оставь definition пустым. "
        "Если слово существует, но не является существительным, установи exists=true, is_noun=false и дай краткое определение. "
        "Если слово существует и является существительным, установи exists=true, is_noun=true и дай краткое определение."
    ),
)

model = os.environ.get("OPENAI_LLM_MODEL", "o4-mini")


class _DummyChain:
    async def apredict(self, *args, **kwargs):  # pragma: no cover - stub
        raise RuntimeError("LLM not available")


try:  # pragma: no cover - environment dependent
    _llm = ChatOpenAI(model=model)
    _chain = LLMChain(llm=_llm, prompt=_prompt)
except TypeError:  # pragma: no cover - unsupported parameters
    logger.error(
        "ChatOpenAI initialization failed for model %s due to unsupported parameter configuration",
        model,
        exc_info=True,
    )
    _chain = _DummyChain()
except Exception:  # pragma: no cover - initialization failures
    logger.warning("ChatOpenAI initialization failed", exc_info=True)
    _chain = _DummyChain()


_cache: Dict[str, str] = {}


async def describe_word(word: str) -> str:
    """Return information about a word using the configured LLM."""
    logger.info("Querying word: %s", word)

    cached = _cache.get(word)
    if cached is not None:
        logger.info("Cache hit for word: %s", word)
        return cached

    try:
        result = await _chain.apredict(word=word)
    except Exception:  # pragma: no cover - network errors
        logger.exception("LLM request failed")
        return "Сервис определения слов временно недоступен."
    logger.info("LLM raw response: %s", result)

    try:
        data = json.loads(result)
        exists = bool(data["exists"])
        is_noun = bool(data["is_noun"])
        definition = data.get("definition", "")
        if not isinstance(definition, str):
            raise ValueError("definition must be a string")
    except Exception:
        logger.exception("Failed to parse LLM response")
        return "Сервис определения слов временно недоступен."

    meaning: Optional[str] = None
    if not exists:
        category = "nonexistent"
        try:
            meaning = lookup_wiktionary_meaning(word)
        except Exception:  # pragma: no cover - safety net around HTML parsing
            logger.exception("Failed to fetch Wiktionary meaning for %s", word)
        if meaning:
            category = "wiktionary"
            message = f"Определение: {meaning}"
        else:
            message = "Такого слова не существует."
    elif not is_noun:
        category = "not_noun"
        message = f"Это слово не является существительным. Определение: {definition}"
    else:
        category = "noun"
        message = f"Определение: {definition}"

    logger.info("LLM parsed response: %s | category: %s", data, category)
    _cache[word] = message
    return message

