"""Utilities for querying LLM about Russian words using LangChain."""

import json
import logging
from typing import Optional, Tuple

from urllib import parse, request
from urllib.error import HTTPError, URLError

from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


def lookup_wiktionary(word: str) -> Optional[Tuple[bool, bool, str]]:
    """Return word info from ru.wiktionary.org using the MediaWiki API.

    The function requests the API with ``action=query&prop=extracts`` and
    attempts to obtain the first line of the ``extract`` field.  The network
    call is performed with ``urllib`` so it can be easily mocked in tests.
    """

    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": word,
        "format": "json",
    }
    query = parse.urlencode(params)
    url = f"https://ru.wiktionary.org/w/api.php?{query}"
    req = request.Request(url, headers={"User-Agent": "wordgame-bot/1.0"})
    try:
        with request.urlopen(req) as resp:  # pragma: no cover - network
            data = json.loads(resp.read())
    except HTTPError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary HTTP error: %s", e)
        return None
    except URLError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary URL error: %s", e.reason)
        return None
    except json.JSONDecodeError as e:  # pragma: no cover - malformed JSON
        logger.exception("Wiktionary JSON error: %s", e)
        return None

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        logger.info("No pages field in response for word '%s'", word)
        return None
    page = next(iter(pages.values()))
    if "missing" in page:
        return (False, False, "")

    extract = page.get("extract")
    if not extract:
        logger.info("No extract found for word '%s'", word)
        return None

    definition = extract.splitlines()[0].strip()
    return (True, True, definition)


_prompt = PromptTemplate(
    input_variables=["word"],
    template=(
        "Ты лингвист. Проанализируй русское слово '{word}'. "
        "Слово может быть редким или заимствованным. "
        "Считай его существующим, если оно встречается в словарях или в составе известных выражений. "
        "Ответь строго в формате JSON с полями 'exists', 'is_noun', 'definition'. "
        "Примеры ответов: {{\"exists\": false, \"is_noun\": false, \"definition\": \"\"}} "
        "или {{\"exists\": true, \"is_noun\": true, \"definition\": \"краткое определение\"}}. "
        "Например, слово 'инь' -> {{\"exists\": true, \"is_noun\": true, \"definition\": \"женское начало в китайской философии\"}}. "
        "Если слово не существует, установи exists=false и оставь definition пустым. "
        "Если слово существует, но не является существительным, установи exists=true, is_noun=false и дай краткое определение. "
        "Если слово существует и является существительным, установи exists=true, is_noun=true и дай краткое определение."
    ),
)

try:  # pragma: no cover - environment dependent
    _llm = ChatOpenAI(temperature=0)
    _chain = LLMChain(llm=_llm, prompt=_prompt)
except Exception:  # pragma: no cover - initialization failures
    logger.warning("ChatOpenAI initialization failed", exc_info=True)

    class _DummyChain:
        async def apredict(self, *args, **kwargs):  # pragma: no cover - stub
            raise RuntimeError("LLM not available")

    _chain = _DummyChain()


async def describe_word(word: str) -> str:
    """Return information about a word using Wiktionary or the configured LLM."""
    logger.info("Querying word: %s", word)

    wiki_data = lookup_wiktionary(word)
    if wiki_data is not None:
        exists, is_noun, definition = wiki_data
        if not exists:
            category = "nonexistent"
            message = "Такого слова не существует."
        elif not is_noun:
            category = "not_noun"
            message = "Это слово не является существительным."
            if definition:
                message += f" Определение: {definition}"
        else:
            category = "noun"
            message = f"Определение: {definition}"
        logger.info("Wiktionary response: %s | category: %s", wiki_data, category)
        return message

    try:
        result = await _chain.apredict(word=word)
    except Exception:  # pragma: no cover - network errors
        logger.exception("LLM request failed")
        return "Ответ модели не распознан"
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
        return "Ответ модели не распознан"

    if not exists:
        category = "nonexistent"
        message = "Такого слова не существует."
    elif not is_noun:
        category = "not_noun"
        message = f"Это слово не является существительным. Определение: {definition}"
    else:
        category = "noun"
        message = f"Определение: {definition}"

    logger.info("LLM parsed response: %s | category: %s", data, category)
    return message

