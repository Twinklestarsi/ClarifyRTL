"""Thin chat wrapper: retries, token ledger, and tolerant JSON extraction."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .env import chat_client


class LLMError(RuntimeError):
    pass


_FENCE = re.compile(r"```(?:json|yaml|yml)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in ``text`` (code fences tolerated)."""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(match.group(1).strip() for match in _FENCE.finditer(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    raise LLMError(f"no JSON object found in response: {text[:400]!r}")


class LLM:
    """OpenAI-compatible chat client with a per-run call ledger."""

    def __init__(self, *, ledger: Path | None = None, model: str | None = None,
                 timeout: float = 300.0):
        client, default_model = chat_client(timeout=timeout)
        self.client = client
        self.model = model or default_model
        self.ledger = Path(ledger) if ledger else None
        self.calls: list[dict[str, Any]] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # ------------------------------------------------------------------ chat
    # No output-token cap: the old default was 8000 and call sites capped at
    # 1200-8000, which truncated JSON inventories and long patches.  ``None``
    # now means "omit max_tokens and let the server use the model limit".
    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
             max_tokens: int | None = None, retries: int = 2, label: str = "",
             json_object: bool = False) -> str:
        last: Exception | None = None
        for attempt in range(retries + 1):
            started = time.time()
            try:
                kwargs: dict[str, Any] = {}
                if json_object:
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=temperature,
                    **kwargs)
                content = response.choices[0].message.content or ""
                usage = getattr(response, "usage", None)
                entry = {
                    "label": label, "attempt": attempt, "model": self.model,
                    "temperature": temperature, "max_tokens": max_tokens,
                    "duration_s": round(time.time() - started, 2),
                    "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                    "response_chars": len(content),
                    "finish_reason": response.choices[0].finish_reason,
                }
                self.calls.append(entry)
                self.prompt_tokens += entry["prompt_tokens"]
                self.completion_tokens += entry["completion_tokens"]
                self._append(entry)
                return content
            except Exception as exc:  # network, rate limit, malformed request
                last = exc
                self._append({"label": label, "attempt": attempt, "error":
                              f"{type(exc).__name__}: {exc}"[:500]})
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"chat failed after {retries + 1} attempts: {last}")

    def json(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        text = self.chat(messages, **kwargs)
        return extract_json(text)

    # ------------------------------------------------------------------ ledger
    def _append(self, entry: dict[str, Any]) -> None:
        if not self.ledger:
            return
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def usage(self) -> dict[str, int]:
        return {"calls": len(self.calls), "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens}