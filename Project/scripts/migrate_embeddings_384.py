#!/usr/bin/env python3
"""
Migrate CommonSource chunk embeddings to the currently configured embedding model.

Default target:
  sentence-transformers/all-MiniLM-L6-v2, 384 dimensions.

The script is intentionally operational and does not change search APIs.

Usage from repo root:
  python Project/scripts/migrate_embeddings_384.py
  python Project/scripts/migrate_embeddings_384.py --batch-size 128 --index-batch-size 256
  python Project/scripts/migrate_embeddings_384.py --dry-run
  python Project/scripts/migrate_embeddings_384.py --reset-state
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_DIR = Path(__file__).resolve().parents[1] / "app"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from core.config import DB_PATH, EMBED_MODEL, EMBED_VECTOR_SIZE, PROJECT_ROOT as CONFIG_PROJECT_ROOT  # noqa: E402
from embed import embed_batch, embed_text, warmup_embeddings  # noqa: E402
from retrieval.qdrant_store import ensure_collection, qdrant_health, upsert_chunks_batch  # noqa: E402
from utils.db import get_conn  # noqa: E402
from utils.vectors import blob_to_embedding, embedding_to_blob  # noqa: E402

log = logging.getLogger("migrate_embeddings_384")

EXPECTED_BLOB_BYTES = EMBED_VECTOR_SIZE * 8
DEFAULT_STATE_FILE = (
    CONFIG_PROJECT_ROOT
    / "data"
    / "cache"
    / "migrations"
    / f"embedding_migration_{EMBED_VECTOR_SIZE}.json"
)


def utc_report_time() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def dimension_histogram(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """
        SELECT
            CASE
                WHEN embedding_blob IS NULL THEN 'missing'
                WHEN LENGTH(embedding_blob) % 8 != 0 THEN 'invalid_bytes_' || LENGTH(embedding_blob)
                ELSE CAST(LENGTH(embedding_blob) / 8 AS TEXT)
            END AS dimension,
            COUNT(*) AS count
        FROM knowledge_chunks
        GROUP BY dimension
        ORDER BY count DESC
        """
    ).fetchall()
    return {str(row["dimension"]): int(row["count"]) for row in rows}


def count_total_chunks(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()["c"])


def count_mismatched_chunks(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM knowledge_chunks
            WHERE embedding_blob IS NULL OR LENGTH(embedding_blob) != ?
            """,
            (EXPECTED_BLOB_BYTES,),
        ).fetchone()["c"]
    )


