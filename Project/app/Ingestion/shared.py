"""Shared ingestion helpers — consistent imports for pipelines."""

from __future__ import annotations

from embed import embed_batch, embed_text, generate_embedding, embedding_to_blob
from utils.vectors import blob_to_embedding

# Re-export chunk_text from primary ingest module
from pathlib import Path
import sys

_INGESTION_DIR = Path(__file__).resolve().parents[1] / "Ingestion"
if str(_INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(_INGESTION_DIR))

from ingest_commonsource import chunk_text  # noqa: E402

__all__ = [
    "chunk_text",
    "embed_text",
    "embed_batch",
    "generate_embedding",
    "embedding_to_blob",
    "blob_to_embedding",
]
