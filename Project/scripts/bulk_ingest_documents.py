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


SUPPORTED = {".docx", ".pdf", ".txt"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk ingest DOCX/PDF/TXT documents with automatic metadata classification.")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--publisher-id", default="")
    parser.add_argument("--publisher-name", default="Bulk Import")
    parser.add_argument("--db", default=str(search_api.DB_PATH))
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        raise SystemExit(f"Source directory not found: {source_dir}")

    search_api.DB_PATH = Path(args.db)
    conn = search_api.get_conn()
    try:
        run_migrations(conn, search_api.MIGRATIONS_DIR)
        search_api.init_publisher_tables(conn)
        ensure_document_metadata_tables(conn)
        if args.publisher_id:
            pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (args.publisher_id,)).fetchone()
            if not pub:
                raise SystemExit(f"Publisher not found: {args.publisher_id}")
        else:
            pub_id = search_api.make_id("pub")
            conn.execute(
                """
                INSERT INTO publishers (id, name, geography, language, contact_email, storage_mode, status, created_at)
                VALUES (?, ?, '', 'en', '', 'federated', 'approved', ?)
                """,
                (pub_id, args.publisher_name, search_api.utc_now()),
            )
            conn.commit()
            pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (pub_id,)).fetchone()

        candidates = source_dir.rglob("*") if args.recursive else source_dir.glob("*")
        files = [path for path in candidates if path.is_file() and path.suffix.lower() in SUPPORTED]
        if args.limit > 0:
            files = files[: args.limit]

        summary = {"ok": 0, "duplicate": 0, "failed": 0, "files": len(files)}
        for path in files:
            try:
                result = search_api.process_document_upload(
                    conn,
                    pub=pub,
                    publisher_id=pub["id"],
                    filename=path.name,
                    raw_bytes=path.read_bytes(),
                    form_data={"source_origin": "bulk_folder", "source_type": "development", "force": "1" if args.force else ""},
                    source_origin="bulk_folder",
                    force=args.force,
                    run_knowledge_layer=True,
                )
                if result.get("duplicate"):
                    summary["duplicate"] += 1
                else:
                    summary["ok"] += 1
                print(f"{result.get('status')}: {path.name} -> {result.get('document_type')} {result.get('categories')}")
            except Exception as exc:
                summary["failed"] += 1
                print(f"failed: {path.name}: {exc}")
        print(json.dumps(summary, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
