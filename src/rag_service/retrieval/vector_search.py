from __future__ import annotations

from typing import Any, Optional

import weaviate
import weaviate.classes as wvc
from weaviate.classes.config import Configure, Property, DataType

from rag_service.config.settings import settings
from rag_service.retrieval.embeddings import EmbeddingGenerator


class VectorSearch:
    def __init__(self, embedding_generator: Optional[EmbeddingGenerator] = None):
        self.embedding_generator = embedding_generator or EmbeddingGenerator()
        self.client = weaviate.connect_to_local(host=settings.weaviate_host, port=settings.weaviate_port)

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.embedding_generator.close()

    def ensure_schema(self) -> None:
        if self.client.collections.exists(settings.weaviate_collection):
            return

        self.client.collections.create(
            name=settings.weaviate_collection,
            description="RAG document chunks with vector embeddings for hybrid retrieval",
            vectorizer_config=Configure.Vectorizer.none(),
            properties=[
                Property(name="text", data_type=DataType.TEXT, index_searchable=True),
                Property(name="title", data_type=DataType.TEXT, index_searchable=True),
                Property(name="section", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="summary", data_type=DataType.TEXT, index_searchable=True),
                Property(name="pages", data_type=DataType.INT_ARRAY, skip_vectorization=True),
                Property(name="whyThisChunk", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="docType", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="chunkId", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="parentDocId", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="createdAt", data_type=DataType.DATE, skip_vectorization=True),
                Property(name="metadata", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="startChar", data_type=DataType.INT, skip_vectorization=True),
                Property(name="endChar", data_type=DataType.INT, skip_vectorization=True),
                # Scoping
                Property(name="tenantId", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="scope", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="workspaceId", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="principalId", data_type=DataType.TEXT, skip_vectorization=True),
            ],
        )

    def add_chunks(self, chunks: list[dict[str, Any]]) -> list[str]:
        collection = self.client.collections.get(settings.weaviate_collection)

        # embeddings
        texts = [c["text"] for c in chunks]
        vectors = self.embedding_generator.generate_batch(texts)
        for c, v in zip(chunks, vectors):
            c["vector"] = v

        inserted: list[str] = []
        with collection.batch.dynamic() as batch:
            for chunk in chunks:
                properties = dict(chunk["properties"])
                batch.add_object(properties=properties, vector=chunk["vector"])
        # Weaviate batch does not return UUIDs directly here; fetch by filtering later if needed.
        return inserted

    def search(self, query: str, filters: Optional[wvc.query.Filter] = None, limit: int = 20, alpha: float = 0.5) -> list[dict[str, Any]]:
        collection = self.client.collections.get(settings.weaviate_collection)

        query_vector = None
        if alpha > 0:
            query_vector = self.embedding_generator.generate_batch([query])[0]

        resp = collection.query.hybrid(
            query=query,
            vector=query_vector,
            alpha=alpha,
            limit=limit,
            filters=filters,
            return_metadata=wvc.query.MetadataQuery(score=True),
        )

        out: list[dict[str, Any]] = []
        for obj in resp.objects:
            out.append(
                {
                    "weaviate_uuid": str(obj.uuid),
                    "score": getattr(obj.metadata, "score", None),
                    "properties": obj.properties,
                }
            )
        return out

