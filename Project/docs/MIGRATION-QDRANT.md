# CommonSource Backend Upgrade — Migration Plan

## Goals

- Modular Flask backend (no frontend changes)
- Shared `embed.py` for ingestion + API
- Qdrant ANN retrieval with SQLite fallback
- Preserve all existing `/api/*` contracts

## Phase 0 — Prerequisites

```powershell
cd Project
pip install -r app/requirements.txt
docker compose up -d qdrant
```

## Phase 1 — Embeddings service (done)

- `app/embed.py` — local MiniLM, disk cache, batch API
- Ingestion imports: `from embed import generate_embedding, embedding_to_blob`
- API uses: `from embed import embed_query`

**Note:** Existing SQLite blobs may use `paraphrase-multilingual-MiniLM-L12-v2`. Set before re-embed:

```powershell
$env:COMMONSOURCE_EMBED_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
```

For new Qdrant index with `all-MiniLM-L6-v2` (default), run full sync without legacy override.

## Phase 2 — Qdrant sync

```powershell
cd Project/app
python ../scripts/sync_qdrant.py --recreate
# Full corpus (~50k chunks, 30–90 min depending on CPU):
python ../scripts/sync_qdrant.py --batch-size 64
```

Enable ANN in API:

```powershell
$env:COMMONSOURCE_USE_QDRANT="true"
$env:QDRANT_URL="http://localhost:6333"
```

## Phase 3 — Run modular API

```powershell
cd Project/app
$env:PYTHONIOENCODING="utf-8"
python search_api.py
```

Search responses include optional `retrieval_backend`: `qdrant+sqlite_hydrate` or `sqlite`.

## Phase 4 — Gradual route extraction

Blueprints under `app/api/` register the same URLs. `search_api.py` remains the entrypoint.

## API compatibility

| Endpoint | Contract | Change |
|----------|----------|--------|
| GET /api/search | `{query, count, results}` | +optional `retrieval_backend` |
| GET /api/ask/layered | unchanged | Uses shared retrieval |
| POST /api/translate | unchanged | |
| POST /api/generate | unchanged | |

## Rollback

```powershell
$env:COMMONSOURCE_USE_QDRANT="false"
```

Restart Flask — falls back to capped SQLite hybrid search.

## Scalability recommendations

1. **HNSW in Qdrant** — default; tune `ef_construct` for bulk ingest
2. **Separate embed worker** — batch re-index off API process
3. **gunicorn** — `gunicorn -w 2 -b 0.0.0.0:5050 'core.app_factory:create_app()'`
4. **Reranker** — cross-encoder on top-50 ANN hits (optional phase 5)
5. **Job queue** — Celery/RQ for `/api/ask/layered` Ollama calls
