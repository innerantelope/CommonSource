"""
ingest_rss.py
=============
RSS/Atom feed ingestion pipeline for CommonSource.

Fetches articles from publisher RSS/Atom feeds and ingests them into
commonsource.db with full provenance metadata and embeddings.

Usage:
    # Single feed
    python3 ingest_rss.py \\
        --feed https://example.com/feed.xml \\
        --publication "Radio Bulbul" \\
        --location "Bhadrak" \\
        --db commonsource.db

    # Batch from CSV (columns: feed_url, publication, location, language)
    python3 ingest_rss.py --feed-list rss_feeds.csv --db commonsource.db

Options:
    --feed          URL of a single RSS/Atom feed
    --publication   Publication name (required for --feed mode)
    --location      Location/region of the publication
    --language      Language code, e.g. hi, en, ta (default: en)
    --feed-list     Path to CSV with columns: feed_url, publication, location, language
    --db            SQLite database path (default: commonsource.db)
    --embed-method  ollama | local | none (default: ollama)
    --embed-model   Embedding model name (default: nomic-embed-text)
    --force         Re-ingest articles that already exist in the DB
    --limit         Max articles to ingest per feed (0 = no limit)
    --source-type   CommonSource evidence layer: news | community | development | official
                    (default: news)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import textwrap
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _check_deps() -> None:
    missing = []
    try:
        import feedparser  # noqa: F401
    except ImportError:
        missing.append("feedparser")
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        missing.append("beautifulsoup4")

    if missing:
        pkgs = " ".join(missing)
        print(f"ERROR: Missing dependencies: {pkgs}")
        print(f"Install with:  pip install {pkgs} --break-system-packages")
        sys.exit(1)

_check_deps()

import feedparser  # noqa: E402
import requests    # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

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
    from content_classifier import classify_source_type, get_source_type_id
except ImportError as exc:
    print(f"ERROR: Could not import local modules: {exc}")
    print("Make sure the app and embed folders are present in the project root.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE        = 800    # target characters per chunk (matches ingest_commonsource.py)
CHUNK_OVERLAP     = 100
MAX_EMBED_CHARS   = 2000
DEFAULT_DB        = str(PROJECT_ROOT / "data" / "database" / "commonsource.db")
DEFAULT_EMBED_METHOD = "ollama"
DEFAULT_EMBED_MODEL  = "nomic-embed-text"
DEFAULT_SOURCE_TYPE  = "news"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

class _MLStripper(HTMLParser):
    """Minimal HTML-to-plaintext stripper using stdlib only."""
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def strip_html(raw: str) -> str:
    """Strip HTML tags and decode entities, returning plain text."""
    if not raw:
        return ""
    # Use BeautifulSoup for robustness (handles malformed HTML better)
    try:
        soup = BeautifulSoup(raw, "html.parser")
        return soup.get_text(separator=" ")
    except Exception:
        # Fallback to stdlib stripper
        s = _MLStripper()
        s.feed(raw or "")
        return s.get_text()


def clean_whitespace(text: str) -> str:
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
# SHA-1 deduplication
# ---------------------------------------------------------------------------

def sha1_of_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def url_already_ingested(conn, url: str) -> bool:
    """Check commonsource_articles for an existing row with this URL."""
    if not url:
        return False
    row = conn.execute(
        "SELECT id FROM commonsource_articles WHERE article_url = ?", (url,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

def fetch_feed(url: str, timeout: int = 30) -> Any:
    """
    Fetch and parse an RSS/Atom feed.
    Uses a browser User-Agent header to avoid 403s from picky servers.
    Returns a feedparser result object.
    """
    headers = {"User-Agent": BROWSER_UA}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.content
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Timeout fetching feed: {url}")
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.response.status_code} fetching feed: {url}")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Network error fetching feed: {exc}")

    parsed = feedparser.parse(content)
    return parsed


# ---------------------------------------------------------------------------
# Entry parsing helpers
# ---------------------------------------------------------------------------

def _entry_content(entry: Any) -> str:
    """Extract the best available content from a feed entry."""
    # content:encoded (full text) — present as entry.content list
    if hasattr(entry, "content") and entry.content:
        for c in entry.content:
            val = c.get("value", "") if isinstance(c, dict) else getattr(c, "value", "")
            if val:
                return val

    # summary (RSS) / description (Atom)
    for attr in ("summary", "description"):
        val = getattr(entry, attr, None)
        if val:
            return val

    return ""


def _entry_author(entry: Any, feed: Any, fallback_publication: str) -> str:
    """Extract author from entry, feed metadata, or fall back to publication name."""
    # Entry-level author
    author = getattr(entry, "author", None) or ""
    if not author:
        # Some feeds use author_detail
        detail = getattr(entry, "author_detail", None)
        if detail:
            author = getattr(detail, "name", "") or ""
    if not author:
        # Feed-level author
        feed_author = getattr(feed.feed, "author", None) or ""
        if feed_author:
            author = feed_author
    if not author:
        author = fallback_publication
    return clean_whitespace(strip_html(author))


def _entry_date(entry: Any) -> str:
    """Parse the best available date from an entry, falling back to today."""
    for field in ("published_parsed", "updated_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).date().isoformat()
            except Exception:
                pass
    # Check string fields in case struct_time failed
    for field in ("published", "updated"):
        val = getattr(entry, field, None)
        if val:
            # Return as-is; it's something
            return str(val)[:10]
    # Fall back to today
    return datetime.now(timezone.utc).date().isoformat()


def _entry_tags(entry: Any) -> List[str]:
    """Extract category/tag list from a feed entry."""
    tags = []
    for tag_obj in getattr(entry, "tags", []) or []:
        term = getattr(tag_obj, "term", None) or (tag_obj.get("term") if isinstance(tag_obj, dict) else None)
        if term:
            tags.append(str(term).strip())
    return tags


# ---------------------------------------------------------------------------
# Single-entry ingestion
# ---------------------------------------------------------------------------

def ingest_entry(
    conn,
    entry: Any,
    feed: Any,
    *,
    publication: str,
    location: str,
    language: str,
    source_type: str,
    embed_method: str,
    embed_model: str,
    force: bool,
) -> Dict[str, Any]:
    """
    Ingest a single feed entry. Returns a result dict with keys:
      status: "ok" | "skipped" | "error"
      title, author, date, url, chunk_count, embedded, error
    """
    result: Dict[str, Any] = {
        "status": "pending",
        "title": "",
        "author": "",
        "date": "",
        "url": "",
        "chunk_count": 0,
        "embedded": 0,
        "error": None,
    }

    title  = clean_whitespace(strip_html(getattr(entry, "title", "") or ""))
    url    = str(getattr(entry, "link", "") or "").strip()
    author = _entry_author(entry, feed, publication)
    date   = _entry_date(entry)
    tags   = _entry_tags(entry)
    source_profile = classify_source(
        {
            "publication": publication,
            "source_type": source_type,
            "content_type": "article",
            "language": language,
        },
        path=url,
        publication=publication,
        default_source_type=source_type,
        source_origin="rss",
    )

    result["title"]  = title or "(no title)"
    result["author"] = author
    result["date"]   = date
    result["url"]    = url

    # --- Deduplication by URL ---
    if not force and url_already_ingested(conn, url):
        result["status"] = "skipped"
        return result

    # --- Extract and clean body text ---
    raw_content = _entry_content(entry)
    body = clean_whitespace(strip_html(raw_content))
    full_text = (title + "\n\n" + body).strip() if body else title

    if not full_text.strip():
        result["status"] = "error"
        result["error"] = "No extractable text in entry"
        return result

    classified_source_type = classify_source_type(
        title=title,
        text=full_text,
        metadata=source_profile,
        path=url,
    )
    source_profile["source_type"] = classified_source_type
    source_profile["source_type_id"] = get_source_type_id(conn, classified_source_type)

    # --- SHA-1 dedup (content-level, catches duplicates across feeds) ---
    sha1 = sha1_of_text(full_text)
    if not force:
        existing = asset_exists_by_sha1(conn, sha1)
        if existing:
            result["status"] = "skipped"
            return result

    # --- Chunk ---
    chunks = chunk_text(full_text)
    if not chunks:
        chunks = [full_text[:CHUNK_SIZE]]
    result["chunk_count"] = len(chunks)

    # --- Build metadata ---
    metadata = {
        "publication": publication,
        "author": author,
        "date_published": date,
        "location": location,
        "language": language,
        "source_type": source_profile["source_type"],
        "source_type_id": source_profile["source_type_id"],
        "content_type": source_profile["content_type"],
        "source_family": source_profile["source_family"],
        "source_medium": source_profile["source_medium"],
        "source_origin": source_profile["source_origin"],
        "tags": tags,
        "source_profile": source_profile,
        "sha1": sha1,
        "chunk_count": len(chunks),
        "ingested_at": utc_now(),
        "source": "rss",
    }

    # --- Insert asset ---
    asset_id = insert_asset(
        conn,
        title=title or "Untitled",
        source_type=source_profile["source_type"],
        source_path=url,
        source_sha1=sha1,
        raw_text=full_text,
        metadata=metadata,
    )

    # --- Insert provenance ---
    insert_commonsource_article(
        conn,
        asset_id=asset_id,
        publication=publication,
        author=author,
        date_published=date,
        location=location,
        article_title=title or "Untitled",
        article_url=url,
        source_type=source_profile["source_type"],
        content_type=source_profile["content_type"],
        source_family=source_profile["source_family"],
        source_medium=source_profile["source_medium"],
        source_origin=source_profile["source_origin"],
        theme=", ".join(tags) if tags else "",
        collection="",
        language=language,
        source_profile=source_profile,
    )

    # --- Embed and store chunks ---
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
# Feed-level ingestion
# ---------------------------------------------------------------------------

def ingest_feed(
    conn,
    *,
    feed_url: str,
    publication: str,
    location: str = "",
    language: str = "en",
    source_type: str = DEFAULT_SOURCE_TYPE,
    embed_method: str = DEFAULT_EMBED_METHOD,
    embed_model: str = DEFAULT_EMBED_MODEL,
    force: bool = False,
    limit: int = 0,
) -> Tuple[int, int, int]:
    """
    Fetch and ingest a single RSS/Atom feed.
    Returns (new, skipped, errors) counts.
    """
    print(f"\nFeed: {feed_url}")
    print(f"  Publication: {publication}  |  Location: {location or '—'}  |  Language: {language}")

    try:
        feed = fetch_feed(feed_url)
    except RuntimeError as exc:
        print(f"  ERROR fetching feed: {exc}")
        return 0, 0, 1

    if feed.bozo and not feed.entries:
        exc_msg = str(getattr(feed, "bozo_exception", "unknown parse error"))
        print(f"  ERROR parsing feed: {exc_msg}")
        return 0, 0, 1

    entries = feed.entries
    if not entries:
        print("  No entries found in feed.")
        return 0, 0, 0

    print(f"  {len(entries)} entries found.")

    new_count = skipped_count = error_count = 0

    for entry in entries:
        if limit > 0 and new_count >= limit:
            break

        try:
            result = ingest_entry(
                conn,
                entry,
                feed,
                publication=publication,
                location=location,
                language=language,
                source_type=source_type,
                embed_method=embed_method,
                embed_model=embed_model,
                force=force,
            )
        except Exception as exc:
            error_count += 1
            title_raw = getattr(entry, "title", "(unknown)")
            print(f"  x  {title_raw[:60]}  ERROR: {exc}")
            continue

        title   = result["title"][:70]
        author  = result["author"]
        date    = result["date"]
        n_chunks = result["chunk_count"]
        n_embed  = result["embedded"]

        if result["status"] == "ok":
            new_count += 1
            embed_str = f"  [{n_embed}/{n_chunks} chunks embedded]" if embed_method != "none" else ""
            print(f"  v  {title}")
            print(f"       {author} · {date}{embed_str}")
            if result["error"]:
                print(f"       (warning: {result['error']})")
        elif result["status"] == "skipped":
            skipped_count += 1
            print(f"  -  {title}  (already ingested)")
        else:
            error_count += 1
            print(f"  x  {title}  ERROR: {result['error']}")

    return new_count, skipped_count, error_count


# ---------------------------------------------------------------------------
# CSV feed-list loading
# ---------------------------------------------------------------------------

def load_feed_list(path: Path) -> List[Dict[str, str]]:
    """
    Load a CSV with columns: feed_url, publication, location, language
    Lines missing feed_url or publication are skipped with a warning.
    """
    feeds = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            url  = row.get("feed_url", "").strip()
            pub  = row.get("publication", "").strip()
            loc  = row.get("location", "").strip()
            lang = row.get("language", "en").strip() or "en"
            source_type = row.get("source_type", "").strip()
            if not url or not pub:
                print(f"  [warn] Skipping CSV row {i}: missing feed_url or publication")
                continue
            feeds.append({
                "feed_url": url,
                "publication": pub,
                "location": loc,
                "language": lang,
                "source_type": source_type,
            })
    return feeds


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest RSS/Atom feeds into the CommonSource knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )

    feed_group = parser.add_mutually_exclusive_group(required=True)
    feed_group.add_argument("--feed",      metavar="URL",  help="Single feed URL")
    feed_group.add_argument("--feed-list", metavar="CSV",  help="CSV file with multiple feeds")

    parser.add_argument("--publication",  default="",              help="Publication name (required with --feed)")
    parser.add_argument("--location",     default="",              help="Location/region of the publication")
    parser.add_argument("--language",     default="en",            help="Language code, e.g. hi, en, ta (default: en)")
    parser.add_argument("--db",           default=DEFAULT_DB,      help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--embed-method", default=DEFAULT_EMBED_METHOD, choices=["ollama", "local", "none"])
    parser.add_argument("--embed-model",  default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--source-type",  default=DEFAULT_SOURCE_TYPE,
                        choices=["news", "community", "development", "official"],
                        help="CommonSource evidence layer (default: news)")
    parser.add_argument("--force",  action="store_true", help="Re-ingest even if URL or SHA-1 already exists")
    parser.add_argument("--limit",  type=int, default=0, help="Max articles to ingest per feed (0 = no limit)")

    args = parser.parse_args()

    # Validate single-feed mode
    if args.feed and not args.publication:
        parser.error("--publication is required when using --feed")

    # Connect and init DB
    db_path = Path(args.db)
    conn = connect_db(db_path)
    init_db(conn)
    print(f"Database: {db_path}")

    # Build feed list
    if args.feed:
        feeds = [{
            "feed_url":    args.feed,
            "publication": args.publication,
            "location":    args.location,
            "language":    args.language,
        }]
    else:
        feed_list_path = Path(args.feed_list)
        if not feed_list_path.exists():
            print(f"ERROR: Feed list not found: {feed_list_path}")
            sys.exit(1)
        feeds = load_feed_list(feed_list_path)
        if not feeds:
            print("No valid feeds found in CSV.")
            sys.exit(0)
        print(f"Loaded {len(feeds)} feed(s) from {feed_list_path.name}")

    # Ingest all feeds
    total_new = total_skipped = total_errors = 0

    for feed_cfg in feeds:
        new, skipped, errors = ingest_feed(
            conn,
            feed_url=feed_cfg["feed_url"],
            publication=feed_cfg["publication"],
            location=feed_cfg.get("location", ""),
            language=feed_cfg.get("language", "en"),
            source_type=feed_cfg.get("source_type") or args.source_type,
            embed_method=args.embed_method,
            embed_model=args.embed_model,
            force=args.force,
            limit=args.limit,
        )
        total_new     += new
        total_skipped += skipped
        total_errors  += errors

    # Summary
    print(f"\n{'─' * 50}")
    print("Ingestion complete.")
    print(f"  {total_new} new  ·  {total_skipped} skipped  ·  {total_errors} errors")

    conn.close()


if __name__ == "__main__":
    main()
