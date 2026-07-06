"""
PageRank computation for document/source importance scoring.

Builds a citation graph from documents and computes PageRank scores to identify
influential sources within the knowledge base.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import networkx as nx
except ImportError:
    nx = None

log = logging.getLogger(__name__)


class CitationGraph:
    """Builds a directed citation graph from documents."""

    def __init__(self):
        """Initialize an empty citation graph."""
        self.graph: nx.DiGraph | None = None
        if nx is None:
            raise ImportError("networkx is required for PageRank computation. Install with: pip install networkx")
        self.graph = nx.DiGraph()

    def add_source(self, source_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add a source/document node to the graph."""
        if not self.graph:
            return
        self.graph.add_node(source_id, metadata=metadata or {})

    def add_citation(self, source_id: str, cited_source_id: str, weight: float = 1.0) -> None:
        """Add a directed edge from source_id -> cited_source_id."""
        if not self.graph:
            return
        # Ensure both nodes exist
        self.graph.add_node(source_id)
        self.graph.add_node(cited_source_id)
        # Add or update edge with weight
        if self.graph.has_edge(source_id, cited_source_id):
            self.graph[source_id][cited_source_id]["weight"] += weight
        else:
            self.graph.add_edge(source_id, cited_source_id, weight=weight)

    def compute_pagerank(self, alpha: float = 0.85, max_iter: int = 100, tol: float = 1e-6) -> Dict[str, float]:
        """
        Compute PageRank scores using standard algorithm.

        Args:
            alpha: Damping factor (default 0.85)
            max_iter: Maximum iterations (default 100)
            tol: Convergence tolerance (default 1e-6)

        Returns:
            Dict mapping source_id -> PageRank score (0-1)
        """
        if not self.graph or len(self.graph) == 0:
            return {}

        # Normalize edge weights if present
        for _, _, data in self.graph.edges(data=True):
            if "weight" not in data:
                data["weight"] = 1.0

        # Compute PageRank
        try:
            pagerank = nx.pagerank(
                self.graph,
                alpha=alpha,
                max_iter=max_iter,
                tol=tol,
                weight="weight"
            )
            return pagerank
        except Exception as e:
            log.error(f"PageRank computation failed: {e}")
            return {}

    def get_node_count(self) -> int:
        """Return the number of nodes in the graph."""
        return len(self.graph) if self.graph else 0

    def get_edge_count(self) -> int:
        """Return the number of edges in the graph."""
        return len(self.graph.edges()) if self.graph else 0


def extract_citations_from_text(text: str, known_sources: Set[str]) -> List[str]:
    """
    Extract potential citation references from text.

    Looks for patterns like "[Author, Year]", "Author (Year)", or known source mentions.

    Args:
        text: Text to search for citations
        known_sources: Set of known source identifiers/names to look for

    Returns:
        List of cited source references found in text
    """
    citations = []
    if not text:
        return citations

    # Pattern 1: [Author, Year] format
    bracket_pattern = r'\[([A-Z][a-z]+ et al\.?|[A-Z][a-z]+(?:\s+&\s+[A-Z][a-z]+)?),\s*\d{4}\]'
    matches = re.findall(bracket_pattern, text)
    citations.extend(matches)

    # Pattern 2: Author (Year) format
    paren_pattern = r'([A-Z][a-z]+ et al\.?|[A-Z][a-z]+(?:\s+&\s+[A-Z][a-z]+)?)\s*\(\d{4}\)'
    matches = re.findall(paren_pattern, text)
    citations.extend(matches)

    # Pattern 3: Look for known source mentions
    for source in known_sources:
        # Case-insensitive search with word boundaries
        if re.search(rf'\b{re.escape(source)}\b', text, re.IGNORECASE):
            citations.append(source)

    return list(set(citations))  # Remove duplicates


