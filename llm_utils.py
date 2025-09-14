"""Utilities for querying LLM about Russian words using LangChain."""

import json
import logging
from typing import Optional, Tuple

from bs4 import BeautifulSoup
from urllib import parse, request
from urllib.error import HTTPError, URLError

from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


def lookup_wiktionary(word: str) -> Optional[Tuple[bool, bool, str]]:
    """Return word info from ru.wiktionary.org.

    The function fetches a Wiktionary page and tries to extract the first
    definition for the word. Several strategies are used to locate the
    definition list and the branch taken is logged.
    """

    url = f"https://ru.wiktionary.org/wiki/{parse.quote(word)}"
    req = request.Request(url, headers={"User-Agent": "wordgame-bot/1.0"})
    try:
        with request.urlopen(req) as resp:  # pragma: no cover - network
            html = resp.read()
    except HTTPError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary HTTP error: %s", e)
        return None
    except URLError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary URL error: %s", e.reason)
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Check if the article exists.
    if soup.find(class_="noarticletext"):
        return (False, False, "")

    content = soup.find(id="mw-content-text") or soup
    branch = "span"
    ol = None

    noun_span = content.select_one('span.mw-headline[id^="Существительное"]')
    if noun_span:
        ol = noun_span.find_next("ol")
    else:
        branch = "no_span"
        ol = content.find("ol")
        if not ol:
            branch = "header_scan"
            for header in content.select("h3, h4"):
                ol = header.find_next("ol")
                if ol:
                    break

    if not ol:
        logger.info("Definition list not found for word '%s'", word)
        return None

    first_li = ol.find("li")
    if not first_li:
        logger.info("No definitions found for word '%s'", word)
        return None

    logger.info("lookup_wiktionary branch: %s", branch)
    definition = first_li.get_text(" ", strip=True)
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

_llm = ChatOpenAI(temperature=0)
_chain = LLMChain(llm=_llm, prompt=_prompt)


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

