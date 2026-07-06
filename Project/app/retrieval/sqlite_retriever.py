"""SQLite-backed candidate retrieval (legacy path, capped)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.config import SQLITE_CANDIDATE_LIMIT
from utils.db import get_conn


def candidate_filter_sql(
    query_vec: Optional[List[float]],
    keywords: List[str],
    extra_conditions: Optional[List[str]] = None,
    fallback_limit: int = SQLITE_CANDIDATE_LIMIT,
) -> tuple[str, List[str], str]:
    conditions = list(extra_conditions or [])
    conditions.append("LENGTH(kc.chunk_text) > 80")
    params: List[str] = []

    if query_vec:
        conditions.append("kc.embedding_blob IS NOT NULL")

    keyword_terms = keywords[:6]
    if keyword_terms:
        fields = [
            "kc.chunk_text",
            "cs.article_title",
            "cs.location",
            "cs.author",
            "cs.publication",
            "cs.source_type",
            "cs.content_type",
            "cs.source_family",
            "cs.source_medium",
            "cs.source_origin",
            "cs.theme",
        ]
        keyword_clauses = []
        for keyword in keyword_terms:
            like = f"%{keyword}%"
            keyword_clauses.append("(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")")
            params.extend([like] * len(fields))
        conditions.append("(" + " OR ".join(keyword_clauses) + ")")

    limit_sql = f"LIMIT {fallback_limit}"
    return "WHERE " + "\n          AND ".join(conditions), params, limit_sql


def fetch_candidate_rows(
    query_vec: Optional[List[float]],
    keywords: List[str],
    *,
    extra_conditions: Optional[List[str]] = None,
    limit: int = SQLITE_CANDIDATE_LIMIT,
) -> List[Dict[str, Any]]:
    where_sql, params, limit_sql = candidate_filter_sql(
        query_vec, keywords, extra_conditions=extra_conditions, fallback_limit=limit
    )
    order_params: List[str] = []
    order_terms: List[str] = []
    for keyword in keywords[:6]:
        like = f"%{keyword}%"
        order_terms.extend([
            "CASE WHEN cs.article_title LIKE ? THEN 5 ELSE 0 END",
            "CASE WHEN kc.chunk_text LIKE ? THEN 3 ELSE 0 END",
            "CASE WHEN cs.theme LIKE ? THEN 2 ELSE 0 END",
            "CASE WHEN cs.publication LIKE ? THEN 1 ELSE 0 END",
        ])
        order_params.extend([like, like, like, like])
    order_sql = (
        "ORDER BY (" + " + ".join(order_terms) + ") DESC, kc.created_at DESC"
        if order_terms
        else "ORDER BY kc.created_at DESC"
    )
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
            ka.source_path
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id
        LEFT JOIN document_metadata dm ON dm.document_id = kc.asset_id
        {where_sql}
        {order_sql}
        {limit_sql}
        """,
        params + order_params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
