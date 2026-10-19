"""Model-independent language-model interface and typed responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Answer plus provider metadata needed for attribution and cost reporting."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class LLMAdapter(Protocol):
    """Provider-independent operations needed by the memory engine."""

    @property
    def name(self) -> str: ...

    def summarize(self, text: str, max_sentences: int = 3) -> str: ...

    def answer(self, question: str, context: Sequence[str]) -> LLMResponse: ...
