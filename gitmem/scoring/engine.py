"""Explainable memory scoring and deterministic tier/action selection."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from gitmem.config import GitMemConfig
from gitmem.models import MemoryAction, MemoryDecision, MemoryTier, MemoryUnit, ScoreBreakdown
from gitmem.tokenizer import TokenCounter
from gitmem.ingest.parser import lexical_similarity


class MemoryScorer:
    """Score memory units using importance, relevance, redundancy, recency, and pins."""

    def __init__(self, config: GitMemConfig, token_counter: TokenCounter | None = None) -> None:
        self.config = config
        self.token_counter = token_counter or TokenCounter()
        self._half_life_days = float(config.raw.get("scoring", {}).get("recency_half_life_days", 30))

    def score(
        self,
        unit: MemoryUnit,
        goal: str,
        other_units: list[MemoryUnit],
        now: datetime | None = None,
    ) -> ScoreBreakdown:
        """Return normalized score components, applying redundancy as a penalty."""
        weights = self.config.scoring_weights
        importance = self._importance(unit)
        relevance = self._relevance(unit.text, goal)
        redundancy = max(
            (lexical_similarity(unit.text, other.text) for other in other_units if other.id != unit.id),
            default=0.0,
        )
        parsed = self._parse_time(unit.timestamp)
        reference = now or datetime.now(UTC)
        age_days = max(0.0, (reference - parsed).total_seconds() / 86400.0)
        recency = math.exp(-math.log(2.0) * age_days / max(0.01, self._half_life_days))
        pin_boost = 1.0 if unit.pinned else 0.0
        total = (
            weights.importance * importance
            + weights.relevance * relevance
            - weights.redundancy * redundancy
            + weights.recency * recency
            + weights.pin_boost * pin_boost
        )
        return ScoreBreakdown(
            importance=round(importance, 6),
            relevance=round(relevance, 6),
            redundancy_penalty=round(redundancy, 6),
            recency=round(recency, 6),
            pin_boost=pin_boost,
            total=round(min(1.0, max(0.0, total)), 6),
        )

    def decide(
        self,
        unit: MemoryUnit,
        score: ScoreBreakdown,
        current_tier: MemoryTier | None = None,
    ) -> MemoryDecision:
        """Choose a single action and explain the score and resulting tier."""
        before = unit.token_count
        thresholds = self.config.scoring_thresholds
        if unit.pinned:
            action, tier, why = MemoryAction.KEEP_VERBATIM, MemoryTier.PINNED, "User-pinned; never compress automatically."
        elif score.total >= thresholds.keep_verbatim:
            action, tier, why = MemoryAction.KEEP_VERBATIM, MemoryTier.HOT, "High combined value; retain the original verbatim."
        elif score.total >= thresholds.summarize:
            action, tier, why = MemoryAction.SUMMARIZE, MemoryTier.WARM, "Useful context; attempt a source-linked faithful summary."
        else:
            action, tier, why = MemoryAction.ARCHIVE, MemoryTier.COLD, "Low current value; archive reversibly and retain the source."
        reason = (
            f"{why} Score {score.total:.3f}; importance {score.importance:.2f}, "
            f"relevance {score.relevance:.2f}, redundancy {score.redundancy_penalty:.2f}, "
            f"recency {score.recency:.2f}, pin boost {score.pin_boost:.2f}."
        )
        return MemoryDecision(
            unit_id=unit.id,
            action=action,
            reason=reason,
            score=score,
            tier_before=current_tier,
            tier_after=tier,
            tokens_before=before,
            tokens_after=before if action != MemoryAction.ARCHIVE else 0,
        )

    @staticmethod
    def _importance(unit: MemoryUnit) -> float:
        baseline = {
            "constraint": 0.95,
            "decision": 0.90,
            "preference": 0.85,
            "fact": 0.78,
            "code": 0.62,
            "turn": 0.42,
        }[unit.unit_type.value]
        if re.search(r"\b(critical|important|must|deadline|budget|never|not)\b", unit.text, re.I):
            baseline = min(1.0, baseline + 0.08)
        return 1.0 if unit.pinned else baseline

    @staticmethod
    def _relevance(text: str, goal: str) -> float:
        if not goal.strip():
            return 0.5
        return lexical_similarity(text, goal)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
