from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    api_key: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_ignore_empty=True)

    rag_api_port: int = Field(default=8021, alias="RAG_API_PORT")

    database_url: str = Field(
        default="postgresql+psycopg://rag:rag@localhost:5432/rag",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_queue: str = Field(default="rag_ingestion_queue", alias="REDIS_QUEUE")
    redis_progress_channel: str = Field(default="ingestion_progress", alias="REDIS_PROGRESS_CHANNEL")

    weaviate_host: str = Field(default="localhost", alias="WEAVIATE_HOST")
    weaviate_port: int = Field(default=8080, alias="WEAVIATE_PORT")
    weaviate_grpc_port: int = Field(default=50051, alias="WEAVIATE_GRPC_PORT")
    weaviate_collection: str = Field(default="ResearchChunk", alias="WEAVIATE_COLLECTION")

    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="rag-service", alias="NEO4J_PASSWORD")
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")

    rag_data_dir: str = Field(default="/data", alias="RAG_DATA_DIR")

    rag_tenants_json: str = Field(
        default='[{"tenant_id":"signal305","api_key":"dev-signal305-key"}]',
        alias="RAG_TENANTS_JSON",
    )

    embeddings_base_url: str = Field(default="http://localhost:1234", alias="EMBEDDINGS_BASE_URL")
    embeddings_model: str = Field(
        default="text-embedding-nomic-embed-text-v1.5-embedding",
        alias="EMBEDDINGS_MODEL",
    )
    embeddings_api_key: Optional[str] = Field(default=None, alias="EMBEDDINGS_API_KEY")

    llm_base_url: str = Field(default="http://localhost:1234", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-oss-120b", alias="LLM_MODEL")
    llm_api_key: Optional[str] = Field(default=None, alias="LLM_API_KEY")
    llm_timeout_s: float = Field(default=300.0, alias="LLM_TIMEOUT_S")

    reranker_enabled: bool = Field(default=True, alias="RERANKER_ENABLED")
    reranker_model: str = Field(default="BAAI/bge-reranker-base", alias="RERANKER_MODEL")
    rerank_oversample: int = Field(default=3, alias="RERANK_OVERSAMPLE")

    dynamic_chunking_enabled: bool = Field(default=True, alias="DYNAMIC_CHUNKING_ENABLED")
    chunker_window_tokens: int = Field(default=16000, alias="CHUNKER_WINDOW_TOKENS")
    chunker_overlap_tokens: int = Field(default=1000, alias="CHUNKER_OVERLAP_TOKENS")
    chunker_llm_max_tokens: int = Field(default=20000, alias="CHUNKER_LLM_MAX_TOKENS")
    chunker_tokenizer_model: str = Field(default="cl100k_base", alias="CHUNKER_TOKENIZER_MODEL")

    entity_extraction_max_entities: int = Field(default=25, alias="ENTITY_EXTRACTION_MAX_ENTITIES")

    graph_enabled: bool = Field(default=True, alias="GRAPH_ENABLED")
    graph_expansion_enabled: bool = Field(default=True, alias="GRAPH_EXPANSION_ENABLED")
    graph_seed_limit: int = Field(default=8, alias="GRAPH_SEED_LIMIT")
    graph_seed_min_rerank_score: float = Field(default=0.2, alias="GRAPH_SEED_MIN_RERANK_SCORE")
    graph_expansion_limit: int = Field(default=20, alias="GRAPH_EXPANSION_LIMIT")
    graph_entity_limit: int = Field(default=25, alias="GRAPH_ENTITY_LIMIT")

    def tenants(self) -> list[Tenant]:
        try:
            raw = json.loads(self.rag_tenants_json)
            if not isinstance(raw, list):
                raise ValueError("RAG_TENANTS_JSON must be a JSON array")
            out: list[Tenant] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                tenant_id = str(item.get("tenant_id") or "").strip()
                api_key = str(item.get("api_key") or "").strip()
                if tenant_id and api_key:
                    out.append(Tenant(tenant_id=tenant_id, api_key=api_key))
            return out
        except Exception:
            return []

    def tenant_id_for_api_key(self, api_key: str) -> Optional[str]:
        for t in self.tenants():
            if t.api_key == api_key:
                return t.tenant_id
        return None


settings = Settings()
