"""Controlled six-session synthetic conversations with ground-truth probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GroundTruthQuestion:
    """A deterministic question, exact expected answer, and critical evidence."""

    question: str
    expected_answer: str
    critical_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyntheticDataset:
    """Synthetic conversational sessions and their ground-truth questions."""

    name: str
    sessions: tuple[tuple[dict[str, Any], ...], ...]
    questions: tuple[GroundTruthQuestion, ...]


def generate_synthetic_dataset(seed: int = 42) -> SyntheticDataset:
    """Generate a stable six-session project plan with a changed deadline."""
    del seed  # Kept in the API so future controlled variants remain reproducible.
    sessions = (
        (
            {"role": "user", "topic": "project", "content": "Project Atlas is a logistics planning pilot."},
            {"role": "user", "topic": "deadline", "content": "The delivery deadline was initially set to Thursday."},
        ),
        (
            {"role": "user", "topic": "budget", "content": "The response budget is 1800 tokens."},
            {"role": "user", "topic": "preferences", "content": "The user prefers compact summaries with details preserved in the source archive."},
        ),
        (
            {"role": "user", "topic": "requirements", "content": "The dashboard must preserve user control over archived memory units."},
        ),
        (
            {"role": "user", "topic": "deadline", "content": "Correction: the delivery deadline moved to Friday."},
        ),
        (
            {"role": "assistant", "topic": "decisions", "content": "We decided to keep the local database in SQLite."},
            {"role": "user", "topic": "retrieval", "content": "Archived units may be rehydrated individually without restoring the whole memory."},
        ),
        (
            {"role": "user", "topic": "security", "content": "Do not keep passwords or API keys in AI memory."},
            {"role": "user", "topic": "evaluation", "content": "The benchmark must report token use, estimated cost, accuracy, and recovery success."},
        ),
    )
    questions = (
        GroundTruthQuestion("What is the delivery deadline?", "Friday", ("Friday",)),
        GroundTruthQuestion("What is the project called?", "Project Atlas", ("Project Atlas",)),
        GroundTruthQuestion("What is the response budget?", "1800 tokens", ("1800",)),
        GroundTruthQuestion("What summary style does the user prefer?", "compact summaries", ("compact summaries",)),
        GroundTruthQuestion("What local database was chosen?", "SQLite", ("SQLite",)),
        GroundTruthQuestion("How are archived units rehydrated?", "rehydrated individually", ("rehydrated individually",)),
        GroundTruthQuestion("Should passwords or API keys be stored?", "Do not keep passwords or API keys", ("Do not keep passwords",)),
    )
    return SyntheticDataset("gitmem-synthetic-v1", sessions, questions)
