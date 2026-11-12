# GitMem Official Live Demonstration Script

**Target**: L&T Techgium Hackathon (Problem Statement PS-2)  
**Estimated Duration**: 5 minutes (Live Demo) or 3 minutes (CLI Fallback)  
**Execution Mode**: 100% Offline with deterministic `MockLLM` and local SQLite  
**Audience**: Technical evaluators, judges, and systems engineers  

---

## Demonstration Overview

This script guides you through an end-to-end demonstration of GitMem. It shows how long-running AI agents can compress context efficiently while preserving complete explainability, auditability, and reversibility through a Git-style versioned commit DAG.

### The 11-Step Sequence
1. **Ingest a conversation** containing a critical fact.
2. **Create a versioned commit** with explainable keep/summarize/archive decisions.
3. **Inject an incorrect summary** into memory (simulating context corruption).
4. **Trigger a Canary CI alert** proving that memory corruption was caught.
5. **Execute Memory Bisect** to binary-search history and isolate the faulty commit.
6. **Restore the known-good snapshot** to roll back memory state.
7. **Verify context recovery** by querying the agent.
8. **Demonstrate selective rehydration** (pulling archived items without state rollback).
9. **Branch and merge** demonstrating contradiction detection and human-in-the-loop conflict review.
10. **Perform What-If Replay** comparing answers, token counts, and latency across branches.
11. **Review Benchmark Dashboard** proving 54.2% token savings with 100% factual accuracy.

---

## Preparation (Before Taking the Stage)

1. Ensure Python 3.11 is active in your terminal.
2. In terminal 1, run the one-click demo seeder:
   ```bash
   python -m gitmem demo
   ```
3. In terminal 2, start the Streamlit UI:
   ```bash
   python -m streamlit run gitmem/ui/app.py --server.port 8000
   ```
4. Open your browser to `http://localhost:8000`.

---

## Live UI Demonstration (Primary Path)

### Step 1: Scenario Overview & Architecture (30 Seconds)
- **Action**: Look at the sidebar in Streamlit.
- **Narrate**: *"Welcome to GitMem. Long-running AI agents face a core dilemma: keeping all context explodes token cost and latency, but summarization silently destroys critical facts. GitMem solves this by treating agent memory as a versioned Git DAG with deterministic faithfulness gates and reversible forgetting."*
- **Visual**: Point out the sidebar showing `Working branch: main`, `Answer adapter: MockLLM (offline)`, `Tokenizer: tiktoken`, and `Retrieval index: tfidf`.

---

### Step 2: Ingestion & Explainable Scoring (30 Seconds)
- **Action**: Click **Memory Explorer** in the sidebar.
- **Narrate**: *"GitMem ingests dialogue into typed units (facts, constraints, decisions) and scores them on 5 explainable signals: importance, relevance, redundancy penalty, recency decay, and user pins."*
- **Visual**: Click on the unit containing `"Correction: the launch deadline moved to 25 September 2027"`. Show its score breakdown:
  - *Tier*: `HOT` or `WARM`
  - *Score explanation*: Points breakdown showing exact contribution of importance and relevance.
- **Highlight**: Explain that the original source unit is retained in the database; it is never erased during compression.

---

### Step 3 & 4: Controlled Corruption & Canary CI Alert (45 Seconds)
- **Action**: Click **Canary / CI** in the sidebar.
- **Narrate**: *"How do we know if context compression corrupted our agent? In GitMem, critical facts are guarded by Canary Probes. In our demo scenario, an unverified summary was injected with a falsified deadline (26 September instead of 25 September)."*
- **Visual**: Show the Canary Runs table:
  - Probe: `"What is the launch deadline?"`
  - Expected: `"25 September 2027"`
  - Actual Answer: `"Project Atlas launch deadline is 26 September 2027."`
  - Status: **FAILED (Red Badge)**.

---

### Step 5: Memory Bisect (45 Seconds)
- **Action**: Click **Blame and Bisect** in the sidebar, then select the **Memory bisect** tab.
- **Narrate**: *"When a canary fails or an agent gives a wrong answer, how do you find where it broke? Like git bisect, GitMem binary-searches first-parent commit history in $O(\log N)$ time."*
- **Action**: Set Question to `"What is the launch deadline?"`, Expected answer to `"25 September 2027"`, and click **Bisect history**.
- **Visual**: Bisect outputs the exact first corrupted commit:
  ```json
  {
    "found": true,
    "commit_hash": "395b5208f6c5...",
    "commit": {
      "message": "DEMO fault injection: incorrect deadline summary"
    },
    "method": "binary search over first-parent history"
  }
  ```

---

### Step 6 & 7: Snapshot Restore & Verification (30 Seconds)
- **Action**: Click **Chat** in the sidebar. Ask: `"What is the launch deadline?"`
- **Narrate**: *"Because GitMem automatically reverted the corrupted commit back to the last known-good snapshot, let's verify what the agent answers now."*
- **Visual**: The assistant replies:  
  **`"Correction: the launch deadline moved to 25 September 2027."`**  
  Point out the live token meter below the answer:
  `72 input tokens · 9 output tokens · 0.8 ms · estimated cost $0.00001080`.
