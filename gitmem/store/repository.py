"""SQLite repository for source units, revisions, summaries, and commits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable

from gitmem.models import (
    MemoryAction,
    MemoryDecision,
    MemoryMembership,
    MemoryTier,
    MemoryUnit,
    ScoreBreakdown,
    UnitType,
)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def canonical_json(value: object) -> str:
    """Serialize JSON deterministically for hashes and persisted explanations."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    """Return a lowercase SHA-256 digest for UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MemoryRepository:
    """Persistence primitives over an already-migrated SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_session(self, session_id: str, label: str = "") -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO sessions(id, started_at, label)
            VALUES (?, ?, ?)
            """,
            (session_id, utc_now(), label),
        )
        self.connection.commit()

    def save_unit(self, unit: MemoryUnit, edit_reason: str = "ingest") -> str:
        """Save an original unit and its first immutable text revision."""
        metadata_json = canonical_json(unit.metadata)
        digest = sha256_text(unit.text)
        revision_id = str(uuid.uuid4())
        self.ensure_session(unit.session_id)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO units(
                    id, unit_type, topic, created_at, session_id, token_count,
                    pinned, metadata_json, original_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit.id,
                    unit.unit_type.value,
                    unit.topic,
                    unit.timestamp,
                    unit.session_id,
                    unit.token_count,
                    int(unit.pinned),
                    metadata_json,
                    digest,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO unit_revisions(
                    id, unit_id, revision_number, text, content_sha256,
                    created_at, edit_reason
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (revision_id, unit.id, unit.text, digest, utc_now(), edit_reason),
            )
        return revision_id

    def add_revision(self, unit_id: str, text: str, reason: str) -> str:
        """Append a new content revision without changing prior commit references."""
        row = self.connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision FROM unit_revisions WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
        if row is None or not self.connection.execute(
            "SELECT 1 FROM units WHERE id = ?", (unit_id,)
        ).fetchone():
            raise KeyError(f"Unknown memory unit: {unit_id}")
        revision_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO unit_revisions(
                    id, unit_id, revision_number, text, content_sha256,
                    created_at, edit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    unit_id,
                    int(row["next_revision"]),
                    text,
                    sha256_text(text),
                    utc_now(),
                    reason,
                ),
            )
        return revision_id

    def update_pinned(self, unit_id: str, pinned: bool) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE units SET pinned = ? WHERE id = ?", (int(pinned), unit_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown memory unit: {unit_id}")

    def get_unit(self, unit_id: str, revision_id: str | None = None) -> MemoryUnit | None:
        """Load unit metadata with a selected revision, or the latest revision."""
        if revision_id:
            row = self.connection.execute(
                """
                SELECT u.*, r.text AS revision_text, r.id AS revision_id
                FROM units u JOIN unit_revisions r ON r.unit_id = u.id
                WHERE u.id = ? AND r.id = ?
                """,
                (unit_id, revision_id),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT u.*, r.text AS revision_text, r.id AS revision_id
                FROM units u JOIN unit_revisions r ON r.unit_id = u.id
                WHERE u.id = ?
                ORDER BY r.revision_number DESC LIMIT 1
                """,
                (unit_id,),
            ).fetchone()
        if not row or row["revision_text"] is None:
            return None
        return self._unit_from_row(row)

    def unit_revision_id(self, unit_id: str) -> str:
        row = self.connection.execute(
            """
            SELECT id FROM unit_revisions WHERE unit_id = ?
            ORDER BY revision_number DESC LIMIT 1
            """,
            (unit_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown or shredded memory unit: {unit_id}")
        return str(row["id"])

    def all_units(self, include_shredded: bool = False) -> list[MemoryUnit]:
        """Return latest readable revisions for all source units."""
        query = """
            SELECT u.*, r.text AS revision_text, r.id AS revision_id
            FROM units u
            JOIN unit_revisions r ON r.unit_id = u.id
            WHERE r.revision_number = (
                SELECT MAX(r2.revision_number) FROM unit_revisions r2 WHERE r2.unit_id = u.id
            )
        """
        if not include_shredded:
            query += " AND r.text IS NOT NULL"
        query += " ORDER BY u.created_at, u.id"
        rows = self.connection.execute(query).fetchall()
        return [self._unit_from_row(row) for row in rows if row["revision_text"] is not None]

    def add_summary(
        self,
        text: str,
        topic: str,
        token_count: int,
        source_unit_ids: Iterable[str],
        verifier_report: dict[str, Any],
    ) -> str:
        """Persist a verified summary and its ordered source-unit links."""
        summary_id = str(uuid.uuid4())
        source_ids = list(dict.fromkeys(source_unit_ids))
        if not source_ids:
            raise ValueError("A summary must be linked to at least one source unit")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO summaries(
                    id, text, topic, token_count, content_sha256, created_at,
                    faithfulness_passed, verifier_report_json
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    summary_id,
                    text,
                    topic,
                    token_count,
                    sha256_text(text),
                    utc_now(),
                    canonical_json(verifier_report),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO summary_sources(summary_id, unit_id, source_order)
                VALUES (?, ?, ?)
                """,
                [(summary_id, unit_id, index) for index, unit_id in enumerate(source_ids)],
            )
        return summary_id

    def get_summary(self, summary_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM summaries WHERE id = ?", (summary_id,)
        ).fetchone()

    def commit_membership(self, commit_hash: str) -> list[MemoryMembership]:
        rows = self.connection.execute(
            """
            SELECT item_id, item_kind, tier, token_count, unit_revision_id
            FROM commit_membership WHERE commit_hash = ? ORDER BY position, item_id
            """,
            (commit_hash,),
        ).fetchall()
        return [
            MemoryMembership(
                item_id=str(row["item_id"]),
                item_kind=str(row["item_kind"]),
                tier=MemoryTier(row["tier"]),
                token_count=int(row["token_count"]),
                revision_id=row["unit_revision_id"],
            )
            for row in rows
        ]

    def membership_rows(self, commit_hash: str) -> list[dict[str, Any]]:
        """Return snapshot rows together with content hashes, never plaintext."""
        rows = self.connection.execute(
            """
            SELECT m.item_id, m.item_kind, m.unit_revision_id, m.summary_id,
                   m.tier, m.token_count, m.position,
                   COALESCE(r.content_sha256, s.content_sha256) AS content_sha256
            FROM commit_membership m
            LEFT JOIN unit_revisions r ON r.id = m.unit_revision_id
            LEFT JOIN summaries s ON s.id = m.summary_id
            WHERE m.commit_hash = ?
            ORDER BY m.item_kind, m.item_id
            """,
            (commit_hash,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_membership_text(self, membership: MemoryMembership) -> str | None:
        if membership.item_kind == "unit":
            if membership.revision_id is None:
                return None
            row = self.connection.execute(
                "SELECT text FROM unit_revisions WHERE id = ?", (membership.revision_id,)
            ).fetchone()
            return str(row["text"]) if row and row["text"] is not None else None
        summary_id = membership.item_id.removeprefix("summary:")
        row = self.get_summary(summary_id)
        return str(row["text"]) if row else None

    def commit_row(self, commit_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM commits WHERE hash = ?", (commit_hash,)
        ).fetchone()

    def branch_head(self, branch: str) -> str | None:
        row = self.connection.execute(
            "SELECT head_hash FROM branches WHERE name = ?", (branch,)
        ).fetchone()
        return str(row["head_hash"]) if row and row["head_hash"] else None

    def branch_names(self) -> list[str]:
        return [
            str(row["name"])
            for row in self.connection.execute("SELECT name FROM branches ORDER BY name")
        ]

    def create_branch(self, name: str, commit_hash: str, description: str = "") -> None:
        if not name.strip():
            raise ValueError("Branch name cannot be empty")
        if not self.commit_row(commit_hash):
            raise KeyError(f"Unknown commit: {commit_hash}")
        with self.connection:
            self.connection.execute(
                "INSERT INTO branches(name, head_hash, created_at, description) VALUES (?, ?, ?, ?)",
                (name, commit_hash, utc_now(), description),
            )

    def create_commit(
        self,
        branch: str,
        message: str,
        topic: str | None,
        tokens_before: int,
        memberships: list[MemoryMembership],
        decisions: list[MemoryDecision],
        parents: tuple[str, ...] | None = None,
        update_branch: bool = True,
    ) -> str:
        """Persist a content-addressed snapshot, decisions, and parent links."""
        if len(parents or ()) > 2:
            raise ValueError("A commit may have at most two parents")
        parent_hashes = parents if parents is not None else (
            (self.branch_head(branch),) if self.branch_head(branch) else ()
        )
        first_parent = parent_hashes[0] if parent_hashes else None
        second_parent = parent_hashes[1] if len(parent_hashes) > 1 else None
        for parent in parent_hashes:
            if not self.commit_row(parent):
                raise KeyError(f"Unknown parent commit: {parent}")
        if not self.connection.execute(
            "SELECT 1 FROM branches WHERE name = ?", (branch,)
        ).fetchone():
            with self.connection:
                self.connection.execute(
                    "INSERT INTO branches(name, head_hash, created_at) VALUES (?, ?, ?)",
                    (branch, first_parent, utc_now()),
                )

        created_at = utc_now()
        member_payload: list[dict[str, Any]] = []
        for position, item in enumerate(memberships):
            if item.item_kind not in ("unit", "summary"):
                raise ValueError(f"Unsupported membership kind: {item.item_kind}")
            revision_id = item.revision_id if item.item_kind == "unit" else None
            summary_id = (
                item.item_id.removeprefix("summary:")
                if item.item_kind == "summary"
                else None
            )
            hash_row = self.connection.execute(
                "SELECT content_sha256 FROM unit_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone() if revision_id else None
            if summary_id:
                hash_row = self.connection.execute(
                    "SELECT content_sha256 FROM summaries WHERE id = ?", (summary_id,)
                ).fetchone()
            if not hash_row:
                raise KeyError(f"Missing content for snapshot item {item.item_id}")
            member_payload.append(
                {
                    "item_id": item.item_id,
                    "item_kind": item.item_kind,
                    "unit_revision_id": revision_id,
                    "summary_id": summary_id,
                    "tier": item.tier.value,
                    "token_count": item.token_count,
                    "position": position,
                    "content_sha256": str(hash_row["content_sha256"]),
                }
            )
        member_payload.sort(key=lambda item: (item["item_kind"], item["item_id"]))
        snapshot_hash = sha256_text(canonical_json(member_payload))
        tokens_after = sum(
            item.token_count for item in memberships if item.tier != MemoryTier.COLD
        )
        decision_payload = sorted(
            (self._decision_dict(decision) for decision in decisions),
            key=lambda item: item["unit_id"],
        )
        payload = {
            "parents": list(parent_hashes),
            "branch": branch,
            "created_at": created_at,
            "message": message,
            "topic": topic,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "snapshot_hash": snapshot_hash,
            "decisions": decision_payload,
            "membership": member_payload,
        }
        payload_json = canonical_json(payload)
        commit_hash = sha256_text(payload_json)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO commits(
                    hash, first_parent_hash, second_parent_hash, branch, created_at,
                    message, topic, tokens_before, tokens_after, snapshot_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_hash,
                    first_parent,
                    second_parent,
                    branch,
                    created_at,
                    message,
                    topic,
                    tokens_before,
                    tokens_after,
                    snapshot_hash,
                    payload_json,
                ),
            )
            for item in member_payload:
                self.connection.execute(
                    """
                    INSERT INTO commit_membership(
                        commit_hash, item_id, item_kind, unit_revision_id, summary_id,
                        tier, token_count, position
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        commit_hash,
                        item["item_id"],
                        item["item_kind"],
                        item["unit_revision_id"],
                        item["summary_id"],
                        item["tier"],
                        item["token_count"],
                        item["position"],
                    ),
                )
            for decision in decisions:
                self.connection.execute(
                    """
                    INSERT INTO commit_decisions(
                        commit_hash, unit_id, action, reason, score_breakdown_json,
                        tier_before, tier_after, tokens_before, tokens_after, summary_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        commit_hash,
                        decision.unit_id,
                        decision.action.value,
                        decision.reason,
                        canonical_json(decision.score.__dict__ if hasattr(decision.score, "__dict__") else {
                            "importance": decision.score.importance,
                            "relevance": decision.score.relevance,
                            "redundancy_penalty": decision.score.redundancy_penalty,
                            "recency": decision.score.recency,
                            "pin_boost": decision.score.pin_boost,
                            "total": decision.score.total,
                        }),
                        decision.tier_before.value if decision.tier_before else None,
                        decision.tier_after.value,
                        decision.tokens_before,
                        decision.tokens_after,
                        decision.summary_id,
                    ),
                )
            if update_branch:
                self.connection.execute(
                    "UPDATE branches SET head_hash = ? WHERE name = ?",
                    (commit_hash, branch),
                )
        return commit_hash

    @staticmethod
    def _decision_dict(decision: MemoryDecision) -> dict[str, Any]:
        return {
            "unit_id": decision.unit_id,
            "action": decision.action.value,
            "reason": decision.reason,
            "score": {
                "importance": decision.score.importance,
                "relevance": decision.score.relevance,
                "redundancy_penalty": decision.score.redundancy_penalty,
                "recency": decision.score.recency,
                "pin_boost": decision.score.pin_boost,
                "total": decision.score.total,
            },
            "tier_before": decision.tier_before.value if decision.tier_before else None,
            "tier_after": decision.tier_after.value,
            "tokens_before": decision.tokens_before,
            "tokens_after": decision.tokens_after,
            "summary_id": decision.summary_id,
        }

    @staticmethod
    def _unit_from_row(row: sqlite3.Row) -> MemoryUnit:
        return MemoryUnit(
            id=str(row["id"]),
            unit_type=UnitType(row["unit_type"]),
            text=str(row["revision_text"]),
            topic=str(row["topic"]),
            timestamp=str(row["created_at"]),
            session_id=str(row["session_id"]),
            token_count=int(row["token_count"]),
            pinned=bool(row["pinned"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def close(self) -> None:
        self.connection.close()
