"""
ingest_audio.py
===============
Audio transcription and ingestion pipeline for CommonSource.

Transcribes audio files using Whisper (faster-whisper preferred, openai-whisper
as fallback), saves a reviewable .txt transcript alongside the audio, then
ingests the transcript into commonsource.db with full provenance and embeddings.

Usage:
    # Single file
    python3 ingest_audio.py \\
        --source path/to/audio.mp3 \\
        --publication "Health on Air" \\
        --author "SMART Network" \\
        --date 2024-03-15 \\
        --location "Lucknow" \\
        --db commonsource.db

    # Batch — all audio files in a folder
    python3 ingest_audio.py \\
        --source-dir path/to/audio_folder \\
        --publication "Community Radio Sitamarhi" \\
        --db commonsource.db

Options:
    --source        Path to a single audio file
    --source-dir    Path to a folder of audio files (recursive with --recursive)
    --publication   Publication / programme name (required)
    --author        Author / reporter name (default: publication name)
    --date          Date string YYYY-MM-DD (default: today)
    --location      Location / region of reporting
    --language      Force language for transcription, e.g. hi, en, ta (default: auto-detect)
    --model         Whisper model: tiny | base | small | medium | large (default: base)
    --db            SQLite database path (default: commonsource.db)
    --embed-method  ollama | local | none (default: ollama)
    --embed-model   Embedding model name (default: nomic-embed-text)
    --force         Re-ingest even if transcript already exists in DB (by SHA-1)
    --recursive     Scan --source-dir recursively
    --yes           Skip interactive prompt — always ingest after transcription
    --source-type   CommonSource evidence layer: news | community | development | official
                    (default: community)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

# ---------------------------------------------------------------------------
# Supported audio extensions
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".mp4"}

# ---------------------------------------------------------------------------
# Whisper backend detection
# ---------------------------------------------------------------------------

_WHISPER_BACKEND: Optional[str] = None  # "faster_whisper" | "openai_whisper" | None


def _detect_whisper() -> Optional[str]:
    global _WHISPER_BACKEND
    if _WHISPER_BACKEND is not None:
        return _WHISPER_BACKEND

    try:
        import faster_whisper  # noqa: F401
        _WHISPER_BACKEND = "faster_whisper"
        return _WHISPER_BACKEND
    except ImportError:
        pass

    try:
        import whisper  # noqa: F401
        _WHISPER_BACKEND = "openai_whisper"
        return _WHISPER_BACKEND
    except ImportError:
        pass

    return None


def _check_whisper_available() -> None:
    backend = _detect_whisper()
    if backend is None:
        print("ERROR: No Whisper backend found.")
        print()
        print("Install faster-whisper (recommended — faster, lower memory):")
        print("  pip install faster-whisper --break-system-packages")
        print()
        print("  OR install openai-whisper (fallback):")
        print("  pip install openai-whisper --break-system-packages")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE       = 800
CHUNK_OVERLAP    = 100
MAX_EMBED_CHARS  = 2000
DEFAULT_DB       = str(PROJECT_ROOT / "data" / "database" / "commonsource.db")
DEFAULT_EMBED_METHOD = "ollama"
DEFAULT_EMBED_MODEL  = "nomic-embed-text"
DEFAULT_SOURCE_TYPE  = "community"
DEFAULT_MODEL    = "base"

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------

try:
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
except ImportError as exc:
    print(f"ERROR: Could not import local modules: {exc}")
    print("Make sure the app and embed folders are present in the project root.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# SHA-1
# ---------------------------------------------------------------------------

def sha1_of_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Chunking  (same logic as ingest_commonsource.py)
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
                    chunks.append(para[i: i + chunk_size])
                current = para[-overlap:] if overlap else ""
            else:
                current = para

    if current:
        chunks.append(current)

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def transcribe_faster_whisper(
    audio_path: Path,
    model_name: str = DEFAULT_MODEL,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Transcribe using faster-whisper.
    Returns (transcript_text, detected_language).
    """
    from faster_whisper import WhisperModel  # type: ignore

    print(f"  Loading faster-whisper model '{model_name}' …")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    kwargs: Dict[str, Any] = {"beam_size": 5}
    if language:
        kwargs["language"] = language

    segments, info = model.transcribe(str(audio_path), **kwargs)
    detected_lang = info.language if hasattr(info, "language") else (language or "unknown")

    lines = []
    for segment in segments:
        lines.append(segment.text.strip())

    transcript = " ".join(lines).strip()
    return transcript, detected_lang


