"""Model-independent language-model adapters."""

from gitmem.llm.base import LLMAdapter, LLMResponse
from gitmem.llm.mock import MockLLM
from gitmem.llm.remote import GeminiAdapter, OllamaAdapter, OpenAICompatibleAdapter

__all__ = [
    "GeminiAdapter",
    "LLMAdapter",
    "LLMResponse",
    "MockLLM",
    "OllamaAdapter",
    "OpenAICompatibleAdapter",
]
