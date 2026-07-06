"""SQLite helpers shared by retrieval and API layers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def source_path_for_response(source_path: str) -> str:
    if not source_path:
        return ""
    path = Path(source_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return str(path) if path.exists() else ""


def _split_payload_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split("|") if item.strip()]


def build_source_result(row: Dict[str, Any], score: float, excerpt: str) -> Dict[str, Any]:
    aid = row["asset_id"]
    original_url = row.get("article_url") or ""
    archive_path = source_path_for_response(row.get("source_path") or "")
    categories = _split_payload_list(row.get("categories"))
    tags = _split_payload_list(row.get("document_tags") or row.get("tags"))
    keywords = _split_payload_list(row.get("document_keywords") or row.get("keywords"))
    return {
        "asset_id": aid,
        "score": round(score, 4),
        "publication": row.get("publication") or "Unknown",
        "author": row.get("author") or "",
        "date": row.get("date_published") or "",
        "location": row.get("location") or "",
        "title": row.get("article_title") or "",
        "excerpt": excerpt,
        "url": original_url,
        "archive_url": f"/api/source/{aid}" if archive_path else "",
        "link_label": (
            "Open original source"
            if original_url
            else ("Open archive file" if archive_path else "")
        ),
        "source_type": row.get("source_type") or "news",
        "content_type": row.get("content_type") or "",
        "source_family": row.get("source_family") or "",
        "source_medium": row.get("source_medium") or "",
        "source_origin": row.get("source_origin") or "",
        "theme": row.get("theme") or "",
        "category": categories[0] if categories else "",
        "categories": categories,
        "tags": tags,
        "keywords": keywords,
        "document_type": row.get("document_type") or row.get("content_type") or "",
        "language": row.get("document_language") or row.get("language") or "",
    }


def hydrate_chunks_by_ids(chunk_ids: List[str]) -> List[Dict[str, Any]]:
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    conn = get_conn()
    rows = conn.execute(
        f"""
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
            COALESCE(dm.language, cs.language, '') AS document_language,
            dm.document_type,
            (
                SELECT group_concat(category, '|')
                FROM (
                    SELECT dc.category
                    FROM document_categories dc
                    WHERE dc.document_id = kc.asset_id
                    ORDER BY dc.confidence_score DESC, dc.category
                )
            ) AS categories,
            (
                SELECT group_concat(tag, '|')
                FROM (
                    SELECT dt.tag
                    FROM document_tags dt
                    WHERE dt.document_id = kc.asset_id
                    ORDER BY dt.confidence_score DESC, dt.tag
                )
            ) AS document_tags,
            (
                SELECT group_concat(keyword, '|')
                FROM (
                    SELECT dk.keyword
                    FROM document_keywords dk
                    WHERE dk.document_id = kc.asset_id
                    ORDER BY dk.confidence_score DESC, dk.keyword
                )
            ) AS document_keywords,
            ka.source_path,
            ka.title AS asset_title
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id
        LEFT JOIN document_metadata dm ON dm.document_id = kc.asset_id
        WHERE kc.id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    conn.close()
    order = {cid: i for i, cid in enumerate(chunk_ids)}
    items = [dict(r) for r in rows]
    items.sort(key=lambda r: order.get(r["chunk_row_id"], 9999))
    return items
