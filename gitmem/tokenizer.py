"""Token counting with a tiktoken implementation and offline whitespace fallback."""

from __future__ import annotations


class TokenCounter:
    """Count model tokens when tiktoken is available, otherwise whitespace units."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding = None
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(encoding_name)
        except (ImportError, ValueError, OSError):
            self._encoding = None

    @property
    def backend(self) -> str:
        """Name the active counter so evaluations can disclose the fallback."""
        return "tiktoken" if self._encoding is not None else "whitespace"

    def count(self, text: str) -> int:
        """Return a nonnegative token estimate for text."""
        if not text.strip():
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return len(text.split())
