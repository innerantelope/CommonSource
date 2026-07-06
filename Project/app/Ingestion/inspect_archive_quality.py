"""
Inspect extraction quality for a CommonSource archive before ingestion.

This does not write to the database. It extracts text from supported files and
prints a CSV-style report so noisy PDFs can be held back for OCR or review.

Example:
    python3 inspect_archive_quality.py \
      --source-dir "sample_docs/SMART" \
      --recursive \
      --output smart_quality_report.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Dict

from ingest_commonsource import extract_text_from_file


def quality_metrics(text: str) -> Dict[str, float | int | str]:
    sample = text[:20000]
    total = len(sample)
    if total == 0:
        return {
            "chars": len(text),
            "words": 0,
            "replacement_ratio": 1.0,
            "control_ratio": 1.0,
            "long_token_count": 0,
            "status": "empty",
        }

    replacement = sample.count("\ufffd")
    controls = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t")
    words = re.findall(r"\S+", text)
    long_tokens = sum(1 for word in words[:5000] if len(word) > 80)

    replacement_ratio = replacement / total
    control_ratio = controls / total
    status = "ok"
    if len(text.strip()) < 200:
        status = "too_short"
    elif replacement_ratio > 0.01 or control_ratio > 0.01 or long_tokens > 20:
        status = "review"

    return {
        "chars": len(text),
        "words": len(words),
        "replacement_ratio": round(replacement_ratio, 4),
        "control_ratio": round(control_ratio, 4),
        "long_token_count": long_tokens,
        "status": status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect archive text extraction quality")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--extensions", default=".docx,.pdf,.txt")
    parser.add_argument("--output", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    extensions = {ext.strip().lower() for ext in args.extensions.split(",") if ext.strip()}
    extensions = {ext if ext.startswith(".") else f".{ext}" for ext in extensions}
    candidates = source_dir.rglob("*") if args.recursive else source_dir.glob("*")
    files = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in extensions)
    if args.limit > 0:
        files = files[: args.limit]

    fieldnames = [
        "filepath",
        "extension",
        "chars",
        "words",
        "replacement_ratio",
        "control_ratio",
        "long_token_count",
        "status",
        "error",
    ]

    rows = []
    counts: Dict[str, int] = {}
    for path in files:
        relative = str(path.relative_to(source_dir))
        row = {"filepath": relative, "extension": path.suffix.lower(), "error": ""}
        try:
            row.update(quality_metrics(extract_text_from_file(path)))
        except Exception as exc:
            row.update({
                "chars": 0,
                "words": 0,
                "replacement_ratio": 1.0,
                "control_ratio": 1.0,
                "long_token_count": 0,
                "status": "error",
                "error": str(exc),
            })
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
        rows.append(row)

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} quality rows to {args.output}")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    print(f"Summary: {summary}")


if __name__ == "__main__":
    main()
