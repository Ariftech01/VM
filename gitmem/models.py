"""Typed domain models shared by GitMem's ingestion, storage, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class UnitType(StrEnum):
    """Kinds of information that can be ingested into memory."""

    TURN = "turn"
    DECISION = "decision"
    CODE = "code"
    CONSTRAINT = "constraint"
    FACT = "fact"
    PREFERENCE = "preference"


class MemoryTier(StrEnum):
    """Storage tier assigned to a memory item in a particular snapshot."""

    PINNED = "pinned"
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class MemoryAction(StrEnum):
    """Explainable action chosen by the scoring engine for an ingested unit."""

    KEEP_VERBATIM = "KEEP_VERBATIM"
    SUMMARIZE = "SUMMARIZE"
    ARCHIVE = "ARCHIVE"


class ConflictStatus(StrEnum):
    """Lifecycle state for a conflict that requires a user decision."""

    OPEN = "open"
    RESOLVED_KEEP_OURS = "resolved_keep_ours"
    RESOLVED_KEEP_THEIRS = "resolved_keep_theirs"
    RESOLVED_KEEP_BOTH = "resolved_keep_both"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class MemoryUnit:
    """An immutable source unit extracted from a conversation or tool result."""

    id: str
    unit_type: UnitType
    text: str
    topic: str
    timestamp: str
    session_id: str
    token_count: int
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("MemoryUnit.id cannot be empty")
        if self.token_count < 0:
            raise ValueError("MemoryUnit.token_count cannot be negative")


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Normalized components used to make a memory decision explainable."""

    importance: float
    relevance: float
    redundancy_penalty: float
    recency: float
    pin_boost: float
    total: float

    def __post_init__(self) -> None:
        for name, value in (
            ("importance", self.importance),
            ("relevance", self.relevance),
            ("redundancy_penalty", self.redundancy_penalty),
            ("recency", self.recency),
            ("pin_boost", self.pin_boost),
            ("total", self.total),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    """A scored, human-readable keep, summarize, or archive decision."""

    unit_id: str
    action: MemoryAction
    reason: str
    score: ScoreBreakdown
    tier_before: MemoryTier | None
    tier_after: MemoryTier
    tokens_before: int
    tokens_after: int
    summary_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryMembership:
    """One item in the active-memory snapshot captured by a commit."""

    item_id: str
    item_kind: str
    tier: MemoryTier
    token_count: int
    revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCommit:
    """A hash-chained memory snapshot and its explainable decisions."""

    hash: str
    parent_hashes: tuple[str, ...]
    branch: str
    timestamp: str
    message: str
    topic: str | None
    tokens_before: int
    tokens_after: int
    decisions: tuple[MemoryDecision, ...] = ()
    membership: tuple[MemoryMembership, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitDiff:
    """Unit-level changes and token impact between two commits."""

    from_hash: str
    to_hash: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    tier_changed: tuple[tuple[str, MemoryTier, MemoryTier], ...]
    token_delta: int


@dataclass(frozen=True, slots=True)
class MergeConflict:
    """A contradiction or incompatible edit awaiting user resolution."""

    id: str
    unit_ids: tuple[str, ...]
    reason: str
    status: ConflictStatus = ConflictStatus.OPEN
    supersedes_unit_id: str | None = None


@dataclass(frozen=True, slots=True)
class CanaryProbe:
    """A critical fact and deterministic question used by Memory CI."""

    id: str
    unit_id: str
    question: str
    expected_answer: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """Context items selected under a token budget with decision explanations."""

    included: tuple[str, ...]
    excluded: tuple[str, ...]
    tokens_used: int
    token_budget: int
    explanations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnswerAttribution:
    """Memory-blame attribution for one answer source."""

    unit_id: str
    commit_hash: str
    decision: MemoryAction
    contribution: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Input/output token counts and observed latency for one inference."""

    input_tokens: int
    output_tokens: int
    latency_ms: float


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Measured evaluation results for one memory strategy."""

    strategy: str
    total_tokens: int
    estimated_cost: float
    response_latency_ms: float
    factual_accuracy: float
    critical_information_recall: float
    recovery_success: bool
