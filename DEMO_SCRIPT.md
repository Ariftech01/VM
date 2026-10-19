# GitMem demo script

**Length:** about 5 minutes  
**Mode:** offline; the deterministic `MockLLM` is enough.

## 1. Start the app

Run `python -m gitmem demo`, or use **Load demo scenario** in the Streamlit sidebar. The scenario creates six sessions with project facts, corrections, requirements, and one synthetic email address.

## 2. Show the current memory answer

Open Chat and ask: **“What is the launch deadline?”**

The current answer should cite the correction: **25 September 2027**. The response panel includes the reconstructed context, source units, input/output tokens, latency, and estimated input cost.

## 3. Explain the compression decision

Open Memory Explorer. Inspect each unit’s tier and score explanation, then compare the source unit with its linked summary. The original remains available; the faithfulness gate prevents summaries from silently dropping detected names, numbers, dates, or negation.

## 4. Show a bad change being caught and recovered

Open Canary / CI. A deliberately faulty summary changes the deadline. The canary should fail. Use the recorded first bad commit from Bisect, then restore the last known-good memory snapshot. Ask the deadline question again; the answer returns to 25 September 2027.

## 5. Compare restore with rehydrate

In History, select a previous commit and restore it to show that restore returns the whole snapshot. In Operations, rehydrate one archived source unit to show that selective rehydration adds only that source to the current state.

## 6. Review a branch conflict

Open Operations, inspect the queued dashboard requirement conflict, select a resolution, and finalize the merge. GitMem keeps both branch histories and records the human-selected resolution.

## 7. Show PII removal and evaluation

Use the confirmed crypto-shred operation on the demo email unit. The stored text and linked answer text are removed while integrity hashes remain. Finally, open Benchmark or run `python -m gitmem benchmark` to compare GitMem with four baselines and download CSV/Markdown/chart outputs.

## Expected scope

This is a reproducible proof of concept, not a production privacy or model-quality certification. The demo uses synthetic facts, a local SQLite database, TF-IDF retrieval in the current workspace, and an offline answer adapter.
