#!/usr/bin/env python3
"""
Sync SQLite knowledge_chunks → Qdrant collection.

Usage (from Project/app):
  python ../scripts/sync_qdrant.py
  python ../scripts/sync_qdrant.py --recreate
  python ../scripts/sync_qdrant.py --batch-size 64 --limit 5000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from core.config import DB_PATH, EMBED_VECTOR_SIZE  # noqa: E402
from embed import embed_batch, warmup_embeddings  # noqa: E402
from utils.vectors import blob_to_embedding  # noqa: E402
from retrieval.qdrant_store import ensure_collection, upsert_chunks_batch  # noqa: E402
from utils.db import get_conn  # noqa: E402


def fetch_chunks(conn, limit: int | None, offset: int) -> list[dict]:
    sql = """
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
            ka.source_path
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id
        WHERE LENGTH(kc.chunk_text) > 80
        ORDER BY kc.id
    """
    params: list = []
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params = [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync CommonSource chunks to Qdrant")
    ap.add_argument("--recreate", action="store_true", help="Drop and recreate collection")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0, help="Max chunks (0 = all)")
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    print(f"DB: {DB_PATH}")
    warmup_embeddings()
    if not ensure_collection(recreate=args.recreate):
        raise SystemExit("Could not connect to Qdrant. Start: docker compose up -d qdrant")

    conn = get_conn()
    total = 0
    offset = args.offset
    batch_size = args.batch_size

    while True:
        remaining = args.limit - total if args.limit else batch_size
        fetch_n = min(batch_size, remaining) if args.limit else batch_size
        rows = fetch_chunks(conn, fetch_n if args.limit else batch_size, offset)
        if not rows:
            break

        vectors = []
        for row in rows:
            vec = blob_to_embedding(row.get("embedding_blob"))
            vectors.append(vec)

        need_embed_idx = [i for i, v in enumerate(vectors) if v is None]
        if need_embed_idx:
            texts = [rows[i]["chunk_text"] for i in need_embed_idx]
            new_vecs = embed_batch(texts, use_cache=True, batch_size=batch_size)
            for idx, vec in zip(need_embed_idx, new_vecs):
                vectors[idx] = vec

        valid_rows = []
        valid_vecs = []
        for row, vec in zip(rows, vectors):
            if vec and len(vec) == EMBED_VECTOR_SIZE:
                valid_rows.append(row)
                valid_vecs.append(vec)

        n = upsert_chunks_batch(valid_rows, valid_vecs)
        total += n
        offset += len(rows)
        print(f"  upserted {n} (total {total}, offset {offset})")

        if args.limit and total >= args.limit:
            break
        if len(rows) < batch_size:
            break

    conn.close()
    print(f"Done. {total} points synced to Qdrant.")


if __name__ == "__main__":
    main()
