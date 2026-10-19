"""Deterministic checks for extractive summaries before they replace active context."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_NUMBER = re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?%?(?!\w)")
_DATE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+)?"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"(?:\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_ENTITY = re.compile(r"\b[A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*){0,3}\b")
_NEGATION = re.compile(
    r"\b(?:no|not|never|neither|cannot|can't|won't|without|mustn't|don't|doesn't|didn't)\b",
    re.IGNORECASE,
)
_STOP_ENTITIES = {"I", "We", "The", "A", "An", "It", "This", "That", "They"}


@dataclass(frozen=True, slots=True)
class FaithfulnessReport:
    """Structured result of checking whether a summary is source-supported."""

    passed: bool
    missing_numbers: tuple[str, ...]
    missing_dates: tuple[str, ...]
    missing_entities: tuple[str, ...]
    missing_negations: tuple[str, ...]
    introduced_numbers: tuple[str, ...]
    introduced_entities: tuple[str, ...]
    reason: str


def verify_summary(source: str, summary: str) -> FaithfulnessReport:
    """Reject summaries that drop critical markers or introduce new ones."""
    if not source.strip() or not summary.strip():
        return FaithfulnessReport(
            False, (), (), (), (), (), (), "Source and summary must both be non-empty."
        )

    source_numbers = Counter(_NUMBER.findall(source))
    summary_numbers = Counter(_NUMBER.findall(summary))
    source_dates = Counter(match.casefold() for match in _DATE.findall(source))
    summary_dates = Counter(match.casefold() for match in _DATE.findall(summary))
    source_entities = Counter(
        entity for entity in _ENTITY.findall(source) if entity not in _STOP_ENTITIES
    )
    summary_entities = Counter(
        entity for entity in _ENTITY.findall(summary) if entity not in _STOP_ENTITIES
    )
    source_negations = Counter(word.casefold() for word in _NEGATION.findall(source))
    summary_negations = Counter(word.casefold() for word in _NEGATION.findall(summary))

    missing_numbers = _missing(source_numbers, summary_numbers)
    missing_dates = _missing(source_dates, summary_dates)
    missing_entities = _missing(source_entities, summary_entities)
    missing_negations = _missing(source_negations, summary_negations)
    introduced_numbers = _missing(summary_numbers, source_numbers)
    introduced_entities = _missing(summary_entities, source_entities)

    passed = not any(
        (
            missing_numbers,
            missing_dates,
            missing_entities,
            missing_negations,
            introduced_numbers,
            introduced_entities,
        )
    )
    reason = (
        "All detected entities, numbers, dates, and negations are preserved."
        if passed
        else "Summary failed source-faithfulness checks; the original must remain verbatim."
    )
    return FaithfulnessReport(
        passed=passed,
        missing_numbers=tuple(missing_numbers),
        missing_dates=tuple(missing_dates),
        missing_entities=tuple(missing_entities),
        missing_negations=tuple(missing_negations),
        introduced_numbers=tuple(introduced_numbers),
        introduced_entities=tuple(introduced_entities),
        reason=reason,
    )


def _missing(source: Counter[str], candidate: Counter[str]) -> list[str]:
    return sorted((source - candidate).elements())
