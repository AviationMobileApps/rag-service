"""
LLM-driven dynamic chunking for RAG.

This is ported from Signal305 and adapted to use an OpenAI-compatible endpoint
(LM Studio) via `rag_service.llm.client.LLMClient`.

The goal is structure-aware chunking (headings/lists/semantic cards) rather than
naive fixed-size splitting.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional

import structlog

try:
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

from rag_service.llm.client import LLMClient


logger = structlog.get_logger()


@dataclass
class PageText:
    page: int  # 1-based page number
    text: str


@dataclass
class SectionSpan:
    section: str
    start_page: int
    end_page: int


@dataclass
class Chunk:
    text: str
    chunk_id: str
    doc_id: str
    doc_type: str
    metadata: dict[str, Any]
    start_char: int
    end_char: int
    section: str
    title: str
    pages: list[int]
    summary: str
    why_this_chunk: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DYNAMIC_CHUNKER_SYSTEM_PROMPT = """You are "DynamicChunker", a model used inside a RAG ingestion pipeline.
Your ONLY job is to split a single document into variable-length, semantically coherent chunks.

You return ONLY a single valid JSON array of chunk objects. No extra text, no prose, no Markdown.

Return JSON like:
[
  {
    "chunk_id": 0,
    "section": "front_matter",
    "title": "Disclaimer & usage notice",
    "pages": [2],
    "text": "...exact document text for this chunk...",
    "summary": "1–3 sentences describing this chunk.",
    "why_this_chunk": "One short sentence explaining why this boundary makes sense."
  }
]

Rules:
- Top-level MUST be a JSON array.
- Each element MUST be an object with keys: chunk_id, section, title, pages, text, summary, why_this_chunk.
- chunk_id is an integer starting at 0 and increasing by 1 in document order.
- text MUST be copied from the document (no paraphrasing).
- Do NOT invent content.

