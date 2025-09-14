"""Utilities for querying LLM about Russian words using LangChain."""

import logging

from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


_prompt = PromptTemplate(
    input_variables=["word"],
    template=(
        "Ты лингвист. Проанализируй русское слово '{word}'. "
        "Если такого слова в русском языке не существует, "
        "ответь: 'Такого слова не существует.' "
        "Если слово существует, но не является существительным, "
        "ответь: 'Это слово не является существительным. Определение: <определение>'. "
        "Если слово существует и является существительным, "
        "ответь: 'Определение: <определение>'. "
        "Даже если слово очень редкое, но существует, обработай его по соответствующей ветке. "
        "Используй одно наиболее распространённое определение."
    ),
)

_llm = ChatOpenAI(temperature=0)
_chain = LLMChain(llm=_llm, prompt=_prompt)


async def describe_word(word: str) -> str:
    """Return information about a word using the configured LLM chain."""
    logger.info("Querying word: %s", word)
    result = await _chain.apredict(word=word)
    lower = result.lower()
    if "такого слова не существует" in lower:
        category = "nonexistent"
    elif "не является существительным" in lower:
        category = "not_noun"
    elif "определение:" in lower:
        category = "noun"
    else:
        category = "unknown"
    logger.info("LLM raw response: %s | category: %s", result, category)
    return result

