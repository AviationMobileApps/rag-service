from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from rag_service.llm.client import LLMClient


logger = structlog.get_logger()


ENTITY_EXTRACTION_SYSTEM_PROMPT = """You are EntityExtractor, used inside a RAG ingestion pipeline.

Extract entities and key concepts that are explicitly mentioned in the provided text chunk.

Output MUST be valid JSON and MUST match this schema:
{
  "entities": [
    {"type": "company", "name": "Acme Corp"},
    {"type": "person", "name": "Jane Doe"},
    {"type": "product", "name": "Widget 2.0"},
    {"type": "concept", "name": "support and resistance"}
  ]
}

Rules:
- Return only entities present in the text (no guesses).
- Use short, lowercase `type` strings (snake_case).
- Prefer fewer, higher-signal entities over exhaustive lists.
- Limit to at most 25 entities.
"""


@dataclass(frozen=True)
class Entity:
    type: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "name": self.name}


def _clean_type(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[\s\-]+", "_", value)
    value = re.sub(r"[^a-z0-9_]", "", value)
    return value[:48]


def _clean_name(value: str) -> str:
    value = " ".join((value or "").strip().split())
    return value[:200]


class EntityExtractor:
    def __init__(self, *, llm: LLMClient, max_entities: int = 25, llm_max_tokens: int = 1200):
        self.llm = llm
        self.max_entities = max_entities
        self.llm_max_tokens = llm_max_tokens

    def extract(self, text: str, *, metadata: Optional[dict[str, Any]] = None) -> list[Entity]:
        user_prompt = f"""Extract entities from this text chunk:\n\n{text}\n\nReturn JSON with an 'entities' array."""
        try:
            data, meta = self.llm.generate_json(
                system_prompt=ENTITY_EXTRACTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=self.llm_max_tokens,
            )
        except Exception as e:
            logger.warning("entity_extraction_failed", error=str(e))
            return []

        raw_entities: list[dict[str, Any]] = []
        if isinstance(data, dict) and isinstance(data.get("entities"), list):
            raw_entities = [e for e in data.get("entities") if isinstance(e, dict)]
        elif isinstance(data, list):
            raw_entities = [e for e in data if isinstance(e, dict)]

        out: list[Entity] = []
        seen: set[tuple[str, str]] = set()
        for e in raw_entities:
            if len(out) >= self.max_entities:
                break
            et = _clean_type(str(e.get("type") or ""))
            name = _clean_name(str(e.get("name") or ""))
            if not et or len(name) < 2:
                continue
            key = (et, name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(Entity(type=et, name=name))

        logger.debug("entities_extracted", count=len(out), model=meta.get("model"), timing_ms=meta.get("timing_ms"))
        return out