Chunking goals:
- Respect headings, subheadings, lists, and repeated "cards"/templates.
- Prefer semantic completeness over rigid length.
- Target ~200–600 tokens per chunk when possible.
- Hard max ~800 tokens per chunk (split on sub-headings/paragraph breaks if needed).
- Never split inside a sentence or inside a list item/bullet.
"""


def extract_text_from_pdf(file_path: str) -> list[PageText]:
    if fitz is None:  # pragma: no cover
        raise RuntimeError("pymupdf is required for PDF extraction. Install via: pip install pymupdf")

    pages: list[PageText] = []
    with fitz.open(file_path) as doc:
        for idx, page in enumerate(doc, start=1):
            try:
                text = page.get_text("text") or ""
            except Exception as e:
                logger.warning("pdf_page_extraction_failed", page=idx, error=str(e))
                text = ""
            if text.strip():
                pages.append(PageText(page=idx, text=text))
    return pages


def extract_text_from_text_file(file_path: str, max_chars_per_page: int = 12000) -> list[PageText]:
    full_text = ""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        full_text = f.read()

    if not full_text.strip():
        return []

    paragraphs = re.split(r"\n\s*\n", full_text)
    pages: list[PageText] = []
    current: list[str] = []
    current_chars = 0
    page_num = 1

    for para in paragraphs:
        para_len = len(para)
        if current and current_chars + para_len > max_chars_per_page:
            pages.append(PageText(page=page_num, text="\n\n".join(current)))
            page_num += 1
            current = []
            current_chars = 0
        current.append(para)
        current_chars += para_len

    if current:
        pages.append(PageText(page=page_num, text="\n\n".join(current)))

    return pages


def _get_encoder(model: str):
    if tiktoken is None:  # pragma: no cover
        raise RuntimeError("tiktoken is required for token counting. Install via: pip install tiktoken")
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def make_windows_with_overlap(
    pages: list[PageText],
    *,
    max_tokens: int,
    overlap_tokens: int,
    tokenizer_model: str,
) -> list[dict[str, Any]]:
    encoder = _get_encoder(tokenizer_model)

    windows: list[dict[str, Any]] = []
    buffer: list[str] = []
    current_pages: list[int] = []
    current_tokens = 0

    for page in pages:
        page_tokens = len(encoder.encode(page.text))
        buffer.append(page.text)
        current_pages.append(page.page)
        current_tokens += page_tokens

        if current_tokens >= max_tokens:
            full_text = "\n\n".join(buffer)
            overlap_ratio = (overlap_tokens / current_tokens) if current_tokens else 0.0
            overlap_chars = int(len(full_text) * overlap_ratio)
            overlap_start = max(0, len(full_text) - overlap_chars)

            windows.append(
                {
                    "text": full_text,
                    "overlap_start": overlap_start,
                    "pages": current_pages.copy(),
                    "token_count": current_tokens,
                }
            )

            overlap_text = full_text[overlap_start:]
            overlap_token_count = len(encoder.encode(overlap_text))
            buffer = [overlap_text]
            current_pages = [current_pages[-1]]
            current_tokens = overlap_token_count

    if buffer:
        full_text = "\n\n".join(buffer)
        windows.append({"text": full_text, "overlap_start": 0, "pages": current_pages.copy(), "token_count": current_tokens})

    return windows


def build_user_message(*, window_text: str, overlap_start: int, section: str) -> str:
    context = window_text[:overlap_start]
    new_text = window_text[overlap_start:]

    message_lines: list[str] = []

    if section and section != "unknown":
        message_lines.append(f'You are currently chunking section: "{section}".')

    if context:
        message_lines.append(
            "The text before the marker '=== NEW WINDOW START ===' is overlap from the previous window. "
            "It has ALREADY been chunked. Use it only as context and DO NOT create new chunks from it."
        )
    else:
        message_lines.append("There is no overlap from the previous window. Everything below is new content to chunk.")

    message_lines.append("\n=== DOCUMENT TEXT ===")
    if context:
        message_lines.append(context)
    message_lines.append("\n=== NEW WINDOW START ===")
    message_lines.append(new_text)

    return "\n".join(message_lines)


def call_dynamic_chunker(*, llm: LLMClient, user_message: str, max_tokens: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        data, meta = llm.generate_json(system_prompt=DYNAMIC_CHUNKER_SYSTEM_PROMPT, user_prompt=user_message, max_tokens=max_tokens)
    except Exception as e:
        logger.warning("dynamic_chunker_llm_failed", error=str(e))
        return [], {"error": str(e)}

    if not isinstance(data, list):
        logger.warning("dynamic_chunker_invalid_top_level", type=str(type(data)))
        return [], meta

    return data, meta


def filter_overlap_chunks(raw_chunks: list[dict[str, Any]], *, overlap_start: int, window_text: str) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for chunk in raw_chunks:
        text = str(chunk.get("text") or "")
        start_idx = window_text.find(text)
        if start_idx == -1:
            filtered.append(chunk)
            continue
        end_idx = start_idx + len(text)
        if end_idx > overlap_start:
            filtered.append(chunk)
    return filtered


def validate_chunk(chunk_dict: dict[str, Any]) -> bool:
    required = ["chunk_id", "section", "title", "pages", "text", "summary", "why_this_chunk"]
    return all(k in chunk_dict for k in required) and isinstance(chunk_dict.get("text"), str) and bool(chunk_dict.get("text"))


def _calculate_chunk_pages(start_char: int, end_char: int, pages: list[PageText]) -> list[int]:
    current_offset = 0
    page_ranges: list[tuple[int, int, int]] = []
    for page in pages:
        page_start = current_offset
        page_end = page_start + len(page.text) + 2  # "\n\n"
        page_ranges.append((page.page, page_start, page_end))
        current_offset = page_end

    chunk_pages: list[int] = []
    for page_num, page_start, page_end in page_ranges:
        if start_char < page_end and end_char > page_start:
            chunk_pages.append(page_num)
    return chunk_pages


def chunk_pages(
    *,
    doc_id: str,
    pages: list[PageText],
    llm: LLMClient,
    doc_type: str = "document",
    metadata: Optional[dict[str, Any]] = None,
    max_window_tokens: int = 16000,
    overlap_tokens: int = 1000,
    llm_max_tokens: int = 20000,
    tokenizer_model: str = "cl100k_base",
) -> list[Chunk]:
    if not pages:
        return []

    windows = make_windows_with_overlap(
        pages,
        max_tokens=max_window_tokens,
        overlap_tokens=overlap_tokens,
        tokenizer_model=tokenizer_model,
    )

    full_doc_text = "\n\n".join(p.text for p in pages)
    char_offset = 0
    all_chunks: list[Chunk] = []

    for i, win in enumerate(windows):
        user_message = build_user_message(window_text=win["text"], overlap_start=win["overlap_start"], section="unknown")
        logger.info("chunking_window", doc_id=doc_id, window=f"{i+1}/{len(windows)}", pages=win["pages"], tokens=win["token_count"])

        raw_chunks, meta = call_dynamic_chunker(llm=llm, user_message=user_message, max_tokens=llm_max_tokens)
        if not raw_chunks:
            logger.warning("chunker_window_no_chunks", doc_id=doc_id, window=i + 1, meta=meta)
            continue

        filtered = filter_overlap_chunks(raw_chunks, overlap_start=win["overlap_start"], window_text=win["text"])
        for chunk_dict in filtered:
            if not validate_chunk(chunk_dict):
                continue

            chunk_text = str(chunk_dict.get("text") or "")
            start_char = full_doc_text.find(chunk_text, char_offset)
            if start_char == -1:
                start_char = char_offset
            end_char = start_char + len(chunk_text)
            char_offset = end_char

            chunk_pages = _calculate_chunk_pages(start_char, end_char, pages)
            all_chunks.append(
                Chunk(
                    text=chunk_text,
                    chunk_id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    doc_type=doc_type,
                    metadata=metadata or {},
                    start_char=start_char,
                    end_char=end_char,
                    section=str(chunk_dict.get("section") or "unknown"),
                    title=str(chunk_dict.get("title") or "Untitled"),
                    pages=chunk_pages,
                    summary=str(chunk_dict.get("summary") or ""),
                    why_this_chunk=str(chunk_dict.get("why_this_chunk") or ""),
                )
            )

    logger.info("dynamic_chunking_complete", doc_id=doc_id, chunks=len(all_chunks), windows=len(windows))
    return all_chunks


def chunk_text_file(
    *,
    doc_id: str,
    text_path: str,
    llm: LLMClient,
    doc_type: str = "document",
    metadata: Optional[dict[str, Any]] = None,
    max_window_tokens: int = 16000,
    overlap_tokens: int = 1000,
    llm_max_tokens: int = 20000,
    tokenizer_model: str = "cl100k_base",
) -> list[Chunk]:
    pages = extract_text_from_text_file(text_path)
    return chunk_pages(
        doc_id=doc_id,
        pages=pages,
        llm=llm,
        doc_type=doc_type,
        metadata=metadata,
        max_window_tokens=max_window_tokens,
        overlap_tokens=overlap_tokens,
        llm_max_tokens=llm_max_tokens,
        tokenizer_model=tokenizer_model,
    )


def chunk_pdf_file(
    *,
    doc_id: str,
    pdf_path: str,
    llm: LLMClient,
    doc_type: str = "document",
    metadata: Optional[dict[str, Any]] = None,
    max_window_tokens: int = 16000,
    overlap_tokens: int = 1000,
    llm_max_tokens: int = 20000,
    tokenizer_model: str = "cl100k_base",
) -> list[Chunk]:
    pages = extract_text_from_pdf(pdf_path)
    return chunk_pages(
        doc_id=doc_id,
        pages=pages,
        llm=llm,
        doc_type=doc_type,
        metadata=metadata,
        max_window_tokens=max_window_tokens,
        overlap_tokens=overlap_tokens,
        llm_max_tokens=llm_max_tokens,
        tokenizer_model=tokenizer_model,
    )
