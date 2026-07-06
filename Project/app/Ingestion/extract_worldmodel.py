"""
extract_worldmodel.py — World-model extraction from knowledge assets via Qwen (Ollama).

For each knowledge_asset that has no row in knowledge_extractions, concatenates its
chunks and sends them to a Qwen model running via Ollama. Parses the structured JSON
response and writes the result to knowledge_extractions with status 'pending_review'.

The expected JSON shape mirrors what knowledge_db.sync_approved_layers() consumes.

Resumable: assets that already have any extraction row are skipped.

Usage:
    python3 extract_worldmodel.py
    python3 extract_worldmodel.py --db commonsource.db
    python3 extract_worldmodel.py --db commonsource.db --batch-size 5
    python3 extract_worldmodel.py --db commonsource.db --publisher Hardnews
    python3 extract_worldmodel.py --db commonsource.db --model qwen2.5:7b
    python3 extract_worldmodel.py --db commonsource.db --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
for _module_path in (APP_DIR, PROJECT_ROOT):
    if str(_module_path) not in sys.path:
        sys.path.insert(0, str(_module_path))

from embed import check_ollama_available
from knowledge_db import connect_db, init_db, insert_extraction, make_id, utc_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt engineering
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a structured knowledge extraction engine for a civic simulation platform.

Given a source document, extract a world model as a single JSON object.
Output ONLY valid JSON — no prose, no markdown fences, no explanation.

Required JSON shape:
{
  "domain_pack_ids": ["string"],   // e.g. ["media_freedom", "public_health"]
  "world_context": {
    "political_context": ["string"],
    "economic_context": ["string"],
    "technical_context": ["string"],
    "legal_context": ["string"],
    "ecological_context": ["string"],
    "social_context": ["string"]
  },
  "actors": [
    {
      "actor_id": "string",
      "name": "string",
      "actor_type": "string",       // e.g. "government", "civil_society", "journalist", "corporation"
      "interests": ["string"],
      "capabilities": ["string"],
      "vulnerabilities": ["string"]
    }
  ],
  "institutions": [
    {
      "institution_id": "string",
      "name": "string",
      "role": "string",
      "jurisdiction": "string"
    }
  ],
  "constraints": [
    {
      "constraint_id": "string",
      "category": "string",         // e.g. "legal", "economic", "political", "social"
      "description": "string"
    }
  ],
  "uncertainties": [
    {
      "uncertainty_id": "string",
      "description": "string",
      "uncertainty_type": "string", // e.g. "political", "empirical", "normative"
      "horizon": "string"           // e.g. "short-term", "medium-term", "long-term"
    }
  ],
  "stressors": [
    {
      "stressor_id": "string",
      "label": "string",
      "level": "string",            // e.g. "high", "medium", "low"
      "mechanism": "string"
    }
  ],
  "thresholds": [
    {
      "threshold_id": "string",
      "variable": "string",
      "trigger_condition": "string",
      "crossing_effect": "string"
    }
  ],
  "dilemmas": [
    {
      "dilemma_id": "string",
      "title": "string",
      "pole_a": "string",
      "pole_b": "string",
      "dilemma_type": "string",     // e.g. "moral", "strategic", "resource"
      "tradeoff_notes": ["string"]
    }
  ],
  "what_if_rules": [
    {
      "rule_id": "string",
      "label": "string",
      "rule_type": "string",        // e.g. "causal", "normative", "institutional"
      "if_clause": "string",
      "then_clause": "string",
      "rationale": "string",
      "source_excerpt": "string"
    }
  ],
  "simulation_opportunities": [
    {
      "opportunity_id": "string",
      "title": "string",
      "scenario_hook": "string",
      "transition_pressures": ["string"],
      "common_tropes": ["string"]
    }
  ],
  "facilitation_notes": [
    {
      "note_id": "string",
      "audience": "string",         // e.g. "facilitator", "player", "observer"
      "implication": "string",
      "guidance": "string"
    }
  ],
  "model_assumptions": ["string"],
  "simulation_practices": [
    {
      "practice_id": "string",
      "label": "string",
      "practice_type": "string",    // e.g. "negotiation", "resource_allocation", "decision_making"
      "canonical_text": "string",
      "usage_note": "string"
    }
  ]
}

Rules:
- Use empty arrays [] for any section with nothing to extract.
- All string IDs should be short slugs (e.g. "actor_001", "constraint_media_law").
- Extract only what is genuinely supported by the document.
- Do not hallucinate facts not present in the source.
"""

USER_PROMPT_TEMPLATE = """Extract a world model from the following document.

Title: {title}
Source type: {source_type}

---
{text}
---

Return ONLY the JSON object. No prose."""


def build_user_prompt(asset: dict, chunk_texts: list[str], max_chars: int = 12000) -> str:
    combined = "\n\n".join(chunk_texts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n[truncated]"
    return USER_PROMPT_TEMPLATE.format(
        title=asset.get("title", "Untitled"),
        source_type=asset.get("source_type", "unknown"),
        text=combined,
    )


# ---------------------------------------------------------------------------
# Ollama chat completion
# ---------------------------------------------------------------------------

def call_qwen(
    user_prompt: str,
    model: str,
    base_url: str,
    timeout: int = 300,
) -> str:
    res = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        },
        timeout=timeout,
    )
    res.raise_for_status()
    data = res.json()
    return data["message"]["content"].strip()


def extract_json(raw: str) -> dict[str, Any]:
    """Strip markdown fences if present, then parse JSON."""
    # Remove ```json ... ``` or ``` ... ```
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    return json.loads(cleaned.strip())


