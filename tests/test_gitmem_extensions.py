from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gitmem.cli import main as cli_main
from gitmem.eval.benchmarks import calculate_baseline_savings
from gitmem.models import MemoryAction, MemoryTier
from gitmem.versioning.engine import GitMemEngine


@pytest.fixture
def test_engine(tmp_path: Path):
    db_file = tmp_path / "gitmem_test.sqlite3"
    instance = GitMemEngine(db_file)
    yield instance
    instance.close()


def make_turn(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


# ---------------------------------------------------------------------------
# 1. What-If Replay Tests
# ---------------------------------------------------------------------------

def test_what_if_compares_snapshots_and_does_not_mutate_state(test_engine: GitMemEngine) -> None:
    first_ingest = test_engine.ingest(
        make_turn("The sprint release target is Version 1.0 on Friday."), "sprint"
    )
    first_commit = first_ingest["commit_hash"]
    initial_commit_count = test_engine.connection.execute("SELECT COUNT(*) FROM commits").fetchone()[0]

    # Create a branch with a modified requirement
    test_engine.create_branch("feature", first_commit)
    test_engine.ingest(
        make_turn("Correction: the sprint release target is Version 2.0 on Wednesday."),
        "sprint",
        branch="feature",
    )
    feature_commit = test_engine.head("feature")
    commit_count_before_replay = test_engine.connection.execute("SELECT COUNT(*) FROM commits").fetchone()[0]

    # Compare via branch names
    replay = test_engine.what_if("What is the sprint release target?", "main", "feature")
    assert replay["question"] == "What is the sprint release target?"
    assert replay["first"]["reference"] == "main"
    assert replay["second"]["reference"] == "feature"
    assert replay["first"]["commit_hash"] == first_commit
    assert replay["second"]["commit_hash"] == feature_commit
    assert replay["answers_differ"] is True
    assert "Version 1.0" in replay["first"]["answer"]
    assert "Version 2.0" in replay["second"]["answer"]
    assert replay["first"]["tokens"] > 0
    assert replay["second"]["tokens"] > 0

    # Ensure no commits were created and no answer_runs persisted
    commit_count_after_replay = test_engine.connection.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    assert commit_count_after_replay == commit_count_before_replay
    answer_runs = test_engine.connection.execute("SELECT COUNT(*) FROM answer_runs").fetchone()[0]
    assert answer_runs == 0


def test_what_if_rejects_empty_question_and_invalid_references(test_engine: GitMemEngine) -> None:
    test_engine.ingest(make_turn("Project initialized."), "init")
    with pytest.raises(ValueError, match="Question cannot be empty"):
        test_engine.what_if("   ", "main", "main")

    with pytest.raises(KeyError, match="Unknown commit, branch, or tag reference"):
        test_engine.what_if("What is the project?", "main", "non_existent_branch_xyz")

    with pytest.raises(ValueError, match="Reference cannot be empty"):
        test_engine.what_if("What is the project?", "", "main")


# ---------------------------------------------------------------------------
# 2. Tag and Cherry-Pick Tests
# ---------------------------------------------------------------------------

def test_tag_creation_and_resolution(test_engine: GitMemEngine) -> None:
    ingest = test_engine.ingest(make_turn("Initial deployment config."), "deploy")
    commit_hash = ingest["commit_hash"]

    test_engine.tag("v1.0-checkpoint", commit_hash, message="Release candidate 1")
    resolved = test_engine.resolve_ref("v1.0-checkpoint")
    assert resolved == commit_hash

    # Resolving short prefix also works
    prefix = commit_hash[:8]
    assert test_engine.resolve_ref(prefix) == commit_hash


def test_cherry_pick_copies_snapshot_items_safely(test_engine: GitMemEngine) -> None:
    first = test_engine.ingest(make_turn("Core architectural rule: use SQLite."), "arch")
    first_commit = first["commit_hash"]

    test_engine.create_branch("experiment", first_commit)
    exp = test_engine.ingest(
        make_turn("Experiment rule: add fast cache."),
        "cache",
        branch="experiment",
    )
    exp_commit = exp["commit_hash"]

    # Cherry pick from experiment back into main
    cherry_commit = test_engine.cherry_pick(exp_commit, branch="main")
    main_members = {m.item_id for m in test_engine.repo.commit_membership(cherry_commit)}
    assert set(exp["unit_ids"]).issubset(main_members)
    assert test_engine.verify_integrity()["valid"] is True


# ---------------------------------------------------------------------------
# 3. Comparative Savings & Zero Denominator Tests
# ---------------------------------------------------------------------------

def test_calculate_baseline_savings_handles_zero_and_missing_values() -> None:
    rows = [
        {
            "strategy": "full-history",
            "total_tokens": 1000,
            "input_tokens": 1000,
            "estimated_cost": 0.00015,
            "response_latency_ms": 1.5,
            "factual_answer_accuracy": 1.0,
            "critical_information_recall": 1.0,
            "recovery_success": False,
        },
        {
            "strategy": "zero-tokens-baseline",
            "total_tokens": 0,
            "input_tokens": 0,
            "estimated_cost": 0.0,
            "response_latency_ms": 0.2,
            "factual_answer_accuracy": 0.0,
            "critical_information_recall": 0.0,
            "recovery_success": False,
        },
        {
            "strategy": "GitMem",
            "total_tokens": 400,
            "input_tokens": 400,
            "estimated_cost": 0.00006,
            "response_latency_ms": 0.8,
            "factual_answer_accuracy": 1.0,
            "critical_information_recall": 1.0,
            "recovery_success": True,
        },
    ]

    savings = calculate_baseline_savings(rows, target_strategy="GitMem")
    assert len(savings) == 2

    fh = next(s for s in savings if s["baseline"] == "full-history")
    assert fh["tokens_saved"] == 600
    assert fh["tokens_saved_pct"] == 60.0  # 100 * (1000 - 400) / 1000
    assert fh["cost_saved"] == 0.00009
    assert fh["cost_saved_pct"] == 60.0
    assert fh["target_recovery"] is True
    assert fh["baseline_recovery"] is False

    # Zero denominator must not raise ZeroDivisionError and must yield None pct
    zb = next(s for s in savings if s["baseline"] == "zero-tokens-baseline")
    assert zb["tokens_saved"] == -400
    assert zb["tokens_saved_pct"] is None
    assert zb["cost_saved_pct"] is None


def test_calculate_baseline_savings_returns_empty_when_target_missing() -> None:
    rows = [{"strategy": "full-history", "total_tokens": 500}]
    assert calculate_baseline_savings(rows, target_strategy="GitMem") == []


# ---------------------------------------------------------------------------
# 4. CLI Subcommand Tests
# ---------------------------------------------------------------------------

def test_cli_what_if_and_error_handling(tmp_path: Path) -> None:
    db_file = tmp_path / "cli_test.sqlite3"
    engine = GitMemEngine(db_file)
    first = engine.ingest(make_turn("Alpha requirement is confirmed."), "alpha")
    first_commit = first["commit_hash"]
    engine.create_branch("beta-branch", first_commit)
    engine.ingest(make_turn("Beta requirement is confirmed."), "beta", branch="beta-branch")
    engine.close()

    # Successful CLI what-if call
    out = io.StringIO()
    with patch("sys.stdout", out):
        code = cli_main([
            "--db", str(db_file),
            "what-if",
            "What is confirmed?",
            "main",
            "beta-branch",
        ])
    assert code == 0
    payload = json.loads(out.getvalue())
    assert "question" in payload
    assert payload["first"]["reference"] == "main"
    assert payload["second"]["reference"] == "beta-branch"

    # Invalid reference returns code 1 and writes clean error to stderr
    err_out = io.StringIO()
    with patch("sys.stderr", err_out):
        fail_code = cli_main([
            "--db", str(db_file),
            "what-if",
            "What is confirmed?",
            "main",
            "missing_branch",
        ])
    assert fail_code == 1
    assert "gitmem error: 'Unknown commit, branch, or tag reference: missing_branch'" in err_out.getvalue()


def test_demo_scenario_executes_reliably_end_to_end(tmp_path: Path) -> None:
    db_file = tmp_path / "demo_test.sqlite3"
    engine = GitMemEngine(db_file)
    from gitmem.demo import load_demo_scenario
    result = load_demo_scenario(engine)
    assert result["already_loaded"] is False
    assert result["canary_alert"] is True
    assert result["bisect"]["found"] is True
    assert result["what_if"]["token_delta"] is not None
    assert result["merge"]["merged"] is False
    assert len(result["merge"]["conflicts"]) > 0
    assert engine.verify_integrity()["valid"] is True

    # Idempotent re-run
    again = load_demo_scenario(engine)
    assert again["already_loaded"] is True
    engine.close()
