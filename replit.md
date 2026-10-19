# GitMem

GitMem is a versioned, user-controlled memory engine for long-running AI agents, designed to make context reduction explainable and reversible.

## Run & Operate

- `pytest -q` — run the GitMem test suite
- `python -m streamlit run gitmem/ui/app.py --server.address 0.0.0.0 --server.port 8000` — run the GitMem UI
- `python -m gitmem --help` — see CLI operations
- `python -m gitmem demo` — load the six-session demo into the local database
- `python -m gitmem benchmark` — run baseline evaluation and save reports under `generated-artifacts/`
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
- The default `MockLLM` is deterministic and offline. Live adapters are opt-in.

## Product

The proof of concept offers reversible memory compression, explainable decisions, Git-like history operations, bounded retrieval, conflict review, answer attribution, canary checks, PII shredding, and baseline evaluation.

## User preferences

Preserve the requested Python 3.11, SQLite, Streamlit, offline-first stack.

## Gotchas

- Semantic retrieval uses a TF-IDF fallback unless local SentenceTransformer weights and FAISS are available; do not download model weights implicitly.
- No LongMemEval or LoCoMo dataset is bundled. Only load an externally obtained local subset after confirming its license permits the intended evaluation.

## Pointers

- See `pyproject.toml` for Python dependencies and `gitmem/config/default.yaml` for default engine settings.
