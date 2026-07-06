"""Flask application factory."""

from __future__ import annotations

import threading

from flask import Flask
from flask_cors import CORS

from core.config import WEB_DIR
from embed import warmup_embeddings


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
    CORS(app)

    from api.pages import pages_bp

    app.register_blueprint(pages_bp)
    # Phase 4: register search_bp, corpus_bp, generation_bp after route cutover

    @app.before_request
    def _ensure_embed_warm() -> None:
        if not getattr(app, "_embed_warm_started", False):
            app._embed_warm_started = True
            threading.Thread(target=warmup_embeddings, daemon=True).start()

    return app
