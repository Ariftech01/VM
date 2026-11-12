"""One-click, repeatable six-session demonstration of GitMem's core workflow."""

from __future__ import annotations

import json
import uuid
from typing import Any

from gitmem.models import MemoryAction, MemoryMembership, MemoryTier
from gitmem.store.repository import canonical_json, sha256_text, utc_now
from gitmem.versioning.engine import GitMemEngine

_SCENARIO_TAG = "gitmem-demo-v1"


def load_demo_scenario(engine: GitMemEngine) -> dict[str, Any]:
    """Seed six sessions and run corruption, canary, bisect, restore, branch, merge, and shred."""
    existing = engine.connection.execute(
        "SELECT commit_hash FROM tags WHERE name = ?", (_SCENARIO_TAG,)
    ).fetchone()
    if existing:
        return {
            "already_loaded": True,
            "commit_hash": str(existing["commit_hash"]),
            "message": "The GitMem demo scenario is already loaded in this database.",
        }

    sessions = (
        (
            {"role": "user", "topic": "project", "content": "Project Atlas is an offline AI memory planning pilot."},
            {"role": "user", "topic": "deadline", "content": "The launch deadline is 18 September 2027."},
        ),
        (
            {"role": "user", "topic": "preferences", "content": "I prefer compact summaries, but keep important source details available."},
            {"role": "assistant", "topic": "decisions", "content": "We decided to use SQLite for local storage."},
        ),
        (
            {"role": "user", "topic": "dashboard", "content": "The dashboard must show archived units and explain each memory decision."},
            {"role": "user", "topic": "security", "content": "A project contact is ria.project@example.test."},
        ),
        (
            {"role": "user", "topic": "deadline", "content": "Correction: the launch deadline moved to 25 September 2027."},
            {"role": "user", "topic": "constraints", "content": "Original memory must not be deleted during reversible compression."},
        ),
        (
            {"role": "assistant", "topic": "review", "content": "We will review the pilot after design feedback."},
            {"role": "user", "topic": "retrieval", "content": "Archived units can be rehydrated individually without restoring the whole memory."},
        ),
        (
            {"role": "user", "topic": "security", "content": "Never store passwords or API keys in active context."},
            {"role": "user", "topic": "evaluation", "content": "The benchmark must compare token use, cost, latency, factual accuracy, and recovery."},
        ),
    )
    session_commits: list[str] = []
    for index, messages in enumerate(sessions, start=1):
        result = engine.ingest(
            messages,
            session_id=f"gitmem-demo-session-{index}",
            current_goal="Project Atlas memory planning",
            session_label=f"Demo planning session {index}",
        )
        session_commits.append(result["commit_hash"])

    compression = engine.compress("Project Atlas memory planning")
    units = engine.repo.all_units()
    critical = next(
        unit for unit in units if "deadline moved to 25 September 2027" in unit.text
    )
    pii = next(unit for unit in units if "ria.project@example.test" in unit.text)
    requirement = next(
        unit for unit in units if "must show archived units" in unit.text
    )
    canary_id = engine.register_canary(
        critical.id,
        "What is the launch deadline?",
        "25 September 2027",
    )
    good_commit = engine.head("main")

    # Fault injection is deliberately outside the production summarization path:
    # it records a known-bad summary so the CI and recovery demo has a real failure.
    wrong_summary = "Project Atlas launch deadline is 26 September 2027."
    bad_summary_id = str(uuid.uuid4())
    current = engine._member_map(good_commit)
    current.pop(critical.id, None)
    for item_id in list(current):
        if not item_id.startswith("summary:"):
            continue
        summary_id = item_id.removeprefix("summary:")
        linked = engine.connection.execute(
            "SELECT 1 FROM summary_sources WHERE summary_id = ? AND unit_id = ?",
            (summary_id, critical.id),
        ).fetchone()
        if linked:
            current.pop(item_id)
    report = {
        "passed": False,
        "reason": "DEMO fault injection: wrong date deliberately bypassed the production faithfulness gate.",
        "introduced_numbers": ["26"],
        "missing_numbers": ["25"],
    }
    with engine.connection:
        engine.connection.execute(
            """
            INSERT INTO summaries(
                id, text, topic, token_count, content_sha256, created_at,
                faithfulness_passed, verifier_report_json
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                bad_summary_id,
                wrong_summary,
                critical.topic,
                engine.token_counter.count(wrong_summary),
                sha256_text(wrong_summary),
                utc_now(),
                canonical_json(report),
            ),
        )
        engine.connection.execute(
            """
            INSERT INTO summary_sources(summary_id, unit_id, source_order)
            VALUES (?, ?, 0)
            """,
            (bad_summary_id, critical.id),
        )
    current[f"summary:{bad_summary_id}"] = MemoryMembership(
        item_id=f"summary:{bad_summary_id}",
        item_kind="summary",
        tier=MemoryTier.WARM,
        token_count=engine.token_counter.count(wrong_summary),
    )
    score = engine.scorer.score(critical, "Project Atlas launch deadline", units)
    from gitmem.models import MemoryDecision

    bad_decision = MemoryDecision(
        unit_id=critical.id,
        action=MemoryAction.SUMMARIZE,
        reason="DEMO fault injection: deliberately incorrect summary introduced to exercise the canary.",
        score=score,
        tier_before=MemoryTier.HOT,
        tier_after=MemoryTier.WARM,
        tokens_before=critical.token_count,
        tokens_after=engine.token_counter.count(wrong_summary),
        summary_id=bad_summary_id,
    )
    fault_commit = engine._create_commit(
        "main",
        "DEMO fault injection: incorrect deadline summary",
        critical.topic,
        engine._visible_tokens(list(engine._member_map(good_commit).values())),
        list(current.values()),
        [bad_decision],
    )
    canary_alert = engine._commit_has_failed_canary(fault_commit)
    bisect_result = engine.bisect(
        "What is the launch deadline?",
        "25 September 2027",
        "main",
        good_commit=good_commit,
    )
    restored_commit = engine.restore(good_commit, "main")
    restored_answer = engine.answer("What is the launch deadline?")["answer"]

    # Force one low-value review turn into the cold tier, then rehydrate only it.
    rehydrate_candidate = next(
        (unit for unit in units if unit.unit_type.value == "turn" and unit.id != critical.id),
        None,
    )
    rehydrated_commit: str | None = None
    if rehydrate_candidate:
        engine.edit_unit(
            rehydrate_candidate.id,
            force_action=MemoryAction.ARCHIVE,
        )
        rehydrated_commit = engine.rehydrate([rehydrate_candidate.id], branch="main")

    branch_name = "demo/requirement-alternative"
    engine.create_branch(branch_name, engine.head("main"), "Contradictory requirement for merge review")
    engine.ingest(
        [
            {
                "role": "user",
                "topic": "dashboard",
                "content": "The dashboard must not show archived units.",
            }
        ],
        session_id="gitmem-demo-branch-session",
        current_goal="Project Atlas dashboard requirements",
        branch=branch_name,
        session_label="Alternative dashboard requirement",
    )
    merge_result = engine.merge("main", branch_name)
    shred_commit = engine.shred(pii.id, confirmed=True, branch="main")
    what_if_result = engine.what_if(
        "The dashboard must show archived units?", "main", branch_name
    )
    engine.tag(_SCENARIO_TAG, engine.head("main"), "Six-session GitMem demo checkpoint")
    return {
        "already_loaded": False,
        "session_commits": session_commits,
        "compression_commit": compression["commit_hash"],
        "canary_id": canary_id,
        "critical_unit_id": critical.id,
        "requirement_unit_id": requirement.id,
        "pii_unit_id": pii.id,
        "good_commit": good_commit,
        "fault_commit": fault_commit,
        "bad_summary_id": bad_summary_id,
        "canary_alert": canary_alert,
        "bisect": bisect_result,
        "restored_commit": restored_commit,
        "restored_answer": restored_answer,
        "rehydrated_unit_id": rehydrate_candidate.id if rehydrate_candidate else None,
        "rehydrated_commit": rehydrated_commit,
        "branch": branch_name,
        "merge": merge_result,
        "what_if": what_if_result,
        "shred_commit": shred_commit,
        "head": engine.head("main"),
        "message": "Demo loaded: compressed memory, injected a bad summary, ran canary and bisect, restored, rehydrated, queued a branch conflict, evaluated what-if replay, and shredded detected PII.",
    }
