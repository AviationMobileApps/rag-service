FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Optional bake step for offline runtime (downloads model weights at build time).
ARG BAKE_RERANKER=1
ARG BAKE_RERANKER_MODEL=BAAI/bge-reranker-base

# System deps (keep minimal; wheels cover most)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir -U pip \
  && python -m pip install --no-cache-dir .

ENV MODEL_CACHE_DIR=/opt/models
ENV HF_HOME=/opt/models/hf
ENV TRANSFORMERS_CACHE=/opt/models/transformers
ENV SENTENCE_TRANSFORMERS_HOME=/opt/models/sentence-transformers

# Pre-download reranker weights into the image so runtime can be fully offline.
RUN if [ "$BAKE_RERANKER" = "1" ]; then \
    BAKE_RERANKER_MODEL="$BAKE_RERANKER_MODEL" python -c "import os; from sentence_transformers import CrossEncoder; m=os.environ['BAKE_RERANKER_MODEL']; CrossEncoder(m, max_length=512); print('baked_reranker', m)"; \
  fi

EXPOSE 8021

CMD ["uvicorn", "rag_service.api.main:app", "--host", "0.0.0.0", "--port", "8021"]
