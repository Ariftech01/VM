"""Command-line interface mirroring GitMem's core UI operations."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any

from gitmem.demo import load_demo_scenario
from gitmem.eval.benchmarks import run_benchmark
from gitmem.eval.loaders import load_local_subset
from gitmem.models import MemoryAction, MemoryTier
from gitmem.versioning.engine import GitMemEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitmem", description="Versioned AI memory operations")
    parser.add_argument("--db", default="data/gitmem.sqlite3", help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    log_parser = subparsers.add_parser("log", help="Show branch history")
    log_parser.add_argument("--branch", default="main")
    log_parser.add_argument("--limit", type=int, default=50)

    diff_parser = subparsers.add_parser("diff", help="Compare two commits")
    diff_parser.add_argument("from_commit")
    diff_parser.add_argument("to_commit")

    for name in ("restore", "checkout"):
        command_parser = subparsers.add_parser(name, help=f"{name.title()} a memory snapshot")
        command_parser.add_argument("commit")
        command_parser.add_argument("--branch", default="main")

    revert_parser = subparsers.add_parser("revert", help="Restore a commit's first parent")
    revert_parser.add_argument("--commit")
    revert_parser.add_argument("--branch", default="main")

    rehydrate_parser = subparsers.add_parser("rehydrate", help="Selectively restore archived units")
    rehydrate_parser.add_argument("unit_ids", nargs="+")
    rehydrate_parser.add_argument("--branch", default="main")
    rehydrate_parser.add_argument("--tier", choices=[tier.value for tier in MemoryTier], default="hot")

    branch_parser = subparsers.add_parser("branch", help="Create a branch")
    branch_parser.add_argument("name")
    branch_parser.add_argument("--from-commit")
    branch_parser.add_argument("--description", default="")

    merge_parser = subparsers.add_parser("merge", help="Three-way merge two branches")
    merge_parser.add_argument("ours")
    merge_parser.add_argument("theirs")
    merge_parser.add_argument("--target")

    cherry_parser = subparsers.add_parser("cherry-pick", help="Copy snapshot items from another commit")
    cherry_parser.add_argument("commit")
    cherry_parser.add_argument("--branch", default="main")

    tag_parser = subparsers.add_parser("tag", help="Create a named checkpoint")
    tag_parser.add_argument("name")
    tag_parser.add_argument("--commit")
    tag_parser.add_argument("--message", default="")

    pin_parser = subparsers.add_parser("pin", help="Pin or unpin a memory unit")
    pin_parser.add_argument("unit_id")
    pin_parser.add_argument("--unpin", action="store_true")
    pin_parser.add_argument("--branch", default="main")

    edit_parser = subparsers.add_parser("edit", help="Edit source text or force a tier decision")
    edit_parser.add_argument("unit_id")
    edit_parser.add_argument("--text")
    edit_parser.add_argument("--force", choices=[action.value for action in MemoryAction])
    edit_parser.add_argument("--branch", default="main")

    chat_parser = subparsers.add_parser("chat", help="Ask a question against a memory snapshot")
    chat_parser.add_argument("question")
    chat_parser.add_argument("--branch", default="main")
    chat_parser.add_argument("--budget", type=int)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest messages from a JSON file")
    ingest_parser.add_argument("json_file")
    ingest_parser.add_argument("--session-id")
    ingest_parser.add_argument("--goal", default="")
    ingest_parser.add_argument("--branch", default="main")

    compress_parser = subparsers.add_parser("compress", help="Score and compress current memory")
    compress_parser.add_argument("--goal", default="")
    compress_parser.add_argument("--branch", default="main")

    blame_parser = subparsers.add_parser("blame", help="Show the source units for an answer")
    blame_parser.add_argument("answer_id")

    bisect_parser = subparsers.add_parser("bisect", help="Find the first failing memory commit")
    bisect_parser.add_argument("question")
    bisect_parser.add_argument("expected_answer")
    bisect_parser.add_argument("--branch", default="main")
    bisect_parser.add_argument("--good-commit")

    shred_parser = subparsers.add_parser("shred", help="Irreversibly shred a PII memory unit")
    shred_parser.add_argument("unit_id")
    shred_parser.add_argument("--confirm", action="store_true", required=True)
    shred_parser.add_argument("--branch", default="main")

    what_if_parser = subparsers.add_parser(
        "what-if", help="Replay a question on two commits/branches and diff answers"
    )
    what_if_parser.add_argument("question", help="Question to evaluate against both snapshots")
    what_if_parser.add_argument("first", help="First commit hash, branch name, or tag")
    what_if_parser.add_argument("second", help="Second commit hash, branch name, or tag")
    what_if_parser.add_argument("--budget", type=int, help="Optional token budget")

    subparsers.add_parser("verify", help="Verify the hash-chained commit history")
    subparsers.add_parser("demo", help="Load and run the six-session demo scenario")
    benchmark_parser = subparsers.add_parser("benchmark", help="Run the synthetic baseline comparison")
    benchmark_parser.add_argument("--output-dir", default="generated-artifacts/gitmem-benchmark")
    benchmark_parser.add_argument("--dataset", help="Optional local LongMemEval/LoCoMo JSON or JSONL subset")
    benchmark_parser.add_argument("--dataset-name", choices=["LongMemEval", "LoCoMo"])
    benchmark_parser.add_argument("--confirm-license", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "benchmark":
        dataset = None
        source_note = None
        if args.dataset:
            if not args.dataset_name:
                parser.error("--dataset-name is required with --dataset")
            dataset = load_local_subset(
                args.dataset,
                args.dataset_name,
                license_confirmed=args.confirm_license,
            )
            source_note = f"local {args.dataset_name} subset ({Path(args.dataset).name})"
        _print(run_benchmark(args.output_dir, dataset, source_note))
        return 0

    engine = GitMemEngine(Path(args.db))
    try:
        result: Any
        if args.command == "log":
            result = engine.log(args.branch, args.limit)
        elif args.command == "diff":
            result = engine.diff(args.from_commit, args.to_commit)
        elif args.command == "restore":
            result = {"commit_hash": engine.restore(args.commit, args.branch)}
        elif args.command == "checkout":
            result = {"commit_hash": engine.checkout(args.commit, args.branch)}
        elif args.command == "revert":
            result = {"commit_hash": engine.revert(args.commit, args.branch)}
        elif args.command == "rehydrate":
            result = {
                "commit_hash": engine.rehydrate(
                    args.unit_ids, args.branch, MemoryTier(args.tier)
                )
            }
        elif args.command == "branch":
            result = {"head": engine.create_branch(args.name, args.from_commit, args.description)}
        elif args.command == "merge":
            result = engine.merge(args.ours, args.theirs, args.target)
        elif args.command == "cherry-pick":
            result = {"commit_hash": engine.cherry_pick(args.commit, args.branch)}
        elif args.command == "tag":
            engine.tag(args.name, args.commit, args.message)
            result = {"tag": args.name}
        elif args.command == "pin":
            result = {
                "commit_hash": engine.edit_unit(
                    args.unit_id, pinned=not args.unpin, branch=args.branch
                )
            }
        elif args.command == "edit":
            result = {
                "commit_hash": engine.edit_unit(
                    args.unit_id,
                    text=args.text,
                    force_action=MemoryAction(args.force) if args.force else None,
                    branch=args.branch,
                )
            }
        elif args.command == "chat":
            result = engine.answer(args.question, args.budget, args.branch)
        elif args.command == "ingest":
            messages = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
            if not isinstance(messages, list):
                raise ValueError("Ingest JSON must be a list of role/content message objects.")
            result = engine.ingest(
                messages, args.session_id, args.goal, args.branch
            )
        elif args.command == "compress":
            result = engine.compress(args.goal, args.branch)
        elif args.command == "blame":
            result = engine.blame(args.answer_id)
        elif args.command == "bisect":
            result = engine.bisect(
                args.question, args.expected_answer, args.branch, args.good_commit
            )
        elif args.command == "shred":
            result = {"commit_hash": engine.shred(args.unit_id, args.confirm, args.branch)}
        elif args.command == "verify":
            result = engine.verify_integrity()
        elif args.command == "what-if":
            result = engine.what_if(
                args.question, args.first, args.second, token_budget=args.budget
            )
        elif args.command == "demo":
            result = load_demo_scenario(engine)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
        _print(result)
        return 0
    except (KeyError, ValueError, PermissionError) as exc:
        print(f"gitmem error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()


def _print(value: Any) -> None:
    def serialize(item: Any) -> Any:
        if dataclasses.is_dataclass(item):
            return dataclasses.asdict(item)
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "keys") and hasattr(item, "__getitem__"):
            return {key: item[key] for key in item.keys()}
        return str(item)

    print(json.dumps(value, ensure_ascii=False, indent=2, default=serialize))


if __name__ == "__main__":
    sys.exit(main())
