FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (keep minimal; wheels cover most)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir -U pip \
  && python -m pip install --no-cache-dir .

ENV MODEL_CACHE_DIR=/models
ENV HF_HOME=/models/hf
ENV TRANSFORMERS_CACHE=/models/transformers
ENV SENTENCE_TRANSFORMERS_HOME=/models/sentence-transformers

EXPOSE 8021

CMD ["uvicorn", "rag_service.api.main:app", "--host", "0.0.0.0", "--port", "8021"]

