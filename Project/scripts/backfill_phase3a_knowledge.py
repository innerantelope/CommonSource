from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
for module_path in (APP_DIR, PROJECT_ROOT):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

import search_api  # noqa: E402
from auth_service import run_migrations  # noqa: E402
from document_classifier import ensure_document_metadata_tables  # noqa: E402
from knowledge_layer import ensure_knowledge_tables, process_article_knowledge  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Phase-3A entities, tags, and relationships.")
    parser.add_argument("--db", default=str(search_api.DB_PATH))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    search_api.DB_PATH = Path(args.db)
    conn = search_api.get_conn()
    processed = 0
    try:
        run_migrations(conn, search_api.MIGRATIONS_DIR)
        ensure_document_metadata_tables(conn)
        ensure_knowledge_tables(conn)
        total_limit = args.limit if args.limit > 0 else 10_000_000
        offset = max(args.offset, 0)
        while processed < total_limit:
            rows = conn.execute(
                """
                SELECT cs.*, ka.raw_text, ka.metadata_json
                FROM commonsource_articles cs
                JOIN knowledge_assets ka ON ka.id = cs.asset_id
                ORDER BY cs.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (min(args.batch_size, total_limit - processed), offset + processed),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                process_article_knowledge(
                    conn,
                    article_id=row["asset_id"],
                    title=row["article_title"] or "",
                    text=row["raw_text"] or "",
                    publication=row["publication"] or "",
                    metadata=metadata,
                )
                processed += 1
            conn.commit()
            print(f"processed={processed}")
        counts = {
            "documents_processed": processed,
            "document_entities": conn.execute("SELECT COUNT(*) FROM document_entities").fetchone()[0],
            "entity_relationships": conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0],
        }
        print(json.dumps(counts, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
