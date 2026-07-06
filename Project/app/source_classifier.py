"""
Source classification helpers for CommonSource ingestion.

The important separation is:
  source_type    -> evidence layer (news, community, development, official)
  content_type   -> document form (magazine_article, radio_script, report, ...)
  source_family  -> canonical source/org lineage (hardnews, smart, aapti-institute)
  source_medium  -> medium used for presentation/filtering (magazine, radio, report, web)
  source_origin  -> how it entered the index (archive, rss, upload, pdf, audio)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


VALID_SOURCE_TYPES = {"news", "community", "development", "official"}

CONTENT_TYPE_ALIASES = {
    "article": "article",
    "news": "article",
    "news_article": "article",
    "magazine": "magazine_article",
    "magazine_article": "magazine_article",
    "hardnews": "magazine_article",
    "script": "radio_script",
    "radio": "radio_script",
    "radio_script": "radio_script",
    "audio": "audio_transcript",
    "audio_transcript": "audio_transcript",
    "transcript": "audio_transcript",
    "report": "report",
    "reports": "report",
    "research": "research_report",
    "research_report": "research_report",
    "paper": "research_report",
    "policy": "policy_brief",
    "policy_brief": "policy_brief",
    "official": "official_record",
    "official_record": "official_record",
    "document": "document",
    "upload": "document",
}

FAMILY_ALIASES = {
    "hardnews": "hardnews",
    "hard news": "hardnews",
    "smart": "smart",
    "health on air": "health-on-air",
    "aapti": "aapti-institute",
    "aapti institute": "aapti-institute",
}


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _path_context(path: Optional[Any]) -> str:
    if not path:
        return ""
    try:
        p = Path(str(path))
        parts = list(p.parts)
        return " ".join(parts + [p.stem, p.suffix])
    except Exception:
        return str(path)


def _meta_first(meta: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(meta.get(key))
        if value:
            return value
    return ""


def normalize_source_type(value: str, fallback: str = "news") -> str:
    value = slugify(value).replace("-", "_")
    if value in VALID_SOURCE_TYPES:
        return value
    if value in {"ngo", "civil_society", "research", "report"}:
        return "development"
    if value in {"radio", "community_radio", "peoples_voice", "people_voice"}:
        return "community"
    if value in {"government", "state", "public_record", "official_record"}:
        return "official"
    return fallback if fallback in VALID_SOURCE_TYPES else "news"


def normalize_content_type(value: str) -> str:
    key = slugify(value).replace("-", "_")
    return CONTENT_TYPE_ALIASES.get(key, key if key else "")


def infer_source_family(publication: str, meta: Dict[str, Any], path: Optional[Any] = None) -> str:
    explicit = _meta_first(meta, "source_family", "source_id", "source_slug")
    if explicit:
        return slugify(explicit)

    pub_key = _clean(publication).lower()
    if pub_key in FAMILY_ALIASES:
        return FAMILY_ALIASES[pub_key]

    context = f"{publication} {_path_context(path)}".lower()
    for hint, family in FAMILY_ALIASES.items():
        if hint in context:
            return family

    return slugify(publication) or "unknown"


def infer_collection(meta: Dict[str, Any], path: Optional[Any]) -> str:
    explicit = _meta_first(meta, "collection")
    if explicit:
        return explicit
    if not path:
        return ""
    try:
        p = Path(str(path))
        ignored = {"scripts", "smart scripts", "reports", "documents", "project"}
        useful = [part for part in p.parts[:-1] if part.lower() not in ignored]
        return useful[-1] if useful else ""
    except Exception:
        return ""


def infer_content_type(
    *,
    explicit: str,
    default: str,
    publication: str,
    source_family: str,
    path: Optional[Any],
    source_origin: str,
) -> str:
    content_type = normalize_content_type(explicit) or normalize_content_type(default)
    if content_type:
        return content_type

    context = f"{publication} {source_family} {_path_context(path)} {source_origin}".lower()
    suffix = Path(str(path)).suffix.lower() if path else ""

    if any(word in context for word in ["radio script", "smart scripts", " script "]):
        return "radio_script"
    if "audio" in context or suffix in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".mp4"}:
        return "audio_transcript"
    if source_family == "aapti-institute" or "research" in context:
        return "research_report"
    if "report" in context or suffix == ".pdf":
        return "report"
    if source_family == "hardnews":
        return "magazine_article"
    if "article" in context or "articles" in context:
        return "article"
    if source_origin == "rss":
        return "article"
    return "document"


def infer_source_type(explicit: str, default: str, content_type: str, source_family: str) -> str:
    if explicit:
        return normalize_source_type(explicit)

    if content_type in {"radio_script", "audio_transcript"}:
        return "community"
    if content_type in {"report", "research_report", "policy_brief"}:
        return "development"
    if content_type == "official_record":
        return "official"
    if source_family == "hardnews" or content_type in {"article", "magazine_article"}:
        return "news"
    return normalize_source_type(default)


def infer_source_medium(content_type: str, source_origin: str) -> str:
    if content_type == "magazine_article":
        return "magazine"
    if content_type in {"radio_script", "audio_transcript"}:
        return "radio"
    if content_type in {"report", "research_report", "policy_brief"}:
        return "report"
    if content_type == "official_record":
        return "official_record"
    if content_type == "article" or source_origin == "rss":
        return "web"
    return "document"


def classify_source(
    meta: Optional[Dict[str, Any]] = None,
    *,
    path: Optional[Any] = None,
    publication: str = "",
    default_source_type: str = "news",
    default_content_type: str = "",
    source_origin: str = "archive",
) -> Dict[str, Any]:
    meta = dict(meta or {})
    publication = _clean(publication) or _meta_first(meta, "publication", "publisher") or "Unknown"
    explicit_origin = _meta_first(meta, "source_origin", "ingest_origin", "source_channel")
    source_origin = slugify(explicit_origin or source_origin or "archive").replace("-", "_")

    source_family = infer_source_family(publication, meta, path)
    content_type = infer_content_type(
        explicit=_meta_first(meta, "content_type", "document_type", "source_format"),
        default=default_content_type,
        publication=publication,
        source_family=source_family,
        path=path,
        source_origin=source_origin,
    )
    source_type = infer_source_type(
        explicit=_meta_first(meta, "source_type", "evidence_layer"),
        default=default_source_type,
        content_type=content_type,
        source_family=source_family,
    )
    source_medium = _meta_first(meta, "source_medium", "medium") or infer_source_medium(content_type, source_origin)
    collection = infer_collection(meta, path)
    theme = _meta_first(meta, "theme") or collection

    return {
        "publication": publication,
        "source_type": source_type,
        "content_type": content_type,
        "source_family": source_family,
        "source_medium": slugify(source_medium).replace("-", "_"),
        "source_origin": source_origin,
        "theme": theme,
        "collection": collection,
        "language": _meta_first(meta, "language"),
        "classification_method": "explicit+rules",
    }
