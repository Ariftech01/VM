"""Git-like operations over immutable memory snapshots and explainable decisions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from gitmem.config import GitMemConfig, load_config
from gitmem.context.builder import ContextCandidate, build_context
from gitmem.faithfulness import verify_summary
from gitmem.ingest.parser import ingest_messages, lexical_similarity
from gitmem.llm.base import LLMAdapter, LLMResponse
from gitmem.llm.mock import MockLLM
from gitmem.models import (
    AnswerAttribution,
    CanaryProbe,
    CommitDiff,
    ContextSelection,
    MemoryAction,
    MemoryDecision,
    MemoryMembership,
    MemoryTier,
    MemoryUnit,
    ScoreBreakdown,
)
from gitmem.retrieval.index import MemoryIndex
from gitmem.scoring.engine import MemoryScorer
from gitmem.store.repository import MemoryRepository, canonical_json, sha256_text, utc_now
from gitmem.tokenizer import TokenCounter
from gitmem.database import DEFAULT_DATABASE_PATH, initialize_database

_PII_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "phone": re.compile(r"(?<!\d)(?:\+?\d[\d .()\-]{7,}\d)(?!\d)"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "aadhaar": re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"),
}
_NEGATION = re.compile(r"\b(not|never|must not|mustn't|cannot|can't|does not|doesn't)\b", re.I)
_DATE_WORD = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
    re.I,
)


class GitMemEngine:
    """Application service for ingestion, memory management, and version control."""

    def __init__(
        self,
        database_path: str | Path = DEFAULT_DATABASE_PATH,
        config: GitMemConfig | None = None,
        config_path: str | Path | None = None,
        llm: LLMAdapter | None = None,
    ) -> None:
        self.config = config or load_config(config_path) if config_path else config or load_config()
        self.token_counter = TokenCounter()
        self.connection = initialize_database(database_path)
        self.repo = MemoryRepository(self.connection)
        self.scorer = MemoryScorer(self.config, self.token_counter)
        self.retrieval_index = MemoryIndex(self.config.embedding_model)
        self.llm: LLMAdapter = llm or MockLLM(self.token_counter)
        self.probe_llm = MockLLM(self.token_counter)
        if "main" not in self.repo.branch_names():
            self.repo.create_commit(
                branch="main",
                message="Initialize GitMem",
                topic=None,
                tokens_before=0,
                memberships=[],
                decisions=[],
                parents=(),
            )

    def close(self) -> None:
        """Close the SQLite connection."""
        self.repo.close()

    def head(self, branch: str = "main") -> str:
        """Return the current head hash for a branch."""
        result = self.repo.branch_head(branch)
        if result is None:
            raise KeyError(f"Unknown branch or empty branch: {branch}")
        return result

    def resolve_ref(self, ref: str) -> str:
        """Resolve a branch name, tag name, full commit hash, or prefix to a commit hash."""
        if not ref or not ref.strip():
            raise ValueError("Reference cannot be empty.")
        cleaned = ref.strip()
        branch_head = self.repo.branch_head(cleaned)
        if branch_head is not None:
            return branch_head
        tag_row = self.connection.execute(
            "SELECT commit_hash FROM tags WHERE name = ?", (cleaned,)
        ).fetchone()
        if tag_row is not None:
            return str(tag_row["commit_hash"])
        commit_row = self.repo.commit_row(cleaned)
        if commit_row is not None:
            return str(commit_row["hash"])
        if len(cleaned) >= 4:
            matches = self.connection.execute(
                "SELECT hash FROM commits WHERE hash LIKE ?", (f"{cleaned}%",)
            ).fetchall()
            if len(matches) == 1:
                return str(matches[0]["hash"])
            elif len(matches) > 1:
                raise ValueError(
                    f"Ambiguous commit reference: {cleaned} matches {len(matches)} commits."
                )
        raise KeyError(f"Unknown commit, branch, or tag reference: {ref}")

    def ingest(
        self,
        messages: Sequence[dict[str, Any]],
        session_id: str | None = None,
        current_goal: str = "",
        branch: str = "main",
        session_label: str = "",
    ) -> dict[str, Any]:
        """Split and save messages, add their source units, and commit the update."""
        session_id = session_id or str(uuid.uuid4())
        self.repo.ensure_session(session_id, session_label)
        units = ingest_messages(messages, session_id, current_goal, self.token_counter)
        if not units:
            raise ValueError("No non-empty message content was provided")
        current = self._member_map(self.head(branch))
        known_units = self.repo.all_units()
        decisions: list[MemoryDecision] = []
        for unit in units:
            revision_id = self.repo.save_unit(unit)
            current[unit.id] = MemoryMembership(
                item_id=unit.id,
                item_kind="unit",
                tier=MemoryTier.PINNED if unit.pinned else MemoryTier.HOT,
                token_count=unit.token_count,
                revision_id=revision_id,
            )
            score = self.scorer.score(unit, current_goal, known_units)
            decisions.append(
                MemoryDecision(
                    unit_id=unit.id,
                    action=MemoryAction.KEEP_VERBATIM,
                    reason="New source unit stored verbatim; compression is a separate reversible commit.",
                    score=score,
                    tier_before=None,
                    tier_after=MemoryTier.PINNED if unit.pinned else MemoryTier.HOT,
                    tokens_before=0,
                    tokens_after=unit.token_count,
                )
            )
        commit_hash = self._create_commit(
            branch,
            f"Ingest {len(units)} memory unit(s)",
            units[0].topic if len({unit.topic for unit in units}) == 1 else None,
            self._visible_tokens(list(current.values())),
            list(current.values()),
            decisions,
        )
        return {
            "session_id": session_id,
            "unit_ids": [unit.id for unit in units],
            "commit_hash": commit_hash,
            "decisions": decisions,
        }

    def compress(self, goal: str = "", branch: str = "main") -> dict[str, Any]:
        """Score active units, summarize only faithful ones, and archive reversibly."""
        parent_hash = self.head(branch)
        current = self._member_map(parent_hash)
        units = self.repo.all_units()
        decisions: list[MemoryDecision] = []
        updated: dict[str, MemoryMembership] = dict(current)
        summaries_created = 0
        for key, membership in list(current.items()):
            if membership.item_kind != "unit":
                continue
            unit = self.repo.get_unit(key, membership.revision_id)
            if unit is None:
                continue
            score = self.scorer.score(unit, goal, units)
            decision = self.scorer.decide(unit, score, membership.tier)
            if membership.tier == MemoryTier.COLD and not unit.pinned:
                decision = MemoryDecision(
                    unit_id=unit.id,
                    action=MemoryAction.ARCHIVE,
                    reason=decision.reason + " It is already archived; no source data was deleted.",
                    score=score,
                    tier_before=membership.tier,
                    tier_after=MemoryTier.COLD,
                    tokens_before=unit.token_count,
                    tokens_after=0,
                )
                decisions.append(decision)
                continue

            if decision.action == MemoryAction.SUMMARIZE:
                summary_text = self.llm.summarize(unit.text)
                report = verify_summary(unit.text, summary_text)
                if report.passed:
                    summary_id = self.repo.add_summary(
                        summary_text,
                        unit.topic,
                        self.token_counter.count(summary_text),
                        [unit.id],
                        {
                            "passed": report.passed,
                            "reason": report.reason,
                            "missing_numbers": report.missing_numbers,
                            "missing_dates": report.missing_dates,
                            "missing_entities": report.missing_entities,
                            "missing_negations": report.missing_negations,
                            "introduced_numbers": report.introduced_numbers,
                            "introduced_entities": report.introduced_entities,
                        },
                    )
                    updated[key] = MemoryMembership(
                        item_id=unit.id,
                        item_kind="unit",
                        tier=MemoryTier.COLD,
                        token_count=unit.token_count,
                        revision_id=membership.revision_id,
                    )
                    summary_member = MemoryMembership(
                        item_id=f"summary:{summary_id}",
                        item_kind="summary",
                        tier=MemoryTier.WARM,
                        token_count=self.token_counter.count(summary_text),
                    )
                    updated[summary_member.item_id] = summary_member
                    summaries_created += 1
                    decision = MemoryDecision(
                        unit_id=unit.id,
                        action=MemoryAction.SUMMARIZE,
                        reason=decision.reason + f" Faithfulness gate passed; summary {summary_id[:8]} links to source {unit.id}.",
                        score=score,
                        tier_before=membership.tier,
                        tier_after=MemoryTier.WARM,
                        tokens_before=unit.token_count,
                        tokens_after=self.token_counter.count(summary_text),
                        summary_id=summary_id,
                    )
                else:
                    decision = MemoryDecision(
                        unit_id=unit.id,
                        action=MemoryAction.KEEP_VERBATIM,
                        reason=decision.reason + f" Summary rejected: {report.reason}",
                        score=score,
                        tier_before=membership.tier,
                        tier_after=MemoryTier.HOT,
                        tokens_before=unit.token_count,
                        tokens_after=unit.token_count,
                    )
                    updated[key] = MemoryMembership(
                        item_id=unit.id,
                        item_kind="unit",
                        tier=MemoryTier.HOT,
                        token_count=unit.token_count,
                        revision_id=membership.revision_id,
                    )
            else:
                updated[key] = MemoryMembership(
                    item_id=unit.id,
                    item_kind="unit",
                    tier=decision.tier_after,
                    token_count=unit.token_count,
                    revision_id=membership.revision_id,
                )
            decisions.append(decision)

        after = list(updated.values())
        commit_hash = self._create_commit(
            branch,
            f"Compress memory for: {goal[:100] or 'current project'}",
            None,
            self._visible_tokens(list(current.values())),
            after,
            decisions,
        )
        return {
            "commit_hash": commit_hash,
            "decisions": decisions,
            "summaries_created": summaries_created,
            "tokens_before": self._visible_tokens(list(current.values())),
            "tokens_after": self._visible_tokens(after),
            "tokens_saved": self._visible_tokens(list(current.values())) - self._visible_tokens(after),
        }

    def build_context(
        self,
        query: str,
        token_budget: int | None = None,
        branch: str = "main",
        commit_hash: str | None = None,
    ) -> tuple[list[ContextCandidate], ContextSelection]:
        """Retrieve current, summarized, and archived material under a token budget."""
        target = commit_hash or self.head(branch)
        memberships = self.repo.commit_membership(target)
        candidates: list[ContextCandidate] = []
        seen: set[str] = set()
        active_summary_sources: set[str] = set()
        for membership in memberships:
            if membership.item_kind != "summary":
                continue
            summary_id = membership.item_id.removeprefix("summary:")
            active_summary_sources.update(
                str(row["unit_id"])
                for row in self.connection.execute(
                    "SELECT unit_id FROM summary_sources WHERE summary_id = ?",
                    (summary_id,),
                )
            )
        recent_turn_ids = {
            str(row["id"])
            for row in self.connection.execute(
                """
                SELECT u.id
                FROM units u JOIN commit_membership cm ON cm.item_id = u.id
                WHERE cm.commit_hash = ? AND cm.item_kind = 'unit'
                  AND u.unit_type = 'turn' AND cm.tier IN ('hot', 'pinned')
                ORDER BY u.created_at DESC LIMIT 3
                """,
                (target,),
            )
        }
        for membership in memberships:
            text = self.repo.get_membership_text(membership)
            if not text or text == "[cryptographically shredded]":
                continue
            if membership.item_kind == "unit":
                unit = self.repo.get_unit(membership.item_id, membership.revision_id)
                if unit is None:
                    continue
                if (
                    membership.tier == MemoryTier.COLD
                    and unit.id in active_summary_sources
                ):
                    continue
                source_ids = (unit.id,)
                base_value = self.scorer._importance(unit)
                kind = unit.unit_type.value
            else:
                summary_id = membership.item_id.removeprefix("summary:")
                source_rows = self.connection.execute(
                    "SELECT unit_id FROM summary_sources WHERE summary_id = ? ORDER BY source_order",
                    (summary_id,),
                ).fetchall()
                source_ids = tuple(str(row["unit_id"]) for row in source_rows)
                base_value = 0.72
                kind = "summary"
            if membership.item_id in seen:
                continue
            seen.add(membership.item_id)
            candidates.append(
                ContextCandidate(
                    id=membership.item_id,
                    text=text,
                    token_count=membership.token_count,
                    tier=membership.tier,
                    value=base_value,
                    kind=kind,
                    source_unit_ids=source_ids,
                )
            )

        self.retrieval_index.build({candidate.id: candidate.text for candidate in candidates})
        retrieval_scores = dict(self.retrieval_index.search(query, top_k=len(candidates)))
        candidates = [
            ContextCandidate(
                id=item.id,
                text=item.text,
                token_count=item.token_count,
                tier=item.tier,
                value=item.value + retrieval_scores.get(item.id, 0.0) * 0.4,
                kind=item.kind,
                source_unit_ids=item.source_unit_ids,
            )
            for item in candidates
            if (
                item.tier == MemoryTier.PINNED
                or item.id in recent_turn_ids
                or retrieval_scores.get(item.id, 0.0) >= 0.045
            )
        ]
        budget = token_budget or self.config.default_token_budget
        return build_context(query, candidates, budget, self.token_counter)

    def answer(
        self,
        question: str,
        token_budget: int | None = None,
        branch: str = "main",
        commit_hash: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        """Answer using an optimized context and persist source-level blame records."""
        target = commit_hash or self.head(branch)
        selected, selection = self.build_context(question, token_budget, branch, target)
        response = self.llm.answer(question, [item.text for item in selected])
        attributions: list[AnswerAttribution] = []
        for item in selected:
            for unit_id in item.source_unit_ids:
                decision_row = self.connection.execute(
                    """
                    SELECT action FROM commit_decisions
                    WHERE commit_hash = ? AND unit_id = ?
                    """,
                    (target, unit_id),
                ).fetchone()
                action = (
                    MemoryAction(decision_row["action"])
                    if decision_row
                    else MemoryAction.KEEP_VERBATIM
                )
                contribution = (
                    "Answer text came from this source unit."
                    if item.kind != "summary"
                    else f"Answer text came through summary {item.id.removeprefix('summary:')}."
                )
                attributions.append(
                    AnswerAttribution(unit_id, target, action, contribution)
                )

        answer_id: str | None = None
        if record:
            row = self.repo.commit_row(target)
            branch_name = str(row["branch"]) if row else branch
            answer_id = str(uuid.uuid4())
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO answer_runs(
                        id, commit_hash, branch, question, answer, model_name,
                        input_tokens, output_tokens, latency_ms, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        answer_id,
                        target,
                        branch_name,
                        question,
                        response.text,
                        response.model,
                        response.input_tokens,
                        response.output_tokens,
                        response.latency_ms,
                        utc_now(),
                    ),
                )
                self.connection.executemany(
                    """
                    INSERT OR IGNORE INTO answer_sources(
                        answer_run_id, unit_id, commit_hash, decision, contribution
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            answer_id,
                            source.unit_id,
                            source.commit_hash,
                            source.decision.value,
                            source.contribution,
                        )
                        for source in attributions
                    ],
                )
        return {
            "answer_id": answer_id,
            "answer": response.text,
            "response": response,
            "commit_hash": target,
            "selected": selected,
            "selection": selection,
            "attributions": attributions,
        }

    def restore(self, commit_hash: str, branch: str = "main") -> str:
        """Restore an entire historical snapshot as a new commit on the branch."""
        if not self.repo.commit_row(commit_hash):
            raise KeyError(f"Unknown commit: {commit_hash}")
        target_members = self.repo.commit_membership(commit_hash)
        readable = [item for item in target_members if self._membership_readable(item)]
        current = self.repo.commit_membership(self.head(branch))
        return self._create_commit(
            branch,
            f"Restore memory snapshot {commit_hash[:12]}",
            None,
            self._visible_tokens(current),
            readable,
            [],
        )

    def checkout(self, commit_hash: str, branch: str = "main") -> str:
        """Checkout is a safe snapshot restore, preserving the audit trail."""
        return self.restore(commit_hash, branch)

    def revert(self, commit_hash: str | None = None, branch: str = "main") -> str:
        """Create a new snapshot restoring the first parent of the selected commit."""
        target = commit_hash or self.head(branch)
        row = self.repo.commit_row(target)
        if not row or not row["first_parent_hash"]:
            raise ValueError("The selected commit has no parent to revert to")
        return self.restore(str(row["first_parent_hash"]), branch)

    def rehydrate(
        self,
        unit_ids: Sequence[str],
        branch: str = "main",
        tier: MemoryTier = MemoryTier.HOT,
    ) -> str:
        """Pull selected archived source units into context without restoring a snapshot."""
        if not unit_ids:
            raise ValueError("Select at least one archived unit to rehydrate")
        current = self._member_map(self.head(branch))
        for unit_id in unit_ids:
            unit = self.repo.get_unit(unit_id)
            if unit is None:
                raise KeyError(f"Unknown or shredded memory unit: {unit_id}")
            current[unit_id] = MemoryMembership(
                item_id=unit_id,
                item_kind="unit",
                tier=MemoryTier.PINNED if unit.pinned else tier,
                token_count=unit.token_count,
                revision_id=self.repo.unit_revision_id(unit_id),
            )
        return self._create_commit(
            branch,
            f"Rehydrate {len(unit_ids)} archived unit(s)",
            None,
            self._visible_tokens(self.repo.commit_membership(self.head(branch))),
            list(current.values()),
            [],
        )

    def edit_unit(
        self,
        unit_id: str,
        text: str | None = None,
        pinned: bool | None = None,
        force_action: MemoryAction | None = None,
        branch: str = "main",
    ) -> str:
        """Manually edit a unit, pin it, or force a keep/archive decision."""
        unit = self.repo.get_unit(unit_id)
        if unit is None:
            raise KeyError(f"Unknown or shredded memory unit: {unit_id}")
        current = self._member_map(self.head(branch))
        existing = current.get(unit_id)
        if text is not None:
            if not text.strip():
                raise ValueError("Edited memory text cannot be empty")
            revision_id = self.repo.add_revision(unit_id, text, "manual edit")
            unit = self.repo.get_unit(unit_id, revision_id)
            assert unit is not None
            for item_id in list(current):
                if not item_id.startswith("summary:"):
                    continue
                summary_id = item_id.removeprefix("summary:")
                linked = self.connection.execute(
                    "SELECT 1 FROM summary_sources WHERE summary_id = ? AND unit_id = ?",
                    (summary_id, unit_id),
                ).fetchone()
                if linked:
                    current.pop(item_id)
        else:
            revision_id = existing.revision_id if existing else self.repo.unit_revision_id(unit_id)
        new_pin = unit.pinned if pinned is None else pinned
        if pinned is not None:
            self.repo.update_pinned(unit_id, pinned)
        if force_action == MemoryAction.ARCHIVE and new_pin:
            new_pin = False
            self.repo.update_pinned(unit_id, False)
        if force_action == MemoryAction.ARCHIVE:
            tier = MemoryTier.COLD
        elif new_pin:
            tier = MemoryTier.PINNED
        else:
            tier = MemoryTier.HOT
        if force_action == MemoryAction.SUMMARIZE:
            raise ValueError("Use compression to generate a source-verified summary")
        if force_action == MemoryAction.KEEP_VERBATIM:
            tier = MemoryTier.HOT
        current[unit_id] = MemoryMembership(
            item_id=unit_id,
            item_kind="unit",
            tier=tier,
            token_count=self.token_counter.count(unit.text),
            revision_id=revision_id,
        )
        zero = ScoreBreakdown(0, 0, 0, 0, 1.0 if new_pin else 0, 1.0 if new_pin else 0)
        action = force_action or MemoryAction.KEEP_VERBATIM
        decision = MemoryDecision(
            unit_id=unit_id,
            action=action,
            reason="Manual user edit; user action takes precedence over automatic scoring.",
            score=zero,
            tier_before=existing.tier if existing else None,
            tier_after=tier,
            tokens_before=existing.token_count if existing else 0,
            tokens_after=self.token_counter.count(unit.text) if tier != MemoryTier.COLD else 0,
        )
        return self._create_commit(
            branch,
            f"Manually update memory unit {unit_id[:8]}",
            unit.topic,
            self._visible_tokens(list(self._member_map(self.head(branch)).values())),
            list(current.values()),
            [decision],
        )

    def create_branch(self, name: str, from_commit: str | None = None, description: str = "") -> str:
        """Create a branch at a selected commit or at the current main head."""
        source = from_commit or self.head("main")
        self.repo.create_branch(name, source, description)
        return source

    def tag(self, name: str, commit_hash: str | None = None, message: str = "") -> None:
        """Create a named checkpoint without modifying history."""
        target = commit_hash or self.head("main")
        with self.connection:
            self.connection.execute(
                "INSERT INTO tags(name, commit_hash, created_at, message) VALUES (?, ?, ?, ?)",
                (name, target, utc_now(), message),
            )

    def log(self, branch: str = "main", limit: int = 100) -> list[dict[str, Any]]:
        """Return the selected branch's reachable commits, newest first."""
        start = self.head(branch)
        queue = deque([start])
        visited: set[str] = set()
        rows: list[sqlite3.Row] = []
        while queue and len(rows) < limit:
            commit_hash = queue.popleft()
            if commit_hash in visited:
                continue
            visited.add(commit_hash)
            row = self.repo.commit_row(commit_hash)
            if not row:
                continue
            rows.append(row)
            parents = [row["first_parent_hash"], row["second_parent_hash"]]
            queue.extend(parent for parent in parents if parent and parent not in visited)
        return [
            {
                "hash": row["hash"],
                "parents": tuple(
                    parent
                    for parent in (row["first_parent_hash"], row["second_parent_hash"])
                    if parent
                ),
                "branch": row["branch"],
                "timestamp": row["created_at"],
                "message": row["message"],
                "topic": row["topic"],
                "tokens_before": row["tokens_before"],
                "tokens_after": row["tokens_after"],
                "canary_failed": self._commit_has_failed_canary(str(row["hash"])),
            }
            for row in rows
        ]

    def diff(self, from_hash: str, to_hash: str) -> CommitDiff:
        """Compare two immutable snapshots, including content/tier changes."""
        old = {member.item_id: member for member in self.repo.commit_membership(from_hash)}
        new = {member.item_id: member for member in self.repo.commit_membership(to_hash)}
        added = tuple(sorted(new.keys() - old.keys()))
        removed = tuple(sorted(old.keys() - new.keys()))
        tier_changed: list[tuple[str, MemoryTier, MemoryTier]] = []
        content_changed: list[str] = []
        for item_id in old.keys() & new.keys():
            if old[item_id].tier != new[item_id].tier:
                tier_changed.append((item_id, old[item_id].tier, new[item_id].tier))
            if old[item_id].revision_id != new[item_id].revision_id:
                content_changed.append(item_id)
        old_row, new_row = self.repo.commit_row(from_hash), self.repo.commit_row(to_hash)
        if not old_row or not new_row:
            raise KeyError("Unknown commit in diff")
        return CommitDiff(
            from_hash=from_hash,
            to_hash=to_hash,
            added=added,
            removed=removed,
            tier_changed=tuple(sorted(tier_changed, key=lambda item: item[0])),
            token_delta=int(new_row["tokens_after"]) - int(old_row["tokens_after"]),
            content_changed=tuple(sorted(content_changed)),
        )

    def cherry_pick(self, commit_hash: str, branch: str = "main") -> str:
        """Apply units present at another commit without replacing current memory."""
        source = self.repo.commit_membership(commit_hash)
        current = self._member_map(self.head(branch))
        changed: list[MemoryDecision] = []
        for item in source:
            if item.item_kind == "unit" and self.repo.get_unit(item.item_id, item.revision_id) is None:
                continue
            if item.item_kind == "summary":
                summary = self.repo.get_summary(item.item_id.removeprefix("summary:"))
                if not summary or summary["text"] == "[cryptographically shredded]":
                    continue
            current[item.item_id] = item
        return self._create_commit(
            branch,
            f"Cherry-pick snapshot items from {commit_hash[:12]}",
            None,
            self._visible_tokens(self.repo.commit_membership(self.head(branch))),
            list(current.values()),
            changed,
        )

    def verify_integrity(self) -> dict[str, Any]:
        """Recompute every commit digest, snapshot digest, and parent link."""
        problems: list[str] = []
        rows = self.connection.execute(
            "SELECT * FROM commits ORDER BY created_at, hash"
        ).fetchall()
        known = {str(row["hash"]) for row in rows}
        for row in rows:
            commit_hash = str(row["hash"])
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                problems.append(f"{commit_hash}: malformed commit payload")
                continue
            if sha256_text(canonical_json(payload)) != commit_hash:
                problems.append(f"{commit_hash}: commit payload hash mismatch")
            expected_row_fields = {
                "branch": row["branch"],
                "created_at": row["created_at"],
                "message": row["message"],
                "topic": row["topic"],
                "tokens_before": row["tokens_before"],
                "tokens_after": row["tokens_after"],
                "snapshot_hash": row["snapshot_hash"],
            }
            for field, value in expected_row_fields.items():
                if payload.get(field) != value:
                    problems.append(f"{commit_hash}: row field {field} differs from hashed payload")
                    break
            parents = [
                parent
                for parent in (row["first_parent_hash"], row["second_parent_hash"])
                if parent
            ]
            if payload.get("parents") != parents:
                problems.append(f"{commit_hash}: parent list mismatch")
            if any(parent not in known for parent in parents):
                problems.append(f"{commit_hash}: missing parent")
            members = self.repo.membership_rows(commit_hash)
            if canonical_json(members) != canonical_json(payload.get("membership", [])):
                problems.append(f"{commit_hash}: active-memory membership was altered")
            snapshot_hash = sha256_text(canonical_json(members))
            if snapshot_hash != row["snapshot_hash"]:
                problems.append(f"{commit_hash}: snapshot hash mismatch")
            if self._stored_decisions(commit_hash) != payload.get("decisions", []):
                problems.append(f"{commit_hash}: decision log was altered")
        return {"valid": not problems, "commit_count": len(rows), "problems": problems}

    def register_canary(self, unit_id: str, question: str, expected_answer: str) -> str:
        """Mark a critical fact with a probe run automatically after each commit."""
        if not self.repo.get_unit(unit_id):
            raise KeyError(f"Unknown or shredded memory unit: {unit_id}")
        probe_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO canary_probes(id, unit_id, question, expected_answer, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (probe_id, unit_id, question, expected_answer, utc_now()),
            )
        for row in reversed(self.log("main", limit=10000)):
            self.run_canaries(str(row["hash"]))
        return probe_id

    def run_canaries(self, commit_hash: str) -> list[dict[str, Any]]:
        """Run deterministic local probes against the context reconstructed at a commit."""
        probes = self.connection.execute(
            "SELECT * FROM canary_probes WHERE enabled = 1 ORDER BY created_at"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for probe in probes:
            selected, _ = self.build_context(
                str(probe["question"]),
                self.config.default_token_budget,
                commit_hash=commit_hash,
            )
            response = self.probe_llm.answer(
                str(probe["question"]), [item.text for item in selected]
            )
            expected = str(probe["expected_answer"]).casefold().strip()
            passed = bool(expected and expected in response.text.casefold())
            result_id = str(uuid.uuid4())
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO canary_runs(
                        id, probe_id, commit_hash, actual_answer, passed, created_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        probe["id"],
                        commit_hash,
                        response.text,
                        int(passed),
                        utc_now(),
                        canonical_json({"expected_answer": expected}),
                    ),
                )
            results.append(
                {
                    "probe_id": probe["id"],
                    "unit_id": probe["unit_id"],
                    "question": probe["question"],
                    "expected_answer": probe["expected_answer"],
                    "actual_answer": response.text,
                    "passed": passed,
                }
            )
        return results

    def canary_results(self, commit_hash: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT p.id AS probe_id, p.unit_id, p.question, p.expected_answer,
                   r.commit_hash, r.actual_answer, r.passed, r.created_at
            FROM canary_probes p LEFT JOIN canary_runs r ON r.probe_id = p.id
        """
        params: tuple[Any, ...] = ()
        if commit_hash:
            query += " WHERE r.commit_hash = ?"
            params = (commit_hash,)
        query += " ORDER BY r.created_at DESC, p.created_at DESC"
        return [dict(row) for row in self.connection.execute(query, params)]

    def auto_revert_failed_canary(self, branch: str = "main") -> str:
        """Restore the newest first-parent ancestor whose registered probes all pass."""
        head = self.head(branch)
        row = self.repo.commit_row(head)
        while row:
            if self._commit_canaries_pass(str(row["hash"])):
                return self.restore(str(row["hash"]), branch)
            parent = row["first_parent_hash"]
            row = self.repo.commit_row(str(parent)) if parent else None
        raise ValueError("No earlier commit passes every registered canary")

    def bisect(
        self,
        question: str,
        expected_answer: str,
        branch: str = "main",
        good_commit: str | None = None,
    ) -> dict[str, Any]:
        """Binary-search first-parent history for the first commit failing a probe."""
        path: list[str] = []
        current: str | None = self.head(branch)
        while current:
            path.append(current)
            row = self.repo.commit_row(current)
            current = str(row["first_parent_hash"]) if row and row["first_parent_hash"] else None
        path.reverse()
        if not path:
            raise ValueError("Branch has no commits")

        def passes(commit_hash: str) -> bool:
            selected, _ = self.build_context(
                question,
                self.config.default_token_budget,
                commit_hash=commit_hash,
            )
            response = self.probe_llm.answer(question, [item.text for item in selected])
            return expected_answer.casefold().strip() in response.text.casefold()

        if passes(path[-1]):
            return {"found": False, "reason": "The current branch answers the probe correctly.", "commit_hash": None}
        if good_commit:
            if good_commit not in path:
                raise ValueError("The known-good commit is not on the selected first-parent history.")
            good_index = path.index(good_commit)
            if not passes(good_commit):
                raise ValueError("The selected known-good commit does not pass the probe.")
        else:
            good_index = -1
            for index in range(len(path) - 2, -1, -1):
                if passes(path[index]):
                    good_index = index
                    break
        if good_index < 0:
            return {
                "found": False,
                "reason": "No earlier passing commit was found; bisect needs a known-good point.",
                "commit_hash": None,
            }
        low, high = good_index + 1, len(path) - 1
        while low < high:
            mid = (low + high) // 2
            if passes(path[mid]):
                low = mid + 1
            else:
                high = mid
        return {
            "found": True,
            "commit_hash": path[low],
            "commit": self.repo.commit_row(path[low]),
            "first_bad_index": low,
            "commits_checked": len(path),
            "method": "binary search over first-parent history",
        }

    def blame(self, answer_id: str) -> list[dict[str, Any]]:
        """Return unit, commit, decision, and contribution records for an answer."""
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT s.unit_id, s.commit_hash, s.decision, s.contribution,
                       c.message AS commit_message, u.topic, u.unit_type
                FROM answer_sources s
                JOIN commits c ON c.hash = s.commit_hash
                JOIN units u ON u.id = s.unit_id
                WHERE s.answer_run_id = ?
                ORDER BY u.topic, u.id
                """,
                (answer_id,),
            )
        ]

    def what_if(
        self,
        question: str,
        first_ref: str,
        second_ref: str,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Replay one question at two snapshots and return answer/token/latency differences."""
        if not question or not question.strip():
            raise ValueError("Question cannot be empty.")
        first_commit = self.resolve_ref(first_ref)
        second_commit = self.resolve_ref(second_ref)
        first = self.answer(question.strip(), token_budget, commit_hash=first_commit, record=False)
        second = self.answer(question.strip(), token_budget, commit_hash=second_commit, record=False)
        return {
            "question": question.strip(),
            "first": {
                "reference": first_ref,
                "commit_hash": first_commit,
                "answer": first["response"].text,
                "tokens": first["response"].input_tokens,
                "latency_ms": first["response"].latency_ms,
            },
            "second": {
                "reference": second_ref,
                "commit_hash": second_commit,
                "answer": second["response"].text,
                "tokens": second["response"].input_tokens,
                "latency_ms": second["response"].latency_ms,
            },
            "answers_differ": first["response"].text != second["response"].text,
            "token_delta": second["response"].input_tokens - first["response"].input_tokens,
            "latency_delta_ms": round(second["response"].latency_ms - first["response"].latency_ms, 4),
        }

    def shred(self, unit_id: str, confirmed: bool, branch: str = "main") -> str:
        """Irreversibly remove detected PII text while retaining hashes and a tombstone."""
        if not confirmed:
            raise PermissionError("Crypto-shred requires explicit confirmation.")
        unit = self.repo.get_unit(unit_id)
        if unit is None:
            raise KeyError(f"Unknown or already shredded memory unit: {unit_id}")
        detected = [name for name, pattern in _PII_PATTERNS.items() if pattern.search(unit.text)]
        if not detected:
            raise ValueError("Crypto-shred is limited to units with PII detected by the configured patterns.")
        revisions = self.connection.execute(
            """
            SELECT id, content_sha256 FROM unit_revisions
            WHERE unit_id = ? AND text IS NOT NULL
            """,
            (unit_id,),
        ).fetchall()
        revision_hashes = [str(row["content_sha256"]) for row in revisions]
        current = self._member_map(self.head(branch))
        for item_id in list(current):
            if item_id == unit_id:
                current.pop(item_id)
            elif item_id.startswith("summary:"):
                summary_id = item_id.removeprefix("summary:")
                linked = self.connection.execute(
                    "SELECT 1 FROM summary_sources WHERE summary_id = ? AND unit_id = ?",
                    (summary_id, unit_id),
                ).fetchone()
                if linked:
                    current.pop(item_id)
        commit_hash = self._create_commit(
            branch,
            f"Crypto-shred PII memory unit {unit_id[:8]}",
            unit.topic,
            self._visible_tokens(self.repo.commit_membership(self.head(branch))),
            list(current.values()),
            [],
        )
        timestamp = utc_now()
        tombstone_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                """
                UPDATE unit_revisions SET text = NULL, shredded_at = ?
                WHERE unit_id = ? AND text IS NOT NULL
                """,
                (timestamp, unit_id),
            )
            self.connection.execute(
                "UPDATE units SET shredded_at = ? WHERE id = ?", (timestamp, unit_id)
            )
            linked_summaries = self.connection.execute(
                "SELECT summary_id FROM summary_sources WHERE unit_id = ?", (unit_id,)
            ).fetchall()
            for row in linked_summaries:
                self.connection.execute(
                    "UPDATE summaries SET text = '[cryptographically shredded]' WHERE id = ?",
                    (row["summary_id"],),
                )
            linked_answers = self.connection.execute(
                "SELECT answer_run_id FROM answer_sources WHERE unit_id = ?", (unit_id,)
            ).fetchall()
            for row in linked_answers:
                self.connection.execute(
                    "UPDATE answer_runs SET question = '[cryptographically shredded]', answer = '[cryptographically shredded]' WHERE id = ?",
                    (row["answer_run_id"],),
                )
            self.connection.execute(
                """
                INSERT INTO shred_tombstones(
                    id, unit_id, commit_hash, original_sha256,
                    shredded_revision_hashes_json, confirmation_text, shredded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tombstone_id,
                    unit_id,
                    commit_hash,
                    self._unit_original_hash(unit_id),
                    canonical_json(revision_hashes),
                    "Explicit irreversible deletion confirmed by operator.",
                    timestamp,
                ),
            )
        return commit_hash

    def merge(
        self,
        ours_branch: str,
        theirs_branch: str,
        target_branch: str | None = None,
    ) -> dict[str, Any]:
        """Three-way merge compatible changes; queue contradictions for user review."""
        target_branch = target_branch or ours_branch
        ours_hash, theirs_hash = self.head(ours_branch), self.head(theirs_branch)
        if ours_hash == theirs_hash:
            return {"merged": False, "conflicts": [], "reason": "Branches already share the same head."}
        base_hash = self._merge_base(ours_hash, theirs_hash)
        base = self._member_map(base_hash) if base_hash else {}
        ours = self._member_map(ours_hash)
        theirs = self._member_map(theirs_hash)
        merged = dict(ours)
        for item_id, member in theirs.items():
            base_member = base.get(item_id)
            ours_member = ours.get(item_id)
            if ours_member == base_member:
                merged[item_id] = member
            elif member == base_member or member == ours_member:
                continue
            else:
                conflict_id = self._record_conflict(
                    base_hash, ours_hash, theirs_hash, [item_id],
                    f"Both branches changed the same memory item {item_id}.",
                )
                return {"merged": False, "conflicts": [self._conflict_dict(conflict_id)]}

        changed_ours = [
            key for key, member in ours.items()
            if key not in base or member != base.get(key)
        ]
        changed_theirs = [
            key for key, member in theirs.items()
            if key not in base or member != base.get(key)
        ]
        queued_conflicts: list[str] = []
        candidate_pairs = {
            (ours_id, theirs_id)
            for ours_id in ours
            for theirs_id in changed_theirs
            if ours_id != theirs_id
        } | {
            (ours_id, theirs_id)
            for ours_id in changed_ours
            for theirs_id in theirs
            if ours_id != theirs_id
        }
        for ours_id, theirs_id in sorted(candidate_pairs):
            ours_unit = self.repo.get_unit(ours_id)
            theirs_unit = self.repo.get_unit(theirs_id)
            if not ours_unit or not theirs_unit:
                continue
            supersession = self._supersession(ours_unit, theirs_unit)
            if supersession:
                newer, older = supersession
                self._record_relation(
                    newer.id,
                    older.id,
                    "supersedes",
                    "Newer explicit correction supersedes prior fact.",
                )
                merged.pop(older.id, None)
                continue
            if self._contradictory(
                ours_unit.text,
                theirs_unit.text,
                ours_unit.topic,
                theirs_unit.topic,
            ):
                conflict_id = self._record_conflict(
                    base_hash,
                    ours_hash,
                    theirs_hash,
                    [ours_id, theirs_id],
                    "Contradictory requirements or facts need an explicit merge choice.",
                )
                queued_conflicts.append(conflict_id)
        if queued_conflicts:
            return {
                "merged": False,
                "conflicts": [self._conflict_dict(conflict_id) for conflict_id in queued_conflicts],
            }

        commit_hash = self._create_commit(
            target_branch,
            f"Merge {theirs_branch} into {ours_branch}",
            None,
            self._visible_tokens(list(ours.values())),
            list(merged.values()),
            [],
            parents=(ours_hash, theirs_hash),
        )
        return {"merged": True, "commit_hash": commit_hash, "conflicts": []}

    def resolve_conflict(self, conflict_id: str, resolution: str) -> dict[str, Any]:
        """Record a user-selected side, both alternatives, or dismissal."""
        allowed = {
            "keep_ours": "resolved_keep_ours",
            "keep_theirs": "resolved_keep_theirs",
            "keep_both": "resolved_keep_both",
            "dismiss": "dismissed",
        }
        if resolution not in allowed:
            raise ValueError("resolution must be keep_ours, keep_theirs, keep_both, or dismiss")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE merge_conflicts SET status = ?, resolution_json = ?, resolved_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (
                    allowed[resolution],
                    canonical_json({"choice": resolution}),
                    utc_now(),
                    conflict_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or already resolved conflict: {conflict_id}")
        return self._conflict_dict(conflict_id)

    def finalize_merge(self, conflict_id: str) -> str:
        """Complete a merge once all queued conflicts for the same heads are resolved."""
        conflict = self.connection.execute(
            "SELECT * FROM merge_conflicts WHERE id = ?", (conflict_id,)
        ).fetchone()
        if not conflict:
            raise KeyError(f"Unknown conflict: {conflict_id}")
        group = self.connection.execute(
            """
            SELECT * FROM merge_conflicts
            WHERE ours_hash = ? AND theirs_hash = ?
            """,
            (conflict["ours_hash"], conflict["theirs_hash"]),
        ).fetchall()
        if any(row["status"] == "open" for row in group):
            raise ValueError("Resolve every conflict for this merge before finalizing.")
        ours_row = self.repo.commit_row(str(conflict["ours_hash"]))
        theirs_row = self.repo.commit_row(str(conflict["theirs_hash"]))
        if not ours_row or not theirs_row:
            raise ValueError("Merge parent snapshot is missing.")
        ours_branch = str(ours_row["branch"])
        theirs_branch = str(theirs_row["branch"])
        target_branch = ours_branch
        ours_hash, theirs_hash = str(conflict["ours_hash"]), str(conflict["theirs_hash"])
        merged = self._member_map(ours_hash)
        for key, member in self._member_map(theirs_hash).items():
            merged.setdefault(key, member)
        for row in group:
            unit_ids = json.loads(row["unit_ids_json"])
            choice = json.loads(row["resolution_json"] or "{}").get("choice")
            if len(unit_ids) == 1:
                key = unit_ids[0]
                if choice == "keep_theirs" and key in self._member_map(theirs_hash):
                    merged[key] = self._member_map(theirs_hash)[key]
                elif choice in ("keep_ours", "dismiss"):
                    if key in self._member_map(ours_hash):
                        merged[key] = self._member_map(ours_hash)[key]
                    elif choice == "dismiss":
                        merged.pop(key, None)
            elif choice == "keep_ours":
                merged.pop(unit_ids[1], None)
            elif choice == "keep_theirs":
                merged.pop(unit_ids[0], None)
            elif choice == "dismiss":
                merged.pop(unit_ids[0], None)
                merged.pop(unit_ids[1], None)
        commit_hash = self._create_commit(
            target_branch,
            f"Merge {theirs_branch} into {ours_branch} after conflict resolution",
            None,
            self._visible_tokens(list(self._member_map(ours_hash).values())),
            list(merged.values()),
            [],
            parents=(ours_hash, theirs_hash),
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE merge_conflicts SET merge_commit_hash = ?
                WHERE ours_hash = ? AND theirs_hash = ?
                """,
                (commit_hash, ours_hash, theirs_hash),
            )
        return commit_hash

    def list_conflicts(self, open_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM merge_conflicts"
        if open_only:
            sql += " WHERE status = 'open'"
        sql += " ORDER BY created_at DESC"
        return [
            self._conflict_dict(str(row["id"]))
            for row in self.connection.execute(sql)
        ]

    def _create_commit(
        self,
        branch: str,
        message: str,
        topic: str | None,
        tokens_before: int,
        memberships: list[MemoryMembership],
        decisions: list[MemoryDecision],
        parents: tuple[str, ...] | None = None,
    ) -> str:
        commit_hash = self.repo.create_commit(
            branch,
            message,
            topic,
            tokens_before,
            memberships,
            decisions,
            parents=parents,
        )
        self.run_canaries(commit_hash)
        return commit_hash

    def _member_map(self, commit_hash: str) -> dict[str, MemoryMembership]:
        return {
            member.item_id: member
            for member in self.repo.commit_membership(commit_hash)
        }

    def _visible_tokens(self, memberships: Iterable[MemoryMembership]) -> int:
        return sum(item.token_count for item in memberships if item.tier != MemoryTier.COLD)

    def _membership_readable(self, item: MemoryMembership) -> bool:
        if item.item_kind == "unit":
            return self.repo.get_unit(item.item_id, item.revision_id) is not None
        summary = self.repo.get_summary(item.item_id.removeprefix("summary:"))
        return bool(summary and summary["text"] != "[cryptographically shredded]")

    def _stored_decisions(self, commit_hash: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT unit_id, action, reason, score_breakdown_json, tier_before,
                   tier_after, tokens_before, tokens_after, summary_id
            FROM commit_decisions WHERE commit_hash = ? ORDER BY unit_id
            """,
            (commit_hash,),
        ).fetchall()
        result = []
        for row in rows:
            score = json.loads(row["score_breakdown_json"])
            result.append(
                {
                    "unit_id": row["unit_id"],
                    "action": row["action"],
                    "reason": row["reason"],
                    "score": score,
                    "tier_before": row["tier_before"],
                    "tier_after": row["tier_after"],
                    "tokens_before": row["tokens_before"],
                    "tokens_after": row["tokens_after"],
                    "summary_id": row["summary_id"],
                }
            )
        return result

    def _commit_has_failed_canary(self, commit_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM canary_runs WHERE commit_hash = ? AND passed = 0 LIMIT 1",
            (commit_hash,),
        ).fetchone()
        return bool(row)

    def _commit_canaries_pass(self, commit_hash: str) -> bool:
        probes = self.connection.execute(
            "SELECT COUNT(*) AS count FROM canary_probes WHERE enabled = 1"
        ).fetchone()
        if not probes or not probes["count"]:
            return True
        rows = self.connection.execute(
            "SELECT passed FROM canary_runs WHERE commit_hash = ?",
            (commit_hash,),
        ).fetchall()
        return len(rows) == int(probes["count"]) and all(bool(row["passed"]) for row in rows)

    def _unit_original_hash(self, unit_id: str) -> str:
        row = self.connection.execute(
            "SELECT original_sha256 FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        if not row:
            raise KeyError(unit_id)
        return str(row["original_sha256"])

    def _merge_base(self, ours: str, theirs: str) -> str | None:
        def ancestors(start: str) -> dict[str, int]:
            found: dict[str, int] = {}
            queue = deque([(start, 0)])
            while queue:
                current, depth = queue.popleft()
                if current in found and found[current] <= depth:
                    continue
                found[current] = depth
                row = self.repo.commit_row(current)
                if row:
                    for parent in (row["first_parent_hash"], row["second_parent_hash"]):
                        if parent:
                            queue.append((str(parent), depth + 1))
            return found

        left, right = ancestors(ours), ancestors(theirs)
        shared = left.keys() & right.keys()
        return min(shared, key=lambda item: left[item] + right[item]) if shared else None

    def _record_conflict(
        self,
        base_hash: str | None,
        ours_hash: str,
        theirs_hash: str,
        unit_ids: list[str],
        reason: str,
    ) -> str:
        conflict_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO merge_conflicts(
                    id, base_hash, ours_hash, theirs_hash, unit_ids_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict_id,
                    base_hash,
                    ours_hash,
                    theirs_hash,
                    canonical_json(unit_ids),
                    reason,
                    utc_now(),
                ),
            )
        return conflict_id

    def _conflict_dict(self, conflict_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM merge_conflicts WHERE id = ?", (conflict_id,)
        ).fetchone()
        if not row:
            raise KeyError(conflict_id)
        result = dict(row)
        result["unit_ids"] = json.loads(result.pop("unit_ids_json"))
        result["resolution"] = json.loads(result["resolution_json"]) if result["resolution_json"] else None
        return result

    @staticmethod
    def _contradictory(left: str, right: str, left_topic: str, right_topic: str) -> bool:
        if left_topic.casefold() != right_topic.casefold() and lexical_similarity(left, right) < 0.2:
            return False
        left_neg, right_neg = bool(_NEGATION.search(left)), bool(_NEGATION.search(right))
        shared = lexical_similarity(left, right) >= 0.18
        if shared and left_neg != right_neg:
            return True
        left_dates, right_dates = _DATE_WORD.findall(left), _DATE_WORD.findall(right)
        date_topic = re.search(r"\b(deadline|launch|ship|release|delivery|due)\b", left + right, re.I)
        if shared and date_topic and left_dates and right_dates:
            return {date.casefold() for date in left_dates} != {date.casefold() for date in right_dates}
        phrases = [
            ("required", "optional"),
            ("must", "may"),
            ("include", "exclude"),
            ("public", "private"),
            ("online", "offline"),
        ]
        lowered_left, lowered_right = left.casefold(), right.casefold()
        return shared and any(
            (one in lowered_left and two in lowered_right)
            or (two in lowered_left and one in lowered_right)
            for one, two in phrases
        )

    @staticmethod
    def _supersession(left: MemoryUnit, right: MemoryUnit) -> tuple[MemoryUnit, MemoryUnit] | None:
        move_phrase = re.compile(r"\b(moved|changed|updated|corrected|now)\b", re.I)
        left_dates, right_dates = _DATE_WORD.findall(left.text), _DATE_WORD.findall(right.text)
        if not left_dates or not right_dates or {v.casefold() for v in left_dates} == {v.casefold() for v in right_dates}:
            return None
        if not move_phrase.search(left.text) and not move_phrase.search(right.text):
            return None
        try:
            left_time = datetime.fromisoformat(left.timestamp.replace("Z", "+00:00"))
            right_time = datetime.fromisoformat(right.timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (left, right) if left_time > right_time else (right, left)

    def _record_relation(self, newer_id: str, older_id: str, relation: str, reason: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO unit_relations(
                    from_unit_id, to_unit_id, relation, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (newer_id, older_id, relation, reason, utc_now()),
            )


MemoryEngine = GitMemEngine