def build_citation_graph_from_db(
    conn: Any,
    confidence_threshold: float = 0.3
) -> Tuple[CitationGraph, Dict[str, Dict[str, Any]]]:
    """
    Build a citation graph from database documents and chunks.

    Analyzes document content to extract citations and build a graph
    of which sources cite which other sources.

    Args:
        conn: SQLite database connection
        confidence_threshold: Minimum confidence score for counting a citation (0-1)

    Returns:
        Tuple of (CitationGraph, source_metadata_dict)
    """
    graph = CitationGraph()
    source_metadata = {}

    try:
        # Get all sources (assets and their CommonSource metadata)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                a.id as asset_id,
                COALESCE(c.publication, 'Unknown') as publication,
                COALESCE(c.author, 'Unknown') as author,
                COALESCE(c.date_published, '') as date_published,
                COALESCE(c.article_title, a.title) as title,
                COUNT(k.id) as chunk_count
            FROM knowledge_assets a
            LEFT JOIN commonsource_articles c ON a.id = c.asset_id
            LEFT JOIN knowledge_chunks k ON a.id = k.asset_id
            GROUP BY a.id
        """)

        sources = cursor.fetchall()
        source_dict = {row["asset_id"]: row for row in sources}

        # Add all sources as nodes
        for asset_id, row in source_dict.items():
            metadata = {
                "publication": row["publication"],
                "author": row["author"],
                "date": row["date_published"],
                "title": row["title"],
                "chunk_count": row["chunk_count"]
            }
            graph.add_source(asset_id, metadata)
            source_metadata[asset_id] = metadata

        # Build set of known source identifiers for citation extraction
        known_sources: Set[str] = set()
        known_sources.update(source_dict.keys())
        # Also add publication names, authors (single words)
        for row in sources:
            if row["publication"]:
                known_sources.add(row["publication"])
            if row["author"]:
                # Add author if single word or first word of multi-word author
                author_words = row["author"].split()
                if author_words:
                    known_sources.add(author_words[0])

        # Extract citations from chunks
        cursor.execute("""
            SELECT asset_id, chunk_text
            FROM knowledge_chunks
            WHERE chunk_text IS NOT NULL AND chunk_text != ''
            ORDER BY asset_id, chunk_index
        """)

        chunks = cursor.fetchall()
        citation_counts: Dict[Tuple[str, str], float] = {}

        for row in chunks:
            asset_id = row["asset_id"]
            chunk_text = row["chunk_text"]

            # Extract citations from this chunk
            citations = extract_citations_from_text(chunk_text, known_sources)

            # Add edges: this asset cites other assets
            for cited in citations:
                # Resolve citation to asset_id if it's a publication/author
                cited_asset_id = None

                if cited in source_dict:
                    cited_asset_id = cited
                else:
                    # Try to find matching source by publication or author
                    for src_id, src_row in source_dict.items():
                        if (cited.lower() in (src_row["publication"] or "").lower() or
                            cited.lower() in (src_row["author"] or "").lower()):
                            cited_asset_id = src_id
                            break

                if cited_asset_id and cited_asset_id != asset_id:
                    key = (asset_id, cited_asset_id)
                    citation_counts[key] = citation_counts.get(key, 0) + 1.0

        # Add weighted edges (higher weight = more citations)
        for (source_id, cited_id), count in citation_counts.items():
            graph.add_citation(source_id, cited_id, weight=count)

        log.info(
            f"Built citation graph: {graph.get_node_count()} nodes, "
            f"{graph.get_edge_count()} edges"
        )

    except Exception as e:
        log.error(f"Error building citation graph: {e}")

    return graph, source_metadata


def compute_and_store_pagerank(
    conn: Any,
    alpha: float = 0.85,
    max_iter: int = 100
) -> Dict[str, float]:
    """
    Compute PageRank scores and store them in the database.

    Args:
        conn: SQLite database connection
        alpha: PageRank damping factor
        max_iter: Maximum iterations

    Returns:
        Dict mapping asset_id -> PageRank score
    """
    # Build graph
    graph, _ = build_citation_graph_from_db(conn)

    # Compute PageRank
    pagerank_scores = graph.compute_pagerank(alpha=alpha, max_iter=max_iter)

    if not pagerank_scores:
        log.warning("No PageRank scores computed")
        return {}

    # Normalize scores to 0-1 range
    if pagerank_scores:
        max_score = max(pagerank_scores.values())
        min_score = min(pagerank_scores.values())
        range_score = max_score - min_score if max_score > min_score else 1.0

        normalized = {}
        for asset_id, score in pagerank_scores.items():
            normalized[asset_id] = (score - min_score) / range_score if range_score > 0 else 0.5
        pagerank_scores = normalized

    # Store in database
    try:
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pagerank_scores (
                asset_id TEXT PRIMARY KEY,
                pagerank_score REAL NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(asset_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
            )
        """)

        # Clear old scores
        cursor.execute("DELETE FROM pagerank_scores")

        # Insert new scores
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        for asset_id, score in pagerank_scores.items():
            cursor.execute(
                """
                INSERT OR REPLACE INTO pagerank_scores (asset_id, pagerank_score, updated_at)
                VALUES (?, ?, ?)
                """,
                (asset_id, score, now)
            )

        conn.commit()
        log.info(f"Stored PageRank scores for {len(pagerank_scores)} assets")

    except Exception as e:
        log.error(f"Error storing PageRank scores: {e}")
        conn.rollback()

    return pagerank_scores


def get_pagerank_score(conn: Any, asset_id: str) -> float:
    """
    Retrieve PageRank score for an asset.

    Args:
        conn: SQLite database connection
        asset_id: ID of the asset

    Returns:
        PageRank score (0-1), or 0.5 if not found
    """
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT pagerank_score FROM pagerank_scores WHERE asset_id = ?", (asset_id,))
        row = cursor.fetchone()
        return row["pagerank_score"] if row else 0.5
    except Exception as e:
        log.debug(f"Error retrieving PageRank for {asset_id}: {e}")
        return 0.5
