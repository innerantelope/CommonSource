"""Hybrid semantic + lexical scoring and result diversification."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from retrieval.keyword import extract_keywords
from utils.vectors import blob_to_embedding, cosine_similarity

log = logging.getLogger(__name__)

# Re-export for callers
__all__ = ["extract_keywords", "score_row", "select_diverse_results", "build_excerpt", "is_boilerplate"]


def _row_value(row: Any, key: str, default: str = "") -> str:
    try:
        return row[key] or default
    except Exception:
        if isinstance(row, dict):
            return row.get(key) or default
    return default


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"\b[a-zA-Z0-9][a-zA-Z0-9'-]*\b", (text or "").lower()))


def keyword_score(chunk_text: str, keywords: List[str]) -> float:
    if not keywords:
        return 0.0
    words = _word_set(chunk_text)
    hits = sum(1 for kw in keywords if kw in words or kw.rstrip("s") in words)
    return hits / len(keywords)


def lexical_score(row: Any, query: str, keywords: List[str]) -> float:
    if not keywords:
        return 0.0

    title = _row_value(row, "article_title")
    body = _row_value(row, "chunk_text")
    location = _row_value(row, "location")
    author = _row_value(row, "author")
    publication = _row_value(row, "publication")
    source_type = _row_value(row, "source_type")
    content_type = _row_value(row, "content_type")
    source_family = _row_value(row, "source_family")
    source_medium = _row_value(row, "source_medium")
    source_origin = _row_value(row, "source_origin")
    theme = _row_value(row, "theme")

    title_score = keyword_score(title, keywords)
    body_score = keyword_score(body, keywords)
    location_score = keyword_score(location, keywords)
    author_score = keyword_score(author, keywords)
    publication_score = keyword_score(publication, keywords)
    source_meta_score = keyword_score(
        " ".join([source_type, content_type, source_family, source_medium, source_origin, theme]),
        keywords,
    )

    q = query.lower().strip()
    phrase_bonus = 0.0
    if len(q) >= 4:
        if q in title.lower():
            phrase_bonus += 0.18
        if q in theme.lower():
            phrase_bonus += 0.16
        if q in location.lower():
            phrase_bonus += 0.12
        if q in body.lower():
            phrase_bonus += 0.08

    score = (
        0.36 * title_score
        + 0.34 * body_score
        + 0.16 * location_score
        + 0.06 * author_score
        + 0.05 * publication_score
        + 0.10 * source_meta_score
        + phrase_bonus
    )
    return min(score, 1.0)


def keyword_match_count(row: Any, keywords: List[str]) -> int:
    if not keywords:
        return 0
    combined = " ".join(
        [
            _row_value(row, "article_title"),
            _row_value(row, "chunk_text"),
            _row_value(row, "location"),
            _row_value(row, "author"),
            _row_value(row, "publication"),
            _row_value(row, "source_type"),
            _row_value(row, "content_type"),
            _row_value(row, "source_family"),
            _row_value(row, "source_medium"),
            _row_value(row, "source_origin"),
            _row_value(row, "theme"),
        ]
    )
    words = _word_set(combined)
    return sum(1 for kw in keywords if kw in words or kw.rstrip("s") in words)


def relevance_score(row: Any, query_vec: List[float], query: str, keywords: List[str]) -> float:
    blob = row.get("embedding_blob") if isinstance(row, dict) else row["embedding_blob"]
    vec = row.get("_query_vec_cache") or blob_to_embedding(blob)
    if not vec:
        return lexical_score(row, query, keywords)
    semantic = cosine_similarity(query_vec, vec)
    lexical = lexical_score(row, query, keywords)

    if keywords:
        if len(keywords) >= 2:
            required = min(2, len(keywords))
            if keyword_match_count(row, keywords) < required:
                return 0.0
        score = 0.45 * semantic + 0.55 * lexical
        if lexical == 0 and semantic < 0.55:
            score *= 0.65
    else:
        score = semantic
    return min(score, 1.0)


def score_row(row: Any, query_vec: Optional[List[float]], query: str, keywords: List[str]) -> float:
    if query_vec:
        return relevance_score(row, query_vec, query, keywords)
    return lexical_score(row, query, keywords)


def score_row_with_pagerank(
    row: Any,
    query_vec: Optional[List[float]],
    query: str,
    keywords: List[str],
    pagerank_score: float = 0.5,
    pagerank_weight: float = 0.1
) -> float:
    """
    Hybrid scoring with PageRank boost.

    Combines semantic/lexical relevance with source importance (PageRank).
    PageRank provides a modest boost to highly-cited sources.

    Args:
        row: Document/chunk row
        query_vec: Query embedding vector
        query: Query text
        keywords: Extracted query keywords
        pagerank_score: PageRank score for the source (0-1), default 0.5
        pagerank_weight: Weight factor for PageRank (0-1), default 0.1 for 10% boost

    Returns:
        Combined score (0-1)
    """
    # Get base relevance score
    base_score = score_row(row, query_vec, query, keywords)

    # Normalize PageRank to reasonable range (0-1)
    pr = max(0.0, min(1.0, pagerank_score))

    # Apply PageRank boost: higher PageRank scores get a multiplicative boost
    # This gives a ~10% boost to highly-ranked sources at max weight
    boost = 1.0 + (pagerank_weight * pr)

    final_score = base_score * boost
    return min(final_score, 1.0)


def result_group_key(row: Dict[str, Any]) -> str:
    publication = (row.get("publication") or "Unknown").strip()
    source_family = (row.get("source_family") or "").strip()
    theme = (row.get("theme") or "").strip()
    content_type = (row.get("content_type") or "").strip()
    if theme:
        return f"{source_family or publication}|theme:{theme}"
    if content_type:
        return f"{source_family or publication}|type:{content_type}"
    return f"{source_family or publication}|unthemed"


def select_diverse_results(
    scored: List[Tuple[float, Dict[str, Any]]],
    top_k: int,
    min_score: float = 0.25,
) -> List[Tuple[float, Dict[str, Any]]]:
    selected: List[Tuple[float, Dict[str, Any]]] = []
    selected_ids: set[str] = set()
    group_counts: Dict[str, int] = {}
    publication_counts: Dict[str, int] = {}

    def consider(score: float, row: Dict[str, Any], *, group_cap: Optional[int], publication_cap: Optional[int]) -> None:
        if len(selected) >= top_k:
            return
        aid = row["asset_id"]
        if aid in selected_ids or score < min_score:
            return
        group = result_group_key(row)
        publication = row.get("publication") or "Unknown"
        if group_cap is not None and group_counts.get(group, 0) >= group_cap:
            return
        if publication_cap is not None and publication_counts.get(publication, 0) >= publication_cap:
            return
        selected.append((score, row))
        selected_ids.add(aid)
        group_counts[group] = group_counts.get(group, 0) + 1
        publication_counts[publication] = publication_counts.get(publication, 0) + 1

    for score, row in scored:
        consider(score, row, group_cap=3, publication_cap=max(4, top_k // 2))
    if len(selected) < top_k:
        for score, row in scored:
            consider(score, row, group_cap=5, publication_cap=None)
    if len(selected) < top_k:
        for score, row in scored:
            consider(score, row, group_cap=None, publication_cap=None)
    return selected


_BOILERPLATE_RE = re.compile(
    r"^\s*(share|tweet|pin|email|print|post script|[\s\n|/\\·•]+)\s*$",
    re.IGNORECASE,
)


def is_boilerplate(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 40:
        return True
    control_chars = sum(1 for ch in stripped if ord(ch) < 32 and ch not in "\n\r\t")
    if control_chars:
        return True
    alnum_chars = sum(1 for ch in stripped if ch.isalnum())
    if alnum_chars < 30 or (alnum_chars / max(len(stripped), 1)) < 0.25:
        return True
    if _BOILERPLATE_RE.match(stripped):
        return True
    tokens = re.split(r"[\s\n]+", stripped)
    if all(t.lower() in {"share", "tweet", "pin", "email", "print", ""} for t in tokens):
        return True
    return False


def build_excerpt(text: str, keywords: List[str], max_chars: int = 400) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    lower = stripped.lower()
    positions = [lower.find(kw) for kw in keywords if kw and lower.find(kw) >= 0]
    if positions:
        first = min(positions)
        start = max(0, first - 130)
        sentence_start = max(stripped.rfind(".", 0, start), stripped.rfind("\n", 0, start))
        if sentence_start > 0 and first - sentence_start < max_chars:
            start = sentence_start + 1
        excerpt = stripped[start : start + max_chars].strip()
        if start > 0:
            excerpt = "..." + excerpt
        if start + max_chars < len(stripped):
            excerpt = excerpt.rstrip() + "..."
        return excerpt
    excerpt = stripped[:max_chars]
    last_period = excerpt.rfind(".")
    if last_period > 180:
        excerpt = excerpt[: last_period + 1]
    return excerpt
