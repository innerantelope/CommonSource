"""
ingest_commonsource.py
======================
CommonSource ingestion pipeline for verified publisher archives.

Reads .docx files from a source directory, matches each against a metadata CSV
(publication, author, date_published, location, title), ingests the text into
the knowledge base with embeddings, and writes provenance into commonsource_articles.

Usage:
    # Step 1 — generate metadata CSV from your .docx files (run once)
    python3 extract_hardnews_meta.py --source-dir sample_docs/hardnews --output hardnews_metadata.csv

    # Step 2 — review and fill any blanks in hardnews_metadata.csv

    # Step 3 — ingest
    python3 ingest_commonsource.py \
        --source-dir sample_docs/hardnews \
        --meta hardnews_metadata.csv \
        --db commonsource.db \
        --embed-method ollama \
        --embed-model nomic-embed-text

Options:
    --source-dir    Folder containing .docx files
    --meta          Path to metadata CSV (from extract_hardnews_meta.py)
    --db            SQLite database path (default: commonsource.db)
    --embed-method  ollama | local | none  (default: ollama)
    --embed-model   Embedding model name (default: nomic-embed-text)
    --dry-run       Preview what would be ingested without writing to DB
    --force         Re-ingest even if article already exists (by SHA-1)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

try:
    from docx import Document as DocxDocument
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx --break-system-packages")
    sys.exit(1)

from knowledge_db import (
    connect_db,
    init_db,
    asset_exists_by_sha1,
    insert_asset,
    insert_commonsource_article,
    get_commonsource_article,
    make_id,
    utc_now,
)
from embed import generate_embedding, embedding_to_blob
from source_classifier import classify_source

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 800          # target characters per chunk
CHUNK_OVERLAP = 100       # overlap between chunks
MAX_EMBED_CHARS = 2000    # truncate chunk before embedding if needed
DEFAULT_DB = str(PROJECT_ROOT / "data" / "database" / "commonsource.db")
DEFAULT_EMBED_METHOD = "ollama"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text_from_docx(path: Path) -> str:
    """Extract plain text from a Word document."""
    try:
        doc = DocxDocument(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        raise RuntimeError(f"Failed to read {path.name}: {e}") from e


def extract_text_from_pdf(path: Path) -> str:
    """Extract plain text from a PDF using PyMuPDF."""
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF is required for PDF support. Run: pip install pymupdf --break-system-packages") from e
    try:
        doc = fitz.open(str(path))
        return "\n\n".join(page.get_text() for page in doc)
    except Exception as e:
        raise RuntimeError(f"Failed to read {path.name}: {e}") from e


def extract_text_from_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_text_from_docx(path)
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(f"Unsupported file type: {suffix}")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks at paragraph boundaries where possible."""
    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            # If paragraph itself is too long, hard-split it
            if len(para) > chunk_size:
                for i in range(0, len(para), chunk_size - overlap):
                    chunks.append(para[i : i + chunk_size])
                current = para[-(overlap):]
            else:
                current = para

    if current:
        chunks.append(current)

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# SHA-1
# ---------------------------------------------------------------------------

