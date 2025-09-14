"""Utilities for querying LLM about Russian words using LangChain."""

import json
import logging

from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


_prompt = PromptTemplate(
    input_variables=["word"],
    template=(
        "Ты лингвист. Проанализируй русское слово '{word}'. "
        "Ответь строго в формате JSON с полями 'exists', 'is_noun', 'definition'. "
        "Примеры ответов: {{\"exists\": false, \"is_noun\": false, \"definition\": \"\"}} "
        "или {{\"exists\": true, \"is_noun\": true, \"definition\": \"краткое определение\"}}. "
        "Если слово не существует, установи exists=false и оставь definition пустым. "
        "Если слово существует, но не является существительным, установи exists=true, is_noun=false и дай краткое определение. "
        "Если слово существует и является существительным, установи exists=true, is_noun=true и дай краткое определение."
    ),
)

_llm = ChatOpenAI(temperature=0)
_chain = LLMChain(llm=_llm, prompt=_prompt)


async def describe_word(word: str) -> str:
    """Return information about a word using the configured LLM chain."""
    logger.info("Querying word: %s", word)
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

