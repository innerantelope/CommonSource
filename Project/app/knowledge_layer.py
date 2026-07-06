from __future__ import annotations

import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


ENTITY_TYPES = {"PERSON", "ORG", "GPE", "LOC", "EVENT", "TOPIC"}
PHASE3_ENTITY_TYPES = {
    "Disease",
    "Program",
    "Government Scheme",
    "Organization",
    "Location",
    "Person",
    "Topic",
    "Campaign",
}

ENTITY_TYPE_DISPLAY = {
    "PERSON": "Person",
    "ORG": "Organization",
    "GPE": "Location",
    "LOC": "Location",
    "EVENT": "Topic",
    "TOPIC": "Topic",
}

PHASE3_ENTITY_COMPATIBILITY = {
    "Disease": "TOPIC",
    "Program": "TOPIC",
    "Government Scheme": "TOPIC",
    "Organization": "ORG",
    "Location": "GPE",
    "Person": "PERSON",
    "Topic": "TOPIC",
    "Campaign": "TOPIC",
}

STOPWORDS = {
    "about", "above", "after", "again", "against", "also", "among", "around", "because",
    "before", "being", "between", "could", "during", "every", "first", "from", "have",
    "into", "more", "most", "other", "over", "said", "same", "than", "that", "their",
    "there", "these", "this", "those", "through", "under", "until", "while", "with",
    "without", "would", "article", "story", "report", "reports", "news", "media",
    "the", "and", "for", "are", "was", "were", "has", "had", "not", "but", "you",
    "all", "can", "will", "its", "our", "your", "they", "them", "his", "her", "she",
    "him", "who", "what", "when", "where", "why", "how",
}

CANONICAL_TAGS = {
    "ai": "Artificial Intelligence",
    "artificial intelligence": "Artificial Intelligence",
    "machine learning": "Artificial Intelligence",
    "ml": "Artificial Intelligence",
    "usa": "United States",
    "u s": "United States",
    "u.s": "United States",
    "u.s.a": "United States",
    "us": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "united kingdom": "United Kingdom",
    "climate": "Climate Change",
    "climate change": "Climate Change",
    "global warming": "Climate Change",
    "covid": "COVID-19",
    "covid 19": "COVID-19",
    "coronavirus": "COVID-19",
}

GPE_NAMES = {
    "Afghanistan", "Africa", "Australia", "Bangladesh", "Brazil", "Canada", "China",
    "Delhi", "Europe", "France", "Germany", "India", "Indonesia", "Japan", "Kenya",
    "Mumbai", "Nepal", "Pakistan", "Russia", "South Africa", "Sri Lanka", "United Kingdom",
    "United States", "USA", "US", "U.S.", "U.S.A.", "Washington",
}

ORG_SUFFIXES = (
    "Agency", "Association", "Authority", "Bank", "Commission", "Committee", "Company",
    "Corporation", "Council", "Court", "Department", "Foundation", "Government", "Group",
    "Institute", "Media", "Ministry", "News", "Office", "Organization", "Press",
    "Trust", "University",
)

EVENT_TERMS = (
    "conference", "crisis", "election", "festival", "flood", "meeting", "protest",
    "summit", "war", "workshop",
)

TOPIC_HINTS = {
    "agriculture", "artificial intelligence", "climate change", "culture", "democracy",
    "education", "environment", "gender", "governance", "health", "housing", "migration",
    "public health", "technology", "water",
}

