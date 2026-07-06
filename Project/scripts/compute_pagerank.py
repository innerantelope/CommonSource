#!/usr/bin/env python3
"""
Compute and store PageRank scores for documents in the knowledge base.

PageRank identifies important sources based on their citation relationships.
This script should be run periodically to keep PageRank scores fresh.

Usage:
    python compute_pagerank.py
    python compute_pagerank.py --alpha 0.85 --max-iter 100

Environment:
    COMMONSOURCE_DB_PATH: Path to knowledge base SQLite file (default: data/database/commonsource.db)
"""

import argparse
import logging
import sys
import os
from pathlib import Path

# Add app directory to path
script_dir = Path(__file__).resolve().parent
app_dir = script_dir.parent / "app"
sys.path.insert(0, str(app_dir))
os.chdir(str(app_dir))

from knowledge_db import connect_db, init_db
from retrieval.pagerank import compute_and_store_pagerank, build_citation_graph_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Compute PageRank scores for knowledge base documents"
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database (default: uses COMMONSOURCE_DB_PATH env var)"
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.85,
        help="PageRank damping factor (default: 0.85)"
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=100,
        help="Maximum iterations for PageRank computation (default: 100)"
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Only analyze citation graph, don't store scores"
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Show top-k highest PageRank sources (default: 20)"
    )

    args = parser.parse_args()

    # Determine database path
    db_path = args.db_path
    if not db_path:
        import os
        from core.config import DB_PATH
        db_path = DB_PATH

    db_path = Path(db_path)
    if not db_path.exists():
        log.error(f"Database not found: {db_path}")
        return 1

    log.info(f"Connecting to database: {db_path}")
    conn = connect_db(db_path)
    init_db(conn)

    try:
        # Build citation graph
        log.info("Building citation graph from knowledge base...")
        graph, source_metadata = build_citation_graph_from_db(conn)

        log.info(f"Citation graph: {graph.get_node_count()} sources, {graph.get_edge_count()} citations")

        if args.analyze_only:
            # Just analyze the graph
            pagerank_scores = graph.compute_pagerank(alpha=args.alpha, max_iter=args.max_iter)

            if pagerank_scores:
                # Show top sources
                sorted_scores = sorted(pagerank_scores.items(), key=lambda x: x[1], reverse=True)

                log.info(f"\nTop {args.top_k} sources by PageRank:")
                log.info("-" * 80)
                for i, (asset_id, score) in enumerate(sorted_scores[:args.top_k], 1):
                    metadata = source_metadata.get(asset_id, {})
                    title = metadata.get("title", "Unknown")
                    pub = metadata.get("publication", "Unknown")
                    log.info(f"{i:3d}. {score:.4f} | {pub:20s} | {title[:50]}")
                log.info("-" * 80)

                # Statistics
                scores_list = list(pagerank_scores.values())
                log.info(f"\nPageRank Statistics:")
                log.info(f"  Mean:   {sum(scores_list) / len(scores_list):.4f}")
                log.info(f"  Min:    {min(scores_list):.4f}")
                log.info(f"  Max:    {max(scores_list):.4f}")
        else:
            # Compute and store PageRank scores
            log.info(f"Computing PageRank scores (alpha={args.alpha}, max_iter={args.max_iter})...")
            pagerank_scores = compute_and_store_pagerank(
                conn,
                alpha=args.alpha,
                max_iter=args.max_iter
            )

            if pagerank_scores:
                # Show top sources
                sorted_scores = sorted(pagerank_scores.items(), key=lambda x: x[1], reverse=True)

                log.info(f"\nTop {args.top_k} sources by PageRank:")
                log.info("-" * 80)
                for i, (asset_id, score) in enumerate(sorted_scores[:args.top_k], 1):
                    metadata = source_metadata.get(asset_id, {})
                    title = metadata.get("title", "Unknown")
                    pub = metadata.get("publication", "Unknown")
                    log.info(f"{i:3d}. {score:.4f} | {pub:20s} | {title[:50]}")
                log.info("-" * 80)

                # Statistics
                scores_list = list(pagerank_scores.values())
                log.info(f"\nPageRank Statistics:")
                log.info(f"  Mean:   {sum(scores_list) / len(scores_list):.4f}")
                log.info(f"  Min:    {min(scores_list):.4f}")
                log.info(f"  Max:    {max(scores_list):.4f}")

                log.info(f"\nPageRank scores stored for {len(pagerank_scores)} documents.")
                log.info("Retrieval pipeline will now use PageRank to boost highly-cited sources.")
            else:
                log.warning("No PageRank scores computed.")
                return 1

    except Exception as e:
        log.error(f"Error computing PageRank: {e}", exc_info=True)
        return 1

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