- **Explain**: Expand **Context selection and memory blame** to show the exact source unit ID and commit hash that generated the answer.

---

### Step 8: Restore vs. Rehydrate Distinction (30 Seconds)
- **Action**: Click **Operations** in the sidebar.
- **Narrate**: *"A major architectural innovation in GitMem is the strict separation between RESTORE and REHYDRATE."*
- **Explain**:
  - **Restore**: Rolls back the *entire active memory state* to a previous commit.
  - **Rehydrate**: Selectively brings *specific archived units* from cold storage back into active context without changing anything else.
- **Visual**: Show the Rehydrate selector with archived items available for reactivation.

---

### Step 9: Branching & 3-Way Merge Conflict Review (45 Seconds)
- **Action**: Stay in **Operations**, scroll to **Merge and review conflicts**.
- **Narrate**: *"Agents branching into parallel tasks can develop conflicting requirements. GitMem detects contradictions and never silently overwrites them."*
- **Visual**: Show the queued conflict between `main` (`"The dashboard must show archived units"`) and branch `demo/requirement-alternative` (`"The dashboard must not show archived units"`):
  - *Conflict Reason*: `"Contradictory requirements or facts need an explicit merge choice."`
  - *Resolution Choices*: `keep_ours`, `keep_theirs`, `keep_both`.
  - Finalize merge button records the explicit human choice.

---

### Step 10: What-If Replay (30 Seconds)
- **Action**: Click **History** in the sidebar, and expand the **What-if replay** expander.
- **Action**:
  - First snapshot: `main`
  - Second snapshot: `demo/requirement-alternative`
  - Question: `"The dashboard must show archived units?"`
  - Click **Replay on both snapshots**.
- **Visual**: Displays the comparative diff:
  - First answer: `"The dashboard must show archived units and explain each memory decision."`
  - Second answer: `"The dashboard must not show archived units."`
  - Token delta & Latency delta.
- **Narrate**: *"What-if replay lets operators test hypothetical prompt outcomes across different memory checkpoints with zero state mutation."*

---

### Step 11: Benchmark Summary & Closing (30 Seconds)
- **Action**: Click **Benchmark** in the sidebar.
- **Narrate**: *"Finally, let's examine the objective numbers. In our synthetic evaluation against 4 industry baselines:"*
- **Visual**: Highlight the **Comparative Savings** table:
  - **vs. Full-History**: GitMem saves **54.2% tokens** and **54.2% cost** with **100% accuracy**.
  - **vs. Sliding-Window**: GitMem saves **18.1% tokens** while boosting accuracy from **57.1% to 100%**.
  - **vs. Rolling-Summary**: Rolling-summary drops accuracy to **28.6%**, whereas GitMem preserves all critical facts.
  - **vs. Retrieval-Based**: Retrieval cannot recover corrupted state (0%), whereas GitMem recovers completely (100%).
- **Concluding Sentence**: *"GitMem brings software engineering rigor—versioning, testing, blame, and reversibility—to AI agent memory."*

---

## Terminal / CLI Fallback Path (3 Minutes)

If the Streamlit web server cannot be accessed during presentation, execute the following commands directly in PowerShell / Bash.

### 1. Show Commit History
```bash
python -m gitmem log --limit 5
```
*Expected*: Outputs JSON list of recent commits including Restore, Rehydrate, and Shred actions.

### 2. Verify Cryptographic Integrity
```bash
python -m gitmem verify
```
*Expected*:
```json
{
  "valid": true,
  "commit_count": 14,
  "problems": []
}
```

### 3. Ask Agent Query with Blame
```bash
python -m gitmem chat "What is the launch deadline?"
```
*Expected*: Returns `"Correction: the launch deadline moved to 25 September 2027."` with attribution list.

### 4. Run What-If Replay Across Branches
```bash
python -m gitmem what-if "The dashboard must show archived units?" main demo/requirement-alternative
```
*Expected*: Outputs structured comparison showing diff between `main` and the alternative requirement branch.

### 5. Run Memory Bisect
```bash
python -m gitmem bisect "What is the launch deadline?" "18 September 2027"
```
*Expected*: Binary search identifies commit `395b5208f6c5...` as the first bad commit.

### 6. Run Baseline Benchmark
```bash
python -m gitmem benchmark
```
*Expected*: Evaluates all 5 baselines and prints the measured metrics and comparative savings table.

---

## Troubleshooting & Reset

If you need to reset the demonstration database completely to clean slate:
```bash
Remove-Item data/gitmem.sqlite3 -Force
python -m gitmem demo
```
This recreates the isolated SQLite database and seeds the 6-session scenario in under 2 seconds.
