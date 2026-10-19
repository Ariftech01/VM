"""Deterministic, fully offline extractive summarizer and rule-based answerer."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

from gitmem.llm.base import LLMResponse
from gitmem.tokenizer import TokenCounter

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a", "an", "are", "as", "at", "be", "by", "can", "did", "do", "does",
    "for", "from", "how", "in", "is", "it", "of", "on", "or", "should",
    "that", "the", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with",
}


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in _WORD.findall(text.casefold()):
        word = raw
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("ed") and len(word) > 5:
            word = word[:-2] + "e" if word[:-2].endswith("at") else word[:-2]
        elif word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
            word = word[:-1]
        if word not in _STOPWORDS:
            terms.add(word)
    return terms


class MockLLM:
    """Offline adapter: no network calls, randomness, or model credentials."""

    name = "mock"

    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self.token_counter = token_counter or TokenCounter()

    def summarize(self, text: str, max_sentences: int = 3) -> str:
        """Select salient source sentences without paraphrasing or inventing facts."""
        sentences = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
        if len(sentences) <= max_sentences:
            return " ".join(sentences)

        def score(sentence: str) -> tuple[int, int]:
            critical = len(re.findall(r"\d|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|must|never|not", sentence, re.I))
            return critical, -sentences.index(sentence)

        selected = sorted(sorted(sentences, key=score, reverse=True)[:max_sentences], key=sentences.index)
        return " ".join(selected)

    def answer(self, question: str, context: Sequence[str]) -> LLMResponse:
        """Return the best-supported source sentence or an explicit abstention."""
        started = time.perf_counter()
        query_terms = _terms(question)
        ranked: list[tuple[float, int, str]] = []
        for index, item in enumerate(context):
            sentences = [part.strip() for part in _SENTENCE_SPLIT.split(item) if part.strip()]
            for sentence in sentences or [item]:
                terms = _terms(sentence)
                overlap = len(query_terms & terms) / max(1, len(query_terms))
                has_answer_signal = bool(
                    re.search(r"\b(is|are|was|were|will|must|deadline|budget|requires|needs?)\b", sentence, re.I)
                )
                correction_signal = bool(
                    re.search(r"\b(correct(?:ion|ed)?|moved to|changed to|updated to|now)\b", sentence, re.I)
                )
                score = overlap + (0.06 if has_answer_signal else 0.0)
                if correction_signal and overlap >= 0.35:
                    score += 0.15
                if overlap >= 0.25:
                    ranked.append((score, -index, sentence))
        if ranked:
            best = max(ranked)[2]
            # Abstain rather than claim weak lexical overlap as a grounded answer.
            answer = best if max(ranked)[0] >= 0.30 else "I could not find that in the available memory."
        else:
            answer = "I could not find that in the available memory."
        elapsed = (time.perf_counter() - started) * 1000.0
        input_tokens = sum(self.token_counter.count(text) for text in context) + self.token_counter.count(question)
        return LLMResponse(
            text=answer,
            model=self.name,
            input_tokens=input_tokens,
            output_tokens=self.token_counter.count(answer),
            latency_ms=elapsed,
        )