ARRAY_KEYS = [
    "domain_pack_ids", "actors", "institutions", "constraints", "uncertainties",
    "stressors", "thresholds", "dilemmas", "what_if_rules", "simulation_opportunities",
    "facilitation_notes", "model_assumptions", "simulation_practices",
]
REQUIRED_KEYS = ["world_context", "actors", "domain_pack_ids"]


def validate_extraction(data: dict) -> list[str]:
    # Fill missing array keys with [] so downstream consumers don't break
    for k in ARRAY_KEYS:
        if k not in data:
            data[k] = []
    if "world_context" not in data:
        data["world_context"] = {}

    errors = [f"Missing key: {k}" for k in REQUIRED_KEYS if not data.get(k)]
    return errors


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_assets_without_extractions(conn, publisher: str | None) -> list[dict]:
    if publisher:
        query = """
            SELECT ka.id, ka.title, ka.source_type, ka.raw_text
            FROM knowledge_assets ka
            JOIN commonsource_articles cs ON cs.asset_id = ka.id
            WHERE cs.publication = ?
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_extractions ke WHERE ke.asset_id = ka.id
              )
            ORDER BY ka.created_at
        """
        params: tuple = (publisher,)
    else:
        query = """
            SELECT ka.id, ka.title, ka.source_type, ka.raw_text
            FROM knowledge_assets ka
            WHERE NOT EXISTS (
                SELECT 1 FROM knowledge_extractions ke WHERE ke.asset_id = ka.id
            )
            ORDER BY ka.created_at
        """
        params = ()
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def fetch_chunks_for_asset(conn, asset_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT chunk_text FROM knowledge_chunks
        WHERE asset_id = ?
        ORDER BY chunk_index
        """,
        (asset_id,),
    ).fetchall()
    return [r["chunk_text"] for r in rows if r["chunk_text"]]


def run_extractions(
    db_path: Path,
    *,
    batch_size: int,
    publisher: str | None,
    model: str,
    ollama_url: str,
    dry_run: bool,
) -> None:
    if not check_ollama_available(ollama_url):
        log.error("Ollama is not reachable at %s — is it running?", ollama_url)
        sys.exit(1)

    conn = connect_db(db_path)
    init_db(conn)

    assets = fetch_assets_without_extractions(conn, publisher)
    total = len(assets)

    if total == 0:
        log.info(
            "No assets without extractions found%s.",
            f" for publisher '{publisher}'" if publisher else "",
        )
        return

    log.info(
        "Found %d asset(s) without extractions%s.",
        total,
        f" for publisher '{publisher}'" if publisher else "",
    )

    if dry_run:
        log.info("Dry run — no writes will happen.")
        for a in assets[:5]:
            log.info("  Would extract: [%s] %s", a["id"], a["title"][:80])
        if total > 5:
            log.info("  ... and %d more.", total - 5)
        return

    done = 0
    failed = 0

    for i in range(0, total, batch_size):
        batch = assets[i : i + batch_size]
        for asset in batch:
            asset_id = asset["id"]
            title = asset.get("title", "?")[:60]

            chunk_texts = fetch_chunks_for_asset(conn, asset_id)
            if not chunk_texts:
                # Fall back to raw_text if no chunks are stored
                raw = (asset.get("raw_text") or "").strip()
                if not raw:
                    log.warning("[%s] %s — no text, skipping.", asset_id, title)
                    failed += 1
                    continue
                chunk_texts = [raw]

            user_prompt = build_user_prompt(asset, chunk_texts)

            try:
                raw_response = call_qwen(user_prompt, model=model, base_url=ollama_url)
                extraction = extract_json(raw_response)
                validation_errors = validate_extraction(extraction)
                status = "pending_review" if not validation_errors else "validation_failed"
            except json.JSONDecodeError as exc:
                log.error("[%s] %s — JSON parse error: %s", asset_id, title, exc)
                extraction = {"raw_response": raw_response if 'raw_response' in dir() else ""}
                validation_errors = [f"JSON parse error: {exc}"]
                status = "error"
                failed += 1
            except Exception as exc:
                log.error("[%s] %s — Qwen call failed: %s", asset_id, title, exc)
                extraction = {}
                validation_errors = [str(exc)]
                status = "error"
                failed += 1

            if status != "error":
                done += 1

            insert_extraction(
                conn,
                asset_id=asset_id,
                model_name=model,
                extraction=extraction,
                status=status,
                validation_errors=validation_errors if validation_errors else None,
            )

            log.info(
                "[%d/%d] %s  status=%s  keys=%d",
                i + batch.index(asset) + 1,
                total,
                title,
                status,
                len(extraction),
            )

        log.info(
            "Batch done. done=%d  failed=%d  remaining=%d",
            done,
            failed,
            total - (i + len(batch)),
        )

    log.info("Finished. extracted=%d  failed=%d  total=%d", done, failed, total)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract world models from knowledge assets via Qwen (Ollama)."
    )
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "database" / "commonsource.db"), help="SQLite DB path")
    ap.add_argument("--batch-size", type=int, default=5, help="Assets per batch")
    ap.add_argument("--publisher", default=None, help="Filter by commonsource_articles.publication")
    ap.add_argument(
        "--model", default="qwen2.5:7b",
        help="Ollama model name (default: qwen2.5:7b)"
    )
    ap.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = ap.parse_args()

    run_extractions(
        Path(args.db),
        batch_size=args.batch_size,
        publisher=args.publisher,
        model=args.model,
        ollama_url=args.ollama_url,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
