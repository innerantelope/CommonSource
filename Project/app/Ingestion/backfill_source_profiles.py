"""
Backfill CommonSource source differentiation fields for existing rows.

This script is safe to rerun. It preserves existing publication/byline/date data
and fills or normalizes the source classification fields introduced after the
first archive ingestion pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

from knowledge_db import connect_db, init_db
from source_classifier import classify_source

DEFAULT_DB = str(PROJECT_ROOT / "data" / "database" / "commonsource.db")


def load_asset_metadata(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def backfill(db_path: Path, *, dry_run: bool = False) -> int:
    conn = connect_db(db_path)
    init_db(conn)

    rows = conn.execute(
        """
        SELECT
          cs.*,
          ka.source_path,
          ka.metadata_json
        FROM commonsource_articles cs
        JOIN knowledge_assets ka ON ka.id = cs.asset_id
        ORDER BY cs.publication, cs.article_title
        """
    ).fetchall()

    updated = 0
    for row in rows:
        meta = load_asset_metadata(row["metadata_json"])
        for key in [
            "publication",
            "source_type",
            "content_type",
            "source_family",
            "source_medium",
            "source_origin",
            "theme",
            "collection",
            "language",
        ]:
            value = row[key] if key in row.keys() else ""
            if value:
                meta[key] = value

        current_content_type = str(meta.get("content_type") or "")
        current_source_type = str(meta.get("source_type") or "")
        publication_key = str(row["publication"] or "").strip().lower()
        if current_content_type == "document":
            meta["content_type"] = ""
            current_content_type = ""
        if str(meta.get("source_medium") or "") == "document":
            meta["source_medium"] = ""
        if (
            current_source_type == "news"
            and current_content_type in {"report", "research_report", "policy_brief"}
            and publication_key not in {"hardnews", "health on air"}
        ):
            meta["source_type"] = ""

        profile = classify_source(
            meta,
            path=row["source_path"] or row["article_url"] or row["article_title"],
            publication=row["publication"],
            default_source_type=row["source_type"] or "news",
            source_origin=meta.get("source_origin") or "archive",
        )

        values = {
            "source_type": profile["source_type"],
            "content_type": profile["content_type"],
            "source_family": profile["source_family"],
            "source_medium": profile["source_medium"],
            "source_origin": profile["source_origin"],
            "theme": row["theme"] or profile["theme"],
            "collection": row["collection"] or profile["collection"],
            "language": row["language"] or profile["language"],
            "source_profile_json": json.dumps(profile, ensure_ascii=False),
        }

        changed = any((row[key] if key in row.keys() else "") != value for key, value in values.items())
        if not changed:
            continue

        updated += 1
        if dry_run:
            continue

        conn.execute(
            """
            UPDATE commonsource_articles
            SET source_type = ?,
                content_type = ?,
                source_family = ?,
                source_medium = ?,
                source_origin = ?,
                theme = ?,
                collection = ?,
                language = ?,
                source_profile_json = ?
            WHERE id = ?
            """,
            (
                values["source_type"],
                values["content_type"],
                values["source_family"],
                values["source_medium"],
                values["source_origin"],
                values["theme"],
                values["collection"],
                values["language"],
                values["source_profile_json"],
                row["id"],
            ),
        )

        meta["source_type"] = values["source_type"]
        meta["content_type"] = values["content_type"]
        meta["source_family"] = values["source_family"]
        meta["source_medium"] = values["source_medium"]
        meta["source_origin"] = values["source_origin"]
        meta["theme"] = values["theme"]
        meta["collection"] = values["collection"]
        meta["language"] = values["language"]
        meta["source_profile"] = profile
        conn.execute(
            "UPDATE knowledge_assets SET source_type = ?, metadata_json = ? WHERE id = ?",
            (values["source_type"], json.dumps(meta, ensure_ascii=False), row["asset_id"]),
        )

    if not dry_run:
        conn.commit()
    conn.close()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CommonSource source profile fields")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    updated = backfill(Path(args.db), dry_run=args.dry_run)
    mode = "would update" if args.dry_run else "updated"
    print(f"{mode} {updated} row(s)")


if __name__ == "__main__":
    main()
