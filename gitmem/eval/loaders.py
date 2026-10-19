"""Local-only adapters for LongMemEval/LoCoMo-style JSON subsets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gitmem.eval.synthetic import GroundTruthQuestion, SyntheticDataset


def load_local_subset(
    path: str | Path,
    dataset_name: str,
    license_confirmed: bool = False,
) -> SyntheticDataset:
    """Load a locally available JSON/JSONL subset after the caller confirms its license."""
    if not license_confirmed:
        raise PermissionError(
            "Confirm that you have permission to use this local dataset before evaluation."
        )
    dataset_path = Path(path)
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Dataset file not found: {dataset_path}")
    raw_text = dataset_path.read_text(encoding="utf-8")
    try:
        raw: Any = json.loads(raw_text)
    except json.JSONDecodeError:
        records = []
        for line_number, line in enumerate(raw_text.splitlines(), start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL at line {line_number}") from exc
        raw = records
    if isinstance(raw, dict):
        records = raw.get("data", raw.get("records", [raw]))
    elif isinstance(raw, list):
        records = raw
    else:
        raise ValueError("Dataset JSON must be an object or array of records")
    if not isinstance(records, list) or not records:
        raise ValueError("Dataset contains no records")

    sessions: list[tuple[dict[str, Any], ...]] = []
    questions: list[GroundTruthQuestion] = []
    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        source_sessions = (
            record.get("sessions")
            or record.get("haystack_sessions")
            or record.get("conversation")
            or []
        )
        sessions.extend(_normalize_sessions(source_sessions, record_index))
        raw_questions = record.get("questions") or record.get("qa") or []
        if not raw_questions and record.get("question") and record.get("answer") is not None:
            raw_questions = [record]
        for qa in raw_questions:
            if not isinstance(qa, dict):
                continue
            question = qa.get("question") or qa.get("query")
            answer = qa.get("answer") or qa.get("expected_answer")
            if isinstance(answer, list):
                answer = answer[0] if answer else ""
            if not isinstance(question, str) or not isinstance(answer, (str, int, float)):
                continue
            expected = str(answer).strip()
            if not expected:
                continue
            evidence = qa.get("evidence") or qa.get("critical_terms") or []
            if isinstance(evidence, str):
                evidence_terms = (evidence,)
            elif isinstance(evidence, list):
                evidence_terms = tuple(
                    str(item.get("text", item)) if isinstance(item, dict) else str(item)
                    for item in evidence
                )
            else:
                evidence_terms = (expected,)
            questions.append(
                GroundTruthQuestion(str(question), expected, evidence_terms or (expected,))
            )
    if not sessions or not questions:
        raise ValueError(
            "Could not identify sessions and question/answer pairs. "
            "Expected sessions/haystack_sessions/conversation and questions/qa fields."
        )
    return SyntheticDataset(f"{dataset_name}:{dataset_path.name}", tuple(sessions), tuple(questions))


def _normalize_sessions(value: Any, record_index: int) -> list[tuple[dict[str, Any], ...]]:
    if isinstance(value, dict):
        sessions = list(value.values())
    elif isinstance(value, list):
        sessions = value
    else:
        return []
    normalized: list[tuple[dict[str, Any], ...]] = []
    for session_index, session in enumerate(sessions):
        turns = session
        if isinstance(session, dict):
            turns = session.get("messages") or session.get("turns") or session.get("dialogue") or []
        if isinstance(turns, dict):
            turns = list(turns.values())
        if not isinstance(turns, list):
            continue
        messages: list[dict[str, Any]] = []
        for turn in turns:
            if isinstance(turn, str):
                content, role = turn, "user"
            elif isinstance(turn, dict):
                content = (
                    turn.get("content")
                    or turn.get("text")
                    or turn.get("utterance")
                    or turn.get("dialogue")
                    or ""
                )
                role = str(turn.get("role") or turn.get("speaker") or "user")
                if isinstance(content, dict):
                    content = content.get("text", "")
            else:
                continue
            if isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        if messages:
            normalized.append(tuple(messages))
    return normalized
