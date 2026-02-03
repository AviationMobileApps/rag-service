from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx
from urllib.parse import urlparse


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

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout_s: float = 120.0,
        reasoning_effort: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.client = httpx.Client(timeout=timeout_s)
        self._strip_inline_code_backticks = self._host_uses_waf_unsafe_markdown(self.base_url)
        # The Airia gateway sits behind Cloudflare and proxies multiple models. In practice it is
        # more reliable to omit token limit parameters (it already enforces server-side limits).
        self._omit_max_tokens_param = self._strip_inline_code_backticks

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def _host_uses_waf_unsafe_markdown(base_url: str) -> bool:
        try:
            host = urlparse(base_url).hostname or ""
        except Exception:
            host = ""
        # Airia gateway sits behind Cloudflare and may block certain Markdown patterns
        # (notably inline code spans like `curl ... http://...`). We strip *inline* backticks
        # as a best-effort workaround, while preserving triple-backtick fences.
        return host.endswith("airia.ai")

    @staticmethod
    def _strip_inline_backticks_preserve_fences(text: str) -> str:
        if "`" not in (text or ""):
            return text

        out: list[str] = []
        i = 0
        in_fence = False
        s = text

        while i < len(s):
            if s.startswith("```", i):
                in_fence = not in_fence
                out.append("```")
                i += 3
                continue

            ch = s[i]
            if ch == "`" and not in_fence:
                j = s.find("`", i + 1)
                if j == -1:
                    out.append(ch)
                    i += 1
                    continue
                if "\n" in s[i + 1 : j]:
                    out.append(ch)
                    i += 1
                    continue
                out.append(s[i + 1 : j])
                i = j + 1
                continue

            out.append(ch)
            i += 1

        return "".join(out)

    @staticmethod
    def _raise_for_status_with_body(resp: httpx.Response) -> None:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = (e.response.text or "").strip()
            if len(body) > 2000:
                body = body[:2000] + "…"
            msg = str(e)
            if body:
                msg = msg + "\n" + body
            raise RuntimeError(msg) from e

    def embeddings(self, model: str, inputs: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = self.client.post(
            f"{self.base_url}/v1/embeddings",
            headers=headers,
            json={"model": model, "input": inputs},
        )
        self._raise_for_status_with_body(resp)
        data = resp.json()
        return [row["embedding"] for row in data["data"]]

    @staticmethod
    def _prefers_max_completion_tokens(model: str) -> bool:
        m = (model or "").strip().lower()
        return m.startswith(("gpt-5", "o1"))

    def chat_completion_text(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> str:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self._strip_inline_code_backticks:
            # Cloudflare WAF can be sensitive to some default Python HTTP user agents.
            headers.setdefault("User-Agent", "Mozilla/5.0")

        if self._strip_inline_code_backticks:
            system_prompt = self._strip_inline_backticks_preserve_fences(system_prompt)
            user_prompt = self._strip_inline_backticks_preserve_fences(user_prompt)

        use_mct = self._prefers_max_completion_tokens(model)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if not self._omit_max_tokens_param and max_tokens and int(max_tokens) > 0:
            payload[("max_completion_tokens" if use_mct else "max_tokens")] = int(max_tokens)
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        url = f"{self.base_url}/v1/chat/completions"

        def _post(json_payload: dict[str, Any]) -> httpx.Response:
            return self.client.post(url, headers=headers, json=json_payload)

        def _sleep_backoff(attempt: int) -> None:
            # 0: ~0.5s, 1: ~1.0s, 2: ~2.0s, capped. Small deterministic jitter avoids lockstep retries.
            base = min(8.0, 0.5 * (2**attempt))
            time.sleep(base + (0.07 * attempt))

        max_attempts = 4 if self._strip_inline_code_backticks else 3
        attempt = 0
        resp: httpx.Response | None = None
        while True:
            try:
                resp = _post(payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                if attempt >= max_attempts:
                    raise RuntimeError(str(e)) from e
                _sleep_backoff(attempt - 1)
                continue

            # Some newer OpenAI models (and gateways that proxy them) reject `max_tokens` in favor of
            # `max_completion_tokens`. If we guessed wrong, retry once with the alternate parameter.
            if resp.status_code == 400 and not self._omit_max_tokens_param:
                try:
                    data = resp.json()
                    err = data.get("error") if isinstance(data, dict) else None
                    param = (err or {}).get("param") if isinstance(err, dict) else None
                    code = (err or {}).get("code") if isinstance(err, dict) else None
                    if code == "unsupported_parameter" and param in {"max_tokens", "max_completion_tokens"}:
                        alt = "max_completion_tokens" if param == "max_tokens" else "max_tokens"
                        payload.pop(param, None)
                        payload[alt] = int(max_tokens)
                        resp = _post(payload)
                except Exception:
                    pass

            # Retry transient gateway/server issues.
            if resp.status_code in {429, 500, 502, 503, 504}:
                body = (resp.text or "").strip()
                attempt += 1
                if attempt < max_attempts:
                    _sleep_backoff(attempt - 1)
                    continue
                # Fall through to raise with body on final attempt.

            break

        assert resp is not None
        self._raise_for_status_with_body(resp)
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
