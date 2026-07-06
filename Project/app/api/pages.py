from flask import Blueprint, send_from_directory
from core.config import WEB_DIR

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def index():
    return send_from_directory(str(WEB_DIR), "landing.html")


@pages_bp.route("/search")
def search_app():
    return send_from_directory(str(WEB_DIR), "index.html")


@pages_bp.route("/join")
def join_page():
    return send_from_directory(str(WEB_DIR), "join.html")


@pages_bp.route("/governance")
def governance_page():
    return send_from_directory(str(WEB_DIR), "governance.html")
