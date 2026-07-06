"""Qdrant vector store for ANN chunk retrieval."""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import (
    EMBED_VECTOR_SIZE,
    QDRANT_API_KEY,
    QDRANT_COLLECTION,
    QDRANT_SEARCH_LIMIT,
    QDRANT_URL,
    USE_QDRANT,
)
from embed import embed_text
from utils.vectors import blob_to_embedding

log = logging.getLogger(__name__)

_client = None
_available: Optional[bool] = None
_last_failure_ts = 0.0
QDRANT_RETRY_SECONDS = float(os.getenv("QDRANT_RETRY_SECONDS", "15"))


def reset_client_cache() -> None:
    """Clear cached Qdrant availability after an operational change."""
    global _client, _available, _last_failure_ts
    _client = None
    _available = None
    _last_failure_ts = 0.0


def _parse_point_id(chunk_row_id: str) -> str:
    """Qdrant point id must be UUID or int — derive stable UUID from chunk id."""
    try:
        uuid.UUID(str(chunk_row_id))
        return str(chunk_row_id)
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"commonsource:{chunk_row_id}"))


def get_client():
    global _client, _available, _last_failure_ts
    if _client is not None:
        return _client
    if _available is False and time.time() - _last_failure_ts < QDRANT_RETRY_SECONDS:
        return None
    try:
        from qdrant_client import QdrantClient

        try:
            _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=3.0)
        except TypeError:
            _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        _client.get_collections()
        _available = True
        return _client
    except Exception as exc:
        log.warning("Qdrant unavailable: %s", exc)
        _client = None
        _available = False
        _last_failure_ts = time.time()
        return None


def is_qdrant_available() -> bool:
    if not USE_QDRANT:
        return False
    global _available
    if _available is False and time.time() - _last_failure_ts >= QDRANT_RETRY_SECONDS:
        _available = None
    if _available is not None:
        return _available
    return get_client() is not None


def qdrant_health() -> Dict[str, Any]:
    """Return an API-safe health report for operators and diagnostics."""
    report: Dict[str, Any] = {
        "configured": USE_QDRANT,
        "available": False,
        "url": QDRANT_URL,
        "collection": QDRANT_COLLECTION,
        "collection_exists": False,
        "points": 0,
        "vector_size": EMBED_VECTOR_SIZE,
        "error": None,
    }
    if not USE_QDRANT:
        report["error"] = "Qdrant disabled by COMMONSOURCE_USE_QDRANT"
        return report

    try:
        client = get_client()
        if not client:
            report["error"] = "Qdrant client unavailable"
            return report

        report["available"] = True
        names = {c.name for c in client.get_collections().collections}
        report["collection_exists"] = QDRANT_COLLECTION in names
        if report["collection_exists"]:
            try:
                count = client.count(collection_name=QDRANT_COLLECTION, exact=True)
                report["points"] = int(getattr(count, "count", 0) or 0)
            except Exception as exc:
                report["error"] = f"Could not count collection points: {exc}"
        return report
    except Exception as exc:
        log.warning("Qdrant health check failed: %s", exc)
        report["error"] = str(exc)
        reset_client_cache()
        return report


def ensure_collection(*, recreate: bool = False) -> bool:
    client = get_client()
    if not client:
        return False
    from qdrant_client.models import Distance, VectorParams

    if recreate:
        try:
            client.delete_collection(QDRANT_COLLECTION)
        except Exception:
            pass

    names = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION in names and not recreate:
        return True

    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBED_VECTOR_SIZE, distance=Distance.COSINE),
    )
    return True


def ensure_collection_report(*, recreate: bool = False) -> Dict[str, Any]:
    """Ensure the configured collection exists and return fresh health details."""
    try:
        ok = ensure_collection(recreate=recreate)
        report = qdrant_health()
        report["ensured"] = ok
        report["recreated"] = bool(recreate and ok)
        return report
    except Exception as exc:
        log.exception("Qdrant collection ensure failed")
        report = qdrant_health()
        report["ensured"] = False
        report["error"] = str(exc)
        return report


def _payload_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if not value:
        return []
    return [part for part in str(value).split("|") if part]


def chunk_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    source_path = row.get("source_path") or ""
    filename = Path(source_path).name if source_path else ""
    return {
        "chunk_id": row.get("chunk_row_id") or row.get("id"),
        "asset_id": row.get("asset_id"),
        "article_id": row.get("asset_id"),
        "chunk_index": int(row.get("chunk_index") or 0),
        "chunk_text": (row.get("chunk_text") or "")[:2000],
        "source": row.get("publication") or "Unknown",
        "source_type": row.get("source_type") or "news",
        "evidence_layer": row.get("source_type") or "news",
        "timestamp": row.get("date_published") or "",
        "source_filename": filename,
        "title": row.get("article_title") or "",
        "author": row.get("author") or "",
        "location": row.get("location") or "",
        "content_type": row.get("content_type") or "",
        "document_type": row.get("document_type") or row.get("content_type") or "",
        "language": row.get("language") or "",
        "categories": _payload_list(row.get("categories")),
        "tags": _payload_list(row.get("tags")),
        "keywords": _payload_list(row.get("keywords")),
        "source_family": row.get("source_family") or "",
        "theme": row.get("theme") or "",
    }


