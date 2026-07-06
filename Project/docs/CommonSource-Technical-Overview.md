# CommonSource / Project_D — Technical Overview

> **Purpose:** Architecture reference, implementation status, and reusable context for future development (including AI-assisted work).  
> **Workspace root:** `Project_D`  
> **Application root:** `Project/`  
> **Primary runtime:** Flask API on port **5050** + static HTML frontends

---

## 1. Current Folder Structure

```
Project_D/
├── Project/
│   ├── app/                          # Python backend (core)
│   │   ├── search_api.py             # Main Flask app: APIs + static hosting (~1,850 lines)
│   │   ├── knowledge_db.py           # SQLite schema + persistence helpers
│   │   ├── domains.py                # GAT domain-pack taxonomy (keyword classification)
│   │   ├── source_classifier.py      # Evidence-layer / content-type / family tagging
│   │   ├── search.py                 # CLI semantic search (depends on missing embed.py)
│   │   ├── requirements.txt
│   │   └── Ingestion/                # Offline batch pipelines (CLI scripts)
│   │       ├── ingest_commonsource.py
│   │       ├── ingest_rss.py
│   │       ├── ingest_pdf.py
│   │       ├── ingest_audio.py
│   │       ├── extract_hardnews_meta.py
│   │       ├── build_archive_metadata.py
│   │       ├── backfill_source_profiles.py
│   │       ├── extract_worldmodel.py
│   │       └── inspect_archive_quality.py
│   ├── frontend/
│   │   ├── index.html                # Main search + RAG UI (~2,250 lines)
│   │   ├── landing.html
│   │   ├── join.html
│   │   └── governance.html
│   ├── data/
│   │   ├── database/commonsource.db
│   │   ├── metadata/
│   │   └── feeds/
│   └── scripts/
│       ├── setup_ollama.ps1
│       └── setup_ollama.sh
```

**Notable absences:** No `embed.py` in the repo (ingestion scripts import it). No package.json, Docker, or CI config in tree.

---

## 2. Frontend Pages & Components

No component framework (no React/Vue). Each page is a single HTML file with embedded CSS and vanilla JavaScript.

| Page | Route | Role |
|------|-------|------|
| landing.html | / | Marketing |
| index.html | /search | Primary app: search, RAG, timeline, script writer |
| join.html | /join | Publisher application (client-only mock submit) |
| governance.html | /governance | Static governance copy |

### index.html functional areas

| Area | Key JS |
|------|--------|
| Articles tab | doSearch(), renderResult(), loadEvidencePanels() |
| Script Writer | startScript(), sendReply() |
| Timeline | renderTimeline() |
| Evidence layers | renderLayeredAnswer() |
| Story arc | loadArc() |
| Translation | translateVisibleResults() |
| i18n EN/HI | setLang(), t() |

---

## 3. Backend Modules

