"""
Retrieval pipeline:

  User Query → Query Embedding → Qdrant ANN (or SQLite fallback)
  → Hybrid re-score (+ PageRank boost) → Diverse top-k → Source cards
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.config import MIN_RELEVANCE_SCORE, QDRANT_SEARCH_LIMIT
from embed import embed_query
from retrieval.keyword import extract_keywords
from retrieval.qdrant_store import ann_search, is_qdrant_available
from retrieval.scoring import (
    build_excerpt,
    is_boilerplate,
    score_row,
    score_row_with_pagerank,
    select_diverse_results,
)
from retrieval.sqlite_retriever import fetch_candidate_rows
from utils.db import build_source_result, hydrate_chunks_by_ids

log = logging.getLogger(__name__)


def _rows_from_qdrant(query_vec: List[float], keywords: List[str], limit: int) -> List[Dict[str, Any]]:
    hits = ann_search(query_vec, limit=max(limit * 4, QDRANT_SEARCH_LIMIT))
    if not hits:
        return []
    chunk_ids = [h["chunk_row_id"] for h in hits if h.get("chunk_row_id")]
    rows = hydrate_chunks_by_ids(chunk_ids)
    score_map = {h["chunk_row_id"]: h["score"] for h in hits}
    for row in rows:
        row["_ann_score"] = score_map.get(row.get("chunk_row_id"), 0.0)
    return rows


def retrieve_sources(
    query: str,
    *,
    top_k: int = 8,
    min_score: float = MIN_RELEVANCE_SCORE,
    extra_sql_conditions: Optional[List[str]] = None,
    candidate_pool: int = 40,
) -> Dict[str, Any]:
    """
    Primary retrieval entry — preserves legacy API result shape.

    Returns: {query, count, results, retrieval_backend}
    """
    query = (query or "").strip()
    if not query:
        return {"error": "No query provided"}

    keywords = extract_keywords(query)
    query_vec = embed_query(query)
    backend = "sqlite"

    rows: List[Dict[str, Any]] = []
    if query_vec and is_qdrant_available() and not extra_sql_conditions:
        try:
            rows = _rows_from_qdrant(query_vec, keywords, candidate_pool)
            backend = "qdrant+sqlite_hydrate"
        except Exception as exc:
            log.warning("Qdrant search failed, falling back to SQLite: %s", exc)
        if not rows:
            backend = "qdrant_empty->sqlite" if backend.startswith("qdrant") else "sqlite"
            rows = fetch_candidate_rows(
                query_vec, keywords, extra_conditions=extra_sql_conditions
            )
    else:
        rows = fetch_candidate_rows(
            query_vec, keywords, extra_conditions=extra_sql_conditions
        )

    # Fetch PageRank scores for all assets (for efficiency)
    pagerank_map: Dict[str, float] = {}
    try:
        from retrieval.pagerank import get_pagerank_score
        from utils.db import get_conn
        conn = get_conn()
        asset_ids = set(row.get("asset_id") for row in rows if row.get("asset_id"))
        for asset_id in asset_ids:
            pagerank_map[asset_id] = get_pagerank_score(conn, asset_id)
        conn.close()
    except Exception as pr_err:
        log.debug(f"Could not fetch PageRank scores: {pr_err}")

    scored: List[tuple] = []
    for row in rows:
        try:
            if is_boilerplate(row.get("chunk_text") or ""):
                continue

            # Get PageRank score from cache
            asset_id = row.get("asset_id")
            pagerank_score = pagerank_map.get(asset_id, 0.5)

            # Use hybrid scoring with PageRank boost
            hybrid = score_row_with_pagerank(
                row, query_vec, query, keywords,
                pagerank_score=pagerank_score,
                pagerank_weight=0.1  # 10% boost from PageRank
            )

            ann = float(row.get("_ann_score") or 0.0)
            if ann > 0:
                score = 0.55 * ann + 0.45 * hybrid
            else:
                score = hybrid
            scored.append((score, row))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)

    results: List[Dict[str, Any]] = []
    for score, row in select_diverse_results(scored, top_k, min_score=min_score):
        excerpt = build_excerpt(row.get("chunk_text") or "", keywords)
        results.append(build_source_result(row, score, excerpt))

    return {
        "query": query,
        "count": len(results),
        "results": results,
        "retrieval_backend": backend,
    }


def retrieve_for_rag(
    query: str,
    *,
    top_k: int = 8,
    min_score: float = MIN_RELEVANCE_SCORE,
    extra_sql_conditions: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns (source_cards, raw_scored_rows) for layered / ask endpoints."""
    data = retrieve_sources(
        query,
        top_k=top_k,
        min_score=min_score,
        extra_sql_conditions=extra_sql_conditions,
        candidate_pool=max(40, top_k * 5),
    )
    cards = data.get("results") or []

    # Re-fetch richer pool for layer tagging
    keywords = extract_keywords(query)
    query_vec = embed_query(query)
    raw_rows = fetch_candidate_rows(query_vec, keywords, extra_conditions=extra_sql_conditions, limit=2500)
    scored: List[tuple] = []
    for row in raw_rows:
        try:
            if is_boilerplate(row.get("chunk_text") or ""):
                continue
            score = score_row(row, query_vec, query, keywords)
            if score >= min_score * 0.8:
                scored.append((score, row))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    return cards, [r for _, r in scored[:80]]
