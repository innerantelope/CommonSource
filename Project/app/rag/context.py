"""RAG context assembly."""

from __future__ import annotations

from typing import Any, Dict, List


def build_context(sources: List[Dict[str, Any]], limit: int = 6) -> str:
    parts = []
    for i, s in enumerate(sources[:limit], 1):
        meta = [s.get("publication") or "Unknown source"]
        if s.get("title"):
            meta.append(f"Title: {s['title']}")
        if s.get("date"):
            meta.append(f"Date: {s['date'][:10]}")
        if s.get("location"):
            meta.append(f"Location: {s['location']}")
        parts.append(
            f"[Source {i}]\n"
            f"Citation metadata, not story actors: {' | '.join(meta)}\n"
            f"Reported passage:\n{s.get('excerpt', '')}\n"
        )
    return "\n".join(parts)
