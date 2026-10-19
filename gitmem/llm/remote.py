"""Optional Ollama, Gemini, and OpenAI-compatible adapters using stdlib HTTP."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Sequence

from gitmem.llm.base import LLMResponse
from gitmem.llm.mock import MockLLM
from gitmem.tokenizer import TokenCounter


class _RemoteAdapter:
    def __init__(self, token_counter: TokenCounter | None = None) -> None:
        self.token_counter = token_counter or TokenCounter()

    def summarize(self, text: str, max_sentences: int = 3) -> str:
        prompt = (
            f"Summarize the source faithfully in at most {max_sentences} sentences. "
            "Do not add facts. Preserve all names, numbers, dates, and negations.\n\n"
            f"SOURCE:\n{text}"
        )
        return self._generate(prompt, text).text

    def answer(self, question: str, context: Sequence[str]) -> LLMResponse:
        prompt = (
            "Answer using only the supplied memory. If it does not contain the answer, "
            "say you could not find it. Quote or faithfully repeat the supporting fact.\n\n"
            f"MEMORY:\n{chr(10).join(context)}\n\nQUESTION:\n{question}"
        )
        return self._generate(prompt, "\n".join(context) + question)

    def _generate(self, prompt: str, source: str) -> LLMResponse:
        raise NotImplementedError


class OllamaAdapter(_RemoteAdapter):
    """Adapter for a locally running Ollama chat endpoint."""

    name = "ollama"

    def __init__(self, model: str = "llama3.2", base_url: str | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")

    def _generate(self, prompt: str, source: str) -> LLMResponse:
        return _post_json(
            f"{self.base_url}/api/generate",
            {"model": self.model, "prompt": prompt, "stream": False},
            self.name,
            self.model,
            self.token_counter,
            source,
            ("response",),
        )


class GeminiAdapter(_RemoteAdapter):
    """Adapter for Gemini API; reads GEMINI_API_KEY from the environment."""

    name = "gemini"

    def __init__(self, model: str = "gemini-2.0-flash", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.model = model

    def _generate(self, prompt: str, source: str) -> LLMResponse:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set; select MockLLM for offline operation.")
        return _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={api_key}",
            {"contents": [{"parts": [{"text": prompt}]}]},
            self.name,
            self.model,
            self.token_counter,
            source,
            ("candidates", 0, "content", "parts", 0, "text"),
        )


class OpenAICompatibleAdapter(_RemoteAdapter):
    """Adapter for OpenAI-compatible chat-completions endpoints."""

    name = "openai-compatible"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.model = model or os.getenv("OPENAI_COMPATIBLE_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.getenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key_env = api_key_env

    def _generate(self, prompt: str, source: str) -> LLMResponse:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"{self.api_key_env} is not set; select MockLLM for offline operation."
            )
        return _post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            self.name,
            self.model,
            self.token_counter,
            source,
            ("choices", 0, "message", "content"),
            api_key=api_key,
        )


def _post_json(
    url: str,
    payload: dict[str, object],
    adapter_name: str,
    model: str,
    token_counter: TokenCounter,
    source: str,
    response_path: tuple[object, ...],
    api_key: str | None = None,
) -> LLMResponse:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{adapter_name} request failed: {exc}") from exc
    elapsed = (time.perf_counter() - started) * 1000.0
    value: object = body
    try:
        for part in response_path:
            value = value[part]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{adapter_name} returned an unexpected response format") from exc
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{adapter_name} returned an empty response")
    return LLMResponse(
        text=value.strip(),
        model=model,
        input_tokens=token_counter.count(source),
        output_tokens=token_counter.count(value),
        latency_ms=elapsed,
    )
