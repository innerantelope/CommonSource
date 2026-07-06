"""
Build a reviewable metadata CSV for a CommonSource archive folder.

This is intentionally conservative: it derives title, collection, and language
from file paths, while leaving author/date/url blank for human cleanup.

Example:
    python3 build_archive_metadata.py \
      --source-dir "sample_docs/SMART" \
      --output smart_metadata.csv \
      --publication SMART \
      --source-type development \
      --recursive
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

from source_classifier import classify_source


LANGUAGE_HINTS = {
    "assamese": "Assamese",
    "english": "English",
    "eng": "English",
    "gujarati": "Gujarati",
    "hindi": "Hindi",
    "marathi": "Marathi",
    "tamil": "Tamil",
    "telugu": "Telugu",
}

LANGUAGE_NAMES = {language.lower() for language in LANGUAGE_HINTS.values()}


def clean_title(path: Path) -> str:
    title = path.stem
    title = re.sub(r"[_\-]+", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def infer_language(relative: Path) -> str:
    parts = [p.lower() for p in relative.parts]
    for part in parts:
        for key, language in LANGUAGE_HINTS.items():
            if key in part:
                return language
    return ""


def infer_collection(relative: Path) -> str:
    parts = list(relative.parts[:-1])
    if not parts:
        return ""
    ignored = {"scripts", "smart scripts", "reports"}
    useful = [p for p in parts if p.lower() not in ignored]
    return useful[-1] if useful else parts[-1]


def infer_content_type(relative: Path) -> str:
    parts = [p.lower() for p in relative.parts]
    if "scripts" in parts or "smart scripts" in parts:
        return "radio_script"
    if "reports" in parts:
        return "report"
    return ""


def infer_source_type(relative: Path, default_source_type: str) -> str:
    if infer_content_type(relative) == "radio_script":
        return "community"
    if infer_content_type(relative) == "report":
        return "development"
    return default_source_type


def infer_theme(relative: Path) -> str:
    parts = list(relative.parts[:-1])
    ignored = {"scripts", "smart scripts", "reports"}
    useful = [
        p for p in parts
        if p.lower() not in ignored
        and p.lower() not in LANGUAGE_NAMES
        and not re.fullmatch(r"script\s*\d+", p.strip(), flags=re.IGNORECASE)
    ]
    if not useful:
        return "Reports" if "reports" in [p.lower() for p in parts] else ""
    return useful[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build metadata CSV for CommonSource archive ingestion")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--publication", required=True)
    parser.add_argument("--source-type", default="development")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--extensions", default=".docx,.pdf,.txt")
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    extensions = {ext.strip().lower() for ext in args.extensions.split(",") if ext.strip()}
    extensions = {ext if ext.startswith(".") else f".{ext}" for ext in extensions}
    candidates = source_dir.rglob("*") if args.recursive else source_dir.glob("*")
    files = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in extensions)

    fieldnames = [
        "filename",
        "filepath",
        "publication",
        "source_type",
        "content_type",
        "source_family",
        "source_medium",
        "source_origin",
        "theme",
        "author",
        "date_published",
        "location",
        "title",
        "url",
        "collection",
        "language",
        "notes",
    ]

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for path in files:
            relative = path.relative_to(source_dir)
            content_type = infer_content_type(relative)
            profile = classify_source(
                {
                    "publication": args.publication,
                    "source_type": infer_source_type(relative, args.source_type),
                    "content_type": content_type,
                    "theme": infer_theme(relative),
                    "collection": infer_collection(relative),
                    "language": infer_language(relative),
                },
                path=relative,
                publication=args.publication,
                default_source_type=args.source_type,
                source_origin="archive",
            )
            writer.writerow({
                "filename": path.name,
                "filepath": str(relative),
                "publication": args.publication,
                "source_type": profile["source_type"],
                "content_type": profile["content_type"],
                "source_family": profile["source_family"],
                "source_medium": profile["source_medium"],
                "source_origin": profile["source_origin"],
                "theme": profile["theme"],
                "author": "",
                "date_published": "",
                "location": "",
                "title": clean_title(path),
                "url": "",
                "collection": profile["collection"],
                "language": profile["language"],
                "notes": "",
            })

    print(f"Wrote {len(files)} metadata rows to {args.output}")


if __name__ == "__main__":
    main()