| Module | Responsibility | Maturity |
|--------|----------------|----------|
| search_api.py | Flask: search, RAG, translation, static routes | Core — active |
| knowledge_db.py | SQLite schema + helpers | Complete schema; partial runtime use |
| source_classifier.py | Evidence-layer tagging | Used in ingestion |
| domains.py | GAT domain packs + keyword classify | Not wired to search UI |
| search.py | CLI semantic search | Broken without embed.py |
| Ingestion/* | Batch ingest pipelines | CLI-only; built current DB |

---

## 4. Current APIs

Base URL: `http://localhost:5050`

### Static pages

GET /, /search, /join, /governance

### Search & RAG

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/search | Hybrid vector + keyword search |
| GET | /api/ask | Single-pass RAG + entities |
| GET | /api/ask/layered | Five evidence layers + gaps |
| GET | /api/arc | Story arc narrative |
| GET | /api/timeline | Year histogram |
| POST | /api/translate | Qwen translation |
| POST | /api/generate | Script Writer completion |

### Corpus & assets

GET /api/stats, /api/articles, /api/source/<id>, /api/models

### Publisher / ingest (weak integration)

POST /api/publisher/register, /api/feed/add, /api/feeds/poll, /api/ingest/upload  
GET /api/publishers

**Caveat:** ingest_rss helpers (poll_all_feeds, init_publisher_tables) may be incomplete; imports expect top-level ingest_rss module.

---

## 5. Technologies Used

| Layer | Stack |
|-------|--------|
| Backend | Python 3.10+, Flask 3.x, flask-cors |
| Database | SQLite (commonsource.db) |
| Embeddings | sentence-transformers MiniLM; optional Ollama nomic-embed-text |
| LLM | Ollama Qwen (qwen2.5:1.5b preferred) |
| Ingestion | python-docx, pypdf, feedparser, pydantic |
| Frontend | HTML5, CSS, vanilla JS |
| Server | Flask dev server (threaded) |

---

## 6. State Management

**Frontend:** Module-level `let` variables (currentQuery, lastSearchResults, swHistory, evidenceAbort). DOM is UI state. No Redux/localStorage.

**Backend:** SQLite durable state; in-memory embedding model + Ollama lock. No auth.

---

## 7. Existing Features

- ~3,116 articles, ~49,627 chunks (per /api/stats)
- Hybrid search with source cards
- Timeline, layered evidence, story arc, translation, Script Writer
- Hindi/English partial i18n
- Offline ingestion already populated DB
- Static landing, join, governance pages

---

## 8. Missing Backend Functionality

- embed.py module (referenced, missing)
- Join form not POSTing to API
- RSS auto-ingest / poll_all_feeds incomplete
- No auth, admin UI, job queue, production WSGI
- World-model tables not exposed via API
- /api/articles no frontend
- No automated tests

---

## 9. Missing AI / RAG Functionality

- No vector ANN index (brute-force over capped SQL rows)
- No reranker, streaming, or RAG eval logging
- World-model extractions not used at query time
- Domain-filtered search not in API/UI
- Official evidence layer sparse in corpus

---

## 10. Suggested Architecture Improvements

**Short term:** Restore embed.py; fix ingest imports; decouple slow layered load; gunicorn; wire join form.

**Medium term:** FTS5/sqlite-vec; background jobs for LLM; admin app.

**Long term:** Unify GAT world-models with search; modularize frontend; publisher dashboards + auth.

---

## 11. Current Project Flow

**Search happy path:** User → index.html → /api/search (embed + SQL + score) → render cards → /api/timeline → /api/ask/layered (Qwen).

**Corpus build:** Archives → metadata CSV → ingest_commonsource → commonsource.db → optional extract_worldmodel.

**Script Writer:** /api/search (top 3) → /api/generate per turn.

---

## 12. Frontend ↔ Backend Integration

| API | index.html | Status |
|-----|------------|--------|
| /api/search | Yes | Integrated |
| /api/timeline | Yes | Integrated |
| /api/ask/layered | Yes | Integrated (slow) |
| /api/arc | Yes | On demand |
| /api/translate | Yes | Integrated |
| /api/generate | Yes | Script Writer |
| /api/stats, /api/models | Yes | Integrated |
| /api/source | Via archive_url | Integrated |
| /api/ask, /api/articles | No | Unused |
| Publisher/upload APIs | No | Not wired |
| join.html form | No | Mock only |

**Dependencies:** Flask on :5050; Ollama with qwen2.5:1.5b for Qwen features.

---

## Implementation Status

### Completed

SQLite corpus + search UI; layered Qwen; translation + Script Writer; static marketing pages; batch ingestion (historical).

### Placeholders

join.html mock submit; publisher APIs incomplete; activateLayer() empty; GAT world-model tables unused online; search.py CLI broken.

### Needs implementation

P0: embed.py + ingest paths. P1: join form + upload UI. P2: admin approval; world-model API; domain filters.

---

## Reusable AI Context Block

```
Project: CommonSource (CMA) — community media archive search + RAG demo.
Stack: Flask search_api.py + static HTML on :5050.
DB: Project/data/database/commonsource.db (~3k articles, ~50k chunks).
Search: hybrid MiniLM + keyword; SQL LIMIT 2500 candidates.
LLM: Ollama Qwen (qwen2.5:1.5b); /api/ask/layered, /api/translate, /api/generate.
Main UI: Project/frontend/index.html at /search.
Ingestion: Project/app/Ingestion/*.py; embed.py missing.
No React, auth, or production deploy assumed.
```

---

## System Design (Concise)

**Style:** Modular monolith — Flask owns HTTP, retrieval, ranking, Ollama.

**Data model:**

```
knowledge_assets ──< knowledge_chunks (embedding_blob)
        └── commonsource_articles (provenance)
        └── knowledge_extractions (offline LLM JSON)
```

**Evidence layers:** news, development, community, official — via source_classifier.

**Deployment today:** Local Flask + Ollama + browser on localhost:5050.

---

*Generated architecture overview for Project_D / CommonSource.*
