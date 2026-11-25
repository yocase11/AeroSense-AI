from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import Document, DocumentChunk


MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _chunks(text: str) -> list[str]:
    normalized = _clean_text(text)
    parts = []
    start = 0
    while start < len(normalized):
        end = min(start + CHUNK_SIZE, len(normalized))
        parts.append(normalized[start:end])
        if end == len(normalized):
            break
        start = end - CHUNK_OVERLAP
    return parts


def _extract_document(filename: str, content: bytes) -> list[tuple[str, int | None]]:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 5 MB upload limit.")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"txt", "md"}:
        try:
            return [(content.decode("utf-8-sig"), None)]
        except UnicodeDecodeError as error:
            raise ValueError("Text documents must be UTF-8 encoded.") from error
    if suffix == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=True)
        if len(reader.pages) > 250:
            raise ValueError("PDF exceeds the 250-page limit.")
        return [
            (page.extract_text() or "", page_number)
            for page_number, page in enumerate(reader.pages, start=1)
        ]
    raise ValueError("Only PDF, TXT, and Markdown documents are supported.")


def _embed(texts: list[str]) -> list[list[float]] | None:
    settings = get_settings()
    if not settings.gemini_api_key:
        return None
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    embeddings = []
    for text in texts:
        response = client.models.embed_content(
            model="gemini-embedding-001",
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=768),
        )
        if not response.embeddings or not response.embeddings[0].values:
            raise RuntimeError("Gemini returned no embedding for a document chunk.")
        vector = [float(value) for value in response.embeddings[0].values]
        if len(vector) != 768:
            raise RuntimeError("Gemini embedding dimension did not match pgvector schema.")
        embeddings.append(vector)
    return embeddings


def ingest_document(
    session: Session,
    *,
    title: str,
    filename: str,
    content: bytes,
    source_url: str | None = None,
    publication_date: datetime | None = None,
) -> dict[str, Any]:
    if not title.strip():
        raise ValueError("Document title is required.")
    pages = _extract_document(filename, content)
    full_text = "\n".join(text for text, _ in pages)
    if not full_text.strip():
        raise ValueError("No extractable text was found in the document.")
    content_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(Document).where(Document.content_hash == content_hash)
    )
    if existing is not None:
        return {"document_id": existing.id, "status": "already_ingested"}

    page_chunks = []
    for page_text, page_number in pages:
        page_chunks.extend((chunk, page_number) for chunk in _chunks(page_text))
    vectors = _embed([text for text, _ in page_chunks])
    document = Document(
        title=title.strip()[:500],
        source_url=source_url,
        publication_date=publication_date,
        content_hash=content_hash,
        created_at=datetime.now(timezone.utc),
        metadata_json={"filename": filename, "page_count": len(pages)},
    )
    session.add(document)
    session.flush()
    for index, (text, page_number) in enumerate(page_chunks):
        session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                text=text,
                page_number=page_number,
                embedding=vectors[index] if vectors else None,
                created_at=datetime.now(timezone.utc),
            )
        )
    return {
        "document_id": document.id,
        "chunks": len(page_chunks),
        "semantic_search": vectors is not None,
        "status": "ingested",
    }


def search_documents(
    session: Session, query: str, limit: int = 5
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("Search query is required.")
    settings = get_settings()
    if settings.gemini_api_key:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        embedded = client.models.embed_content(
            model="gemini-embedding-001",
            contents=query,
            config=types.EmbedContentConfig(output_dimensionality=768),
        )
        vector = embedded.embeddings[0].values
        chunks = session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(DocumentChunk.embedding.cosine_distance(vector))
            .limit(limit)
        ).all()
        mode = "pgvector_cosine"
    else:
        terms = [term for term in re.findall(r"[\w.-]+", query.lower()) if len(term) > 2]
        if not terms:
            return {"available": False, "mode": "lexical", "results": []}
        candidates = session.scalars(
            select(DocumentChunk).where(DocumentChunk.text.ilike(f"%{terms[0]}%")).limit(100)
        ).all()
        ranked = sorted(
            candidates,
            key=lambda chunk: sum(chunk.text.lower().count(term) for term in terms),
            reverse=True,
        )
        chunks = ranked[:limit]
        mode = "lexical_fallback"

    results = []
    for chunk in chunks:
        document = session.get(Document, chunk.document_id)
        if document is None:
            continue
        results.append(
            {
                "document_id": document.id,
                "title": document.title,
                "source_url": document.source_url,
                "page_number": chunk.page_number,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
            }
        )
    return {
        "available": bool(results),
        "mode": mode,
        "results": results,
        "message": None if results else "No matching ingested documents were found.",
    }
