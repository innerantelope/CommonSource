from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from content_classifier import classify_source_type, ensure_source_types, get_source_type_id  # noqa: E402


DEFAULT_DB = PROJECT_ROOT / "data" / "database" / "commonsource.db"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def classify_row(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    metadata: Dict[str, Any] = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    try:
        profile = json.loads(row["source_profile_json"] or "{}")
        metadata.update(profile)
    except Exception:
        pass
    metadata.setdefault("source_type", row["source_type"] or "news")
    metadata.setdefault("content_type", row["content_type"] or "")
    return classify_source_type(
        title=row["article_title"] or row["title"] or "",
        text=row["raw_text"] or "",
        metadata=metadata,
        path=row["source_path"] or row["article_url"] or "",
    )


def reindex(db_path: Path, *, batch_size: int, limit: int, only_missing: bool, dry_run: bool) -> Dict[str, Any]:
    conn = connect(db_path)
    counts: Counter[str] = Counter()
    processed = 0
    updated = 0
    try:
        ensure_source_types(conn)
        offset = 0
        while True:
            where = "WHERE cs.source_type_id IS NULL" if only_missing else ""
            remaining = max(0, limit - processed) if limit else batch_size
            current_batch = min(batch_size, remaining) if limit else batch_size
            if current_batch <= 0:
                break
            rows = conn.execute(
                f"""
                SELECT cs.*, ka.title, ka.raw_text, ka.source_path, ka.metadata_json
                FROM commonsource_articles cs
                JOIN knowledge_assets ka ON ka.id = cs.asset_id
                {where}
                ORDER BY cs.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (current_batch, offset),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                slug = classify_row(conn, row)
                counts[slug] += 1
                processed += 1
                source_type_id = get_source_type_id(conn, slug)
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE commonsource_articles
                        SET source_type = ?, source_type_id = ?
                        WHERE asset_id = ?
                        """,
                        (slug, source_type_id, row["asset_id"]),
                    )
                    updated += 1
            if not dry_run:
                conn.commit()
            if limit and processed >= limit:
                break
            if only_missing:
                offset = 0
            else:
                offset += len(rows)
        return {
            "db": str(db_path),
            "processed": processed,
            "updated": updated,
            "dry_run": dry_run,
            "counts": dict(counts),
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex CommonSource article source types.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--limit", type=int, default=0, help="0 means all articles")
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = reindex(
        args.db,
        batch_size=max(1, args.batch_size),
        limit=max(0, args.limit),
        only_missing=args.only_missing,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
