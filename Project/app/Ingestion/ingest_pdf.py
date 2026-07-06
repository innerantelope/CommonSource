"""
ingest_pdf.py
=============
CommonSource ingestion pipeline for PDF documents (reports, newsletters, papers).

Reads .pdf files from a source directory, ingests the extracted text into the
knowledge base with provenance, and writes records into commonsource_articles.

Usage:
    python3 ingest_pdf.py \
        --source-dir sample_docs/SMART \
        --publisher "SMART" \
        --db commonsource.db \
        --embed-method none

    python3 ingest_pdf.py \
        --source-dir sample_docs/Aapti \
        --publisher "Aapti Institute" \
        --db commonsource.db \
        --embed-method none

    python3 ingest_pdf.py \
        --source-dir "sample_docs/Sigma" \
        --publisher "Sigma" \
        --db commonsource.db \
        --embed-method none

Options:
    --source-dir    Folder containing .pdf files (searched recursively with --recursive)
    --publisher     Publisher / organisation name (used in provenance)
    --location      Optional location tag for all documents in this batch
    --db            SQLite database path (default: commonsource.db)
    --embed-method  ollama | local | none  (default: none)
    --embed-model   Embedding model name (default: nomic-embed-text)
    --dry-run       Preview what would be ingested without writing to DB
    --force         Re-ingest even if SHA-1 already exists
    --recursive     Scan .pdf files in subdirectories too
    --limit         Maximum new files to ingest; 0 means all
    --meta          Optional metadata CSV (filename, title, author, date_published, location, url)
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
    import pypdf
except ImportError:
    print("ERROR: pypdf not installed. Run: pip install pypdf --break-system-packages")
    sys.exit(1)

from knowledge_db import (
    connect_db,
    init_db,
    asset_exists_by_sha1,
    insert_asset,
    insert_commonsource_article,
    make_id,
    utc_now,
)
from embed import generate_embedding, embedding_to_blob
from source_classifier import classify_source
from content_classifier import classify_source_type, get_source_type_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MAX_EMBED_CHARS = 2000
DEFAULT_DB = str(PROJECT_ROOT / "data" / "database" / "commonsource.db")
DEFAULT_EMBED_METHOD = "none"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


# ---------------------------------------------------------------------------
# Text extraction from PDF
# ---------------------------------------------------------------------------

def extract_text_from_pdf(path: Path) -> str:
    """Extract plain text from a PDF using pypdf."""
    try:
        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text.strip())
        return "\n\n".join(pages)
    except Exception as e:
        raise RuntimeError(f"Failed to read {path.name}: {e}") from e


# ---------------------------------------------------------------------------
# Chunking (identical strategy to ingest_commonsource.py)
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
            if len(para) > chunk_size:
                for i in range(0, len(para), chunk_size - overlap):
                    chunks.append(para[i : i + chunk_size])
                current = para[-overlap:]
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
# Optional metadata CSV
# ---------------------------------------------------------------------------

def load_metadata_csv(path: Path) -> Dict[str, Dict[str, str]]:
    meta: Dict[str, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            filename = row.get("filename", "").strip()
            if filename:
                meta[filename] = {k: v.strip() for k, v in row.items()}
    return meta


# ---------------------------------------------------------------------------
# Derive a clean title from filename
# ---------------------------------------------------------------------------

def title_from_filename(path: Path) -> str:
    """Turn a filename like 'Climate-Literacy-Report-6.pdf' into a readable title."""
    stem = path.stem
    # Replace dashes, underscores, and multiple spaces
    title = stem.replace("-", " ").replace("_", " ")
    # Remove trailing version numbers like ' 6', ' 1 1', etc.
    import re
    title = re.sub(r'\s+\d+(\s+\d+)*\s*$', '', title)
    # Title-case
    title = title.strip().title()
    return title or stem


# ---------------------------------------------------------------------------
# Single document ingestion
# ---------------------------------------------------------------------------

def ingest_document(
    conn,
    *,
    path: Path,
    meta: Dict[str, str],
    embed_method: str,
    embed_model: str,
    dry_run: bool,
    force: bool,
    default_source_type: str = "development",
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
        raw_text = extract_text_from_pdf(path)
    except RuntimeError as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    if not raw_text.strip():
        result["status"] = "error"
        result["error"] = "Empty document after extraction (possibly scanned/image PDF)"
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

    # --- Build title ---
    title = meta.get("title") or title_from_filename(path)
    source_profile = classify_source(
        meta,
        path=path,
        publication=meta.get("publication") or meta.get("publisher") or "",
        default_source_type=default_source_type,
        default_content_type="report",
        source_origin="pdf",
    )
    classified_source_type = classify_source_type(
        title=title,
        text=raw_text,
        metadata=source_profile,
        path=str(path),
    )
    source_profile["source_type"] = classified_source_type
    source_profile["source_type_id"] = get_source_type_id(conn, classified_source_type)

    # --- Insert asset ---
    asset_id = insert_asset(
        conn,
        title=title,
        source_type=source_profile["source_type"],
        source_path=str(path),
        source_sha1=sha1,
        raw_text=raw_text,
        metadata={
            "publication": meta.get("publication") or meta.get("publisher", ""),
            "author": meta.get("author", ""),
            "date_published": meta.get("date_published", ""),
            "location": meta.get("location", ""),
            "source_type": source_profile["source_type"],
            "source_type_id": source_profile["source_type_id"],
            "content_type": source_profile["content_type"],
            "source_family": source_profile["source_family"],
            "source_medium": source_profile["source_medium"],
            "source_origin": source_profile["source_origin"],
            "theme": source_profile["theme"],
            "collection": source_profile["collection"],
            "language": source_profile["language"],
            "source_profile": source_profile,
            "sha1": sha1,
            "chunk_count": len(chunks),
            "ingested_at": utc_now(),
            "source": "commonsource_pdf",
            "file_type": "pdf",
        },
    )
    result["asset_id"] = asset_id

    # --- Insert provenance ---
    insert_commonsource_article(
        conn,
        asset_id=asset_id,
        publication=meta.get("publication") or meta.get("publisher", ""),
        author=meta.get("author", ""),
        date_published=meta.get("date_published", ""),
        location=meta.get("location", ""),
        article_title=title,
        article_url=meta.get("url", ""),
        source_type=source_profile["source_type"],
        content_type=source_profile["content_type"],
        source_family=source_profile["source_family"],
        source_medium=source_profile["source_medium"],
        source_origin=source_profile["source_origin"],
        theme=source_profile["theme"],
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
                f"{asset_id}_c{idx:04d}",
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
        description="Ingest PDF documents into the CommonSource knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    parser.add_argument("--source-dir", required=True, help="Folder containing .pdf files")
    parser.add_argument("--publisher", default="", help="Publisher / organisation name")
    parser.add_argument("--location", default="", help="Default location tag for this batch")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--embed-method", default=DEFAULT_EMBED_METHOD, choices=["ollama", "local", "none"])
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--source-type", default="development", choices=["news", "community", "development", "official"])
    parser.add_argument("--meta", default="", help="Optional metadata CSV path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        print(f"ERROR: Source directory not found: {source_dir}")
        sys.exit(1)

    # Load optional metadata CSV
    metadata: Dict[str, Dict[str, str]] = {}
    if args.meta:
        meta_path = Path(args.meta)
        if meta_path.exists():
            metadata = load_metadata_csv(meta_path)
            print(f"Loaded metadata for {len(metadata)} file(s) from {meta_path.name}")

    # Find PDFs
    pdf_files = sorted(source_dir.rglob("*.pdf") if args.recursive else source_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"No .pdf files found in {source_dir}")
        sys.exit(0)
    print(f"Found {len(pdf_files)} PDF(s) in {source_dir}")

    # Connect DB
    if args.dry_run:
        conn = None
        print("\n[DRY RUN — nothing will be written]\n")
    else:
        db_path = Path(args.db)
        conn = connect_db(db_path)
        init_db(conn)
        print(f"Database: {db_path}\n")

    ok = skipped = errors = 0

    for path in pdf_files:
        # Build meta from CSV if available, else defaults
        file_meta = metadata.get(path.name, {})
        if not file_meta.get("publication") and not file_meta.get("publisher"):
            file_meta["publication"] = args.publisher
        if not file_meta.get("location") and args.location:
            file_meta["location"] = args.location

        result = ingest_document(
            conn,
            path=path,
            meta=file_meta,
            embed_method=args.embed_method,
            embed_model=args.embed_model,
            dry_run=args.dry_run,
            force=args.force,
            default_source_type=args.source_type,
        )

        pub = file_meta.get("publication") or file_meta.get("publisher") or args.publisher or "?"
        date = file_meta.get("date_published", "") or "no date"

        if result["status"] == "ok":
            ok += 1
            title = file_meta.get("title") or title_from_filename(path)
            embedded_str = f"  {result['embedded']}/{result['chunk_count']} chunks embedded" if args.embed_method != "none" else f"  {result['chunk_count']} chunks"
            print(f"  ✓  {path.name}")
            print(f"       \"{title}\"  ·  {pub}  ·  {date}{embedded_str}")
        elif result["status"] == "skipped":
            skipped += 1
            print(f"  —  {path.name}  (already ingested, skipping)")
        elif result["status"] == "dry-run":
            title = file_meta.get("title") or title_from_filename(path)
            print(f"  ~  {path.name}  →  {result['chunk_count']} chunks  ·  {pub}")
            print(f"       title: \"{title}\"")
        else:
            errors += 1
            print(f"  ✗  {path.name}  ERROR: {result['error']}")

        if args.limit > 0 and ok >= args.limit:
            print(f"\nReached limit of {args.limit}.")
            break

    print(f"\n{'─'*50}")
    if args.dry_run:
        print(f"Dry run complete. {len(pdf_files)} PDF(s) found.")
    else:
        print(f"Ingestion complete.")
        print(f"  ✓  {ok} ingested")
        if skipped:
            print(f"  —  {skipped} skipped (already in DB)")
        if errors:
            print(f"  ✗  {errors} errors")

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
