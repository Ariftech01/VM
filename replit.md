# GitMem

GitMem is a versioned, user-controlled memory engine for long-running AI agents, designed to make context reduction explainable and reversible.

## Run & Operate

- `python -m unittest discover -s tests -v` — run the foundation test suite
- `python -m streamlit run gitmem/ui/app.py` — run the GitMem UI (created in a later phase)
- `pnpm run typecheck` — typecheck the surrounding workspace packages
- The core uses local SQLite and runs offline; no remote database or API key is required.

## Stack

- Python 3.11, SQLite, and Streamlit
- Offline-first deterministic mock language-model adapter
- Optional embeddings: sentence-transformers with FAISS, with a TF-IDF fallback
- Optional token counting: tiktoken, with a whitespace fallback
- The surrounding Replit workspace remains a pnpm monorepo.

## Where things live

- `gitmem/` — modular Python implementation
- `gitmem/models.py` — typed memory and versioning domain contracts
- `gitmem/migrations/` — ordered SQLite schema migrations
- `gitmem/config/default.yaml` — scoring, retrieval, tokenizer, and evaluation settings
- `tests/` — automated Python tests

## Architecture decisions

- Commit snapshots reference content revisions rather than duplicating original text.
- Archiving changes active-memory membership; originals remain stored by default.
- Optional embedding and tokenizer dependencies preserve a lightweight offline fallback path.

## Product

The app is being built to offer reversible memory compression, explainable decisions, Git-like history operations, bounded retrieval, conflict review, answer attribution, canary checks, and baseline evaluation.

## User preferences

Preserve the requested Python 3.11, SQLite, Streamlit, offline-first stack and proceed phase by phase.

## Gotchas

- The Streamlit UI, LLM adapters, and remaining features are not implemented yet; Phase 1 is the foundation only.

## Pointers

- See `pyproject.toml` for Python dependencies and `gitmem/config/default.yaml` for default engine settings.
