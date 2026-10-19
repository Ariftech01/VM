from __future__ import annotations

import json
from pathlib import Path

import pytest

from gitmem.context.builder import ContextCandidate, build_context
from gitmem.eval.benchmarks import run_benchmark
from gitmem.eval.loaders import load_local_subset
from gitmem.faithfulness import verify_summary
from gitmem.llm.mock import MockLLM
from gitmem.models import MemoryAction, MemoryTier
from gitmem.tokenizer import TokenCounter
from gitmem.versioning.engine import GitMemEngine


@pytest.fixture
def engine(tmp_path: Path):
    instance = GitMemEngine(tmp_path / "gitmem.sqlite3")
    yield instance
    instance.close()


def message(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def test_summary_gate_catches_omitted_dates_and_negation() -> None:
    report = verify_summary(
        "The launch date is 18 September 2027. Do not store API keys.",
        "The launch date is 18 September 2027. Store API keys.",
    )
    assert not report.passed
    assert report.missing_negations == ("not",)


def test_knapsack_selects_maximum_value_with_stable_ties() -> None:
    counter = TokenCounter()
    candidates = [
        ContextCandidate("a", "one two three four", 4, MemoryTier.HOT, 5.0, "fact"),
        ContextCandidate("b", "five six", 2, MemoryTier.WARM, 4.0, "fact"),
        ContextCandidate("c", "seven eight", 2, MemoryTier.WARM, 4.0, "fact"),
    ]
    selected, explanation = build_context("unrelated", candidates, 4, counter)
    assert {item.id for item in selected} == {"b", "c"}
    assert explanation.tokens_used == 4
    assert "budget" in explanation.explanations["a"].lower()


def test_restore_is_whole_snapshot_and_rehydrate_is_selective(engine: GitMemEngine) -> None:
    first = engine.ingest(message("Project name is Atlas."), "first")
    first_commit = first["commit_hash"]
    second = engine.ingest(message("The service target is 99.9 percent availability."), "second")
    second_unit = second["unit_ids"][0]
    engine.edit_unit(second_unit, force_action=MemoryAction.ARCHIVE)

    restored = engine.restore(first_commit)
    restored_ids = {item.item_id for item in engine.repo.commit_membership(restored)}
    assert restored_ids == set(first["unit_ids"])

    rehydrated = engine.rehydrate([second_unit])
    memberships = {
        item.item_id: item for item in engine.repo.commit_membership(rehydrated)
    }
    assert set(memberships) == set(first["unit_ids"] + [second_unit])
    assert memberships[second_unit].tier == MemoryTier.HOT


def test_edit_invalidates_stale_summary_for_current_context(engine: GitMemEngine) -> None:
    ingested = engine.ingest(
        message("The project deadline is Friday and the owner is Sam."),
        "deadline",
        current_goal="track project deadline",
    )
    engine.compress("track project deadline")
    unit_id = ingested["unit_ids"][0]
    engine.edit_unit(unit_id, text="The project deadline is Monday and the owner is Sam.")
    active = engine.repo.commit_membership(engine.head())
    summary_ids = {item.item_id for item in active if item.item_kind == "summary"}
    linked_summaries = {
        str(row["summary_id"])
        for row in engine.connection.execute(
            "SELECT summary_id FROM summary_sources WHERE unit_id = ?", (unit_id,)
        )
    }
    assert not summary_ids.intersection(f"summary:{summary_id}" for summary_id in linked_summaries)


def test_canary_bisect_and_auto_revert_recover_a_bad_edit(engine: GitMemEngine) -> None:
    ingested = engine.ingest(message("The delivery deadline is Friday."), "deadline")
    good_commit = ingested["commit_hash"]
    unit_id = ingested["unit_ids"][0]
    engine.register_canary(unit_id, "What is the delivery deadline?", "Friday")

    bad_commit = engine.edit_unit(
        unit_id, text="The delivery deadline is Monday."
    )
    assert engine._commit_has_failed_canary(bad_commit)
    bisect = engine.bisect(
        "What is the delivery deadline?",
        "Friday",
        good_commit=good_commit,
    )
    assert bisect["found"]
    assert bisect["commit_hash"] == bad_commit

    recovered = engine.auto_revert_failed_canary()
    assert recovered != good_commit
    assert engine.canary_results(recovered)[0]["passed"] == 1
    assert "Friday" in engine.answer(
        "What is the delivery deadline?", record=False
    )["answer"]


def test_branch_merge_queues_and_finalizes_conflicts(engine: GitMemEngine) -> None:
    engine.ingest(
        message("The dashboard must show archived memories in the archive view."),
        "requirements",
    )
    engine.create_branch("experiment")
    engine.ingest(
        message("The dashboard must not show archived memories in the archive view."),
        "requirements-branch",
        branch="experiment",
    )
    result = engine.merge("main", "experiment")
    assert not result["merged"]
    assert result["conflicts"]
    assert all(item["status"] == "open" for item in result["conflicts"])

    for conflict in result["conflicts"]:
        engine.resolve_conflict(conflict["id"], "keep_ours")
    merge_commit = engine.finalize_merge(result["conflicts"][0]["id"])
    row = engine.repo.commit_row(merge_commit)
    assert row["second_parent_hash"] == engine.head("experiment")
    assert engine.verify_integrity()["valid"]


def test_crypto_shred_removes_pii_from_revisions_and_preserves_hash_audit(
    engine: GitMemEngine,
) -> None:
    ingested = engine.ingest(
        message("Contact is alex@example.com for the private account update."),
        "private",
    )
    unit_id = ingested["unit_ids"][0]
    with pytest.raises(PermissionError):
        engine.shred(unit_id, confirmed=False)
    engine.shred(unit_id, confirmed=True)

    stored_text = [
        str(row["text"])
        for row in engine.connection.execute(
            "SELECT text FROM unit_revisions WHERE unit_id = ?", (unit_id,)
        )
        if row["text"] is not None
    ]
    assert not any("alex@example.com" in text for text in stored_text)
    assert engine.connection.execute(
        "SELECT 1 FROM shred_tombstones WHERE unit_id = ?", (unit_id,)
    ).fetchone()
    assert engine.verify_integrity()["valid"]


def test_commit_payload_tampering_is_detected(engine: GitMemEngine) -> None:
    commit_hash = engine.ingest(message("A harmless project fact."), "test")["commit_hash"]
    engine.connection.execute(
        "UPDATE commits SET message = ? WHERE hash = ?",
        ("tampered message", commit_hash),
    )
    result = engine.verify_integrity()
    assert not result["valid"]
    assert result["problems"]


def test_local_dataset_loader_requires_license_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "subset.jsonl"
    source.write_text(
        json.dumps(
            {
                "sessions": [[{"speaker": "user", "text": "The city is Pune."}]],
                "questions": [{"question": "Which city?", "answer": "Pune"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PermissionError):
        load_local_subset(source, "LoCoMo")
    dataset = load_local_subset(source, "LoCoMo", license_confirmed=True)
    assert dataset.name.startswith("LoCoMo:")
    assert dataset.questions[0].expected_answer == "Pune"
    assert dataset.sessions[0][0]["content"] == "The city is Pune."


def test_mock_llm_is_deterministic_and_can_abstain() -> None:
    model = MockLLM()
    first = model.answer("Which project?", ["Project Atlas is the planning pilot."])
    second = model.answer("Which project?", ["Project Atlas is the planning pilot."])
    missing = model.answer("What is the launch date?", ["The chosen database is SQLite."])
    assert first.text == second.text
    assert first.text == "Project Atlas is the planning pilot."
    assert "could not find" in missing.text


def test_benchmark_writes_all_baseline_reports_and_recovers(tmp_path: Path) -> None:
    result = run_benchmark(tmp_path / "reports")
    assert {row["strategy"] for row in result["rows"]} == {
        "full-history",
        "sliding-window",
        "rolling-summary",
        "retrieval-based",
        "GitMem",
    }
    gitmem_row = next(row for row in result["rows"] if row["strategy"] == "GitMem")
    assert gitmem_row["recovery_success"]
    assert gitmem_row["factual_answer_accuracy"] == 1.0
    assert Path(result["csv_path"]).is_file()
    assert Path(result["markdown_path"]).is_file()
    assert all(Path(path).is_file() for path in result["chart_paths"])
