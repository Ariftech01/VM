"""Measured comparisons against full-history and context-management baselines."""

from __future__ import annotations

import csv
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gitmem.config import load_config
from gitmem.eval.synthetic import GroundTruthQuestion, SyntheticDataset, generate_synthetic_dataset
from gitmem.llm.mock import MockLLM
from gitmem.retrieval.index import MemoryIndex
from gitmem.tokenizer import TokenCounter
from gitmem.versioning.engine import GitMemEngine


STRATEGIES = (
    "full-history",
    "sliding-window",
    "rolling-summary",
    "retrieval-based",
    "GitMem",
)


def run_benchmark(
    output_dir: str | Path = "generated-artifacts/gitmem-benchmark",
    dataset: SyntheticDataset | None = None,
    source_note: str | None = None,
) -> dict[str, Any]:
    """Run the synthetic suite and write measured CSV, Markdown, and chart outputs."""
    dataset = dataset or generate_synthetic_dataset()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    counter = TokenCounter()
    mock = MockLLM(counter)
    all_units: list[dict[str, str]] = []
    session_unit_ids: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="gitmem-eval-") as temp_dir:
        engine = GitMemEngine(Path(temp_dir) / "benchmark.sqlite3")
        for session_index, messages in enumerate(dataset.sessions, start=1):
            result = engine.ingest(
                messages,
                session_id=f"benchmark-session-{session_index}",
                current_goal="Project Atlas planning and memory requirements",
            )
            session_ids: list[str] = []
            for unit_id in result["unit_ids"]:
                unit = engine.repo.get_unit(unit_id)
                if unit:
                    all_units.append({"id": unit.id, "text": unit.text, "topic": unit.topic})
                    session_ids.append(unit.id)
            session_unit_ids.append(session_ids)
            engine.compress("Project Atlas planning and memory requirements")

        latest_units = engine.repo.all_units()
        correction = next(
            (unit for unit in latest_units if "moved to Friday" in unit.text),
            next((unit for unit in latest_units if "deadline" in unit.topic), None),
        )
        if correction:
            engine.register_canary(
                correction.id,
                "What is the delivery deadline?",
                "Friday",
            )

        retrieved_index = MemoryIndex(engine.config.embedding_model)
        retrieved_index.build({unit["id"]: unit["text"] for unit in all_units})
        metrics: dict[str, dict[str, Any]] = {
            strategy: {
                "strategy": strategy,
                "total_tokens": 0,
                "estimated_cost": 0.0,
                "response_latency_ms": 0.0,
                "factual_correct": 0,
                "critical_present": 0,
                "question_count": len(dataset.questions),
                "recovery_success": False,
                "details": [],
            }
            for strategy in STRATEGIES
        }

        for qa in dataset.questions:
            for strategy in STRATEGIES:
                context = _context_for(
                    strategy,
                    qa,
                    all_units,
                    retrieved_index,
                    engine,
                )
                response = mock.answer(qa.question, context)
                row = metrics[strategy]
                row["input_tokens"] = row.get("input_tokens", 0) + response.input_tokens
                row["output_tokens"] = row.get("output_tokens", 0) + response.output_tokens
                row["total_tokens"] += response.input_tokens
                row["response_latency_ms"] += response.latency_ms
                row["factual_correct"] += int(
                    qa.expected_answer.casefold() in response.text.casefold()
                )
                context_text = "\n".join(context).casefold()
                row["critical_present"] += int(
                    all(term.casefold() in context_text for term in qa.critical_terms)
                )
                row["details"].append(
                    {
                        "question": qa.question,
                        "expected": qa.expected_answer,
                        "answer": response.text,
                        "input_tokens": response.input_tokens,
                        "output_tokens": response.output_tokens,
                        "tokens": response.input_tokens,
                        "correct": qa.expected_answer.casefold() in response.text.casefold(),
                    }
                )

        # Deliberately corrupt one critical fact, verify the canary, then restore.
        original_commit = engine.head("main")
        recovery_success = False
        recovery_detail = "No deadline unit found for recovery test."
        if correction:
            engine.edit_unit(
                correction.id,
                text="Correction: the delivery deadline moved to Saturday.",
            )
            corrupted_commit = engine.head("main")
            detected = engine._commit_has_failed_canary(corrupted_commit)
            engine.restore(original_commit, "main")
            restored_answer = engine.answer(
                "What is the delivery deadline?", record=False
            )["answer"]
            recovery_success = detected and "Friday" in restored_answer
            recovery_detail = (
                f"corruption_flagged={detected}; restored_answer={restored_answer!r}"
            )
        metrics["GitMem"]["recovery_success"] = recovery_success
        metrics["GitMem"]["recovery_detail"] = recovery_detail
        engine.close()

    rows: list[dict[str, Any]] = []
    price = load_config().cost_per_1k_input_tokens
    for strategy in STRATEGIES:
        source = metrics[strategy]
        count = max(1, source["question_count"])
        rows.append(
            {
                "strategy": strategy,
                "input_tokens": source.get("input_tokens", source["total_tokens"]),
                "output_tokens": source.get("output_tokens", 0),
                "total_tokens": source["total_tokens"],
                "estimated_cost": round(source["total_tokens"] * price / 1000.0, 8),
                "response_latency_ms": round(source["response_latency_ms"], 4),
                "factual_answer_accuracy": round(source["factual_correct"] / count, 4),
                "critical_information_recall": round(source["critical_present"] / count, 4),
                "recovery_success": bool(source["recovery_success"]),
                "questions": count,
                "recovery_detail": source.get("recovery_detail", "This baseline has no version-restore operation."),
            }
        )

    savings = calculate_baseline_savings(rows, target_strategy="GitMem")
    csv_path = output / "benchmark-results.csv"
    savings_csv_path = output / "benchmark-savings.csv"
    markdown_path = output / "benchmark-results.md"
    _write_csv(csv_path, rows)
    if savings:
        _write_csv(savings_csv_path, savings)
    markdown_path.write_text(
        _markdown_report(rows, dataset, metrics, source_note, savings=savings), encoding="utf-8"
    )
    chart_paths = _write_charts(output, rows)
    return {
        "dataset": dataset.name,
        "rows": rows,
        "savings": savings,
        "csv_path": str(csv_path),
        "savings_csv_path": str(savings_csv_path),
        "markdown_path": str(markdown_path),
        "chart_paths": chart_paths,
        "retrieval_backend": retrieved_index.backend,
        "answers": {strategy: metrics[strategy]["details"] for strategy in STRATEGIES},
    }


