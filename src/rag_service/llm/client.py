from __future__ import annotations

import time
from typing import Any, Optional

from rag_service.config.settings import settings
from rag_service.llm.openai_compat import OpenAICompatClient


class LLMClient:
    """Small wrapper around an OpenAI-compatible chat endpoint (e.g. LM Studio)."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        timeout_s: float = 300.0,
    ):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        self.reasoning_effort = reasoning_effort or settings.llm_reasoning_effort
        self.client = OpenAICompatClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout_s=timeout_s,
            reasoning_effort=self.reasoning_effort,
        )

    def close(self) -> None:
        self.client.close()

    def generate_text(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> dict[str, Any]:
        t0 = time.time()
        answer = self.client.chat_completion_text(
            model=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )
        return {"answer": answer, "model": self.model, "timing_ms": int((time.time() - t0) * 1000)}

    def generate_json(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[Any, dict[str, Any]]:
        t0 = time.time()
        data = self.client.chat_completion_json(
            model=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )
        meta = {"model": self.model, "timing_ms": int((time.time() - t0) * 1000)}
        return data, meta