PHASE3_ENTITY_ALIASES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "Disease": {
        "Anemia": ("anemia", "anaemia"),
        "Cancer": ("cancer",),
        "Corona Pandemic": ("corona pandemic", "coronavirus pandemic", "covid pandemic"),
        "COVID-19": ("covid", "covid-19", "coronavirus"),
        "Dengue": ("dengue",),
        "Diarrhea": ("diarrhea", "diarrhoea"),
        "Filaria": ("filaria", "filariasis"),
        "HIV/AIDS": ("hiv", "aids", "hiv/aids"),
        "Jaundice": ("jaundice",),
        "Malaria": ("malaria",),
        "Tuberculosis": ("tuberculosis", "tb"),
    },
    "Government Scheme": {
        "ABHA ID": ("abha", "abha id"),
        "Ayushman Bharat": ("ayushman bharat",),
        "ICDS": ("icds", "integrated child development services"),
        "Janani Suraksha Yojana": ("janani suraksha yojana", "jsy"),
        "National Health Mission": ("national health mission", "nhm"),
        "Nikshay": ("nikshay",),
        "POSHAN Abhiyaan": ("poshan abhiyaan", "poshan"),
        "Swachh Bharat Mission": ("swachh bharat", "swachh bharat mission", "clean india mission"),
        "TB Mukt Bharat": ("tb mukt bharat",),
    },
    "Program": {
        "Antenatal Care": ("antenatal care", "anc"),
        "Family Planning": ("family planning",),
        "Immunization": ("immunization", "vaccination"),
        "Maternal Health": ("maternal health",),
        "Menstrual Health": ("menstrual health", "menstrual hygiene"),
        "Nutrition": ("nutrition",),
        "Postnatal Care": ("postnatal care", "pnc"),
        "Vector Control": ("vector control",),
    },
    "Campaign": {
        "Deworming": ("deworming",),
        "Mass Drug Administration": ("mass drug administration", "mda"),
        "Poshan Maah": ("poshan maah",),
        "TB Prevention": ("tb prevention",),
        "Vaccination Campaign": ("vaccination campaign",),
    },
    "Organization": {
        "Gram Panchayat": ("gram panchayat", "village panchayat"),
        "UNICEF": ("unicef",),
        "WHO": ("who", "w.h.o.", "world health organization"),
    },
    "Topic": {
        "Community Development": ("community development",),
        "Governance": ("governance",),
        "Media and Communication": ("media and communication", "communication"),
        "Public Health": ("public health",),
        "Water Conservation": ("water conservation", "water harvesting", "save water"),
        "Water Scarcity": ("water scarcity",),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    return re.sub(r"-+", "-", text).strip("-")


def canonical_tag(value: str) -> str:
    clean = re.sub(r"\s+", " ", (value or "").strip())
    if not clean:
        return ""
    key = re.sub(r"[^a-z0-9]+", " ", clean.lower()).strip()
    if key in CANONICAL_TAGS:
        return CANONICAL_TAGS[key]
    if clean.isupper() and len(clean) <= 5:
        return clean
    small_words = {"and", "for", "in", "of", "the", "to"}
    parts = clean.split()
    return " ".join(part.lower() if i and part.lower() in small_words else part.capitalize() for i, part in enumerate(parts))


def canonical_entity(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).strip(".,;:!?")


def normalized_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def phase3_entity_type(entity_type: str) -> str:
    return ENTITY_TYPE_DISPLAY.get(entity_type, "Topic")


def compatible_entity_type(phase3_type: str) -> str:
    return PHASE3_ENTITY_COMPATIBILITY.get(phase3_type, "TOPIC")


def resolve_phase3_alias(name: str, phase3_type: Optional[str] = None) -> Tuple[str, str, str]:
    """Return canonical name, compatible legacy type, and display type for known aliases."""
    key = normalized_alias(name)
    for display_type, canonical_terms in PHASE3_ENTITY_ALIASES.items():
        if phase3_type and phase3_type != display_type:
            continue
        for canonical_name, aliases in canonical_terms.items():
            alias_keys = {normalized_alias(canonical_name), *(normalized_alias(alias) for alias in aliases)}
            if key in alias_keys:
                return canonical_name, compatible_entity_type(display_type), display_type
    fallback_type = phase3_type if phase3_type in PHASE3_ENTITY_TYPES else "Topic"
    return canonical_entity(name), compatible_entity_type(fallback_type), fallback_type


def tokenise(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text or "")


def keyword_candidates(title: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> List[Tuple[str, float]]:
    metadata = metadata or {}
    weighted_text = " ".join([title or "", title or "", metadata.get("theme", ""), metadata.get("collection", ""), text or ""])
    tokens = [t.lower().strip("'") for t in tokenise(weighted_text)]
    tokens = [t for t in tokens if t not in STOPWORDS and not t.isdigit()]
    counts = Counter(tokens)
    bigrams = Counter(
        f"{tokens[i]} {tokens[i + 1]}"
        for i in range(len(tokens) - 1)
        if tokens[i] not in STOPWORDS and tokens[i + 1] not in STOPWORDS
    )
    scored: List[Tuple[str, float]] = []
    for token, count in counts.items():
        if count > 1 or token in TOPIC_HINTS:
            scored.append((token, min(0.95, 0.45 + count / 18)))
    for phrase, count in bigrams.items():
        if count > 1 or phrase in TOPIC_HINTS or any(term in phrase for term in TOPIC_HINTS):
            scored.append((phrase, min(0.98, 0.55 + count / 12)))
    return sorted(scored, key=lambda item: item[1], reverse=True)


def extract_tags(title: str, text: str, metadata: Optional[Dict[str, Any]] = None, *, limit: int = 15) -> List[Dict[str, Any]]:
    metadata = metadata or {}
    candidates: List[Tuple[str, float]] = []
    for field in ("theme", "collection", "source_type", "content_type", "language"):
        value = str(metadata.get(field) or "")
        for part in re.split(r"[,;|]", value):
            part = part.strip()
            if part:
                candidates.append((part, 0.9))
    candidates.extend(keyword_candidates(title, text, metadata))
    for entity in extract_entities(title, text)[:10]:
        if entity["entity_type"] in {"ORG", "GPE", "LOC", "TOPIC"}:
            candidates.append((entity["name"], min(0.9, entity["confidence"])))

    tags: Dict[str, Dict[str, Any]] = {}
    for raw, confidence in candidates:
        name = canonical_tag(raw)
        if not name or len(name) < 3:
            continue
        slug = slugify(name)
        if not slug or slug in STOPWORDS:
            continue
        existing = tags.get(slug)
        if not existing or confidence > existing["confidence"]:
            tags[slug] = {"name": name, "slug": slug, "confidence": round(float(confidence), 3)}
    return sorted(tags.values(), key=lambda item: item["confidence"], reverse=True)[:limit]


def _add_entity(
    found: Dict[Tuple[str, str], Dict[str, Any]],
    name: str,
    entity_type: str,
    confidence: float,
    *,
    phase3_type: Optional[str] = None,
) -> None:
    name = canonical_entity(name)
    if not name or entity_type not in ENTITY_TYPES or len(name) < 3:
        return
    display_type = phase3_type if phase3_type in PHASE3_ENTITY_TYPES else phase3_entity_type(entity_type)
    canonical_name, canonical_entity_type, canonical_display_type = resolve_phase3_alias(name, display_type)
    name = canonical_name
    entity_type = canonical_entity_type
    display_type = canonical_display_type
    key = (name.lower(), entity_type)
    existing = found.get(key)
    if existing:
        existing["confidence"] = max(existing["confidence"], confidence)
        existing["mentions"] += 1
        existing["phase3_type"] = display_type
    else:
        found[key] = {
            "name": name,
            "entity_type": entity_type,
            "phase3_type": display_type,
            "canonical_name": name,
            "confidence": confidence,
            "mentions": 1,
        }


def extract_entities(title: str, text: str, *, limit: int = 30) -> List[Dict[str, Any]]:
    sample = f"{title or ''}\n{text or ''}"[:40000]
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for name in GPE_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", sample, re.IGNORECASE):
            canonical = "United States" if name in {"USA", "US", "U.S.", "U.S.A."} else name
            _add_entity(found, canonical, "GPE", 0.88)

    org_suffix_re = "|".join(re.escape(suffix) for suffix in ORG_SUFFIXES)
    for match in re.finditer(rf"\b([A-Z][A-Za-z&.-]*(?:\s+[A-Z][A-Za-z&.-]*){{0,5}}\s+(?:{org_suffix_re}))\b", sample):
        _add_entity(found, match.group(1), "ORG", 0.82)

    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", sample):
        phrase = match.group(1)
        if phrase in GPE_NAMES or any(phrase.endswith(suffix) for suffix in ORG_SUFFIXES):
            continue
        if phrase.split()[0] in {"The", "This", "That", "CommonSource"}:
            continue
        _add_entity(found, phrase, "PERSON", 0.58)

    for match in re.finditer(r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:Election|Summit|Conference|Festival|War|Flood|Crisis|Protest))\b", sample):
        _add_entity(found, match.group(1), "EVENT", 0.8)
    lower_sample = sample.lower()
    for term in EVENT_TERMS:
        if term in lower_sample:
            _add_entity(found, term, "EVENT", 0.55)
    for topic in TOPIC_HINTS:
        if re.search(rf"\b{re.escape(topic)}\b", lower_sample):
            _add_entity(found, canonical_tag(topic), "TOPIC", 0.78, phase3_type="Topic")

    for display_type, canonical_terms in PHASE3_ENTITY_ALIASES.items():
        compatible_type = compatible_entity_type(display_type)
        for canonical_name, aliases in canonical_terms.items():
            mentions = 0
            for alias in aliases:
                pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
                mentions += len(re.findall(pattern, lower_sample, re.IGNORECASE))
            if mentions:
                confidence = min(0.96, 0.72 + mentions * 0.045)
                for _ in range(min(mentions, 4)):
                    _add_entity(
                        found,
                        canonical_name,
                        compatible_type,
                        confidence,
                        phase3_type=display_type,
                    )

    entities = list(found.values())
    for entity in entities:
        entity["confidence"] = round(min(0.99, entity["confidence"] + min(entity["mentions"], 5) * 0.025), 3)
    return sorted(entities, key=lambda item: (item["confidence"], item["mentions"]), reverse=True)[:limit]


def ensure_knowledge_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK (entity_type IN ('PERSON', 'ORG', 'GPE', 'LOC', 'EVENT', 'TOPIC')),
            canonical_name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
        CREATE TABLE IF NOT EXISTS article_entities (
            article_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            PRIMARY KEY(article_id, entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_article_entities_article ON article_entities(article_id);
        CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_id);
        CREATE TABLE IF NOT EXISTS document_entities (
            document_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            mentions INTEGER NOT NULL DEFAULT 1,
            extraction_method TEXT NOT NULL DEFAULT 'rule',
            created_at TEXT NOT NULL,
            PRIMARY KEY(document_id, entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_document_entities_document ON document_entities(document_id);
        CREATE INDEX IF NOT EXISTS idx_document_entities_entity ON document_entities(entity_id);
        CREATE INDEX IF NOT EXISTS idx_document_entities_type ON document_entities(entity_type);
        CREATE TABLE IF NOT EXISTS entity_relationships (
            source_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            PRIMARY KEY(source_entity_id, target_entity_id, relationship_type)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_relationships_source ON entity_relationships(source_entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_relationships_target ON entity_relationships(target_entity_id);
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        CREATE INDEX IF NOT EXISTS idx_tags_slug ON tags(slug);
        CREATE TABLE IF NOT EXISTS article_tags (
            article_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            PRIMARY KEY(article_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_article_tags_article ON article_tags(article_id);
        CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id);
        CREATE TABLE IF NOT EXISTS document_citations (
            document_id TEXT PRIMARY KEY,
            apa TEXT NOT NULL DEFAULT '',
            mla TEXT NOT NULL DEFAULT '',
            chicago TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id TEXT PRIMARY KEY,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            canonical_entity_id TEXT,
            confidence REAL NOT NULL DEFAULT 0.8,
            created_at TEXT NOT NULL,
            UNIQUE(normalized_alias, entity_type)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical ON entity_aliases(canonical_name, entity_type);
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(canonical_entity_id);
        """
    )
    seed_entity_aliases(conn)


def seed_entity_aliases(conn: sqlite3.Connection) -> None:
    now = utc_now()
    for display_type, canonical_terms in PHASE3_ENTITY_ALIASES.items():
        legacy_type = compatible_entity_type(display_type)
        for canonical_name, aliases in canonical_terms.items():
            for alias in (canonical_name, *aliases):
                normalized = normalized_alias(alias)
                if not normalized:
                    continue
                conn.execute(
                    """
                    INSERT INTO entity_aliases
                      (id, alias, normalized_alias, canonical_name, entity_type, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(normalized_alias, entity_type)
                    DO UPDATE SET
                      canonical_name = excluded.canonical_name,
                      confidence = CASE
                          WHEN excluded.confidence > entity_aliases.confidence THEN excluded.confidence
                          ELSE entity_aliases.confidence
                      END
                    """,
                    (make_id("alias"), alias, normalized, canonical_name, legacy_type, 0.92, now),
                )


def _upsert_entity(conn: sqlite3.Connection, entity: Dict[str, Any]) -> str:
    canonical_name, legacy_type, display_type = resolve_phase3_alias(
        entity["canonical_name"],
        entity.get("phase3_type") or phase3_entity_type(entity["entity_type"]),
    )
    entity["name"] = canonical_name
    entity["canonical_name"] = canonical_name
    entity["entity_type"] = legacy_type
    entity["phase3_type"] = display_type
    existing = conn.execute(
        "SELECT id FROM entities WHERE canonical_name = ? AND entity_type = ?",
        (entity["canonical_name"], entity["entity_type"]),
    ).fetchone()
    if existing:
        entity_id = str(existing["id"])
    else:
        entity_id = make_id("ent")
        conn.execute(
            """
            INSERT INTO entities (id, name, entity_type, canonical_name, description, created_at)
            VALUES (?, ?, ?, ?, '', ?)
            """,
            (entity_id, entity["name"], entity["entity_type"], entity["canonical_name"], utc_now()),
        )
    conn.execute(
        """
        UPDATE entity_aliases
        SET canonical_entity_id = ?
        WHERE canonical_name = ? AND entity_type = ?
        """,
        (entity_id, entity["canonical_name"], entity["entity_type"]),
    )
    conn.execute(
        """
        INSERT INTO entity_aliases
          (id, alias, normalized_alias, canonical_name, entity_type, canonical_entity_id, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_alias, entity_type)
        DO UPDATE SET
          canonical_name = excluded.canonical_name,
          canonical_entity_id = excluded.canonical_entity_id,
          confidence = CASE
              WHEN excluded.confidence > entity_aliases.confidence THEN excluded.confidence
              ELSE entity_aliases.confidence
          END
        """,
        (
            make_id("alias"),
            entity["name"],
            normalized_alias(entity["name"]),
            entity["canonical_name"],
            entity["entity_type"],
            entity_id,
            float(entity.get("confidence") or 0.8),
            utc_now(),
        ),
    )
    return entity_id


def _upsert_tag(conn: sqlite3.Connection, tag: Dict[str, Any]) -> str:
    existing = conn.execute("SELECT id FROM tags WHERE slug = ?", (tag["slug"],)).fetchone()
    if existing:
        return str(existing["id"])
    tag_id = make_id("tag")
    conn.execute(
        "INSERT INTO tags (id, name, slug, description, created_at) VALUES (?, ?, ?, '', ?)",
        (tag_id, tag["name"], tag["slug"], utc_now()),
    )
    return tag_id


def _relationship_candidates(entity_rows: Iterable[Dict[str, Any]]) -> List[Tuple[str, str, str, float]]:
    entities = list(entity_rows)
    relationships: List[Tuple[str, str, str, float]] = []
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for entity in entities:
        by_type.setdefault(entity["entity_type"], []).append(entity)
    for person in by_type.get("PERSON", [])[:5]:
        for org in by_type.get("ORG", [])[:4]:
            relationships.append((person["id"], org["id"], "affiliated_with", 0.6))
    for org in by_type.get("ORG", [])[:5]:
        for place in (by_type.get("GPE", []) + by_type.get("LOC", []))[:4]:
            relationships.append((org["id"], place["id"], "located_in", 0.55))
    for event in by_type.get("EVENT", [])[:5]:
        for place in (by_type.get("GPE", []) + by_type.get("LOC", []))[:4]:
            relationships.append((event["id"], place["id"], "occurs_in", 0.6))
    topics = by_type.get("TOPIC", [])[:6]
    for i, source in enumerate(topics):
        for target in topics[i + 1:]:
            relationships.append((source["id"], target["id"], "related_topic", 0.5))
            relationships.append((target["id"], source["id"], "related_topic", 0.5))
    return relationships


def process_article_knowledge(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    title: str,
    text: str,
    publication: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_knowledge_tables(conn)
    metadata = dict(metadata or {})
    if publication:
        metadata.setdefault("publication", publication)

    entities = extract_entities(title, text)
    tags = extract_tags(title, text, metadata, limit=15)
    conn.execute("DELETE FROM article_entities WHERE article_id = ?", (article_id,))
    conn.execute("DELETE FROM document_entities WHERE document_id = ?", (article_id,))
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))

    stored_entities: List[Dict[str, Any]] = []
    for entity in entities:
        entity_id = _upsert_entity(conn, entity)
        conn.execute(
            """
            INSERT OR REPLACE INTO article_entities (article_id, entity_id, confidence)
            VALUES (?, ?, ?)
            """,
            (article_id, entity_id, entity["confidence"]),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO document_entities
              (document_id, entity_id, entity_type, confidence, mentions, extraction_method, created_at)
            VALUES (?, ?, ?, ?, ?, 'rule', ?)
            """,
            (
                article_id,
                entity_id,
                entity.get("phase3_type") or phase3_entity_type(entity["entity_type"]),
                entity["confidence"],
                int(entity.get("mentions") or 1),
                utc_now(),
            ),
        )
        stored_entities.append({**entity, "id": entity_id})

    stored_tags: List[Dict[str, Any]] = []
    for tag in tags:
        tag_id = _upsert_tag(conn, tag)
        conn.execute(
            """
            INSERT OR REPLACE INTO article_tags (article_id, tag_id, confidence)
            VALUES (?, ?, ?)
            """,
            (article_id, tag_id, tag["confidence"]),
        )
        stored_tags.append({**tag, "id": tag_id})

    relationship_count = 0
    for source_id, target_id, rel_type, weight in _relationship_candidates(stored_entities):
        if source_id == target_id:
            continue
        conn.execute(
            """
            INSERT INTO entity_relationships
              (source_entity_id, target_entity_id, relationship_type, weight)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_entity_id, target_entity_id, relationship_type)
            DO UPDATE SET weight = CASE
                WHEN excluded.weight > entity_relationships.weight THEN excluded.weight
                ELSE entity_relationships.weight
            END
            """,
            (source_id, target_id, rel_type, weight),
        )
        relationship_count += 1

    return {
        "article_id": article_id,
        "entities": stored_entities,
        "tags": stored_tags,
        "entity_count": len(stored_entities),
        "tag_count": len(stored_tags),
        "relationship_count": relationship_count,
    }
