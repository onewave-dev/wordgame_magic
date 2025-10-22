"""Test configuration and compatibility shims."""

import sys
import types


def _ensure_langchain_shims() -> None:
    """Provide minimal shims for langchain modules required in tests."""

    if "langchain.chains" not in sys.modules:
        chains_module = types.ModuleType("langchain.chains")

        class DummyLLMChain:  # pragma: no cover - simple compatibility shim
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def apredict(self, *args, **kwargs):
                raise RuntimeError("LLM not available")

        chains_module.LLMChain = DummyLLMChain  # type: ignore[attr-defined]
        sys.modules["langchain.chains"] = chains_module

    if "langchain.prompts" not in sys.modules:
        prompts_module = types.ModuleType("langchain.prompts")

        class DummyPromptTemplate:  # pragma: no cover - simple compatibility shim
            def __init__(self, *args, **kwargs) -> None:
                pass

        prompts_module.PromptTemplate = DummyPromptTemplate  # type: ignore[attr-defined]
        sys.modules["langchain.prompts"] = prompts_module


_ensure_langchain_shims()

