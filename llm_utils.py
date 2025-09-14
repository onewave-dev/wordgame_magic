"""Utilities for querying LLM about Russian words using LangChain."""

from langchain.chat_models import ChatOpenAI
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate


_prompt = PromptTemplate(
    input_variables=["word"],
    template=(
        "Ты лингвист. Проанализируй русское слово '{word}'. "
        "Если такого слова в русском языке не существует или оно крайне редкое, "
        "ответь: 'Такого слова не существует или оно крайне редкое.' "
        "Если слово существует, но не является существительным, "
        "ответь: 'Это слово не является существительным. Определение: <определение>'. "
        "Если слово существует и является существительным, "
        "ответь: 'Определение: <определение>'. "
        "Используй одно наиболее распространённое определение."
    ),
)

_llm = ChatOpenAI(temperature=0)
_chain = LLMChain(llm=_llm, prompt=_prompt)


async def describe_word(word: str) -> str:
    """Return information about a word using the configured LLM chain."""
    return await _chain.apredict(word=word)

