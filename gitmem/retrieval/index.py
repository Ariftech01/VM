"""Offline semantic index with an automatic TF-IDF fallback."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Mapping


class MemoryIndex:
    """FAISS + all-MiniLM-L6-v2 when installed and cached, otherwise sparse TF-IDF."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self.backend = "tfidf"
        self._model = None
        self._faiss = None
        self._index = None
        self._ids: list[str] = []
        self._texts: dict[str, str] = {}
        self._vectors: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        try:
            import faiss
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                model_name, device="cpu", local_files_only=True
            )
            self._faiss = faiss
            self.backend = "sentence-transformers + FAISS"
        except Exception:
            # Model weights are deliberately not downloaded: operation stays offline.
            self._model = None
            self._faiss = None
            self.backend = "TF-IDF (offline fallback)"

    def build(self, items: Mapping[str, str]) -> None:
        """Build the in-memory index from item IDs and text."""
        self._ids = list(items)
        self._texts = dict(items)
        if self._model is not None and self._faiss is not None and self._ids:
            try:
                import numpy as np

                vectors = self._model.encode(
                    [self._texts[item_id] for item_id in self._ids],
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                ).astype("float32")
                self._index = self._faiss.IndexFlatIP(vectors.shape[1])
                self._index.add(vectors)
                return
            except Exception:
                self._index = None
                self.backend = "TF-IDF (embedding unavailable)"
        self._build_tfidf()

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return the top matching IDs and cosine-like scores."""
        if not self._ids or top_k <= 0:
            return []
        limit = min(top_k, len(self._ids))
        if self._index is not None and self._model is not None:
            try:
                import numpy as np

                vector = self._model.encode(
                    [query], normalize_embeddings=True, convert_to_numpy=True
                ).astype("float32")
                scores, indexes = self._index.search(vector, limit)
                return [
                    (self._ids[int(index)], float(score))
                    for index, score in zip(indexes[0], scores[0], strict=True)
                    if int(index) >= 0
                ]
            except Exception:
                self.backend = "TF-IDF (embedding unavailable)"
                self._index = None
        query_vector = self._vectorize(query)
        ranked = [
            (item_id, self._cosine(query_vector, vector))
            for item_id, vector in zip(self._ids, self._vectors, strict=True)
        ]
        return sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit]

    def _build_tfidf(self) -> None:
        tokenized = [self._terms(self._texts[item_id]) for item_id in self._ids]
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        count = max(1, len(tokenized))
        self._idf = {
            term: math.log(1.0 + count / (1.0 + frequency))
            for term, frequency in document_frequency.items()
        }
        self._vectors = [self._vectorize_tokens(tokens) for tokens in tokenized]

    @staticmethod
    def _terms(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.casefold())

    def _vectorize(self, text: str) -> dict[str, float]:
        return self._vectorize_tokens(self._terms(text))

    def _vectorize_tokens(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        vector = {
            term: (1.0 + math.log(count)) * self._idf.get(term, 0.0)
            for term, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        return {term: value / norm for term, value in vector.items()} if norm else {}

    @staticmethod
    def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
        if not left or not right:
            return 0.0
        return sum(value * right.get(term, 0.0) for term, value in left.items())
