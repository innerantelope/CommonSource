#!/usr/bin/env python3
"""
search.py — Semantic search over the GAT Platform knowledge base.

Loads all embedded chunks from SQLite, computes cosine similarity against a
query embedding, and returns the most relevant passages across all domain packs.

This is the retrieval layer that simulation authors use to surface evidence
from the Books+Academic PDF library when building scenarios.

Usage:
    python search.py \\
        --db outputs/gat_knowledge.db \\
        --query "collective action failure in governance reform" \\
        --top-k 5

    # Filter by domain pack
    python search.py \\
        --db outputs/gat_knowledge.db \\
        --query "labour market disruption from automation" \\
        --domain ai_and_automation \\
        --top-k 8

    # Use local embeddings (no Ollama required)
    python search.py \\
        --db outputs/gat_knowledge.db \\
        --query "just transition coal mining communities" \\
        --embed-method local \\
        --top-k 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from embed import generate_embedding, blob_to_embedding, rank_by_similarity
from knowledge_db import connect_db, init_db, get_all_chunks_with_embeddings


def search_knowledge_base(
    db_path: Path,
    query: str,
    *,
    top_k: int = 10,
    domain_filter: Optional[str] = None,
    embed_method: str = "ollama",
    embed_model: str = "nomic-embed-text",
    local_model: str = "all-MiniLM-L6-v2",
    ollama_base_url: str = "http://localhost:11434",
    min_score: float = 0.0,
) -> List[dict]:
    """
    Semantic search over all embedded chunks in the knowledge base.

    Returns a list of result dicts sorted by cosine similarity (highest first).
    Each result includes: score, chunk_text (truncated), title, domain, asset_id.
    """
    # Load all chunks with embeddings
    conn = connect_db(db_path)
    init_db(conn)
    all_chunks = get_all_chunks_with_embeddings(conn)

    # Optionally filter by domain
    if domain_filter:
        # Get asset_ids in the target domain
        domain_assets = set(
            row["asset_id"]
            for row in conn.execute(
                "SELECT asset_id FROM domain_classifications WHERE domain_pack_id = ?",
                (domain_filter,),
            ).fetchall()
        )
        all_chunks = [c for c in all_chunks if c["asset_id"] in domain_assets]

    conn.close()

    if not all_chunks:
        return []

    # Deserialise embeddings
    candidates = []
    for chunk in all_chunks:
        blob = chunk.get("embedding_blob")
        if not blob:
            continue
        embedding = blob_to_embedding(blob)
        candidates.append({
            "asset_id": chunk["asset_id"],
            "chunk_id": chunk["chunk_id"],
            "chunk_text": chunk["chunk_text"],
            "title": chunk.get("title", ""),
            "source_path": chunk.get("source_path", ""),
            "embedding": embedding,
        })

    if not candidates:
        return []

    # Generate query embedding
    query_embedding = generate_embedding(
        query,
        method=embed_method,
        ollama_model=embed_model,
        ollama_base_url=ollama_base_url,
        local_model=local_model,
    )
    if not query_embedding:
        print("❌ Could not generate query embedding.", file=sys.stderr)
        return []

    # Rank by cosine similarity
    ranked = rank_by_similarity(query_embedding, candidates, top_k=top_k)
    ranked = [r for r in ranked if r["score"] >= min_score]

    # Clean up output (remove raw embedding from results)
    for r in ranked:
        r.pop("embedding", None)

    return ranked


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Semantic search over GAT Platform knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", default="outputs/gat_knowledge.db", help="SQLite DB path")
    parser.add_argument("--query", required=True, help="Search query text")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to return")
    parser.add_argument("--domain", help="Filter by domain pack id (e.g. decarbonisation)")
    parser.add_argument(
        "--embed-method", choices=["ollama", "local"], default="ollama", help="Embedding backend"
    )
    parser.add_argument("--embed-model", default="nomic-embed-text", help="Ollama embedding model")
    parser.add_argument("--local-model", default="all-MiniLM-L6-v2", help="Local embedding model")
    parser.add_argument("--ollama-base-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument("--min-score", type=float, default=0.0, help="Minimum similarity score")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f'\nSearching: "{args.query}"')
    if args.domain:
        print(f"Domain filter: {args.domain}")
    print()

    results = search_knowledge_base(
        db_path,
        args.query,
        top_k=args.top_k,
        domain_filter=args.domain,
        embed_method=args.embed_method,
        embed_model=args.embed_model,
        local_model=args.local_model,
        ollama_base_url=args.ollama_base_url,
        min_score=args.min_score,
    )

    if not results:
        print("No results found. Have you run ingest_library.py yet?")
        return

    for i, r in enumerate(results, 1):
        score = r["score"]
        title = r["title"][:60]
        text = r["chunk_text"][:300].replace("\n", " ")
        print(f"[{i}] score={score:.4f}  |  {title}")
        print(f"    {text}...")
        print()


if __name__ == "__main__":
    main()
