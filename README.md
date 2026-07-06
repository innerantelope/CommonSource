# CommonSource (Project_D)

CommonSource is a community-focused, AI-powered media archive search and evidence retrieval system designed to empower local organizations, journalists, and non-governmental organizations (NGOs). 

It acts as a digital custodian for community archives, indexing local news feeds, PDFs, audio transcripts, and word documents. CommonSource enables users to search historical archives using hybrid semantic/lexical matching, synthesize evidence from multiple viewpoints, trace long-term story progression, translate content across languages (English and Hindi), and author grounded reports and scripts.

---

## 📖 Table of Contents

- [Core Capabilities](#-core-capabilities)
- [System Architecture & Tech Stack](#-system-architecture--tech-stack)
- [Database Schema & Models](#-database-schema--models)
- [Ingestion & Search Retrieval Flow](#-ingestion--search-retrieval-flow)
- [Configuration & Environment Variables](#-configuration--environment-variables)
- [Quick Start Guide](#-quick-start-guide)
- [Project Layout](#-project-layout)
- [Detailed Documentation Files](#-detailed-documentation-files)

---

## 🚀 Core Capabilities

1. **Hybrid Semantic & Lexical Search**  
   Combines semantic embeddings (384-dimensional vectors generated via MiniLM local models) with traditional BM25-like lexical keyword search over SQLite records. This yields accurate matches even when query terminology differs from the source text.
   
2. **PageRank-Boosted Relevance**  
   Extracts parenthetical and text-based citations between documents, building a citation graph using NetworkX. Highly cited documents are pre-calculated offline and assigned a PageRank value, providing up to a 10% search score boost.

3. **Multi-Perspective Evidence Layering**  
   Automatically categorizes search results into five distinct evidence categories: *News/Media*, *Development/Governance*, *Community Views*, *Academic Research*, and *Official PR*. The LLM synthesizes answers from each category to highlight coverage gaps and distinct viewpoints.

4. **Interactive Script Writer**  
   Provides an interactive AI environment enabling scriptwriters, podcasters, and journalists to collaborate with the LLM in drafting radio or podcast scripts. The generated output is strictly grounded in and cited back to the indexed articles.

5. **Chronological Timeline & Story Arcs**  
   Generates interactive yearly histograms mapping when topics peaked and employs LLMs to outline the narrative progression and historical development of stories over multi-year periods.

6. **Localized English/Hindi Translation**  
   Integrates English-to-Hindi and Hindi-to-English translation capabilities (powered by `deep-translator` or local LLMs) for search results, metadata, and synthesized answers, ensuring accessibility for regional leaders.

7. **Secure Authentication & Roles**  
   A full-featured authentication layer supporting roles: `super_admin`, `admin`, `publisher`, `reviewer`, and `reader` with JWT token rotation, CSRF verification, and brute-force lockout policies.

---

## 🛠️ System Architecture & Tech Stack

```
                     ┌──────────────────┐
                     │   Web Browser    │
                     │  (HTML, CSS, JS) │
                     └────────┬─────────┘
                              │ HTTP APIs / Static Files
                              ▼
                     ┌──────────────────┐
                     │    Flask App     │
                     │ (search_api.py)  │
                     └────┬─────────┬───┘
                          │         │
            Embed / RAG   ▼         ▼ Metadata Hydration
         ┌──────────────────┐     ┌──────────────────┐
         │ Qdrant Vector DB │     │ SQLite Database  │
         │  & Ollama/Gemini │     │(commonsource.db) │
         └──────────────────┘     └──────────────────┘
```

- **Frontend**: Clean, responsive static HTML pages (`landing.html`, `index.html`, `join.html`, `governance.html`) utilizing Vanilla CSS and JavaScript. No compilation is required.
- **Application Server**: Python Flask backend server running on port `5050` with CORS support.
- **Relational Database**: SQLite (`commonsource.db`) containing text chunks, structured metadata, domain pack linkages, user details, and PageRank scores.
- **Vector Database**: Qdrant running in Docker or Qdrant Cloud for semantic vector search, utilizing the `commonsource_chunks` collection (384 dimensions, Cosine similarity).
- **Embeddings Model**: Local `sentence-transformers/all-MiniLM-L6-v2` or `paraphrase-multilingual-MiniLM-L12-v2`.
- **LLM Synthesis Providers**: Supports Gemini API (`gemini-2.5-flash` primary) or local models via Ollama (`gemma3:4b`, `qwen2.5:1.5b`).

---

## 🗄️ Database Schema & Models

CommonSource uses SQLite to maintain relational links. The main tables defined in `Project/app/knowledge_db.py` include:

*   `knowledge_assets`: Root table representing any raw file, RSS feed item, or article.
*   `commonsource_articles`: Stores article metadata (e.g., publisher, author, URL, publish date, source type, family, and evidence layer).
*   `knowledge_chunks`: Stores text chunks and legacy binary vector embeddings.
*   `pagerank_scores`: Houses normalized PageRank scores for each asset.
*   `domain_classifications` & `domain_pack_links`: Links assets to taxonomy domains (e.g., Water, Health, Climate).
*   `knowledge_extractions`: Extracted entities, actors, constraints, and JSON-based world-model components.
*   `causal_network` & `approved_world_models`: Houses systems-dynamics linkages (e.g., Event A increases Stressor B) mapping out causal relations found in local reporting.

---

## 🔄 Ingestion & Search Retrieval Flow

### Ingestion Flow
1. **Extract**: Text is extracted from raw documents (`.pdf`, `.docx`), feed feeds (`RSS`), or transcribed audio files.
2. **Chunk & Classify**: Text is divided into semantic paragraphs. The source is classified using `source_classifier.py` and categorized into an evidence layer.
3. **Embed**: Chunks are embedded into a 384-dimensional vector space using `sentence-transformers` and cached.
4. **Load**: Chunks and metadata are loaded into the SQLite database and synchronized to the Qdrant `commonsource_chunks` collection.
5. **Graph**: The citation graph is calculated offline using citation pattern extraction and NetworkX, updating the `pagerank_scores` table.

### Retrieval Flow
1. **Query Embed**: The query is converted into a 384-dimension vector via `embed.py`.
2. **ANN Vector Search**: Qdrant matches the vector against the chunk collection. *Fallback*: If Qdrant is disabled/offline, the system performs a capped hybrid search query directly in SQLite.
3. **Hydration**: Chunks returned from vector matches are hydrated with SQL metadata (author, publisher, dates, source type).
4. **Hybrid Scoring**: Results are scored combined: `0.45 * Semantic Similarity + 0.45 * Lexical Score + 1.10 * PageRank Boost`.
5. **RAG Context Synthesis**: Matches are compiled into prompt contexts and dispatched to Gemini or Ollama to generate grounded responses.

---

## ⚙️ Configuration & Environment Variables

Configure these variables in `Project/.env` or as environment variables:

| Variable | Default Value | Description |
|---|---|---|
| `COMMONSOURCE_PORT` | `5050` | Port for the Flask application. |
| `COMMONSOURCE_USE_QDRANT` | `false` | Set to `true` to use Qdrant for semantic search. |
| `QDRANT_URL` | `http://localhost:6333` | Connection URL for Qdrant database. |
| `COMMONSOURCE_LLM_PROVIDER` | `ollama` | LLM service provider (`ollama`, `gemini`, `groq`, `openrouter`, or `auto`). |
| `COMMONSOURCE_LLM_MODEL` | `gemini-2.5-flash` | Model identifier used for synthesis and generation. |
| `COMMONSOURCE_JWT_SECRET` | *(Auto-generated)* | 64+ character secret key for signing JWTs. |
| `COMMONSOURCE_REQUIRE_JWT_SECRET`| `0` | If `1`, forces strict secret enforcement (Production mode). |

---

## ⚡ Quick Start Guide

### 1. Prerequisites
Make sure you have Python 3.10+, Docker, and the required dependencies installed:
```powershell
pip install -r Project/app/requirements.txt
```

### 2. Run Database & Backend
Ensure SQLite is initialized. To start the application server:
```powershell
cd Project/app
python search_api.py
```
Open your browser at `http://localhost:5050/` to access the search portal.

### 3. Sync Qdrant (Optional)
If running Qdrant in Docker (`docker compose up -d qdrant`), synchronize the vector index:
```powershell
cd Project
python scripts/sync_qdrant.py --recreate
```

### 4. Recalculate PageRank Scores
Run the network analysis script to re-calculate citation graph scores after modifying documents:
```powershell
cd Project
python scripts/compute_pagerank.py
```

---

## 📁 Project Layout

- `Project/app/`: Backend core containing Flask blueprints, database definitions, embedding services, LLM utilities, and ingestion logic.
  - [search_api.py](file:///c:/Users/Ayush/Documents/Project_D/Project/app/search_api.py): Main entrypoint and monolithic server.
  - [knowledge_db.py](file:///c:/Users/Ayush/Documents/Project_D/Project/app/knowledge_db.py): SQLite schemas and database handlers.
  - [embed.py](file:///c:/Users/Ayush/Documents/Project_D/Project/app/embed.py): Embedding generation using MiniLM.
  - [retrieval/](file:///c:/Users/Ayush/Documents/Project_D/Project/app/retrieval/): Retrieval pipeline, Qdrant store client, and scoring/PageRank modules.
- `Project/frontend/`: Static user interface assets.
  - [index.html](file:///c:/Users/Ayush/Documents/Project_D/Project/frontend/index.html): Search, timeline, layering, and script writer interface.
  - [landing.html](file:///c:/Users/Ayush/Documents/Project_D/Project/frontend/landing.html): Introduction and onboarding interface.
- `Project/scripts/`: Offline processing and system management utilities.
  - [sync_qdrant.py](file:///c:/Users/Ayush/Documents/Project_D/Project/scripts/sync_qdrant.py): Coordinates SQLite to Qdrant vector synchronizations.
  - [compute_pagerank.py](file:///c:/Users/Ayush/Documents/Project_D/Project/scripts/compute_pagerank.py): Network graph analysis and ranking pipeline calculation.

---

## 📄 Detailed Documentation Files

Check out the following files in the `Project/docs` directory for granular guides:
- [CommonSource Technical Overview](file:///c:/Users/Ayush/Documents/Project_D/Project/docs/CommonSource-Technical-Overview.md): Project architecture overview and technical outline.
- [Architecture Upgrade Plan](file:///c:/Users/Ayush/Documents/Project_D/Project/docs/ARCHITECTURE-UPGRADE.md): Details on blueprints, modularization, and service decapsulations.
- [PageRank Integration Guide](file:///c:/Users/Ayush/Documents/Project_D/Project/docs/PAGERANK-IMPLEMENTATION.md): Guide to NetworkX-based citation graph analysis and rank boosting.
- [Qdrant Migration Guide](file:///c:/Users/Ayush/Documents/Project_D/Project/docs/MIGRATION-QDRANT.md): Setup, collection properties, and sync metrics.
- [Free Demo Deployment Guide](file:///c:/Users/Ayush/Documents/Project_D/Project/docs/FREE_DEPLOYMENT.md): Deployment paths utilizing Cloudflare Tunnels, SQLite databases, and free LLM APIs.
- [CommonSource Overview Word Document](file:///c:/Users/Ayush/Documents/Project_D/CommonSource_Overview.docx): A formatted Word document summarizing what CommonSource does.
