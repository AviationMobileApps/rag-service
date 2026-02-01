from __future__ import annotations

from typing import Optional

from rag_service.config.settings import settings
from rag_service.llm.openai_compat import OpenAICompatClient


class EmbeddingGenerator:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = (base_url or settings.embeddings_base_url).rstrip("/")
        self.model = model or settings.embeddings_model
        self.api_key = api_key or settings.embeddings_api_key
        self.client = OpenAICompatClient(base_url=self.base_url, api_key=self.api_key, timeout_s=60.0)

    def close(self) -> None:
        self.client.close()

    def generate_batch(self, texts: list[str]) -> list[list[float]]:
        normalized = [" ".join((t or "").split()) for t in texts]
        return self.client.embeddings(model=self.model, inputs=normalized)