def upsert_chunk(
    row: Dict[str, Any],
    vector: Optional[List[float]] = None,
) -> bool:
    client = get_client()
    if not client:
        return False
    from qdrant_client.models import PointStruct

    chunk_id = row.get("chunk_row_id") or row.get("id")
    if not chunk_id:
        return False

    vec = vector
    if vec is None:
        vec = blob_to_embedding(row.get("embedding_blob"))
    if vec is None and row.get("chunk_text"):
        vec = embed_text(row["chunk_text"], use_cache=True)
    if not vec or len(vec) != EMBED_VECTOR_SIZE:
        return False

    point_id = _parse_point_id(str(chunk_id))
    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[PointStruct(id=point_id, vector=vec, payload=chunk_payload(row))],
    )
    return True


def upsert_chunks_batch(rows: List[Dict[str, Any]], vectors: List[List[float]]) -> int:
    client = get_client()
    if not client or not rows:
        return 0
    from qdrant_client.models import PointStruct

    points = []
    for row, vec in zip(rows, vectors):
        if not vec or len(vec) != EMBED_VECTOR_SIZE:
            continue
        chunk_id = row.get("chunk_row_id") or row.get("id")
        if not chunk_id:
            continue
        points.append(
            PointStruct(
                id=_parse_point_id(str(chunk_id)),
                vector=vec,
                payload=chunk_payload(row),
            )
        )
    if not points:
        return 0
    client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    return len(points)


def index_sqlite_chunks(*, limit: int = 1000, offset: int = 0, recreate: bool = False) -> Dict[str, Any]:
    """Index embedded SQLite chunks into Qdrant without changing SQLite schema."""
    if not ensure_collection(recreate=recreate):
        return {
            "ok": False,
            "indexed": 0,
            "skipped": 0,
            "limit": limit,
            "offset": offset,
            "error": "Qdrant unavailable or collection could not be created",
            "health": qdrant_health(),
        }

    from utils.db import get_conn

    conn = get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM knowledge_chunks WHERE embedding_blob IS NOT NULL"
        ).fetchone()["c"]
        rows = conn.execute(
            """
            SELECT
                kc.id AS chunk_row_id,
                kc.asset_id,
                kc.chunk_index,
                kc.chunk_text,
                kc.embedding_blob,
                cs.publication,
                cs.author,
                cs.date_published,
                cs.location,
                cs.article_title,
                cs.article_url,
                cs.source_type,
                cs.content_type,
                cs.source_family,
                cs.source_medium,
                cs.source_origin,
                cs.theme,
                COALESCE(dm.language, cs.language, '') AS language,
                dm.document_type,
                (
                    SELECT group_concat(dc.category, '|')
                    FROM document_categories dc
                    WHERE dc.document_id = kc.asset_id
                ) AS categories,
                (
                    SELECT group_concat(dt.tag, '|')
                    FROM document_tags dt
                    WHERE dt.document_id = kc.asset_id
                ) AS tags,
                (
                    SELECT group_concat(dk.keyword, '|')
                    FROM document_keywords dk
                    WHERE dk.document_id = kc.asset_id
                ) AS keywords,
                ka.source_path
            FROM knowledge_chunks kc
            LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
            LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id
            LEFT JOIN document_metadata dm ON dm.document_id = kc.asset_id
            WHERE kc.embedding_blob IS NOT NULL
            ORDER BY kc.id
            LIMIT ? OFFSET ?
            """,
            (max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    finally:
        conn.close()

    prepared_rows: List[Dict[str, Any]] = []
    vectors: List[List[float]] = []
    skipped = 0
    for row in rows:
        item = dict(row)
        vector = blob_to_embedding(item.get("embedding_blob"))
        if not vector or len(vector) != EMBED_VECTOR_SIZE:
            skipped += 1
            continue
        prepared_rows.append(item)
        vectors.append(vector)

    indexed = upsert_chunks_batch(prepared_rows, vectors)
    return {
        "ok": True,
        "indexed": indexed,
        "skipped": skipped + max(0, len(prepared_rows) - indexed),
        "limit": limit,
        "offset": offset,
        "total_embedded_chunks": total,
        "next_offset": offset + len(rows),
        "done": offset + len(rows) >= total,
        "health": qdrant_health(),
    }


def ann_search(
    query_vector: List[float],
    *,
    limit: int = QDRANT_SEARCH_LIMIT,
    source_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return hits: chunk_row_id, score, payload."""
    client = get_client()
    if not client or not query_vector:
        return []

    query_filter = None
    if source_type:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_filter = Filter(
            must=[FieldCondition(key="evidence_layer", match=MatchValue(value=source_type))]
        )

    try:
        hits = client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=query_vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
    except Exception as exc:
        log.warning("Qdrant ANN search failed: %s", exc)
        return []
    results = []
    for hit in hits:
        payload = hit.payload or {}
        chunk_id = payload.get("chunk_id")
        results.append(
            {
                "chunk_row_id": chunk_id,
                "score": float(hit.score),
                "payload": payload,
            }
        )
    return results