def transcribe_openai_whisper(
    audio_path: Path,
    model_name: str = DEFAULT_MODEL,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Transcribe using openai-whisper.
    Returns (transcript_text, detected_language).
    """
    import whisper  # type: ignore

    print(f"  Loading openai-whisper model '{model_name}' …")
    model = whisper.load_model(model_name)

    kwargs: Dict[str, Any] = {}
    if language:
        kwargs["language"] = language

    result = model.transcribe(str(audio_path), **kwargs)
    transcript = result.get("text", "").strip()
    detected_lang = result.get("language", language or "unknown")
    return transcript, detected_lang


def transcribe(
    audio_path: Path,
    model_name: str = DEFAULT_MODEL,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Transcribe audio, auto-selecting the best available backend.
    Returns (transcript_text, detected_language).
    """
    backend = _detect_whisper()
    if backend == "faster_whisper":
        return transcribe_faster_whisper(audio_path, model_name, language)
    elif backend == "openai_whisper":
        return transcribe_openai_whisper(audio_path, model_name, language)
    else:
        raise RuntimeError("No Whisper backend available — see installation instructions above.")


# ---------------------------------------------------------------------------
# Transcript file I/O
# ---------------------------------------------------------------------------

def transcript_path_for(audio_path: Path) -> Path:
    """Return the .txt path that sits alongside the audio file."""
    return audio_path.with_suffix(".txt")


def save_transcript(txt_path: Path, transcript: str, meta: Dict[str, str]) -> None:
    """Write transcript to a .txt file with a brief header."""
    header_lines = [
        f"# Transcript: {meta.get('title', txt_path.stem)}",
        f"# Publication: {meta.get('publication', '')}",
        f"# Author: {meta.get('author', '')}",
        f"# Date: {meta.get('date', '')}",
        f"# Location: {meta.get('location', '')}",
        f"# Language: {meta.get('language', '')}",
        f"# Model: {meta.get('model', '')}",
        f"# Transcribed: {utc_now()}",
        "",
    ]
    txt_path.write_text("\n".join(header_lines) + "\n" + transcript, encoding="utf-8")


def load_transcript_body(txt_path: Path) -> str:
    """Read the transcript .txt, stripping the header comment lines."""
    raw = txt_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    # Skip lines starting with "# "
    body_lines = [ln for ln in lines if not ln.startswith("# ")]
    return "\n".join(body_lines).strip()


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------

def prompt_ingest(txt_path: Path, transcript: str, auto_yes: bool) -> Optional[str]:
    """
    Show a preview of the transcript and ask the user whether to ingest.

    Returns:
      "yes"  — ingest now
      "no"   — skip
      or None if auto_yes is True (treated as yes)
    """
    preview = transcript[:300].replace("\n", " ")
    if len(transcript) > 300:
        preview += " …"

    print()
    print("  Preview (first 300 chars):")
    print(f"  {preview}")
    print()

    if auto_yes:
        print(f"  Transcript saved to: {txt_path}")
        print("  Auto-ingesting (--yes flag set).")
        return "yes"

    print(f"  Transcript saved to: {txt_path.name}")
    while True:
        try:
            answer = input("  Ingest now? [y/N/edit]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "no"

        if answer in ("y", "yes"):
            return "yes"
        elif answer in ("e", "edit"):
            editor = os.environ.get("EDITOR", "nano")
            print(f"  Opening {txt_path} in {editor} …")
            subprocess.call([editor, str(txt_path)])
            return "yes"  # ingest the (potentially edited) file
        elif answer in ("n", "no", ""):
            return "no"
        else:
            print("  Please enter y, n, or edit.")


# ---------------------------------------------------------------------------
# DB deduplication helpers
# ---------------------------------------------------------------------------

def delete_existing_asset(conn, asset_id: str) -> None:
    """Remove an existing asset and all dependent rows before force re-ingest."""
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
# Core ingest
# ---------------------------------------------------------------------------

def ingest_transcript(
    conn,
    *,
    transcript: str,
    audio_path: Path,
    publication: str,
    author: str,
    date: str,
    location: str,
    language: str,
    source_type: str,
    embed_method: str,
    embed_model: str,
    force: bool,
) -> Dict[str, Any]:
    """
    Ingest a transcript text into the knowledge base.
    Returns result dict with keys: status, asset_id, chunk_count, embedded, error.
    """
    result: Dict[str, Any] = {
        "status": "pending",
        "asset_id": None,
        "chunk_count": 0,
        "embedded": 0,
        "error": None,
    }

    if not transcript.strip():
        result["status"] = "error"
        result["error"] = "Empty transcript"
        return result

    sha1 = sha1_of_text(transcript)
    existing = asset_exists_by_sha1(conn, sha1)
    if existing:
        if not force:
            result["status"] = "skipped"
            result["asset_id"] = existing
            return result
        delete_existing_asset(conn, existing)

    chunks = chunk_text(transcript)
    if not chunks:
        chunks = [transcript[:CHUNK_SIZE]]
    result["chunk_count"] = len(chunks)

    title = audio_path.stem
    source_profile = classify_source(
        {
            "publication": publication,
            "source_type": source_type,
            "content_type": "audio_transcript",
            "language": language,
        },
        path=audio_path,
        publication=publication,
        default_source_type=source_type,
        default_content_type="audio_transcript",
        source_origin="audio",
    )

    metadata = {
        "publication": publication,
        "author": author,
        "date_published": date,
        "location": location,
        "language": language,
        "source_type": source_profile["source_type"],
        "content_type": source_profile["content_type"],
        "source_family": source_profile["source_family"],
        "source_medium": source_profile["source_medium"],
        "source_origin": source_profile["source_origin"],
        "theme": source_profile["theme"],
        "collection": source_profile["collection"],
        "source_profile": source_profile,
        "audio_file": audio_path.name,
        "sha1": sha1,
        "chunk_count": len(chunks),
        "ingested_at": utc_now(),
        "source": "audio_transcript",
    }

    asset_id = insert_asset(
        conn,
        title=title,
        source_type=source_profile["source_type"],
        source_path=str(audio_path),
        source_sha1=sha1,
        raw_text=transcript,
        metadata=metadata,
    )
    result["asset_id"] = asset_id

    insert_commonsource_article(
        conn,
        asset_id=asset_id,
        publication=publication,
        author=author,
        date_published=date,
        location=location,
        article_title=title,
        article_url="",
        source_type=source_profile["source_type"],
        content_type=source_profile["content_type"],
        source_family=source_profile["source_family"],
        source_medium=source_profile["source_medium"],
        source_origin=source_profile["source_origin"],
        theme=source_profile["theme"],
        collection=source_profile["collection"],
        language=language,
        source_profile=source_profile,
    )

    embed_errors = 0
    for idx, chunk_str in enumerate(chunks):
        embedding_blob = None
        embedding_model_label = None

        if embed_method and embed_method != "none":
            try:
                vec = generate_embedding(
                    chunk_str[:MAX_EMBED_CHARS],
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
                chunk_str,
                len(chunk_str) // 4,
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
# Single file pipeline
# ---------------------------------------------------------------------------

def process_audio_file(
    conn,
    audio_path: Path,
    *,
    publication: str,
    author: str,
    date: str,
    location: str,
    language: Optional[str],
    model_name: str,
    source_type: str,
    embed_method: str,
    embed_model: str,
    force: bool,
    auto_yes: bool,
) -> Dict[str, Any]:
    """
    Full pipeline for one audio file:
      transcribe -> save .txt -> prompt -> ingest.

    Returns a result dict with keys:
      status: "ok" | "skipped" | "skipped_user" | "error"
      audio_file, transcript_path, language, chunk_count, embedded, error
    """
    result: Dict[str, Any] = {
        "status": "pending",
        "audio_file": audio_path.name,
        "transcript_path": None,
        "language": language or "auto",
        "chunk_count": 0,
        "embedded": 0,
        "error": None,
    }

    txt_path = transcript_path_for(audio_path)

    # --- Check if transcript already exists on disk ---
    if txt_path.exists() and not force:
        print(f"  Transcript already exists: {txt_path.name}")
        print("  Loading existing transcript (use --force to re-transcribe).")
        transcript = load_transcript_body(txt_path)
        detected_lang = language or "unknown"
    else:
        # --- Transcribe ---
        print(f"  Transcribing: {audio_path.name}")
        try:
            transcript, detected_lang = transcribe(audio_path, model_name, language)
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"Transcription failed: {exc}"
            return result

        if not transcript.strip():
            result["status"] = "error"
            result["error"] = "Whisper returned empty transcript"
            return result

        result["language"] = detected_lang
        print(f"  Detected language: {detected_lang}")

        # --- Save transcript ---
        meta_for_header = {
            "title": audio_path.stem,
            "publication": publication,
            "author": author,
            "date": date,
            "location": location,
            "language": detected_lang,
            "model": model_name,
        }
        save_transcript(txt_path, transcript, meta_for_header)
        print(f"  Transcript saved: {txt_path.name}  ({len(transcript)} chars)")

    result["transcript_path"] = str(txt_path)

    # --- Interactive prompt ---
    decision = prompt_ingest(txt_path, transcript, auto_yes)
    if decision != "yes":
        result["status"] = "skipped_user"
        print("  Skipped (user declined).")
        return result

    # If user edited, reload from file
    if not auto_yes:
        transcript = load_transcript_body(txt_path)

    # --- Ingest ---
    ingest_result = ingest_transcript(
        conn,
        transcript=transcript,
        audio_path=audio_path,
        publication=publication,
        author=author,
        date=date,
        location=location,
        language=result["language"],
        source_type=source_type,
        embed_method=embed_method,
        embed_model=embed_model,
        force=force,
    )

    result["status"] = ingest_result["status"]
    result["chunk_count"] = ingest_result["chunk_count"]
    result["embedded"] = ingest_result["embedded"]
    result["error"] = ingest_result.get("error")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Check Whisper availability before anything else
    _check_whisper_available()

    parser = argparse.ArgumentParser(
        description="Transcribe and ingest audio files into the CommonSource knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source",     metavar="FILE",   help="Path to a single audio file")
    source_group.add_argument("--source-dir", metavar="DIR",    help="Folder of audio files")

    parser.add_argument("--publication", required=True, help="Publication / programme name")
    parser.add_argument("--author",      default="",   help="Author / reporter name (default: publication name)")
    parser.add_argument("--date",        default="",   help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--location",    default="",   help="Location / region of reporting")
    parser.add_argument("--language",    default=None, help="Force language code, e.g. hi, en, ta (default: auto)")
    parser.add_argument("--model",       default=DEFAULT_MODEL,
                        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
                        help=f"Whisper model size (default: {DEFAULT_MODEL})")
    parser.add_argument("--db",           default=DEFAULT_DB,       help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--embed-method", default=DEFAULT_EMBED_METHOD, choices=["ollama", "local", "none"])
    parser.add_argument("--embed-model",  default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--source-type",  default=DEFAULT_SOURCE_TYPE,
                        choices=["news", "community", "development", "official"],
                        help=f"CommonSource evidence layer (default: {DEFAULT_SOURCE_TYPE})")
    parser.add_argument("--force",     action="store_true", help="Re-transcribe and re-ingest even if already exists")
    parser.add_argument("--recursive", action="store_true", help="Scan --source-dir recursively")
    parser.add_argument("--yes", "-y", action="store_true", dest="auto_yes",
                        help="Skip interactive prompt and always ingest")

    args = parser.parse_args()

    # Defaults
    author = args.author or args.publication
    date   = args.date or datetime.now(timezone.utc).date().isoformat()

    # Connect and init DB
    db_path = Path(args.db)
    conn = connect_db(db_path)
    init_db(conn)
    print(f"Database: {db_path}")

    backend = _detect_whisper()
    print(f"Whisper backend: {backend}  |  model: {args.model}")

    # Build file list
    if args.source:
        audio_path = Path(args.source)
        if not audio_path.exists():
            print(f"ERROR: File not found: {audio_path}")
            sys.exit(1)
        if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            print(f"WARNING: {audio_path.suffix} is not a recognised audio extension.")
        audio_files = [audio_path]
    else:
        source_dir = Path(args.source_dir)
        if not source_dir.exists():
            print(f"ERROR: Directory not found: {source_dir}")
            sys.exit(1)
        glob_fn = source_dir.rglob if args.recursive else source_dir.glob
        audio_files = sorted(
            p for p in glob_fn("*")
            if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not audio_files:
            print(f"No audio files found in {source_dir}")
            sys.exit(0)
        print(f"Found {len(audio_files)} audio file(s) in {source_dir}")

    # Process each file
    ok = skipped = skipped_user = errors = 0

    for audio_path in audio_files:
        print(f"\n{'─' * 50}")
        print(f"File: {audio_path.name}")

        result = process_audio_file(
            conn,
            audio_path,
            publication=args.publication,
            author=author,
            date=date,
            location=args.location,
            language=args.language,
            model_name=args.model,
            source_type=args.source_type,
            embed_method=args.embed_method,
            embed_model=args.embed_model,
            force=args.force,
            auto_yes=args.auto_yes,
        )

        status      = result["status"]
        n_chunks    = result["chunk_count"]
        n_embedded  = result["embedded"]
        lang        = result["language"]
        err         = result["error"]

        if status == "ok":
            ok += 1
            embed_str = f"  [{n_embedded}/{n_chunks} chunks embedded]" if args.embed_method != "none" else ""
            print(f"  v  Ingested  ({lang})  {n_chunks} chunks{embed_str}")
            if err:
                print(f"     (warning: {err})")
        elif status == "skipped":
            skipped += 1
            print(f"  -  Already ingested (use --force to re-ingest)")
        elif status == "skipped_user":
            skipped_user += 1
        elif status == "error":
            errors += 1
            print(f"  x  ERROR: {err}")

    # Summary
    print(f"\n{'─' * 50}")
    print("Done.")
    print(f"  {ok} ingested  ·  {skipped + skipped_user} skipped  ·  {errors} errors")
    if skipped_user:
        print(f"  ({skipped_user} skipped at user prompt)")

    conn.close()


if __name__ == "__main__":
    main()
