"""Phase 1 tests for GitMem's schema, configuration, and domain contracts."""

from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path

from gitmem.config import load_config
from gitmem.database import apply_migrations, initialize_database
from gitmem.models import MemoryAction, MemoryTier, MemoryUnit, ScoreBreakdown, UnitType


class FoundationTests(unittest.TestCase):
    def test_config_loads_and_has_normalized_weights(self) -> None:
        config = load_config()
        weights = config.scoring_weights
        self.assertAlmostEqual(
            weights.importance
            + weights.relevance
            + weights.redundancy
            + weights.recency
            + weights.pin_boost,
            1.0,
        )
        self.assertEqual(config.embedding_model, "sentence-transformers/all-MiniLM-L6-v2")
        self.assertGreater(config.default_token_budget, 0)

    def test_initial_migration_creates_core_tables_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "gitmem.sqlite3"
            connection = initialize_database(db_path)
            try:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {
                        "sessions",
                        "units",
                        "unit_revisions",
                        "commits",
                        "commit_decisions",
                        "commit_membership",
                        "canary_probes",
                        "shred_tombstones",
                    }.issubset(names)
                )
                self.assertEqual(
                    apply_migrations(connection),
                    [],
                    "A second migration pass must not reapply schema changes",
                )
            finally:
                connection.close()

    def test_revision_and_commit_membership_constraints_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = initialize_database(Path(temp_dir) / "gitmem.sqlite3")
            try:
                connection.execute(
                    "INSERT INTO sessions(id, started_at) VALUES ('s1', '2026-10-19T00:00:00Z')"
                )
                connection.execute(
                    """
                    INSERT INTO units(
                        id, unit_type, created_at, session_id, token_count, original_sha256
                    ) VALUES ('u1', 'fact', '2026-10-19T00:00:00Z', 's1', 5, 'a')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO unit_revisions(
                        id, unit_id, revision_number, text, content_sha256, created_at
                    ) VALUES ('r1', 'u1', 1, 'Deadline is Friday', 'a', '2026-10-19T00:00:00Z')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO commits(
                        hash, branch, created_at, message, tokens_before, tokens_after, snapshot_hash
                    ) VALUES (?, 'main', '2026-10-19T00:00:00Z', 'initial', 5, 5, ?)
                    """,
                    ("a" * 64, "b" * 64),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO commit_membership(
                            commit_hash, item_id, item_kind, unit_revision_id, tier, token_count
                        ) VALUES (?, 'bad', 'unit', NULL, 'hot', 5)
                        """,
                        ("a" * 64,),
                    )
                connection.rollback()
            finally:
                connection.close()

    def test_domain_models_reject_invalid_values(self) -> None:
        unit = MemoryUnit(
            id="u1",
            unit_type=UnitType.FACT,
            text="The launch is on Friday.",
            topic="launch",
            timestamp="2026-10-19T00:00:00Z",
            session_id="s1",
            token_count=6,
        )
        score = ScoreBreakdown(0.8, 0.7, 0.1, 0.9, 0.0, 0.72)
        self.assertEqual(unit.unit_type, UnitType.FACT)
        self.assertEqual(MemoryAction.KEEP_VERBATIM.value, "KEEP_VERBATIM")
        self.assertEqual(MemoryTier.PINNED.value, "pinned")
        self.assertEqual(score.total, 0.72)
        with self.assertRaises(ValueError):
            MemoryUnit(
                id="",
                unit_type=UnitType.TURN,
                text="",
                topic="",
                timestamp="2026-10-19T00:00:00Z",
                session_id="s1",
                token_count=-1,
            )


if __name__ == "__main__":
    unittest.main()
