"""Keyword extraction and lexical scoring."""

from __future__ import annotations

import re
from typing import Any, List

_STOPWORDS = {
    "tell", "me", "about", "what", "is", "are", "was", "were", "how", "who",
    "when", "where", "why", "which", "the", "a", "an", "in", "of", "and",
    "or", "to", "for", "do", "did", "has", "have", "had", "its", "it",
    "this", "that", "these", "those", "on", "at", "by", "from", "with",
    "give", "explain", "describe", "list", "find", "show", "get",
}


def extract_keywords(query: str) -> List[str]:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", query.lower())
    return [w for w in words if w not in _STOPWORDS]
