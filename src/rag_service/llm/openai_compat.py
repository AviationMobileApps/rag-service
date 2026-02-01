from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM response."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
        text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(1))


class OpenAICompatClient:
    """Minimal OpenAI-compatible client (LM Studio / other compatible servers)."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self.client.close()

    def embeddings(self, model: str, inputs: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = self.client.post(
            f"{self.base_url}/v1/embeddings",
            headers=headers,
            json={"model": model, "input": inputs},
        )
        resp.raise_for_status()
        data = resp.json()
        return [row["embedding"] for row in data["data"]]

    def chat_completion_text(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        resp = self.client.post(f"{self.base_url}/v1/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def chat_completion_json(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> Any:
        raw = self.chat_completion_text(model=model, system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens)
        return _extract_json(raw)


def timed(fn):
    def wrapper(*args, **kwargs):
        t0 = time.time()
        out = fn(*args, **kwargs)
        return out, int((time.time() - t0) * 1000)

    return wrapper

