from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


SOURCE_TYPES = ("news", "report", "research", "magazine", "opinion", "fact-check", "dataset", "other")
MIGRATION_ID = "006_source_type_classification"

RULES = {
    "fact-check": (
        "fact check", "fact-check", "claim", "verdict", "false", "misleading", "debunk",
    ),
    "dataset": (
        "dataset", "data set", "variables", "codebook", "data dictionary", "sample size",
        "methodology", "download data",
    ),
    "research": (
        "abstract", "literature review", "peer reviewed", "study", "hypothesis",
        "references", "bibliography", "journal", "research paper",
    ),
    "report": (
        "executive summary", "methodology", "findings", "recommendations", "annual report",
        "assessment", "evaluation", "policy brief",
    ),
    "opinion": (
        "opinion", "column", "commentary", "editorial", "op-ed", "viewpoint",
    ),
    "magazine": (
        "magazine", "feature", "long read", "longread", "cover story", "profile",
    ),
}


def normalize_source_type(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug if slug in SOURCE_TYPES else "news"


def _contains_any(text: str, phrases: Iterable[str]) -> int:
    return sum(1 for phrase in phrases if phrase in text)


def classify_source_type(
    *,
    title: str = "",
    text: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    path: str = "",
) -> str:
    metadata = metadata or {}
    haystack = " ".join(
        str(part or "")
        for part in [
            title,
            path,
            metadata.get("source_type"),
            metadata.get("content_type"),
            metadata.get("theme"),
            metadata.get("collection"),
            text[:12000],
        ]
    ).lower()

    scores = {slug: _contains_any(haystack, phrases) for slug, phrases in RULES.items()}
    if scores["fact-check"] >= 2 or ("fact check" in haystack and "verdict" in haystack):
        return "fact-check"
    if scores["dataset"] >= 2:
        return "dataset"
    if scores["research"] >= 2:
        return "research"
    if scores["report"] >= 2:
        return "report"
    if scores["opinion"] >= 1:
        return "opinion"
    if scores["magazine"] >= 1:
        return "magazine"

    explicit = normalize_source_type(str(metadata.get("source_type") or ""))
    return explicit if explicit != "news" else "news"


def ensure_source_types(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE
        )
        """
    )
    for slug in SOURCE_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO source_types (name, slug) VALUES (?, ?)",
            (slug, slug),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_types_slug ON source_types(slug)")
    columns = conn.execute("PRAGMA table_info(commonsource_articles)").fetchall()
    if not columns:
        return
    if not any(str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) == "source_type_id" for row in columns):
        conn.execute("ALTER TABLE commonsource_articles ADD COLUMN source_type_id INTEGER")
    conn.execute(
        """
        UPDATE commonsource_articles
        SET source_type_id = (
            SELECT id FROM source_types
            WHERE slug = CASE
                WHEN lower(source_type) IN ('report', 'research', 'magazine', 'opinion', 'fact-check', 'dataset', 'other')
                    THEN lower(source_type)
                ELSE 'news'
            END
        )
        WHERE source_type_id IS NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cs_articles_source_type_id ON commonsource_articles(source_type_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, ?)",
        (MIGRATION_ID, datetime.now(timezone.utc).isoformat()),
    )


def get_source_type_id(conn: sqlite3.Connection, slug: str) -> int:
    ensure_source_types(conn)
    normalized = normalize_source_type(slug)
    row = conn.execute("SELECT id FROM source_types WHERE slug = ?", (normalized,)).fetchone()
    if row:
        return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
    conn.execute("INSERT INTO source_types (name, slug) VALUES (?, ?)", (normalized, normalized))
    row = conn.execute("SELECT id FROM source_types WHERE slug = ?", (normalized,)).fetchone()
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
