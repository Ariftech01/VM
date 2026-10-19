"""Exact value-maximizing knapsack context reconstruction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from gitmem.ingest.parser import lexical_similarity
from gitmem.models import ContextSelection, MemoryTier
from gitmem.tokenizer import TokenCounter


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    """A candidate memory item with explicit value and source attribution."""

    id: str
    text: str
    token_count: int
    tier: MemoryTier
    value: float
    kind: str
    source_unit_ids: tuple[str, ...] = ()

    @property
    def value_per_token(self) -> float:
        return self.value / max(1, self.token_count)


def build_context(
    query: str,
    candidates: Sequence[ContextCandidate],
    token_budget: int,
    token_counter: TokenCounter | None = None,
) -> tuple[list[ContextCandidate], ContextSelection]:
    """Select the maximum-value set that fits using a 0/1 knapsack dynamic program."""
    if token_budget < 0:
        raise ValueError("token_budget cannot be negative")
    counter = token_counter or TokenCounter()
    normalized: list[ContextCandidate] = []
    for candidate in candidates:
        if not candidate.text.strip():
            continue
        tokens = candidate.token_count or counter.count(candidate.text)
        relevance = lexical_similarity(query, candidate.text)
        tier_value = {
            MemoryTier.PINNED: 1.0,
            MemoryTier.HOT: 0.75,
            MemoryTier.WARM: 0.62,
            MemoryTier.COLD: 0.42,
        }[candidate.tier]
        query_boost = relevance
        if candidate.tier in (MemoryTier.PINNED, MemoryTier.HOT):
            query_boost = max(query_boost, 0.4)
        value = max(candidate.value, 0.02) + tier_value * 0.35 + query_boost * 0.65
        normalized.append(
            ContextCandidate(
                id=candidate.id,
                text=candidate.text,
                token_count=tokens,
                tier=candidate.tier,
                value=value,
                kind=candidate.kind,
                source_unit_ids=candidate.source_unit_ids,
            )
        )

    # DP stores both utility and chosen indices so ties resolve deterministically.
    dp: list[tuple[int, tuple[int, ...]] | None] = [None] * (token_budget + 1)
    dp[0] = (0, ())
    for index, candidate in enumerate(normalized):
        cost = candidate.token_count
        if cost > token_budget:
            continue
        utility = max(1, round(candidate.value * 1000))
        for remaining in range(token_budget, cost - 1, -1):
            prior = dp[remaining - cost]
            if prior is None:
                continue
            proposal = (prior[0] + utility, prior[1] + (index,))
            existing = dp[remaining]
            if existing is None or proposal[0] > existing[0]:
                dp[remaining] = proposal

    best_capacity, best = max(
        ((capacity, state) for capacity, state in enumerate(dp) if state is not None),
        key=lambda pair: (pair[1][0], -pair[0]),
    )
    del best_capacity
    chosen = set(best[1])
    included = [candidate for index, candidate in enumerate(normalized) if index in chosen]
    excluded = [candidate for index, candidate in enumerate(normalized) if index not in chosen]
    tokens_used = sum(candidate.token_count for candidate in included)
    explanations: dict[str, str] = {}
    for candidate in included:
        explanations[candidate.id] = (
            f"Included by knapsack optimization: value/token {candidate.value_per_token:.3f}, "
            f"{candidate.token_count} tokens, {candidate.tier.value} tier."
        )
    for candidate in excluded:
        reason = (
            f"Excluded by the {token_budget}-token budget; including its "
            f"{candidate.token_count} tokens would reduce the optimal total value."
        )
        if candidate.token_count > token_budget:
            reason = f"Excluded: its {candidate.token_count} tokens exceed the full budget."
        explanations[candidate.id] = reason
    return included, ContextSelection(
        included=tuple(candidate.id for candidate in included),
        excluded=tuple(candidate.id for candidate in excluded),
        tokens_used=tokens_used,
        token_budget=token_budget,
        explanations=explanations,
    )
