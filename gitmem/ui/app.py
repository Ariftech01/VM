"""Inspectible Streamlit interface for GitMem's versioned memory engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from gitmem.config import load_config
from gitmem.demo import load_demo_scenario
from gitmem.eval.benchmarks import run_benchmark
from gitmem.llm import GeminiAdapter, MockLLM, OllamaAdapter, OpenAICompatibleAdapter
from gitmem.models import MemoryAction, MemoryTier
from gitmem.versioning.engine import GitMemEngine


st.set_page_config(page_title="GitMem", page_icon="G", layout="wide")
config = load_config()
if "gitmem_engine" not in st.session_state:
    st.session_state.gitmem_engine = GitMemEngine()
engine: GitMemEngine = st.session_state.gitmem_engine


def show_error(error: Exception) -> None:
    st.error(f"{type(error).__name__}: {error}")


def safe_action(action: Any) -> Any:
    try:
        return action()
    except Exception as exc:
        show_error(exc)
        return None


def unit_options() -> tuple[list[str], dict[str, Any]]:
    units = engine.repo.all_units()
    return [unit.id for unit in units], {unit.id: unit for unit in units}


def current_membership() -> list[Any]:
    try:
        return engine.repo.commit_membership(engine.head(st.session_state.get("branch", "main")))
    except KeyError:
        return []


def render_sidebar() -> str:
    st.sidebar.title("GitMem")
    st.sidebar.caption("Versioned memory for AI agents")
    branch_names = engine.repo.branch_names()
    if not branch_names:
        branch_names = ["main"]
    default_branch = st.session_state.get("branch", "main")
    if default_branch not in branch_names:
        default_branch = branch_names[0]
    st.session_state.branch = st.sidebar.selectbox(
        "Working branch", branch_names, index=branch_names.index(default_branch)
    )
    provider = st.sidebar.selectbox(
        "Answer adapter",
        ("MockLLM (offline)", "Ollama (local)", "Gemini (API)", "OpenAI-compatible"),
        key="adapter_choice",
    )
    if provider == "MockLLM (offline)":
        engine.llm = MockLLM(engine.token_counter)
        st.sidebar.success("Offline mode")
    elif provider == "Ollama (local)":
        engine.llm = OllamaAdapter(token_counter=engine.token_counter)
        st.sidebar.info("Uses a local Ollama endpoint")
    elif provider == "Gemini (API)":
        engine.llm = GeminiAdapter(token_counter=engine.token_counter)
        st.sidebar.info("Requires GEMINI_API_KEY in the environment")
    else:
        engine.llm = OpenAICompatibleAdapter(token_counter=engine.token_counter)
        st.sidebar.info("Requires compatible endpoint settings in the environment")
    pages = (
        "Chat",
        "Memory Explorer",
        "History",
        "Operations",
        "Blame and Bisect",
        "Canary / CI",
        "Benchmark",
    )
    page = st.sidebar.radio("Workspace", pages)
    st.sidebar.divider()
    if st.sidebar.button("Load demo scenario", use_container_width=True):
        result = safe_action(lambda: load_demo_scenario(engine))
        if result:
            st.session_state.demo_result = result
    st.sidebar.caption(f"Retrieval index: {engine.retrieval_index.backend}")
    st.sidebar.caption(f"Tokenizer: {engine.token_counter.backend}")
    st.sidebar.caption(f"Database: {engine.config.database_path}")
    return page


def render_chat() -> None:
    st.title("Chat with memory")
    st.write("Ask the agent against a selected memory snapshot. Each response records its source units and token use.")
    branch = st.session_state.branch
    head = engine.head(branch)
    st.caption(f"Branch `{branch}` · head `{head[:12]}` · adapter `{engine.llm.name}`")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    for item in st.session_state.chat_history:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])
            if item.get("details"):
                st.caption(item["details"])

    question = st.chat_input("Ask about decisions, facts, or requirements in memory")
    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        try:
            result = engine.answer(
                question,
                token_budget=st.session_state.get("chat_budget_value"),
                branch=branch,
            )
            response = result["response"]
            meter = (
                f"{response.input_tokens} input tokens · {response.output_tokens} output tokens · "
                f"{response.latency_ms:.1f} ms · estimated input cost "
                f"${response.input_tokens * config.cost_per_1k_input_tokens / 1000:.8f}"
            )
            with st.chat_message("assistant"):
                st.markdown(response.text)
                st.caption(meter)
                with st.expander("Context selection and memory blame"):
                    st.write("Included context")
                    for candidate in result["selected"]:
                        st.write(f"- `{candidate.id}` · {candidate.tier.value} · {candidate.token_count} tokens")
                    st.write("Selection explanation")
                    for item_id, explanation in result["selection"].explanations.items():
                        st.write(f"- `{item_id}`: {explanation}")
                    st.write("Source units")
                    for source in result["attributions"]:
                        st.write(f"- `{source.unit_id}` · {source.decision.value} · {source.contribution}")
            st.session_state.chat_history.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "details": meter,
                    "answer_id": result["answer_id"],
                }
            )
            # Store both sides as another ordinary, reversible conversation update.
            engine.ingest(
                [
                    {"role": "user", "content": question, "topic": "conversation"},
                    {"role": "assistant", "content": response.text, "topic": "conversation"},
                ],
                current_goal=question,
                branch=branch,
                session_label="Chat session",
            )
            st.rerun()
        except Exception as exc:
            show_error(exc)

    with st.expander("Context budget"):
        budget = st.number_input(
            "Token budget for the next answer",
            min_value=1,
            max_value=10000,
            value=int(config.default_token_budget),
            step=100,
            key="chat_budget",
        )
        st.session_state.chat_budget_value = int(budget)

    demo_result = st.session_state.get("demo_result")
    if demo_result:
        with st.expander("Demo run status", expanded=True):
            st.write(demo_result.get("message", ""))
            if demo_result.get("canary_alert") is not None:
                st.write(f"Canary detected injected regression: {demo_result['canary_alert']}")
            if demo_result.get("bisect"):
                st.write("Bisect:", demo_result["bisect"].get("commit_hash") or demo_result["bisect"].get("reason"))
            if demo_result.get("merge"):
                st.write("Merge review:", demo_result["merge"])


def render_memory_explorer() -> None:
    st.title("Memory Explorer")
    st.write("Inspect every readable source, its active tier, scoring explanation, and source-linked summary.")
    branch = st.session_state.branch
    head = engine.head(branch)
    members = {item.item_id: item for item in engine.repo.commit_membership(head)}
    rows = engine.connection.execute(
        """
        SELECT u.*, r.id AS revision_id, r.text AS text
        FROM units u JOIN unit_revisions r ON r.unit_id = u.id
        WHERE r.revision_number = (
            SELECT MAX(r2.revision_number) FROM unit_revisions r2 WHERE r2.unit_id = u.id
        ) AND r.text IS NOT NULL
        ORDER BY u.created_at DESC
        """
    ).fetchall()
    selected_topic = st.selectbox(
        "Topic filter",
        ["All"] + sorted({str(row["topic"]) for row in rows}),
    )
    filtered = [row for row in rows if selected_topic == "All" or row["topic"] == selected_topic]
    table = []
    for row in filtered:
        unit_id = str(row["id"])
        member = members.get(unit_id)
        decision = engine.connection.execute(
            """
            SELECT action, reason, score_breakdown_json FROM commit_decisions
            WHERE commit_hash = ? AND unit_id = ?
            """,
            (head, unit_id),
        ).fetchone()
        table.append(
            {
                "unit_id": unit_id[:12],
                "type": row["unit_type"],
                "topic": row["topic"],
                "tier": member.tier.value if member else "not in this snapshot",
                "tokens": row["token_count"],
                "pinned": bool(row["pinned"]),
                "text": row["text"][:240],
                "action": decision["action"] if decision else "—",
            }
        )
        with st.expander(f"{row['unit_type']} · {row['topic']} · {unit_id[:12]}"):
            st.markdown(str(row["text"]))
            st.write(
                {
                    "tier": member.tier.value if member else "not in this snapshot",
                    "tokens": int(row["token_count"]),
                    "pinned": bool(row["pinned"]),
                    "content hash": engine.connection.execute(
                        "SELECT content_sha256 FROM unit_revisions WHERE id = ?",
                        (row["revision_id"],),
                    ).fetchone()["content_sha256"],
                }
            )
            if decision:
                st.write("Decision:", decision["action"])
                st.write("Reason:", decision["reason"])
                st.json(json.loads(decision["score_breakdown_json"]))
            source_summaries = engine.connection.execute(
                """
                SELECT s.id, s.text, s.faithfulness_passed, s.verifier_report_json
                FROM summaries s JOIN summary_sources x ON x.summary_id = s.id
                WHERE x.unit_id = ? ORDER BY s.created_at DESC
                """,
                (unit_id,),
            ).fetchall()
            for summary in source_summaries:
                st.write(f"Summary `{summary['id'][:12]}` · faithfulness passed: {bool(summary['faithfulness_passed'])}")
                st.write(summary["text"])
                st.json(json.loads(summary["verifier_report_json"]))
    st.dataframe(table, use_container_width=True, hide_index=True)

    summaries = engine.connection.execute(
        """
        SELECT s.id, s.topic, s.text, s.token_count, s.faithfulness_passed
        FROM summaries s JOIN commit_membership m ON m.summary_id = s.id
        WHERE m.commit_hash = ? ORDER BY s.topic, s.created_at
        """,
        (head,),
    ).fetchall()
    if summaries:
        st.subheader("Active summaries")
        st.dataframe(
            [
                {
                    "id": row["id"][:12],
                    "topic": row["topic"],
                    "tokens": row["token_count"],
                    "faithfulness": "passed" if row["faithfulness_passed"] else "failed (demo fault injection)",
                    "text": row["text"],
                }
                for row in summaries
            ],
            use_container_width=True,
            hide_index=True,
        )


def render_history() -> None:
    st.title("History")
    branch = st.session_state.branch
    commits = engine.log(branch, limit=250)
    st.write("Commit history forms a directed acyclic graph. Restore creates a new audit entry; it does not rewrite history.")
    if commits:
        st.dataframe(
            [
                {
                    "hash": item["hash"][:12],
                    "parents": ", ".join(parent[:8] for parent in item["parents"]),
                    "branch": item["branch"],
                    "timestamp": item["timestamp"],
                    "message": item["message"],
                    "tokens": f"{item['tokens_before']} → {item['tokens_after']}",
                    "canary": "failed" if item["canary_failed"] else "passed / none",
                }
                for item in commits
            ],
            use_container_width=True,
            hide_index=True,
        )
        commit_hashes = [item["hash"] for item in commits]
        left, right = st.columns(2)
        old_hash = left.selectbox("Diff from", commit_hashes, format_func=lambda value: value[:12])
        new_hash = right.selectbox("Diff to", commit_hashes, index=0, format_func=lambda value: value[:12])
        if st.button("Compare snapshots"):
            if old_hash == new_hash:
                st.info("Choose two different commits.")
            else:
                diff = engine.diff(old_hash, new_hash)
                st.json(
                    {
                        "added": diff.added,
                        "removed": diff.removed,
                        "tier_changed": [
                            (item_id, before.value, after.value)
                            for item_id, before, after in diff.tier_changed
                        ],
                        "content_changed": diff.content_changed,
                        "token_delta": diff.token_delta,
                    }
                )
        with st.expander("What-if replay"):
            first, second = st.columns(2)
            first_commit = first.selectbox("First snapshot", commit_hashes, key="what_if_first")
            second_commit = second.selectbox("Second snapshot", commit_hashes, key="what_if_second")
            question = st.text_input("Question to replay", value="What is the current deadline?")
            if st.button("Replay on both snapshots"):
                result = safe_action(lambda: engine.what_if(question, first_commit, second_commit))
                if result:
                    st.json(result)
    else:
        st.info("No commits on this branch yet.")


def render_operations() -> None:
    st.title("Memory operations")
    st.warning(
        "RESTORE replaces the complete active snapshot. REHYDRATE adds only selected archived source units "
        "to the current snapshot. Neither deletes history."
    )
    tabs = st.tabs(("Restore", "Rehydrate", "Branch and merge", "Manual edit", "Crypto-shred"))
    with tabs[0]:
        commits = engine.log(st.session_state.branch, limit=250)
        options = [item["hash"] for item in commits]
        if options:
            target = st.selectbox("Snapshot to restore", options, format_func=lambda value: value[:12])
            st.caption("This changes the whole active-memory membership to match the chosen historical snapshot, then records a new commit.")
            if st.button("Restore full snapshot"):
                result = safe_action(lambda: engine.restore(target, st.session_state.branch))
                if result:
                    st.success(f"Restored as `{result[:12]}`")
        else:
            st.info("No snapshots are available.")
    with tabs[1]:
        units = engine.repo.all_units()
        members = {item.item_id: item for item in current_membership()}
        archived = [
            unit for unit in units
            if (members.get(unit.id) is None or members[unit.id].tier == MemoryTier.COLD)
        ]
        if archived:
            selected = st.multiselect(
                "Archived units to pull into the current context",
                [unit.id for unit in archived],
                format_func=lambda unit_id: f"{unit_id[:12]} · {next(unit.text[:70] for unit in archived if unit.id == unit_id)}",
            )
            st.caption("This is selective rehydration. Other active-memory items and tiers are left unchanged.")
            if st.button("Rehydrate selected units") and selected:
                result = safe_action(lambda: engine.rehydrate(selected, st.session_state.branch))
                if result:
                    st.success(f"Rehydrated as `{result[:12]}`")
        else:
            st.info("No archived units are currently available.")
    with tabs[2]:
        branch_col, merge_col = st.columns(2)
        with branch_col:
            new_name = st.text_input("New branch name")
            description = st.text_input("Description")
            if st.button("Create branch") and new_name.strip():
                result = safe_action(
                    lambda: engine.create_branch(new_name.strip(), description=description)
                )
                if result:
                    st.success(f"Created `{new_name}` at `{result[:12]}`")
        with merge_col:
            branch_names = engine.repo.branch_names()
            if len(branch_names) > 1:
                ours = st.selectbox("Merge into", branch_names, index=branch_names.index(st.session_state.branch) if st.session_state.branch in branch_names else 0)
                theirs = st.selectbox("Merge from", [name for name in branch_names if name != ours])
                if st.button("Attempt three-way merge"):
                    result = safe_action(lambda: engine.merge(ours, theirs))
                    if result:
                        st.session_state.branch = ours
                        if result.get("merged"):
                            st.success(f"Merge commit `{result['commit_hash'][:12]}`")
                        elif result.get("conflicts"):
                            st.warning("Conflicts were queued. No conflicting value was silently overwritten.")
                            st.json(result["conflicts"])
            else:
                st.info("Create another branch before merging.")
        all_conflicts = engine.list_conflicts()
        conflicts = [conflict for conflict in all_conflicts if conflict["status"] == "open"]
        if all_conflicts:
            st.subheader("Conflict resolution queue")
            for conflict in conflicts:
                unit_ids = conflict["unit_ids"]
                st.write(conflict["reason"])
                for unit_id in unit_ids:
                    unit = engine.repo.get_unit(unit_id)
                    st.write(f"- `{unit_id[:12]}`: {unit.text if unit else '[unavailable]'}")
                choice = st.selectbox(
                    f"Resolution for {conflict['id'][:8]}",
                    ("keep_ours", "keep_theirs", "keep_both", "dismiss"),
                    key=f"resolve_{conflict['id']}",
                )
                if st.button("Save resolution", key=f"save_{conflict['id']}"):
                    safe_action(lambda conflict_id=conflict["id"], selected=choice: engine.resolve_conflict(conflict_id, selected))
                    st.rerun()
            ready = [
                conflict for conflict in all_conflicts
                if conflict["status"] != "open" and not conflict["merge_commit_hash"]
                and not any(
                    other["status"] == "open"
                    and other["ours_hash"] == conflict["ours_hash"]
                    and other["theirs_hash"] == conflict["theirs_hash"]
                    for other in all_conflicts
                )
            ]
            if ready:
                finalize_id = ready[0]["id"]
                if st.button("Finalize resolved merge"):
                    result = safe_action(lambda: engine.finalize_merge(finalize_id))
                    if result:
                        st.success(f"Merge finalized as `{result[:12]}`")
    with tabs[3]:
        options, units_by_id = unit_options()
        if options:
            unit_id = st.selectbox(
                "Memory unit",
                options,
                format_func=lambda value: f"{value[:12]} · {units_by_id[value].topic} · {units_by_id[value].text[:50]}",
            )
            unit = units_by_id[unit_id]
            text = st.text_area("Edit source text", value=unit.text, height=100)
            pin_col, force_col = st.columns(2)
            pinned = pin_col.checkbox("Pinned", value=unit.pinned)
            force = force_col.selectbox(
                "Force decision",
                ("automatic / verbatim", "force keep", "force archive"),
            )
            if st.button("Save manual change"):
                force_action = {
                    "automatic / verbatim": None,
                    "force keep": MemoryAction.KEEP_VERBATIM,
                    "force archive": MemoryAction.ARCHIVE,
                }[force]
                result = safe_action(
                    lambda: engine.edit_unit(
                        unit_id,
                        text=text if text != unit.text else None,
                        pinned=pinned,
                        force_action=force_action,
                        branch=st.session_state.branch,
                    )
                )
                if result:
                    st.success(f"Saved as `{result[:12]}`")
    with tabs[4]:
        options, units_by_id = unit_options()
        pii_units = [
            unit_id for unit_id in options
            if any(pattern.search(units_by_id[unit_id].text) for pattern in engine_module_pii_patterns())
        ]
        st.error(
            "Crypto-shred is irreversible. It removes source text from every revision, redacts linked summaries "
            "and recorded answers, and writes a hash-preserving tombstone."
        )
        if pii_units:
            unit_id = st.selectbox(
                "PII-detected unit",
                pii_units,
                format_func=lambda value: f"{value[:12]} · {units_by_id[value].text[:60]}",
            )
            confirmed = st.checkbox("I understand this delete is irreversible.")
            typed = st.text_input("Type SHRED to confirm")
            if st.button("Crypto-shred this unit", disabled=not (confirmed and typed == "SHRED")):
                result = safe_action(lambda: engine.shred(unit_id, confirmed=True, branch=st.session_state.branch))
                if result:
                    st.success(f"Shredded and recorded in `{result[:12]}`")
        else:
            st.info("No current source units match the configured PII patterns.")
        tombstones = engine.connection.execute(
            "SELECT unit_id, commit_hash, original_sha256, shredded_at FROM shred_tombstones ORDER BY shredded_at DESC"
        ).fetchall()
        if tombstones:
            st.subheader("Shred tombstones")
            st.dataframe([dict(row) for row in tombstones], use_container_width=True, hide_index=True)


def engine_module_pii_patterns() -> list[Any]:
    # Kept in the engine so CLI and UI use the identical PII detector.
    from gitmem.versioning.engine import _PII_PATTERNS

    return list(_PII_PATTERNS.values())


def render_blame_bisect() -> None:
    st.title("Blame and bisect")
    blame_tab, bisect_tab = st.tabs(("Memory blame", "Memory bisect"))
    with blame_tab:
        answers = engine.connection.execute(
            "SELECT id, question, answer, commit_hash, created_at FROM answer_runs ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        if answers:
            answer_id = st.selectbox(
                "Answer run",
                [row["id"] for row in answers],
                format_func=lambda value: next(
                    f"{row['created_at']} · {row['question'][:70]}" for row in answers if row["id"] == value
                ),
            )
            selected = next(row for row in answers if row["id"] == answer_id)
            st.markdown(f"**Answer:** {selected['answer']}")
            st.caption(f"Commit `{selected['commit_hash'][:12]}`")
            sources = engine.blame(answer_id)
            for source in sources:
                unit = engine.repo.get_unit(source["unit_id"])
                with st.expander(
                    f"{source['topic']} · {source['decision']} · {source['unit_id'][:12]}"
                ):
                    st.write(source["contribution"])
                    st.markdown(unit.text if unit else "[Source was crypto-shredded]")
                    st.caption(f"Commit: {source['commit_message']}")
        else:
            st.info("Ask a question in Chat to create a blame record.")
    with bisect_tab:
        branch = st.selectbox("History to search", engine.repo.branch_names(), key="bisect_branch")
        question = st.text_input("Failing probe question", value="What is the launch deadline?")
        expected = st.text_input("Expected answer", value="25 September 2027")
        history = engine.log(branch, limit=250)
        commit_hashes = [row["hash"] for row in history]
        good_commit = st.selectbox(
            "Known-good commit (optional)",
            ["Auto-detect"] + commit_hashes,
            format_func=lambda value: value if value == "Auto-detect" else value[:12],
        ) if commit_hashes else "Auto-detect"
        if st.button("Bisect history"):
            result = safe_action(
                lambda: engine.bisect(
                    question,
                    expected,
                    branch,
                    None if good_commit == "Auto-detect" else good_commit,
                )
            )
            if result:
                st.json(result)


def render_canary_ci() -> None:
    st.title("Canary / Memory CI")
    st.write("Critical facts can be probed after every new memory commit. Failed runs are visible here and can be reverted safely.")
    unit_ids, units_by_id = unit_options()
    if unit_ids:
        with st.form("new_canary"):
            unit_id = st.selectbox(
                "Critical source fact",
                unit_ids,
                format_func=lambda value: f"{value[:12]} · {units_by_id[value].text[:70]}",
            )
            question = st.text_input("Probe question")
            expected = st.text_input("Expected answer")
            submitted = st.form_submit_button("Register canary")
        if submitted:
            result = safe_action(lambda: engine.register_canary(unit_id, question, expected))
            if result:
                st.success(f"Registered canary `{result[:12]}`. It is evaluated on commit.")
    probes = engine.connection.execute(
        "SELECT id, unit_id, question, expected_answer, enabled FROM canary_probes ORDER BY created_at"
    ).fetchall()
    if probes:
        st.subheader("Registered probes")
        st.dataframe([dict(row) for row in probes], use_container_width=True, hide_index=True)
        if st.button("Run canaries on current head"):
            result = safe_action(lambda: engine.run_canaries(engine.head(st.session_state.branch)))
            if result:
                st.json(result)
        results = engine.canary_results()
        st.subheader("Recent CI results")
        st.dataframe(results, use_container_width=True, hide_index=True)
        current_head = engine.head(st.session_state.branch)
        if engine._commit_has_failed_canary(current_head):
            st.error("The current head has a failed memory probe.")
            if st.button("Auto-revert to latest passing snapshot"):
                result = safe_action(lambda: engine.auto_revert_failed_canary(st.session_state.branch))
                if result:
                    st.success(f"Restored as `{result[:12]}`")
    else:
        st.info("No canaries are registered yet.")


def render_benchmark() -> None:
    st.title("Benchmark dashboard")
    st.write("Run the deterministic synthetic evaluation. Reported numbers are computed locally from actual MockLLM runs.")
    if st.button("Run all baseline comparisons", type="primary"):
        result = safe_action(lambda: run_benchmark())
        if result:
            st.session_state.benchmark_result = result
    result = st.session_state.get("benchmark_result")
    if not result:
        st.info("Run the comparison to generate the CSV, Markdown report, and charts.")
        return
    st.caption(
        f"Dataset: {result['dataset']} · retrieval backend: {result['retrieval_backend']}"
    )
    rows = result["rows"]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    cols = st.columns(3)
    gitmem = next(row for row in rows if row["strategy"] == "GitMem")
    cols[0].metric("GitMem input tokens", f"{gitmem['total_tokens']:,}")
    cols[1].metric("Factual accuracy", f"{gitmem['factual_answer_accuracy']:.1%}")
    cols[2].metric("Recovery test", "Passed" if gitmem["recovery_success"] else "Failed")
    for chart_path in result["chart_paths"]:
        if Path(chart_path).exists():
            st.image(chart_path, use_container_width=True)
    csv_path = Path(result["csv_path"])
    markdown_path = Path(result["markdown_path"])
    if csv_path.exists():
        st.download_button(
            "Download results CSV",
            csv_path.read_bytes(),
            file_name=csv_path.name,
            mime="text/csv",
        )
    if markdown_path.exists():
        st.download_button(
            "Download report Markdown",
            markdown_path.read_text(encoding="utf-8"),
            file_name=markdown_path.name,
            mime="text/markdown",
        )
    with st.expander("Per-question answers"):
        for strategy, answers in result["answers"].items():
            st.subheader(strategy)
            st.dataframe(answers, use_container_width=True, hide_index=True)


page = render_sidebar()
if page == "Chat":
    render_chat()
elif page == "Memory Explorer":
    render_memory_explorer()
elif page == "History":
    render_history()
elif page == "Operations":
    render_operations()
elif page == "Blame and Bisect":
    render_blame_bisect()
elif page == "Canary / CI":
    render_canary_ci()
else:
    render_benchmark()