def _context_for(
    strategy: str,
    question: GroundTruthQuestion,
    units: list[dict[str, str]],
    index: MemoryIndex,
    engine: GitMemEngine,
) -> list[str]:
    if strategy == "full-history":
        return [unit["text"] for unit in units]
    if strategy == "sliding-window":
        return [unit["text"] for unit in units[-5:]]
    if strategy == "rolling-summary":
        return [MockLLM().summarize("\n".join(unit["text"] for unit in units), max_sentences=4)]
    if strategy == "retrieval-based":
        ids = [item_id for item_id, _ in index.search(question.question, top_k=4)]
        by_id = {unit["id"]: unit["text"] for unit in units}
        return [by_id[item_id] for item_id in ids if item_id in by_id]
    selected, _ = engine.build_context(question.question, branch="main")
    return [item.text for item in selected]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def calculate_baseline_savings(
    rows: list[dict[str, Any]],
    target_strategy: str = "GitMem",
) -> list[dict[str, Any]]:
    """Compute token, cost, and latency delta/savings for target_strategy vs all other baselines.

    Formula for savings percentage: 100 * (baseline_val - target_val) / baseline_val.
    When baseline is zero, percentage is None (reported as 'N/A' or unavailable).
    """
    target = next((row for row in rows if row["strategy"] == target_strategy), None)
    if not target:
        return []

    comparisons: list[dict[str, Any]] = []
    for row in rows:
        if row["strategy"] == target_strategy:
            continue
        baseline_strategy = row["strategy"]
        b_tokens = row.get("input_tokens", row.get("total_tokens", 0))
        g_tokens = target.get("input_tokens", target.get("total_tokens", 0))
        token_savings = b_tokens - g_tokens
        token_savings_pct = (
            round(100.0 * token_savings / b_tokens, 2) if b_tokens > 0 else None
        )

        b_cost = row.get("estimated_cost", 0.0)
        g_cost = target.get("estimated_cost", 0.0)
        cost_savings = round(b_cost - g_cost, 8)
        cost_savings_pct = (
            round(100.0 * (b_cost - g_cost) / b_cost, 2) if b_cost > 0.0 else None
        )

        b_latency = row.get("response_latency_ms", 0.0)
        g_latency = target.get("response_latency_ms", 0.0)
        latency_delta = round(g_latency - b_latency, 4)

        comparisons.append(
            {
                "baseline": baseline_strategy,
                "target": target_strategy,
                "baseline_tokens": b_tokens,
                "target_tokens": g_tokens,
                "tokens_saved": token_savings,
                "tokens_saved_pct": token_savings_pct,
                "baseline_cost": b_cost,
                "target_cost": g_cost,
                "cost_saved": cost_savings,
                "cost_saved_pct": cost_savings_pct,
                "baseline_latency_ms": b_latency,
                "target_latency_ms": g_latency,
                "latency_delta_ms": latency_delta,
                "baseline_accuracy": row.get("factual_answer_accuracy", 0.0),
                "target_accuracy": target.get("factual_answer_accuracy", 0.0),
                "baseline_recall": row.get("critical_information_recall", 0.0),
                "target_recall": target.get("critical_information_recall", 0.0),
                "baseline_recovery": row.get("recovery_success", False),
                "target_recovery": target.get("recovery_success", False),
            }
        )
    return comparisons


