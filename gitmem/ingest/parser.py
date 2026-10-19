"""Deterministic conversation and tool-output unit extraction."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from gitmem.models import MemoryUnit, UnitType
from gitmem.tokenizer import TokenCounter

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_TOPIC_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CODE = re.compile(r"```|^\s{4,}(?:def |class |import |const |function )", re.MULTILINE)
_CONSTRAINT = re.compile(
    r"\b(must|must not|required|requirement|constraint|never|cannot|can't|only|"
    r"do not|don't|should not|deadline|budget|limit)\b",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(r"\b(prefer|preference|I like|I dislike|always use|avoid)\b", re.I)
_DECISION = re.compile(r"\b(decided|decision|we will|agreed|approved|chosen|selected)\b", re.I)
_FACT = re.compile(r"\b(is|are|was|were|has|have|deadline|budget|located|called|equals)\b", re.I)
_WORDS = re.compile(r"[a-z0-9]+")


def infer_unit_type(text: str, role: str = "user") -> UnitType:
    """Classify a text fragment using transparent, deterministic rules."""
    if _CODE.search(text):
        return UnitType.CODE
    if _PREFERENCE.search(text):
        return UnitType.PREFERENCE
    if _CONSTRAINT.search(text):
        return UnitType.CONSTRAINT
    if _DECISION.search(text):
        return UnitType.DECISION
    if _FACT.search(text) or re.search(r"\d", text):
        return UnitType.FACT
    return UnitType.TURN


def infer_topic(text: str, fallback: str = "general") -> str:
    """Choose a stable lightweight topic label without an external model."""
    words = [word.casefold() for word in _TOPIC_WORD.findall(text)]
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "what", "when",
        "where", "which", "have", "has", "will", "would", "could", "should",
        "must", "not", "into", "about", "then", "than", "there",
    }
    candidates = [word for word in words if word not in stop]
    if not candidates:
        return fallback
    counts: dict[str, int] = {}
    for word in candidates:
        counts[word] = counts.get(word, 0) + 1
    return max(counts, key=lambda word: (counts[word], -candidates.index(word)))


def ingest_messages(
    messages: Sequence[Mapping[str, Any]],
    session_id: str,
    current_goal: str = "",
    token_counter: TokenCounter | None = None,
) -> list[MemoryUnit]:
    """Split role/content messages into typed, timestamped memory units."""
    if not session_id.strip():
        raise ValueError("session_id cannot be empty")
    counter = token_counter or TokenCounter()
    units: list[MemoryUnit] = []
    now = datetime.now(UTC).isoformat()
    for message in messages:
        text_value = message.get("content", message.get("text", ""))
        if not isinstance(text_value, str) or not text_value.strip():
            continue
        role = str(message.get("role", "user"))
        message_timestamp = str(message.get("timestamp", now))
        supplied_topic = str(message.get("topic", "")).strip()
        fragments = [fragment.strip() for fragment in _SPLIT.split(text_value) if fragment.strip()]
        if not fragments:
            continue
        for fragment in fragments:
            unit_type = infer_unit_type(fragment, role)
            topic = supplied_topic or infer_topic(fragment, infer_topic(current_goal, "general"))
            units.append(
                MemoryUnit(
                    id=str(uuid.uuid4()),
                    unit_type=unit_type,
                    text=fragment,
                    topic=topic,
                    timestamp=message_timestamp,
                    session_id=session_id,
                    token_count=counter.count(fragment),
                    pinned=bool(message.get("pinned", False)),
                    metadata={
                        "role": role,
                        "source": str(message.get("source", "conversation")),
                    },
                )
            )
    return units


def lexical_similarity(left: str, right: str) -> float:
    """Jaccard similarity over normalized words for offline conflict and retrieval."""
    a = set(_WORDS.findall(left.casefold()))
    b = set(_WORDS.findall(right.casefold()))
    return len(a & b) / max(1, len(a | b))
