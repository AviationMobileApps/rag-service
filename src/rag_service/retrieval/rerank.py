from __future__ import annotations

import os
from functools import lru_cache

from sentence_transformers import CrossEncoder

from rag_service.config.settings import settings


@lru_cache(maxsize=1)
def _get_reranker() -> CrossEncoder:
    # Ensure model cache is honored inside Docker.
    model_cache_dir = os.getenv("SENTENCE_TRANSFORMERS_HOME") or os.getenv("MODEL_CACHE_DIR")
    if model_cache_dir:
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", model_cache_dir)
    return CrossEncoder(settings.reranker_model, max_length=512)


def rerank(query: str, candidates: list[dict], text_key: str = "text") -> list[dict]:
    if not settings.reranker_enabled:
        return candidates

    model = _get_reranker()
    pairs = [(query, c.get(text_key) or "") for c in candidates]
    scores = model.predict(pairs)

    out = []
    for c, s in zip(candidates, scores):
        c2 = dict(c)
        c2["rerank_score"] = float(s)
        out.append(c2)

    out.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    return out

