"""
knowledge_db.py — SQLite persistence layer for the GAT Platform knowledge base.

Schema overview:
  knowledge_assets      — one row per ingested document (raw text + metadata)
  knowledge_chunks      — text chunks from each asset (with optional embeddings)
  knowledge_extractions — structured world-model extractions per asset (from Mistral)
  approved_world_models — human-reviewed extractions promoted to the knowledge base
  domain_pack_links     — many-to-many: approved world models ↔ domain packs
  domain_classifications — lightweight keyword-based domain assignments per asset
  layer_* tables        — normalized, queryable slices of approved world models
  commonsource_articles — CommonSource provenance: publication, author, date, location

The embeddings stored in knowledge_chunks.embedding_blob are IEEE-754 double-precision
floats packed with Python's struct module. Use embed.blob_to_embedding() to unpack.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from domains import DOMAIN_PACK_IDS, get_domain_pack, normalize_domain_pack_id
from content_classifier import classify_source_type, ensure_source_types, get_source_type_id

log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_assets (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          source_type TEXT NOT NULL,
          source_path TEXT,
          source_sha1 TEXT UNIQUE,
          raw_text TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_assets_sha1 ON knowledge_assets(source_sha1);
        CREATE INDEX IF NOT EXISTS idx_assets_created ON knowledge_assets(created_at);

        -- CommonSource provenance table — publication, author, date, location per article
        -- Linked to knowledge_assets by asset_id. One row per ingested article.
        CREATE TABLE IF NOT EXISTS commonsource_articles (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL UNIQUE,
          publication TEXT NOT NULL,          -- e.g. "Hardnews"
          author TEXT,                         -- byline / journalist name
          date_published TEXT,                 -- ISO date string: YYYY-MM-DD or free text
          location TEXT,                       -- city/region of reporting
          article_title TEXT,                  -- headline (may differ from filename)
          article_url TEXT,                    -- original URL if known
          source_type TEXT NOT NULL DEFAULT 'news',
          source_type_id INTEGER,
          content_type TEXT DEFAULT '',
          source_family TEXT DEFAULT '',
          source_medium TEXT DEFAULT '',
          source_origin TEXT DEFAULT '',
          theme TEXT DEFAULT '',
          collection TEXT DEFAULT '',
          language TEXT DEFAULT '',
          source_profile_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_cs_articles_asset ON commonsource_articles(asset_id);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_pub ON commonsource_articles(publication);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_author ON commonsource_articles(author);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_date ON commonsource_articles(date_published);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_type ON commonsource_articles(source_type);

        -- Text chunks with optional embeddings (one row per chunk per asset)
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          chunk_id TEXT NOT NULL,
          chunk_text TEXT NOT NULL,
          token_estimate INTEGER,
          embedding_blob BLOB,           -- struct-packed IEEE-754 doubles (use embed.blob_to_embedding)
          embedding_model TEXT,          -- which model produced the embedding
          created_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_asset ON knowledge_chunks(asset_id);

        -- Domain pack auto-classification per asset (keyword-based, Phase 1)
        CREATE TABLE IF NOT EXISTS domain_classifications (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL,
          domain_pack_id TEXT NOT NULL,
          score INTEGER NOT NULL,
          matched_keywords_json TEXT,
          method TEXT NOT NULL DEFAULT 'keyword',
          created_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_domain_cls_asset ON domain_classifications(asset_id);
        CREATE INDEX IF NOT EXISTS idx_domain_cls_pack ON domain_classifications(domain_pack_id);

        -- Structured world-model extractions from Mistral 7B
        CREATE TABLE IF NOT EXISTS knowledge_extractions (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL,
          model_name TEXT NOT NULL,
          extraction_json TEXT NOT NULL,
          validation_errors_json TEXT,
          status TEXT NOT NULL,
          reviewed_by TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_extractions_asset ON knowledge_extractions(asset_id);
        CREATE INDEX IF NOT EXISTS idx_extractions_status ON knowledge_extractions(status);

        CREATE TABLE IF NOT EXISTS approved_world_models (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL,
          approved_json TEXT NOT NULL,
          domain_pack_id TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS domain_pack_links (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          domain_pack_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id)
        );

        CREATE TABLE IF NOT EXISTS layer_context_fragments (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          context_type TEXT NOT NULL,
          text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_context_model ON layer_context_fragments(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_context_type ON layer_context_fragments(context_type);

        CREATE TABLE IF NOT EXISTS layer_actors (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          actor_id TEXT,
          name TEXT NOT NULL,
          actor_type TEXT,
          interests_json TEXT,
          capabilities_json TEXT,
          vulnerabilities_json TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_actors_model ON layer_actors(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_actors_name ON layer_actors(name);

        CREATE TABLE IF NOT EXISTS layer_institutions (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          institution_id TEXT,
          name TEXT NOT NULL,
          role TEXT,
          jurisdiction TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_institutions_model ON layer_institutions(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_institutions_name ON layer_institutions(name);

        CREATE TABLE IF NOT EXISTS layer_constraints (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          constraint_id TEXT,
          category TEXT,
          description TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_constraints_model ON layer_constraints(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_uncertainties (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          uncertainty_id TEXT,
          description TEXT NOT NULL,
          uncertainty_type TEXT,
          horizon TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_uncertainties_model ON layer_uncertainties(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_stressors (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          stressor_id TEXT,
          label TEXT NOT NULL,
          level TEXT,
          mechanism TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_stressors_model ON layer_stressors(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_stressors_label ON layer_stressors(label);

        CREATE TABLE IF NOT EXISTS layer_thresholds (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          threshold_id TEXT,
          variable TEXT,
          trigger_condition TEXT NOT NULL,
          crossing_effect TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_thresholds_model ON layer_thresholds(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_dilemmas (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          dilemma_id TEXT,
          title TEXT NOT NULL,
          pole_a TEXT,
          pole_b TEXT,
          tradeoff_notes_json TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_dilemmas_model ON layer_dilemmas(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_simulation_opportunities (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          opportunity_id TEXT,
          title TEXT NOT NULL,
          scenario_hook TEXT,
          transition_pressures_json TEXT,
          common_tropes_json TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_opps_model ON layer_simulation_opportunities(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_facilitation_notes (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          note_id TEXT,
          audience TEXT,
          implication TEXT NOT NULL,
          guidance TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_fac_notes_model ON layer_facilitation_notes(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_model_assumptions (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          assumption_text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_assumptions_model ON layer_model_assumptions(approved_world_model_id);

        CREATE TABLE IF NOT EXISTS layer_what_if_rules (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          rule_id TEXT,
          label TEXT NOT NULL,
          if_clause TEXT NOT NULL,
          then_clause TEXT NOT NULL,
          rationale TEXT,
          source_excerpt TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_what_if_model ON layer_what_if_rules(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_what_if_label ON layer_what_if_rules(label);

        CREATE TABLE IF NOT EXISTS layer_simulation_practices (
          id TEXT PRIMARY KEY,
          approved_world_model_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          practice_id TEXT,
          label TEXT NOT NULL,
          practice_type TEXT,
          canonical_text TEXT NOT NULL,
          usage_note TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(approved_world_model_id) REFERENCES approved_world_models(id) ON DELETE CASCADE,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_sim_practice_model ON layer_simulation_practices(approved_world_model_id);
        CREATE INDEX IF NOT EXISTS idx_layer_sim_practice_type ON layer_simulation_practices(practice_type);

        CREATE TABLE IF NOT EXISTS canonical_merl_dilemmas (
          id TEXT PRIMARY KEY,
          seed_id TEXT UNIQUE NOT NULL,
          label TEXT NOT NULL,
          pole_a TEXT,
          pole_b TEXT,
          description TEXT,
          source_games_json TEXT,
          tags_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS canonical_merl_game_rules (
          id TEXT PRIMARY KEY,
          seed_id TEXT UNIQUE NOT NULL,
          label TEXT NOT NULL,
          if_clause TEXT NOT NULL,
          then_clause TEXT NOT NULL,
          description TEXT,
          source_games_json TEXT,
          tags_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mechanic_library (
          id TEXT PRIMARY KEY,
          seed_id TEXT,
          label TEXT NOT NULL,
          mechanic_type TEXT NOT NULL,
          description TEXT NOT NULL,
          trigger_conditions TEXT,
          player_effects TEXT,
          design_notes TEXT,
          source_origin TEXT,
          domain_pack_ids_json TEXT,
          tags_json TEXT,
          source_asset_id TEXT,

          -- Differentiation fields
          facilitation_notes TEXT,
          player_experience TEXT,
          feedback_loop_type TEXT,
          collective_dynamics TEXT,
          merl_lever_ids_json TEXT,
          observable_signal TEXT,

          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS narrative_structures (
          id TEXT PRIMARY KEY,
          source_asset_id TEXT NOT NULL,
          work_title TEXT NOT NULL,
          character_label TEXT,
          character_role TEXT,
          arc_type TEXT,
          arc_description TEXT,
          tension_type TEXT,
          tension_description TEXT,
          resolution_type TEXT,
          simulation_utility TEXT,
          tags_json TEXT,

          -- Differentiation fields
          agency_moments_json TEXT,
          power_dynamics TEXT,
          behavioral_signal TEXT,
          collective_dimension TEXT,
          simulation_role TEXT,

          created_at TEXT NOT NULL,
          FOREIGN KEY(source_asset_id) REFERENCES knowledge_assets(id)
        );

        CREATE TABLE IF NOT EXISTS causal_network (
          id TEXT PRIMARY KEY,
          from_node_type TEXT NOT NULL,
          from_node_label TEXT NOT NULL,
          from_node_id TEXT,
          relation_type TEXT NOT NULL,
          to_node_type TEXT NOT NULL,
          to_node_label TEXT NOT NULL,
          to_node_id TEXT,
          if_condition TEXT,
          then_effect TEXT NOT NULL,
          domain_pack_id TEXT,
          source_extraction_id TEXT,
          source_asset_id TEXT,
          tags_json TEXT,

          -- Differentiation fields
          magnitude TEXT,
          reversibility TEXT,
          time_horizon TEXT,
          probability_weight REAL,
          confidence TEXT,
          feedback_loop_id TEXT,
          feedback_loop_role TEXT,
          actor_differential_json TEXT,
          experience_description TEXT,

          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_causal_loop ON causal_network(feedback_loop_id);
        CREATE INDEX IF NOT EXISTS idx_causal_magnitude ON causal_network(magnitude);

        CREATE TABLE IF NOT EXISTS world_state_templates (
          id TEXT PRIMARY KEY,
          seed_id TEXT,
          label TEXT NOT NULL,
          domain_pack_id TEXT NOT NULL,
          description TEXT,

          -- Starting conditions
          tracks_json TEXT NOT NULL,
          actors_json TEXT,
          canon_rules_json TEXT,
          open_uncertainties_json TEXT,

          -- Simulation scaffolding
          suggested_mechanics_json TEXT,
          entry_tensions_json TEXT,
          facilitation_guide TEXT,

          source_asset_ids_json TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_wst_domain ON world_state_templates(domain_pack_id);
        CREATE INDEX IF NOT EXISTS idx_wst_seed ON world_state_templates(seed_id);

        -- PageRank scores for document importance based on citation graph
        CREATE TABLE IF NOT EXISTS pagerank_scores (
          asset_id TEXT PRIMARY KEY,
          pagerank_score REAL NOT NULL DEFAULT 0.5,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_pagerank_score ON pagerank_scores(pagerank_score DESC);
        CREATE INDEX IF NOT EXISTS idx_pagerank_updated ON pagerank_scores(updated_at);
        """
    )
    ensure_column(conn, "layer_dilemmas", "dilemma_type", "TEXT")
    ensure_column(conn, "layer_dilemmas", "source_origin", "TEXT")
    ensure_column(conn, "layer_what_if_rules", "rule_type", "TEXT")
    ensure_column(conn, "layer_what_if_rules", "source_origin", "TEXT")
    for column_name, column_sql in [
        ("seed_id", "TEXT"),
        ("label", "TEXT"),
        ("mechanic_type", "TEXT"),
        ("description", "TEXT"),
        ("trigger_conditions", "TEXT"),
        ("player_effects", "TEXT"),
        ("design_notes", "TEXT"),
        ("source_origin", "TEXT"),
        ("domain_pack_ids_json", "TEXT"),
        ("tags_json", "TEXT"),
        ("source_asset_id", "TEXT"),
        ("facilitation_notes", "TEXT"),
        ("player_experience", "TEXT"),
        ("feedback_loop_type", "TEXT"),
        ("collective_dynamics", "TEXT"),
        ("merl_lever_ids_json", "TEXT"),
        ("observable_signal", "TEXT"),
        ("created_at", "TEXT"),
    ]:
        ensure_column(conn, "mechanic_library", column_name, column_sql)

    for column_name, column_sql in [
        ("source_asset_id", "TEXT"),
        ("work_title", "TEXT"),
        ("character_label", "TEXT"),
        ("character_role", "TEXT"),
        ("arc_type", "TEXT"),
        ("arc_description", "TEXT"),
        ("tension_type", "TEXT"),
        ("tension_description", "TEXT"),
        ("resolution_type", "TEXT"),
        ("simulation_utility", "TEXT"),
        ("tags_json", "TEXT"),
        ("agency_moments_json", "TEXT"),
        ("power_dynamics", "TEXT"),
        ("behavioral_signal", "TEXT"),
        ("collective_dimension", "TEXT"),
        ("simulation_role", "TEXT"),
        ("created_at", "TEXT"),
    ]:
        ensure_column(conn, "narrative_structures", column_name, column_sql)

    for column_name, column_sql in [
        ("from_node_type", "TEXT"),
        ("from_node_label", "TEXT"),
        ("from_node_id", "TEXT"),
        ("relation_type", "TEXT"),
        ("to_node_type", "TEXT"),
        ("to_node_label", "TEXT"),
        ("to_node_id", "TEXT"),
        ("if_condition", "TEXT"),
        ("then_effect", "TEXT"),
        ("domain_pack_id", "TEXT"),
        ("source_extraction_id", "TEXT"),
        ("source_asset_id", "TEXT"),
        ("tags_json", "TEXT"),
        ("magnitude", "TEXT"),
        ("reversibility", "TEXT"),
        ("time_horizon", "TEXT"),
        ("probability_weight", "REAL"),
        ("confidence", "TEXT"),
        ("feedback_loop_id", "TEXT"),
        ("feedback_loop_role", "TEXT"),
        ("actor_differential_json", "TEXT"),
        ("experience_description", "TEXT"),
        ("created_at", "TEXT"),
    ]:
        ensure_column(conn, "causal_network", column_name, column_sql)

    for column_name, column_sql in [
        ("seed_id", "TEXT"),
        ("label", "TEXT"),
        ("domain_pack_id", "TEXT"),
        ("description", "TEXT"),
        ("tracks_json", "TEXT"),
        ("actors_json", "TEXT"),
        ("canon_rules_json", "TEXT"),
        ("open_uncertainties_json", "TEXT"),
        ("suggested_mechanics_json", "TEXT"),
        ("entry_tensions_json", "TEXT"),
        ("facilitation_guide", "TEXT"),
        ("source_asset_ids_json", "TEXT"),
        ("created_at", "TEXT"),
    ]:
        ensure_column(conn, "world_state_templates", column_name, column_sql)
    ensure_source_types(conn)
    ensure_column(conn, "commonsource_articles", "source_type", "TEXT NOT NULL DEFAULT 'news'")
    ensure_column(conn, "commonsource_articles", "source_type_id", "INTEGER")
    ensure_column(conn, "commonsource_articles", "content_type", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "source_family", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "source_medium", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "source_origin", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "theme", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "collection", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "language", "TEXT DEFAULT ''")
    ensure_column(conn, "commonsource_articles", "source_profile_json", "TEXT DEFAULT '{}'")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_type ON commonsource_articles(source_type);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_type_id ON commonsource_articles(source_type_id);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_content_type ON commonsource_articles(content_type);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_family ON commonsource_articles(source_family);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_medium ON commonsource_articles(source_medium);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_source_origin ON commonsource_articles(source_origin);
        CREATE INDEX IF NOT EXISTS idx_cs_articles_theme ON commonsource_articles(theme);
        CREATE INDEX IF NOT EXISTS idx_causal_loop ON causal_network(feedback_loop_id);
        CREATE INDEX IF NOT EXISTS idx_causal_magnitude ON causal_network(magnitude);
        CREATE INDEX IF NOT EXISTS idx_wst_domain ON world_state_templates(domain_pack_id);
        CREATE INDEX IF NOT EXISTS idx_wst_seed ON world_state_templates(seed_id);
        """
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(str(row["name"]) == column_name for row in columns):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def _json_text(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _aggregate_pack_counts(rows: List[sqlite3.Row], key_name: str = "domain_pack_id") -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        raw_id = row[key_name]
        normalized = normalize_domain_pack_id(str(raw_id)) if raw_id is not None else None
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + int(row["c"])
    ordered = {pack_id: counts[pack_id] for pack_id in DOMAIN_PACK_IDS if pack_id in counts}
    extras = {pack_id: count for pack_id, count in counts.items() if pack_id not in ordered}
    return {**ordered, **extras}


def _as_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return []


def _extract_context_map(approved_json: Dict[str, Any]) -> Dict[str, List[str]]:
    if approved_json.get("world_context"):
        world_context = approved_json.get("world_context") or {}
        return {
            "political": _as_string_list(world_context.get("political_context")),
            "economic": _as_string_list(world_context.get("economic_context")),
            "technical": _as_string_list(world_context.get("technical_context")),
            "legal": _as_string_list(world_context.get("legal_context")),
            "ecological": _as_string_list(world_context.get("ecological_context")),
            "social": _as_string_list(world_context.get("social_context")),
        }
    contexts = approved_json.get("contexts") or {}
    return {
        "political": _as_string_list(contexts.get("political")),
        "economic": _as_string_list(contexts.get("economic")),
        "technical": _as_string_list(contexts.get("technical")),
        "legal": _as_string_list(contexts.get("legal")),
        "ecological": _as_string_list(contexts.get("ecological")),
        "social": _as_string_list(contexts.get("social")),
    }


def _actor_name(actor: Dict[str, Any]) -> str:
    return actor.get("name") or actor.get("label") or "Unknown actor"


def _institution_name(institution: Dict[str, Any]) -> str:
    return institution.get("name") or institution.get("label") or "Unknown institution"


def clear_approved_layers(conn: sqlite3.Connection, approved_world_model_id: str) -> None:
    tables = [
        "layer_context_fragments",
        "layer_actors",
        "layer_institutions",
        "layer_constraints",
        "layer_uncertainties",
        "layer_stressors",
        "layer_thresholds",
        "layer_dilemmas",
        "layer_simulation_opportunities",
        "layer_facilitation_notes",
        "layer_model_assumptions",
        "layer_what_if_rules",
        "layer_simulation_practices",
    ]
    for table in tables:
        conn.execute(
            f"DELETE FROM {table} WHERE approved_world_model_id = ?",
            (approved_world_model_id,),
        )


def sync_approved_layers(
    conn: sqlite3.Connection,
    *,
    approved_world_model_id: str,
    asset_id: str,
    approved_json: Dict[str, Any],
) -> None:
    clear_approved_layers(conn, approved_world_model_id)

    context_map = _extract_context_map(approved_json)
    for context_type, entries in context_map.items():
        for text in entries or []:
            conn.execute(
                """
                INSERT INTO layer_context_fragments
                  (id, approved_world_model_id, asset_id, context_type, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (make_id("ctx"), approved_world_model_id, asset_id, context_type, text, utc_now()),
            )

    for actor in approved_json.get("actors", []) or []:
        conn.execute(
            """
            INSERT INTO layer_actors
              (id, approved_world_model_id, asset_id, actor_id, name, actor_type,
               interests_json, capabilities_json, vulnerabilities_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lactor"),
                approved_world_model_id,
                asset_id,
                actor.get("actor_id"),
                _actor_name(actor),
                actor.get("actor_type") or actor.get("role"),
                _json_text(actor.get("interests")),
                _json_text(actor.get("capabilities")),
                _json_text(actor.get("vulnerabilities")),
                utc_now(),
            ),
        )

    for institution in approved_json.get("institutions", []) or []:
        conn.execute(
            """
            INSERT INTO layer_institutions
              (id, approved_world_model_id, asset_id, institution_id, name, role, jurisdiction, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("linst"),
                approved_world_model_id,
                asset_id,
                institution.get("institution_id"),
                _institution_name(institution),
                institution.get("role") or institution.get("kind"),
                institution.get("jurisdiction"),
                utc_now(),
            ),
        )

    for constraint in approved_json.get("constraints", []) or []:
        conn.execute(
            """
            INSERT INTO layer_constraints
              (id, approved_world_model_id, asset_id, constraint_id, category, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lcon"),
                approved_world_model_id,
                asset_id,
                constraint.get("constraint_id"),
                constraint.get("category"),
                constraint.get("description") or "",
                utc_now(),
            ),
        )

    for uncertainty in approved_json.get("uncertainties", []) or []:
        conn.execute(
            """
            INSERT INTO layer_uncertainties
              (id, approved_world_model_id, asset_id, uncertainty_id, description, uncertainty_type, horizon, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lunc"),
                approved_world_model_id,
                asset_id,
                uncertainty.get("uncertainty_id"),
                uncertainty.get("description") or uncertainty.get("label") or "",
                uncertainty.get("uncertainty_type") or uncertainty.get("label"),
                uncertainty.get("horizon"),
                utc_now(),
            ),
        )

    for stressor in approved_json.get("stressors", []) or []:
        conn.execute(
            """
            INSERT INTO layer_stressors
              (id, approved_world_model_id, asset_id, stressor_id, label, level, mechanism, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lstr"),
                approved_world_model_id,
                asset_id,
                stressor.get("stressor_id"),
                stressor.get("label") or "Unnamed stressor",
                stressor.get("level") or stressor.get("severity"),
                stressor.get("mechanism"),
                utc_now(),
            ),
        )

    for threshold in approved_json.get("thresholds", []) or []:
        conn.execute(
            """
            INSERT INTO layer_thresholds
              (id, approved_world_model_id, asset_id, threshold_id, variable, trigger_condition, crossing_effect, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lthr"),
                approved_world_model_id,
                asset_id,
                threshold.get("threshold_id"),
                threshold.get("variable") or threshold.get("label"),
                threshold.get("trigger_condition") or threshold.get("description") or "",
                threshold.get("crossing_effect") or threshold.get("description"),
                utc_now(),
            ),
        )

    for dilemma in approved_json.get("dilemmas", []) or []:
        conn.execute(
            """
            INSERT INTO layer_dilemmas
              (id, approved_world_model_id, asset_id, dilemma_id, title, pole_a, pole_b, dilemma_type, source_origin, tradeoff_notes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("ldil"),
                approved_world_model_id,
                asset_id,
                dilemma.get("dilemma_id"),
                dilemma.get("title") or dilemma.get("label") or "Untitled dilemma",
                dilemma.get("pole_a"),
                dilemma.get("pole_b"),
                dilemma.get("dilemma_type"),
                dilemma.get("source_origin"),
                _json_text(dilemma.get("tradeoff_notes")),
                utc_now(),
            ),
        )

    for opportunity in approved_json.get("simulation_opportunities", []) or []:
        conn.execute(
            """
            INSERT INTO layer_simulation_opportunities
              (id, approved_world_model_id, asset_id, opportunity_id, title, scenario_hook,
               transition_pressures_json, common_tropes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lopp"),
                approved_world_model_id,
                asset_id,
                opportunity.get("opportunity_id"),
                opportunity.get("title") or "Untitled opportunity",
                opportunity.get("scenario_hook"),
                _json_text(opportunity.get("transition_pressures")),
                _json_text(opportunity.get("common_tropes")),
                utc_now(),
            ),
        )

    for hook in approved_json.get("simulation_hooks", []) or []:
        conn.execute(
            """
            INSERT INTO layer_simulation_opportunities
              (id, approved_world_model_id, asset_id, opportunity_id, title, scenario_hook,
               transition_pressures_json, common_tropes_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lopp"),
                approved_world_model_id,
                asset_id,
                None,
                "Simulation hook",
                hook,
                _json_text([]),
                _json_text(approved_json.get("trope_risks")),
                utc_now(),
            ),
        )

    for note in approved_json.get("facilitation_notes", []) or []:
        conn.execute(
            """
            INSERT INTO layer_facilitation_notes
              (id, approved_world_model_id, asset_id, note_id, audience, implication, guidance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lfac"),
                approved_world_model_id,
                asset_id,
                note.get("note_id"),
                note.get("audience"),
                note.get("implication") or "",
                note.get("guidance"),
                utc_now(),
            ),
        )

    for assumption in approved_json.get("model_assumptions", []) or []:
        conn.execute(
            """
            INSERT INTO layer_model_assumptions
              (id, approved_world_model_id, asset_id, assumption_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                make_id("lassump"),
                approved_world_model_id,
                asset_id,
                assumption,
                utc_now(),
            ),
        )

    for rule in approved_json.get("what_if_rules", []) or []:
        conn.execute(
            """
            INSERT INTO layer_what_if_rules
              (id, approved_world_model_id, asset_id, rule_id, label, rule_type, if_clause, then_clause, source_origin, rationale, source_excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lwhatif"),
                approved_world_model_id,
                asset_id,
                rule.get("rule_id"),
                rule.get("label") or "Untitled rule",
                rule.get("rule_type"),
                rule.get("if_clause") or "",
                rule.get("then_clause") or "",
                rule.get("source_origin"),
                rule.get("rationale"),
                rule.get("source_excerpt"),
                utc_now(),
            ),
        )

    for practice in approved_json.get("simulation_practices", []) or []:
        conn.execute(
            """
            INSERT INTO layer_simulation_practices
              (id, approved_world_model_id, asset_id, practice_id, label, practice_type, canonical_text, usage_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lsimprac"),
                approved_world_model_id,
                asset_id,
                practice.get("practice_id"),
                practice.get("label") or "Untitled practice",
                practice.get("practice_type"),
                practice.get("canonical_text") or "",
                practice.get("usage_note"),
                utc_now(),
            ),
        )

    conn.commit()


def rebuild_all_layers(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT id, asset_id, approved_json FROM approved_world_models"
    ).fetchall()
    clear_counts = {"approved_world_models": len(rows)}
    for row in rows:
        sync_approved_layers(
            conn,
            approved_world_model_id=str(row["id"]),
            asset_id=str(row["asset_id"]),
            approved_json=json.loads(str(row["approved_json"])),
        )
    return clear_counts


def get_db_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    stats = {
        "knowledge_assets": conn.execute("SELECT COUNT(*) AS c FROM knowledge_assets").fetchone()["c"],
        "knowledge_chunks": conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()["c"],
        "domain_classifications": conn.execute("SELECT COUNT(*) AS c FROM domain_classifications").fetchone()["c"],
        "knowledge_extractions": conn.execute("SELECT COUNT(*) AS c FROM knowledge_extractions").fetchone()["c"],
        "approved_world_models": conn.execute("SELECT COUNT(*) AS c FROM approved_world_models").fetchone()["c"],
        "layer_context_fragments": conn.execute("SELECT COUNT(*) AS c FROM layer_context_fragments").fetchone()["c"],
        "layer_actors": conn.execute("SELECT COUNT(*) AS c FROM layer_actors").fetchone()["c"],
        "layer_institutions": conn.execute("SELECT COUNT(*) AS c FROM layer_institutions").fetchone()["c"],
        "layer_constraints": conn.execute("SELECT COUNT(*) AS c FROM layer_constraints").fetchone()["c"],
        "layer_uncertainties": conn.execute("SELECT COUNT(*) AS c FROM layer_uncertainties").fetchone()["c"],
        "layer_stressors": conn.execute("SELECT COUNT(*) AS c FROM layer_stressors").fetchone()["c"],
        "layer_thresholds": conn.execute("SELECT COUNT(*) AS c FROM layer_thresholds").fetchone()["c"],
        "layer_dilemmas": conn.execute("SELECT COUNT(*) AS c FROM layer_dilemmas").fetchone()["c"],
        "layer_simulation_opportunities": conn.execute("SELECT COUNT(*) AS c FROM layer_simulation_opportunities").fetchone()["c"],
        "layer_facilitation_notes": conn.execute("SELECT COUNT(*) AS c FROM layer_facilitation_notes").fetchone()["c"],
        "layer_model_assumptions": conn.execute("SELECT COUNT(*) AS c FROM layer_model_assumptions").fetchone()["c"],
        "layer_what_if_rules": conn.execute("SELECT COUNT(*) AS c FROM layer_what_if_rules").fetchone()["c"],
        "layer_simulation_practices": conn.execute("SELECT COUNT(*) AS c FROM layer_simulation_practices").fetchone()["c"],
        "canonical_merl_dilemmas": conn.execute("SELECT COUNT(*) AS c FROM canonical_merl_dilemmas").fetchone()["c"],
        "canonical_merl_game_rules": conn.execute("SELECT COUNT(*) AS c FROM canonical_merl_game_rules").fetchone()["c"],
        "mechanic_library": conn.execute("SELECT COUNT(*) AS c FROM mechanic_library").fetchone()["c"],
        "narrative_structures": conn.execute("SELECT COUNT(*) AS c FROM narrative_structures").fetchone()["c"],
        "causal_network": conn.execute("SELECT COUNT(*) AS c FROM causal_network").fetchone()["c"],
        "world_state_templates": conn.execute("SELECT COUNT(*) AS c FROM world_state_templates").fetchone()["c"],
    }
    statuses = conn.execute(
        "SELECT status, COUNT(*) AS c FROM knowledge_extractions GROUP BY status ORDER BY c DESC"
    ).fetchall()
    stats["extraction_statuses"] = {str(row["status"]): row["c"] for row in statuses}
    domains = conn.execute(
        "SELECT domain_pack_id, COUNT(*) AS c FROM domain_classifications GROUP BY domain_pack_id ORDER BY c DESC"
    ).fetchall()
    stats["domain_classifications_by_pack"] = _aggregate_pack_counts(domains)

    approved_by_pack = conn.execute(
        "SELECT domain_pack_id, COUNT(*) AS c FROM approved_world_models WHERE domain_pack_id IS NOT NULL GROUP BY domain_pack_id ORDER BY c DESC"
    ).fetchall()
    stats["approved_world_models_by_pack"] = _aggregate_pack_counts(approved_by_pack)

    layer_queries = {
        "layer_context_fragments": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_context_fragments l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_actors": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_actors l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_institutions": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_institutions l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_constraints": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_constraints l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_uncertainties": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_uncertainties l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_stressors": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_stressors l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_thresholds": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_thresholds l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_dilemmas": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_dilemmas l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_simulation_opportunities": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_simulation_opportunities l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_facilitation_notes": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_facilitation_notes l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_model_assumptions": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_model_assumptions l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_what_if_rules": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_what_if_rules l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
        "layer_simulation_practices": "SELECT dpl.domain_pack_id AS domain_pack_id, COUNT(*) AS c FROM layer_simulation_practices l JOIN domain_pack_links dpl ON dpl.approved_world_model_id = l.approved_world_model_id GROUP BY dpl.domain_pack_id",
    }
    stats["layer_counts_by_pack"] = {
        layer_name: _aggregate_pack_counts(conn.execute(sql).fetchall())
        for layer_name, sql in layer_queries.items()
    }
    stats["domain_pack_labels"] = {
        pack_id: (get_domain_pack(pack_id) or {}).get("label", pack_id)
        for pack_id in DOMAIN_PACK_IDS
    }
    return stats


def asset_exists_by_sha1(conn: sqlite3.Connection, sha1: str) -> Optional[str]:
    """Return the existing asset id if a document with this SHA-1 is already ingested."""
    row = conn.execute(
        "SELECT id FROM knowledge_assets WHERE source_sha1 = ?", (sha1,)
    ).fetchone()
    return str(row["id"]) if row else None


def insert_chunks(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    chunks: List[Dict[str, Any]],
) -> List[str]:
    """
    Insert chunk rows for an asset.

    Each dict in `chunks` should have:
      chunk_index (int), chunk_id (str), chunk_text (str),
      token_estimate (int), embedding_blob (bytes|None), embedding_model (str|None)
    """
    ids = []
    for c in chunks:
        chunk_row_id = make_id("chunk")
        conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_chunks
              (id, asset_id, chunk_index, chunk_id, chunk_text,
               token_estimate, embedding_blob, embedding_model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_row_id,
                asset_id,
                c["chunk_index"],
                c["chunk_id"],
                c["chunk_text"],
                c.get("token_estimate"),
                c.get("embedding_blob"),
                c.get("embedding_model"),
                utc_now(),
            ),
        )
        ids.append(chunk_row_id)
    conn.commit()
    return ids


def insert_domain_classifications(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    classifications: List[Dict[str, Any]],
    method: str = "keyword",
) -> None:
    """Insert domain classification results for an asset."""
    for cls in classifications:
        normalized_domain_id = normalize_domain_pack_id(cls["domain_id"])
        if not normalized_domain_id:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO domain_classifications
              (id, asset_id, domain_pack_id, score, matched_keywords_json, method, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("dcls"),
                asset_id,
                normalized_domain_id,
                cls["score"],
                json.dumps(cls.get("matched_keywords", []), ensure_ascii=False),
                method,
                utc_now(),
            ),
        )
    conn.commit()


def get_all_chunks_with_embeddings(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    Return all chunks that have embeddings stored.
    Used for in-memory similarity search.
    """
    rows = conn.execute(
        """
        SELECT kc.id, kc.asset_id, kc.chunk_id, kc.chunk_text,
               kc.embedding_blob, kc.embedding_model,
               ka.title, ka.source_path
        FROM knowledge_chunks kc
        JOIN knowledge_assets ka ON kc.asset_id = ka.id
        WHERE kc.embedding_blob IS NOT NULL
        """
    ).fetchall()
    return [dict(r) for r in rows]


def insert_asset(
    conn: sqlite3.Connection,
    *,
    title: str,
    source_type: str,
    source_path: str,
    source_sha1: str,
    raw_text: str,
    metadata: Dict[str, Any],
) -> str:
    asset_id = make_id("asset")
    conn.execute(
        """
        INSERT INTO knowledge_assets (id, title, source_type, source_path, source_sha1, raw_text, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            title,
            source_type,
            source_path,
            source_sha1,
            raw_text,
            json.dumps(metadata, ensure_ascii=False),
            utc_now(),
        ),
    )
    conn.commit()
    return asset_id


def insert_extraction(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    model_name: str,
    extraction: Dict[str, Any],
    status: str,
    validation_errors: Optional[Any] = None,
) -> str:
    extraction_id = make_id("extract")
    conn.execute(
        """
        INSERT INTO knowledge_extractions (id, asset_id, model_name, extraction_json, validation_errors_json, status, reviewed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            extraction_id,
            asset_id,
            model_name,
            json.dumps(extraction, ensure_ascii=False),
            json.dumps(validation_errors, ensure_ascii=False) if validation_errors is not None else None,
            status,
            utc_now(),
        ),
    )
    conn.commit()
    return extraction_id


def approve_extraction(
    conn: sqlite3.Connection,
    *,
    extraction_id: str,
    reviewer: str,
    approved_json: Dict[str, Any],
) -> str:
    row = conn.execute(
        "SELECT asset_id FROM knowledge_extractions WHERE id = ?",
        (extraction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Extraction not found: {extraction_id}")

    asset_id = str(row["asset_id"])
    approved_id = make_id("approved")
    domain_pack_id = None
    packs = [pack for pack in [normalize_domain_pack_id(pack) for pack in (approved_json.get("domain_pack_ids") or [])] if pack]
    approved_json["domain_pack_ids"] = packs
    if packs:
        domain_pack_id = packs[0]

    conn.execute(
        """
        INSERT INTO approved_world_models (id, asset_id, approved_json, domain_pack_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            approved_id,
            asset_id,
            json.dumps(approved_json, ensure_ascii=False),
            domain_pack_id,
            utc_now(),
        ),
    )

    for pack in packs:
        conn.execute(
            """
            INSERT INTO domain_pack_links (id, approved_world_model_id, domain_pack_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (make_id("dplink"), approved_id, pack, utc_now()),
        )

    conn.execute(
        "UPDATE knowledge_extractions SET status = ?, reviewed_by = ? WHERE id = ?",
        ("approved", reviewer, extraction_id),
    )
    sync_approved_layers(
        conn,
        approved_world_model_id=approved_id,
        asset_id=asset_id,
        approved_json=approved_json,
    )
    conn.commit()
    return approved_id


# ---------------------------------------------------------------------------
# CommonSource article provenance
# ---------------------------------------------------------------------------

def insert_commonsource_article(
    conn: sqlite3.Connection,
    *,
    asset_id: str,
    publication: str,
    author: Optional[str] = None,
    date_published: Optional[str] = None,
    location: Optional[str] = None,
    article_title: Optional[str] = None,
    article_url: Optional[str] = None,
    source_type: str = "news",
    content_type: str = "",
    source_family: str = "",
    source_medium: str = "",
    source_origin: str = "",
    theme: str = "",
    collection: str = "",
    language: str = "",
    source_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert provenance metadata for a CommonSource article. Returns the new row id."""
    article_id = make_id("cs")
    profile = source_profile or {
        "publication": publication,
        "source_type": source_type or "news",
        "content_type": content_type or "",
        "source_family": source_family or "",
        "source_medium": source_medium or "",
        "source_origin": source_origin or "",
        "theme": theme or "",
        "collection": collection or "",
        "language": language or "",
    }
    asset = conn.execute(
        "SELECT raw_text, source_path, metadata_json FROM knowledge_assets WHERE id = ?",
        (asset_id,),
    ).fetchone()
    raw_text = str(asset["raw_text"] or "") if asset else ""
    asset_path = str(asset["source_path"] or "") if asset else ""
    metadata = dict(profile)
    if asset:
        try:
            metadata = {**json.loads(asset["metadata_json"] or "{}"), **profile}
        except Exception:
            metadata = dict(profile)
    classified_source_type = classify_source_type(
        title=article_title or "",
        text=raw_text,
        metadata={**metadata, "source_type": source_type or metadata.get("source_type") or "news"},
        path=article_url or asset_path,
    )
    source_type_id = get_source_type_id(conn, classified_source_type)
    source_type = classified_source_type
    profile["source_type"] = classified_source_type
    profile["source_type_id"] = source_type_id
    conn.execute(
        """
        INSERT OR REPLACE INTO commonsource_articles
          (id, asset_id, publication, author, date_published, location, article_title, article_url,
           source_type, source_type_id, content_type, source_family, source_medium, source_origin,
           theme, collection, language, source_profile_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            asset_id,
            publication,
            author or "",
            date_published or "",
            location or "",
            article_title or "",
            article_url or "",
            source_type or "news",
            source_type_id,
            content_type or "",
            source_family or "",
            source_medium or "",
            source_origin or "",
            theme or "",
            collection or "",
            language or "",
            json.dumps(profile, ensure_ascii=False),
            utc_now(),
        ),
    )
    try:
        metadata = profile
        from knowledge_layer import process_article_knowledge
        process_article_knowledge(
            conn,
            article_id=asset_id,
            title=article_title or "",
            text=raw_text,
            publication=publication,
            metadata=metadata,
        )
    except Exception as exc:
        log.warning("Knowledge layer processing skipped for asset_id=%s: %s", asset_id, exc)
    conn.commit()
    return article_id


def get_commonsource_article(conn: sqlite3.Connection, asset_id: str) -> Optional[Dict[str, Any]]:
    """Return provenance metadata for a given asset_id, or None if not found."""
    row = conn.execute(
        "SELECT * FROM commonsource_articles WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_commonsource_articles(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return all CommonSource articles with their provenance metadata."""
    rows = conn.execute(
        """
        SELECT cs.*, ka.title as asset_title, ka.source_path
        FROM commonsource_articles cs
        JOIN knowledge_assets ka ON ka.id = cs.asset_id
        ORDER BY cs.date_published DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def search_chunks_with_provenance(
    conn: sqlite3.Connection, asset_ids: List[str]
) -> List[Dict[str, Any]]:
    """
    Given a list of asset_ids (from a similarity search), return chunk text
    joined with CommonSource provenance for citation formatting.
    """
    if not asset_ids:
        return []
    placeholders = ",".join("?" * len(asset_ids))
    rows = conn.execute(
        f"""
        SELECT
            kc.asset_id,
            kc.chunk_text,
            cs.publication,
            cs.author,
            cs.date_published,
            cs.location,
            cs.article_title,
            cs.article_url
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        WHERE kc.asset_id IN ({placeholders})
        ORDER BY kc.chunk_index ASC
        """,
        asset_ids,
    ).fetchall()
    return [dict(r) for r in rows]