def _markdown_report(
    rows: list[dict[str, Any]],
    dataset: SyntheticDataset,
    metrics: dict[str, dict[str, Any]],
    source_note: str | None = None,
    savings: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        f"# GitMem synthetic benchmark ({dataset.name})",
        "",
        "Results below are generated by running the deterministic synthetic suite locally. "
        "They are measurements for this run, not general performance guarantees.",
        "",
        "| Strategy | Input tokens | Estimated cost | Latency (ms) | Factual accuracy | Critical recall | Restore recovery |",
        "|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['strategy']} | {row['total_tokens']} | ${row['estimated_cost']:.8f} | "
            f"{row['response_latency_ms']:.4f} | {row['factual_answer_accuracy']:.1%} | "
            f"{row['critical_information_recall']:.1%} | {'yes' if row['recovery_success'] else 'no'} |"
        )

    if savings:
        lines.extend(
            [
                "",
                "## Comparative Savings vs Baselines",
                "",
                "Calculated as `100 * (Baseline - GitMem) / Baseline` for tokens and cost. "
                "A positive value indicates GitMem used fewer tokens or cost less.",
                "",
                "| Baseline | Baseline Tokens | GitMem Tokens | Tokens Saved | Token Savings % | Cost Saved | Baseline Accuracy | GitMem Accuracy |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for s in savings:
            pct_str = f"{s['tokens_saved_pct']:+.1f}%" if s['tokens_saved_pct'] is not None else "N/A"
            cost_str = f"${s['cost_saved']:+.8f}"
            lines.append(
                f"| {s['baseline']} | {s['baseline_tokens']} | {s['target_tokens']} | "
                f"{s['tokens_saved']:+d} | {pct_str} | {cost_str} | "
                f"{s['baseline_accuracy']:.1%} | {s['target_accuracy']:.1%} |"
            )

    lines.extend(
        [
            "",
            "## Method",
            "",
            f"- {len(dataset.sessions)} controlled sessions and {len(dataset.questions)} ground-truth questions.",
            f"- Dataset source: {source_note or 'synthetic generator included with GitMem'}.",
            "- Every strategy answers with the deterministic MockLLM; contexts are selected by each named baseline.",
            "- Input-token counts include the question and selected context, measured with the configured tokenizer adapter.",
            "- Estimated costs use the editable price in `gitmem/config/default.yaml`; these are estimates, not provider billing.",
            "- Latency is measured wall-clock time for the local deterministic answerer and includes ordinary runtime noise.",
            "- Recovery is tested by changing the deadline to Saturday, checking the canary, restoring the saved commit, and checking the answer.",
            "- The four baselines have no version restore operation, so their recovery-success value is false by design.",
            "",
            "## Recovery check",
            "",
            metrics["GitMem"].get("recovery_detail", "Not run."),
            "",
            "## Limitations",
            "",
            "- This is a small synthetic dataset generated by code, not LongMemEval or LoCoMo.",
            "- Lexical answering favors simple fact questions and does not represent quality from a hosted LLM.",
            "- If the local embedding model is not already cached, retrieval uses the transparent TF-IDF fallback.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_charts(output: Path, rows: list[dict[str, Any]]) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    strategies = [str(row["strategy"]) for row in rows]
    outputs: list[str] = []
    for metric, title, filename, color in (
        ("total_tokens", "Input tokens by memory strategy", "tokens.png", "#3478f6"),
        ("factual_answer_accuracy", "Factual answer accuracy", "accuracy.png", "#22a06b"),
    ):
        fig, axis = plt.subplots(figsize=(9, 4.5))
        values = [float(row[metric]) for row in rows]
        axis.bar(strategies, values, color=color)
        axis.set_title(title)
        axis.set_ylabel("Tokens" if metric == "total_tokens" else "Accuracy")
        axis.tick_params(axis="x", rotation=18)
        if metric == "factual_answer_accuracy":
            axis.set_ylim(0, 1)
        fig.tight_layout()
        path = output / filename
        fig.savefig(path, dpi=140)
        plt.close(fig)
        outputs.append(str(path))
    return outputs