def sha1_of_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def delete_existing_asset(conn, asset_id: str) -> None:
    """Remove an existing asset and dependent CommonSource rows before force re-ingest."""
    for table in [
        "commonsource_articles",
        "knowledge_chunks",
        "domain_classifications",
        "knowledge_extractions",
        "approved_world_models",
    ]:
        conn.execute(f"DELETE FROM {table} WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM knowledge_assets WHERE id = ?", (asset_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Metadata CSV loading
# ---------------------------------------------------------------------------

def load_metadata_csv(path: Path, key_mode: str = "both") -> Dict[str, Dict[str, str]]:
    """
    Load metadata CSV into a dict keyed by filename and, when present, relative filepath.
    """
    meta: Dict[str, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filename = row.get("filename", "").strip()
            filepath = row.get("filepath", "").strip()
            value = {
                    "publication": row.get("publication", "").strip(),
                    "author": row.get("author", "").strip(),
                    "date_published": row.get("date_published", "").strip(),
                    "location": row.get("location", "").strip(),
                    "title": row.get("title", "").strip(),
                    "url": row.get("url", "").strip(),
                    "source_type": row.get("source_type", "").strip(),
                    "content_type": row.get("content_type", "").strip(),
                    "source_family": row.get("source_family", "").strip(),
                    "source_medium": row.get("source_medium", "").strip(),
                    "source_origin": row.get("source_origin", "").strip(),
                    "theme": row.get("theme", "").strip(),
                    "collection": row.get("collection", "").strip(),
                    "language": row.get("language", "").strip(),
                }
            if filename and key_mode in {"filename", "both"}:
                meta[filename] = value
            if filepath and key_mode in {"filepath", "both"}:
                meta[filepath] = value
    return meta


# ---------------------------------------------------------------------------
# Single article ingestion
# ---------------------------------------------------------------------------

def ingest_article(
    conn,
    *,
    path: Path,
    meta: Dict[str, str],
    embed_method: str,
    embed_model: str,
    dry_run: bool,
    force: bool,
    default_source_type: str = "news",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "filename": path.name,
        "status": "pending",
        "asset_id": None,
        "chunk_count": 0,
        "embedded": 0,
        "skipped": False,
        "error": None,
    }

    # --- Extract text ---
    try:
        raw_text = extract_text_from_file(path)
    except RuntimeError as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    if not raw_text.strip():
        result["status"] = "error"
        result["error"] = "Empty document after extraction"
        return result

    # --- Deduplication by SHA-1 ---
    sha1 = sha1_of_text(raw_text)
    if not dry_run:
        existing = asset_exists_by_sha1(conn, sha1)
        if existing:
            if not force:
                result["status"] = "skipped"
                result["skipped"] = True
                result["asset_id"] = existing
                return result
            delete_existing_asset(conn, existing)

    # --- Chunk ---
    chunks = chunk_text(raw_text)
    result["chunk_count"] = len(chunks)

    if dry_run:
        result["status"] = "dry-run"
        return result

    # --- Insert asset ---
    title = meta.get("title") or path.stem
    source_profile = classify_source(
        meta,
        path=path,
        publication=meta.get("publication") or "Hardnews",
        default_source_type=default_source_type,
        source_origin="archive",
    )
    source_type = source_profile["source_type"]
    content_type = source_profile["content_type"]
    theme = source_profile["theme"]
    metadata_json_str = json.dumps({
        "publication": meta.get("publication"),
        "author": meta.get("author"),
        "date_published": meta.get("date_published"),
        "location": meta.get("location"),
        "source_type": source_type,
        "content_type": content_type,
        "source_family": source_profile["source_family"],
        "source_medium": source_profile["source_medium"],
        "source_origin": source_profile["source_origin"],
        "theme": theme,
        "collection": source_profile["collection"],
        "language": source_profile["language"],
        "source_profile": source_profile,
        "sha1": sha1,
        "chunk_count": len(chunks),
        "ingested_at": utc_now(),
        "source": "commonsource",
    })

    asset_id = insert_asset(
        conn,
        title=title,
        source_type=source_type,
        source_path=str(path),
        source_sha1=sha1,
        raw_text=raw_text,
        metadata=json.loads(metadata_json_str),
    )
    result["asset_id"] = asset_id

    # --- Insert provenance ---
    insert_commonsource_article(
        conn,
        asset_id=asset_id,
        publication=meta.get("publication") or "Hardnews",
        author=meta.get("author") or "",
        date_published=meta.get("date_published") or "",
        location=meta.get("location") or "",
        article_title=title,
        article_url=meta.get("url") or "",
        source_type=source_type,
        content_type=content_type,
        source_family=source_profile["source_family"],
        source_medium=source_profile["source_medium"],
        source_origin=source_profile["source_origin"],
        theme=theme,
        collection=source_profile["collection"],
        language=source_profile["language"],
        source_profile=source_profile,
    )

    # --- Embed and store chunks ---
    embed_errors = 0
    for idx, chunk_text_str in enumerate(chunks):
        embedding_blob = None
        embedding_model_label = None

        if embed_method and embed_method != "none":
            try:
                vec = generate_embedding(
                    chunk_text_str[:MAX_EMBED_CHARS],
                    method=embed_method,
                    ollama_model=embed_model,
                    local_model=embed_model,
                )
                if vec:
                    embedding_blob = embedding_to_blob(vec)
                    embedding_model_label = embed_model
                    result["embedded"] += 1
            except Exception:
                embed_errors += 1

        chunk_id = f"{asset_id}_c{idx:04d}"
        conn.execute(
            """
            INSERT INTO knowledge_chunks
              (id, asset_id, chunk_index, chunk_id, chunk_text, token_estimate,
               embedding_blob, embedding_model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("chunk"),
                asset_id,
                idx,
                chunk_id,
                chunk_text_str,
                len(chunk_text_str) // 4,
                embedding_blob,
                embedding_model_label,
                utc_now(),
            ),
        )

    conn.commit()
    result["status"] = "ok"
    if embed_errors:
        result["error"] = f"{embed_errors} chunk(s) failed to embed"

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest a CommonSource publisher archive into the knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    parser.add_argument("--source-dir", required=True, help="Folder containing supported document files")
    parser.add_argument("--meta", required=True, help="Metadata CSV path (from extract_hardnews_meta.py)")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--embed-method", default=DEFAULT_EMBED_METHOD, choices=["ollama", "local", "none"])
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if SHA-1 already exists")
    parser.add_argument("--recursive", action="store_true", help="Scan supported files recursively")
    parser.add_argument("--limit", type=int, default=0, help="Maximum new files to ingest; 0 means all")
    parser.add_argument("--extensions", default=".docx,.pdf,.txt", help="Comma-separated extensions to ingest")
    parser.add_argument("--default-publication", default="Hardnews", help="Publication to use when metadata is blank")
    parser.add_argument("--default-source-type", default="news", help="Evidence layer: news, development, community, official")
    parser.add_argument("--metadata-key", choices=["filename", "filepath", "both"], default="both")
    parser.add_argument("--require-meta", action="store_true", help="Skip files that do not have a metadata row")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    meta_path = Path(args.meta)

    if not source_dir.exists():
        print(f"ERROR: Source directory not found: {source_dir}")
        sys.exit(1)
    if not meta_path.exists():
        print(f"ERROR: Metadata CSV not found: {meta_path}")
        print("Run extract_hardnews_meta.py first to generate it.")
        sys.exit(1)

    # Load metadata
    metadata = load_metadata_csv(meta_path, key_mode=args.metadata_key)
    print(f"Loaded metadata for {len(metadata)} article(s) from {meta_path.name}")

    # Find supported files
    extensions = {ext.strip().lower() for ext in args.extensions.split(",") if ext.strip()}
    extensions = {ext if ext.startswith(".") else f".{ext}" for ext in extensions}
    candidates = source_dir.rglob("*") if args.recursive else source_dir.glob("*")
    doc_files = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in extensions)
    if not doc_files:
        print(f"No supported files found in {source_dir} for {', '.join(sorted(extensions))}")
        sys.exit(0)
    print(f"Found {len(doc_files)} supported file(s) in {source_dir}")

    # Connect DB
    if args.dry_run:
        conn = None
        print("\n[DRY RUN — nothing will be written]\n")
    else:
        db_path = Path(args.db)
        conn = connect_db(db_path)
        init_db(conn)
        print(f"Database: {db_path}\n")

    # Process each file
    results = []
    ok = skipped = errors = 0

    source_dir_resolved = source_dir.resolve()
    processed = 0
    for path in doc_files:
        relative_key = str(path.resolve().relative_to(source_dir_resolved))
        meta = metadata.get(relative_key) or metadata.get(path.name, {})
        if not meta:
            if args.require_meta:
                skipped += 1
                print(f"  —  {path.name}  (not in metadata CSV, skipping)")
                continue
            print(f"  ⚠️  {path.name} — not found in metadata CSV, using defaults")
            meta = {}
        meta = {
            **meta,
            "publication": meta.get("publication") or args.default_publication,
        }
        processed += 1

        result = ingest_article(
            conn,
            path=path,
            meta=meta,
            embed_method=args.embed_method,
            embed_model=args.embed_model,
            dry_run=args.dry_run,
            force=args.force,
            default_source_type=args.default_source_type,
        )
        results.append(result)

        pub = meta.get("publication", "?")
        author = meta.get("author", "?") or "no author"
        date = meta.get("date_published", "") or "no date"

        if result["status"] == "ok":
            ok += 1
            embedded_str = f"  {result['embedded']}/{result['chunk_count']} chunks embedded"
            print(f"  ✓  {path.name}")
            print(f"       {pub} · {author} · {date}{embedded_str}")
        elif result["status"] == "skipped":
            skipped += 1
            print(f"  —  {path.name}  (already ingested, skipping)")
        elif result["status"] == "dry-run":
            print(f"  ~  {path.name}  →  {result['chunk_count']} chunks  ·  {pub} · {author} · {date}")
        else:
            errors += 1
            print(f"  ✗  {path.name}  ERROR: {result['error']}")

        if args.limit > 0 and (ok >= args.limit or processed >= args.limit):
            if args.dry_run:
                print(f"\nReached dry-run preview limit of {args.limit} file(s).")
            else:
                print(f"\nReached limit of {args.limit} newly ingested/processed file(s).")
            break

    # Summary
    print(f"\n{'─'*50}")
    if args.dry_run:
        print(f"Dry run complete. {processed} of {len(doc_files)} file(s) previewed.")
    else:
        print(f"Ingestion complete.")
        print(f"  ✓  {ok} ingested")
        if skipped:
            print(f"  —  {skipped} skipped")
        if errors:
            print(f"  ✗  {errors} errors")

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
