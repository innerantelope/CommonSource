"""
embed.py — CommonSource embedding service.

Used by ingestion pipelines, CLI search, retrieval layer, and Flask API.

Default model: sentence-transformers/all-MiniLM-L6-v2 (384-dim).
Override with COMMONSOURCE_EMBED_MODEL (e.g. paraphrase-multilingual-MiniLM-L12-v2 for legacy corpus).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

from core.config import (
    EMBED_CACHE_DIR,
    EMBED_MAX_CHARS,
    EMBED_MODEL,
    EMBED_VECTOR_SIZE,
    OLLAMA_BASE,
    OLLAMA_EMBED_MODEL,
)
from utils.vectors import blob_to_embedding, embedding_to_blob

log = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None
_ready = threading.Event()
_ollama_lock = threading.Lock()


def check_ollama_available(base_url: str = OLLAMA_BASE) -> bool:
    try:
        import socket
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or "127.0.0.1"
        port = urlparse(base_url).port or 11434
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


def _cache_path(text: str, model_name: str) -> Path:
    key = hashlib.sha256(f"{model_name}\n{text[:EMBED_MAX_CHARS]}".encode("utf-8")).hexdigest()
    return EMBED_CACHE_DIR / f"{key}.json"


def _read_cache(text: str, model_name: str) -> Optional[List[float]]:
    path = _cache_path(text, model_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        vec = data.get("vector")
        if isinstance(vec, list) and len(vec) == EMBED_VECTOR_SIZE:
            return [float(x) for x in vec]
    except Exception:
        return None
    return None


def _write_cache(text: str, model_name: str, vec: List[float]) -> None:
    try:
        EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(text, model_name)
        path.write_text(
            json.dumps({"model": model_name, "vector": vec}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("embedding cache write skipped: %s", exc)


def _load_model():
    global _model
    from sentence_transformers import SentenceTransformer

    with _lock:
        if _model is None:
            log.info("Loading embedding model: %s", EMBED_MODEL)
            _model = SentenceTransformer(EMBED_MODEL)
    _ready.set()
    return _model


def warmup_embeddings() -> None:
    try:
        embed_text("warmup", use_cache=False)
    except Exception as exc:
        log.warning("embedding warmup failed: %s", exc)
    finally:
        _ready.set()


def embed_text(text: str, *, use_cache: bool = True) -> Optional[List[float]]:
    """Embed a single string with the local sentence-transformers model."""
    text = (text or "").strip()
    if not text:
        return None

    if use_cache:
        cached = _read_cache(text, EMBED_MODEL)
        if cached is not None:
            return cached

    model = _load_model()
    truncated = text[:EMBED_MAX_CHARS]
    with _lock:
        vec = model.encode(truncated, normalize_embeddings=True).tolist()

    if use_cache:
        _write_cache(text, EMBED_MODEL, vec)
    return vec


def embed_batch(texts: Sequence[str], *, use_cache: bool = True, batch_size: int = 32) -> List[Optional[List[float]]]:
    """Batch-embed multiple texts (efficient for ingestion)."""
    cleaned = [(t or "").strip() for t in texts]
    out: List[Optional[List[float]]] = [None] * len(cleaned)
    pending_idx: List[int] = []
    pending_text: List[str] = []

    for i, text in enumerate(cleaned):
        if not text:
            continue
        if use_cache:
            cached = _read_cache(text, EMBED_MODEL)
            if cached is not None:
                out[i] = cached
                continue
        pending_idx.append(i)
        pending_text.append(text[:EMBED_MAX_CHARS])

    if not pending_text:
        return out

    model = _load_model()
    with _lock:
        for start in range(0, len(pending_text), batch_size):
            batch = pending_text[start : start + batch_size]
            vecs = model.encode(batch, normalize_embeddings=True, batch_size=batch_size)
            for offset, vec in enumerate(vecs):
                i = pending_idx[start + offset]
                v = vec.tolist() if hasattr(vec, "tolist") else list(vec)
                out[i] = v
                if use_cache and cleaned[i]:
                    _write_cache(cleaned[i], EMBED_MODEL, v)
    return out


def embed_ollama(text: str, *, model: str = OLLAMA_EMBED_MODEL, base_url: str = OLLAMA_BASE) -> Optional[List[float]]:
    if not check_ollama_available(base_url):
        return None
    try:
        import requests

        r = requests.post(
            f"{base_url}/api/embeddings",
            json={"model": model, "prompt": text[:EMBED_MAX_CHARS]},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("embedding")
    except Exception as exc:
        log.debug("Ollama embed failed: %s", exc)
        return None


def generate_embedding(
    text: str,
    *,
    method: str = "local",
    ollama_model: str = OLLAMA_EMBED_MODEL,
    local_model: Optional[str] = None,  # noqa: ARG001 — kept for ingestion API compat
    ollama_base_url: str = OLLAMA_BASE,
    use_cache: bool = True,
) -> Optional[List[float]]:
    """
  Ingestion-compatible entry point.

  method: local | ollama | none
    """
    if method == "none":
        return None
    if method == "ollama":
        vec = embed_ollama(text, model=ollama_model, base_url=ollama_base_url)
        if vec:
            return vec
    return embed_text(text, use_cache=use_cache)


def embed_query(text: str) -> Optional[List[float]]:
    """Query embedding for retrieval (local first, Ollama fallback)."""
    vec = embed_text(text)
    if vec:
        return vec
    return embed_ollama(text)


# Legacy aliases used by search_api / search.py
def embed(text: str) -> Optional[List[float]]:
    return embed_query(text)


def rank_by_similarity(
    query_vec: List[float],
    chunks: Iterable[dict],
    *,
    top_k: int = 10,
    min_score: float = 0.0,
) -> List[dict]:
    """Rank chunk dicts that have embedding_blob or vector field."""
    scored = []
    for chunk in chunks:
        vec = chunk.get("vector")
        if vec is None:
            vec = blob_to_embedding(chunk.get("embedding_blob"))
        if not vec:
            continue
        score = cosine_similarity(query_vec, vec)
        if score >= min_score:
            item = dict(chunk)
            item["score"] = score
            scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
