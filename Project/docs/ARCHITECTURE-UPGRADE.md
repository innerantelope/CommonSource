# CommonSource Backend Upgrade — Architecture & Status

## 1. Refactored Folder Structure

```
Project/app/
├── api/                    # Flask blueprints (phase-in; pages ready)
│   ├── pages.py
│   ├── search_routes.py    # /api/search (optional cutover)
│   ├── corpus.py           # (next)
│   └── generation_routes.py
├── core/
│   ├── config.py           # env-driven settings
│   └── app_factory.py      # create_app() for future entry
├── embeddings/             # (namespace; canonical: ../embed.py)
├── retrieval/
│   ├── pipeline.py         # PRIMARY: retrieve_sources()
│   ├── qdrant_store.py     # ANN + payloads
│   ├── sqlite_retriever.py # capped SQL fallback
│   ├── scoring.py          # hybrid re-score
│   └── keyword.py
├── rag/
│   ├── ollama_client.py    # Qwen / Ollama
│   └── context.py          # build_context()
├── ingestion/
│   └── shared.py           # embed + chunk_text for pipelines
├── services/               # (expand: layered, publisher)
├── models/                 # (reserved: pydantic DTOs)
├── utils/
│   ├── db.py
│   └── vectors.py
├── embed.py                # ★ embedding service
├── search_api.py           # legacy monolith (routes + RAG); search → pipeline
└── Ingestion/              # existing CLI scripts (unchanged paths)
```

## 2. Migration Plan

See **`MIGRATION-QDRANT.md`** for step-by-step commands.

| Phase | Action | Risk |
|-------|--------|------|
| 0 | `pip install -r app/requirements.txt` | Low |
| 1 | `embed.py` live; ingestion uses `from embed import ...` | Low |
| 2 | `docker compose up -d qdrant` + `sync_qdrant.py` | Medium (re-embed time) |
| 3 | `COMMONSOURCE_USE_QDRANT=true`; restart API | Low |
| 4 | Move remaining routes to blueprints | Low (incremental) |

## 3. Qdrant Integration

- **Docker:** `Project/docker-compose.yml`
- **Collection:** `commonsource_chunks` (384-dim cosine)
- **Payload fields:** `chunk_id`, `asset_id`, `article_id`, `chunk_index`, `chunk_text`, `source`, `source_type`, `evidence_layer`, `timestamp`, `source_filename`, `title`, + metadata
- **Sync:** `python Project/scripts/sync_qdrant.py --recreate`

## 4. embed.py

| Feature | Implementation |
|---------|----------------|
| Model default | `sentence-transformers/all-MiniLM-L6-v2` |
| Cache | `Project/data/cache/embeddings/*.json` |
| Batch | `embed_batch(texts, batch_size=32)` |
| Ingestion API | `generate_embedding(..., method=local\|ollama\|none)` |
| Blobs | `embedding_to_blob` / `blob_to_embedding` in `utils/vectors.py` |

## 5. Flask Architecture

- **Today:** `search_api.py` remains the process entry; **`GET /api/search`** delegates to `retrieval.pipeline.retrieve_sources()`.
- **Next:** `python -c "from core.app_factory import create_app; create_app().run(...)"` after moving routes off monolith.
- **Config:** `core/config.py` — `QDRANT_URL`, `COMMONSOURCE_USE_QDRANT`, `COMMONSOURCE_EMBED_MODEL`, etc.

## 6. Retrieval Pipeline

```
User Query
  → embed_query()                    [embed.py]
  → Qdrant ANN (top N)                [qdrant_store.ann_search]
  → hydrate rows from SQLite          [utils.db.hydrate_chunks_by_ids]
  → hybrid score (ANN + lexical)      [scoring.score_row]
  → select_diverse_results            [scoring]
  → build_source_result               [utils.db]
  → JSON {query, count, results}
```

**Fallback:** If Qdrant down or `COMMONSOURCE_USE_QDRANT=false` → `sqlite_retriever.fetch_candidate_rows` (LIMIT 2500).

## 7. API Compatibility

- **Unchanged:** URL paths, query params, response keys for `results[]` cards.
- **Added (optional):** `retrieval_backend` in internal dict (stripped from `/api/search` JSON for strict compat).
- **Frontend:** No changes required to `index.html`.

## 8. Step-by-Step Migration

1. Install deps + start Qdrant  
2. Run `sync_qdrant.py` (off-peak; CPU-bound)  
3. Set env vars; restart `python search_api.py`  
4. Verify: `GET /api/search?q=hindi&k=8`  
5. Verify layered view still works (uses legacy path in monolith)  
6. Optionally set `COMMONSOURCE_EMBED_MODEL` to multilingual model until re-index complete  

## 9. Minimal Breaking Changes

- Search logic path changed internally; scores may shift slightly with Qdrant ANN ordering.
- Default embed model name changed in config — **override env** if not re-syncing Qdrant.
- Publisher routes (`/api/publisher/*`) still depend on incomplete `ingest_rss` helpers — not modified.

## 10. Scalability Recommendations

1. **Qdrant cluster** for HA when moving off laptop demos  
2. **Dedicated sync worker** after each ingest batch  
3. **gunicorn** + 2 workers (watch Ollama lock)  
4. **Cross-encoder rerank** on top-50 ANN hits  
5. **Separate read replica** or export to Postgres if metadata queries grow  
6. **FTS5** in SQLite for keyword prefilter before ANN  
7. **Async job queue** for `/api/ask/layered` (60s+ Qwen calls)  

---

## Completed vs Placeholder vs Needs Work

| Area | Status |
|------|--------|
| embed.py | **Completed** |
| Qdrant client + sync script | **Completed** |
| Retrieval pipeline | **Completed** |
| /api/search wired | **Completed** |
| Modular blueprints (all routes) | **In progress** — pages blueprint ready |
| rag/synthesis split from monolith | **Placeholder** — still in search_api.py |
| Publisher/RSS API | **Needs implementation** |
| search.py CLI | **Needs embed.py path** (fixed via app/embed.py) |
