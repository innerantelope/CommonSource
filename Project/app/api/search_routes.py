"""Search, RAG, timeline, arc — API contract preserved."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

search_bp = Blueprint("search_api", __name__)


@search_bp.route("/api/search")
def search():
    from retrieval.pipeline import retrieve_sources

    query = request.args.get("q", "").strip()
    top_k = min(int(request.args.get("k", 8)), 20)
    if not query:
        return jsonify({"error": "No query provided"}), 400
    data = retrieve_sources(query, top_k=top_k)
    if data.get("error"):
        return jsonify(data), 400
    # Frontend expects query, count, results only
    return jsonify({
        "query": data["query"],
        "count": data["count"],
        "results": data["results"],
    })


# Layered / ask / arc / timeline remain on legacy search_api until full cutover.
# Registering stubs would duplicate routes — search_api.py imports this blueprint
# and keeps legacy handlers for other endpoints.