def count_valid_embeddings(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM knowledge_chunks
            WHERE embedding_blob IS NOT NULL AND LENGTH(embedding_blob) = ?
            """,
            (EXPECTED_BLOB_BYTES,),
        ).fetchone()["c"]
    )


def safe_blob_to_embedding(blob: Optional[bytes]) -> Optional[List[float]]:
    try:
        return blob_to_embedding(blob)
    except Exception as exc:
        log.warning("Skipping unreadable embedding blob: %s", exc)
        return None


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Ignoring unreadable state file %s: %s", path, exc)
        return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch_migration_batch(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    cursor: Optional[str],
) -> List[sqlite3.Row]:
    cursor_sql = "AND id > ?" if cursor else ""
    params: Tuple[Any, ...]
    if cursor:
        params = (EXPECTED_BLOB_BYTES, cursor, batch_size)
    else:
        params = (EXPECTED_BLOB_BYTES, batch_size)
    return conn.execute(
        f"""
        SELECT id, chunk_text, embedding_blob, embedding_model
        FROM knowledge_chunks
        WHERE (embedding_blob IS NULL OR LENGTH(embedding_blob) != ?)
        {cursor_sql}
        ORDER BY id
        LIMIT ?
        """,
        params,
    ).fetchall()


def compute_batch_embeddings(texts: List[str], *, batch_size: int) -> List[Optional[List[float]]]:
    try:
        return embed_batch(texts, use_cache=True, batch_size=batch_size)
    except Exception as exc:
        log.warning("Batch embedding failed, falling back to per-row embedding: %s", exc)
        vectors: List[Optional[List[float]]] = []
        for text in texts:
            try:
                vectors.append(embed_text(text, use_cache=True))
            except Exception as row_exc:
                log.warning("Single chunk embedding failed: %s", row_exc)
                vectors.append(None)
        return vectors


def migrate_embeddings(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    limit: int,
    state_file: Path,
    reset_state: bool,
) -> Dict[str, Any]:
    if reset_state and state_file.exists():
        state_file.unlink()

    state = load_state(state_file)
    cursor = state.get("last_chunk_id")
    processed = int(state.get("processed", 0) or 0)
    migrated = int(state.get("migrated", 0) or 0)
    skipped = int(state.get("skipped", 0) or 0)
    errors = int(state.get("errors", 0) or 0)

    if state.get("migration_complete"):
        return {
            "processed": processed,
            "migrated": migrated,
            "skipped": skipped,
            "errors": errors,
            "state_file": str(state_file),
            "resumed": True,
            "complete": True,
        }

    warmup_embeddings()
    while True:
        if limit and processed >= limit:
            break
        fetch_n = min(batch_size, limit - processed) if limit else batch_size
        rows = fetch_migration_batch(conn, batch_size=fetch_n, cursor=cursor)
        if not rows:
            state["migration_complete"] = True
            break

        texts = [(row["chunk_text"] or "").strip() for row in rows]
        vectors = compute_batch_embeddings(texts, batch_size=batch_size)

        updates: List[Tuple[bytes, str, str]] = []
        batch_skipped = 0
        batch_errors = 0
        for row, text, vector in zip(rows, texts, vectors):
            if not text:
                batch_skipped += 1
                continue
            if not vector:
                batch_errors += 1
                continue
            if len(vector) != EMBED_VECTOR_SIZE:
                batch_errors += 1
                log.warning(
                    "Embedding dimension mismatch after recompute for chunk %s: got %s expected %s",
                    row["id"],
                    len(vector),
                    EMBED_VECTOR_SIZE,
                )
                continue
            updates.append((embedding_to_blob(vector), EMBED_MODEL, row["id"]))

        with conn:
            conn.executemany(
                """
                UPDATE knowledge_chunks
                SET embedding_blob = ?, embedding_model = ?
                WHERE id = ?
                """,
                updates,
            )

        cursor = rows[-1]["id"]
        processed += len(rows)
        migrated += len(updates)
        skipped += batch_skipped
        errors += batch_errors

        state.update(
            {
                "model": EMBED_MODEL,
                "dimension": EMBED_VECTOR_SIZE,
                "last_chunk_id": cursor,
                "processed": processed,
                "migrated": migrated,
                "skipped": skipped,
                "errors": errors,
                "updated_at": utc_report_time(),
            }
        )
        save_state(state_file, state)
        print(
            f"migration progress: processed={processed} migrated={migrated} "
            f"skipped={skipped} errors={errors} cursor={cursor}",
            flush=True,
        )

        if len(rows) < fetch_n:
            state["migration_complete"] = True
            break

    if state.get("migration_complete"):
        state["completed_at"] = utc_report_time()
        save_state(state_file, state)

    return {
        "processed": processed,
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "state_file": str(state_file),
        "resumed": bool(state),
        "complete": bool(state.get("migration_complete")),
    }


def metadata_sql_parts(conn: sqlite3.Connection) -> Tuple[str, str]:
    joins = [
        "LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id",
        "LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id",
    ]
    fields = [
        "kc.id AS chunk_row_id",
        "kc.asset_id",
        "kc.chunk_index",
        "kc.chunk_text",
        "kc.embedding_blob",
        "cs.publication",
        "cs.author",
        "cs.date_published",
        "cs.location",
        "cs.article_title",
        "cs.article_url",
        "cs.source_type",
        "cs.content_type",
        "cs.source_family",
        "cs.source_medium",
        "cs.source_origin",
        "cs.theme",
        "ka.source_path",
    ]
    if table_exists(conn, "document_metadata"):
        joins.append("LEFT JOIN document_metadata dm ON dm.document_id = kc.asset_id")
        fields.extend(["COALESCE(dm.language, cs.language, '') AS language", "dm.document_type"])
    else:
        fields.extend(["COALESCE(cs.language, '') AS language", "'' AS document_type"])
    if table_exists(conn, "document_categories"):
        fields.append(
            """
            (
                SELECT group_concat(dc.category, '|')
                FROM document_categories dc
                WHERE dc.document_id = kc.asset_id
            ) AS categories
            """
        )
    else:
        fields.append("'' AS categories")
    if table_exists(conn, "document_tags"):
        fields.append(
            """
            (
                SELECT group_concat(dt.tag, '|')
                FROM document_tags dt
                WHERE dt.document_id = kc.asset_id
            ) AS tags
            """
        )
    else:
        fields.append("'' AS tags")
    if table_exists(conn, "document_keywords"):
        fields.append(
            """
            (
                SELECT group_concat(dk.keyword, '|')
                FROM document_keywords dk
                WHERE dk.document_id = kc.asset_id
            ) AS keywords
            """
        )
    else:
        fields.append("'' AS keywords")
    return ",\n            ".join(fields), "\n        ".join(joins)


def fetch_index_batch(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    offset: int,
    fields_sql: str,
    joins_sql: str,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            {fields_sql}
        FROM knowledge_chunks kc
        {joins_sql}
        WHERE kc.embedding_blob IS NOT NULL
        ORDER BY kc.id
        LIMIT ? OFFSET ?
        """,
        (batch_size, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def rebuild_qdrant(conn: sqlite3.Connection, *, batch_size: int, limit: int) -> Dict[str, Any]:
    if not ensure_collection(recreate=True):
        return {
            "indexed": 0,
            "skipped": 0,
            "errors": 1,
            "complete": False,
            "health": qdrant_health(),
            "error": "Qdrant unavailable or collection could not be recreated",
        }

    fields_sql, joins_sql = metadata_sql_parts(conn)
    indexed = 0
    skipped = 0
    errors = 0
    processed = 0
    offset = 0

    while True:
        if limit and processed >= limit:
            break
        fetch_n = min(batch_size, limit - processed) if limit else batch_size
        rows = fetch_index_batch(
            conn,
            batch_size=fetch_n,
            offset=offset,
            fields_sql=fields_sql,
            joins_sql=joins_sql,
        )
        if not rows:
            break

        valid_rows: List[Dict[str, Any]] = []
        valid_vectors: List[List[float]] = []
        for row in rows:
            vector = safe_blob_to_embedding(row.get("embedding_blob"))
            if vector and len(vector) == EMBED_VECTOR_SIZE:
                valid_rows.append(row)
                valid_vectors.append(vector)
            else:
                skipped += 1

        try:
            count = upsert_chunks_batch(valid_rows, valid_vectors)
            indexed += count
            skipped += max(0, len(valid_rows) - count)
        except Exception as exc:
            errors += len(valid_rows)
            log.exception("Qdrant batch upsert failed at offset %s: %s", offset, exc)

        processed += len(rows)
        offset += len(rows)
        print(
            f"qdrant progress: processed={processed} indexed={indexed} "
            f"skipped={skipped} errors={errors}",
            flush=True,
        )

        if len(rows) < fetch_n:
            break

    return {
        "processed": processed,
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "complete": not bool(limit and processed >= limit),
        "health": qdrant_health(),
    }


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate CommonSource embeddings to current vector dimension")
    parser.add_argument("--batch-size", type=int, default=128, help="SQLite migration embedding batch size")
    parser.add_argument("--index-batch-size", type=int, default=256, help="Qdrant upsert batch size")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows per phase for testing; 0 means all")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--reset-state", action="store_true", help="Discard saved migration cursor")
    parser.add_argument("--phase", choices=["all", "migrate", "index"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="Report mismatch stats without modifying SQLite/Qdrant")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    started = time.time()

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = get_conn()
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        before_histogram = dimension_histogram(conn)
        total_chunks = count_total_chunks(conn)
        target_before = count_mismatched_chunks(conn)
        valid_before = count_valid_embeddings(conn)

        base_report: Dict[str, Any] = {
            "started_at": utc_report_time(),
            "database": str(DB_PATH),
            "embedding_model": EMBED_MODEL,
            "expected_dimension": EMBED_VECTOR_SIZE,
            "expected_blob_bytes": EXPECTED_BLOB_BYTES,
            "total_chunks": total_chunks,
            "valid_embeddings_before": valid_before,
            "mismatched_or_missing_before": target_before,
            "dimension_histogram_before": before_histogram,
        }

        if args.dry_run:
            base_report.update(
                {
                    "dry_run": True,
                    "elapsed_seconds": round(time.time() - started, 3),
                }
            )
            print(json.dumps(base_report, indent=2, sort_keys=True))
            return 0

        migration_report: Dict[str, Any] = {"skipped_phase": True}
        qdrant_report: Dict[str, Any] = {"skipped_phase": True}

        if args.phase in {"all", "migrate"}:
            migration_report = migrate_embeddings(
                conn,
                batch_size=max(1, args.batch_size),
                limit=max(0, args.limit),
                state_file=args.state_file,
                reset_state=args.reset_state,
            )

        after_histogram = dimension_histogram(conn)
        target_after = count_mismatched_chunks(conn)
        valid_after = count_valid_embeddings(conn)

        if args.phase in {"all", "index"}:
            qdrant_report = rebuild_qdrant(
                conn,
                batch_size=max(1, args.index_batch_size),
                limit=max(0, args.limit),
            )

        elapsed = time.time() - started
        final_report = {
            **base_report,
            "completed_at": utc_report_time(),
            "dry_run": False,
            "migration": migration_report,
            "qdrant": qdrant_report,
            "valid_embeddings_after": valid_after,
            "mismatched_or_missing_after": target_after,
            "dimension_histogram_after": after_histogram,
            "total_migrated": int(migration_report.get("migrated", 0) or 0),
            "total_indexed": int(qdrant_report.get("indexed", 0) or 0),
            "total_skipped": int(migration_report.get("skipped", 0) or 0)
            + int(qdrant_report.get("skipped", 0) or 0),
            "total_errors": int(migration_report.get("errors", 0) or 0)
            + int(qdrant_report.get("errors", 0) or 0),
            "elapsed_seconds": round(elapsed, 3),
        }
        print(json.dumps(final_report, indent=2, sort_keys=True))
        return 0 if final_report["total_errors"] == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
