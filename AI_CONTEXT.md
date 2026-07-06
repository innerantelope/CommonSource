# CommonSource AI Context

## Project Overview

CommonSource is an AI-powered community media archive search and evidence retrieval system.

Purpose:
- Search local journalism archives
- Surface evidence from multiple source types
- Generate grounded answers
- Preserve institutional knowledge
- Support community organizations and NGOs

---

## Tech Stack

Backend:
- Python
- Flask
- SQLite
- Ollama
- Qwen

Frontend:
- HTML
- CSS
- Vanilla JavaScript

Retrieval:
- Qdrant
- MiniLM Embeddings
- Hybrid Search

Corpus:
- ~3,116 Articles
- ~49,627 Chunks

---

## Folder Structure

Project/
├── app/
│   ├── search_api.py
│   ├── knowledge_db.py
│   ├── embed.py
│   ├── retrieval/
│   ├── rag/
│   ├── api/
│   ├── ingestion/
│   └── utils/
│
├── frontend/
│   ├── index.html
│   ├── landing.html
│   ├── join.html
│   └── governance.html
│
├── data/
│   └── database/
│       └── commonsource.db

---

## Current Retrieval Flow

User Query
→ Embedding Generation
→ Qdrant ANN Search
→ SQLite Metadata Hydration
→ Hybrid Scoring
→ Context Assembly
→ Qwen Generation
→ Frontend Display

---

## Current APIs

GET /api/search
GET /api/ask
GET /api/ask/layered
GET /api/timeline
GET /api/arc
POST /api/translate
POST /api/generate

---

## Current Migration Status

Completed:
- embed.py
- Qdrant Integration
- Retrieval Pipeline
- ANN Search
- Search API Wiring

In Progress:
- Blueprint Migration
- Route Modularization

Needs Work:
- Publisher APIs
- RSS Pipeline
- World Model APIs
- Search Debugging
- Qwen Reliability

---

## Important Constraints

- Do NOT redesign frontend.
- Preserve existing API contracts.
- Preserve index.html compatibility.
- Focus on backend improvements.
- Focus on retrieval quality.
- Focus on debugging before adding features.

---

## Current Problems

1. Articles sometimes not returned correctly.
2. Qwen generation reliability issues.
3. Retrieval debugging needed.
4. Need better observability.
5. search_api.py still partially monolithic.

---

## Key Files

search_api.py
retrieval/pipeline.py
retrieval/qdrant_store.py
retrieval/scoring.py
knowledge_db.py
embed.py

These files are the highest priority for debugging and improvements.