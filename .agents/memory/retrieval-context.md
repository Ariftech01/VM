---
name: Retrieval context selection
description: Why GitMem suppresses duplicate archived source when an active linked summary is available.
---

When a faithful summary is active, retrieval should normally select that summary instead of also injecting the same archived source text. Keep the original revision available for explicit selective rehydration and historical restore.

**Why:** Including both forms duplicates tokens and makes the context budget and compression comparison misleading, without improving the default answer path.

**How to apply:** Preserve this deduplication when changing retrieval, and rerun the synthetic comparison plus restore/rehydrate tests. Allow the full source to win when its query relevance is materially stronger.
