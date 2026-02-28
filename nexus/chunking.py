# Version: v1.0
"""
nexus.chunking — Document chunking utilities for large document ingestion.

Splits documents exceeding MAX_DOCUMENT_SIZE into smaller chunks while
preserving context via overlap. Uses LlamaIndex's SentenceSplitter for
intelligent sentence-boundary-aware splitting.
"""

from llama_index.core.node_parser import SentenceSplitter

from nexus.config import (
    logger,
    MAX_DOCUMENT_SIZE,
    INGEST_CHUNK_SIZE,
    INGEST_CHUNK_OVERLAP,
)


def needs_chunking(text: str) -> bool:
    """Check if a document exceeds MAX_DOCUMENT_SIZE and needs chunking.

    Args:
        text: The document text to check.

    Returns:
        True if the document exceeds MAX_DOCUMENT_SIZE bytes, False otherwise.
    """
    return len(text.encode("utf-8")) > MAX_DOCUMENT_SIZE


def chunk_document(text: str) -> list[str]:
    """Split a large document into smaller chunks.

    Uses LlamaIndex's SentenceSplitter for intelligent sentence-boundary
    splitting. Chunks are sized according to INGEST_CHUNK_SIZE with
    INGEST_CHUNK_OVERLAP overlap for context preservation.

    Args:
        text: The document text to chunk.

    Returns:
        List of text chunks. If the document doesn't need chunking,
        returns a single-element list with the original text.
    """
    if not needs_chunking(text):
        return [text]

    doc_size = len(text.encode("utf-8"))
    logger.info(
        f"Chunking large document: {doc_size} bytes > {MAX_DOCUMENT_SIZE} byte limit"
    )

    splitter = SentenceSplitter(
        chunk_size=INGEST_CHUNK_SIZE,
        chunk_overlap=INGEST_CHUNK_OVERLAP,
    )

    chunks = splitter.split_text(text)
    logger.info(f"Document split into {len(chunks)} chunks")
    return chunks
