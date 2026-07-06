"""Central configuration for CommonSource backend."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent

DB_PATH = Path(os.getenv("COMMONSOURCE_DB", str(PROJECT_ROOT / "data" / "database" / "commonsource.db")))
WEB_DIR = Path(os.getenv("COMMONSOURCE_WEB_DIR", str(PROJECT_ROOT / "frontend")))
PORT = int(os.getenv("COMMONSOURCE_PORT", "5050"))

# Embeddings — default matches migration target; override to keep legacy multilingual model.
EMBED_MODEL = os.getenv(
    "COMMONSOURCE_EMBED_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)
EMBED_CACHE_DIR = Path(
    os.getenv("COMMONSOURCE_EMBED_CACHE", str(PROJECT_ROOT / "data" / "cache" / "embeddings"))
)
EMBED_MAX_CHARS = int(os.getenv("COMMONSOURCE_EMBED_MAX_CHARS", "2000"))
EMBED_VECTOR_SIZE = int(os.getenv("COMMONSOURCE_EMBED_DIM", "384"))

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Qdrant
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "") or None
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "commonsource_chunks")
USE_QDRANT = os.getenv("COMMONSOURCE_USE_QDRANT", "true").lower() in ("1", "true", "yes")
QDRANT_SEARCH_LIMIT = int(os.getenv("QDRANT_SEARCH_LIMIT", "80"))

# Retrieval
SQLITE_CANDIDATE_LIMIT = int(os.getenv("SQLITE_CANDIDATE_LIMIT", "2500"))
MIN_RELEVANCE_SCORE = float(os.getenv("MIN_RELEVANCE_SCORE", "0.25"))

GENERATION_MODELS = [
    "qwen2.5:1.5b",
    "qwen3:1.7b",
    "qwen3:1.7b-q4_K_M",
    "qwen3-1.7b",
    "qwen3-1.7b:latest",
    "qwen2.5:3b",
    "qwen2.5:latest",
    "llama3.2:1b",
    "llama3.2:3b",
    "gemma3:1b",
    "gemma2:2b",
    "qwen3:4b",
    "qwen3-4b-local",
    "qwen3-4b-local:latest",
]

TRANSLATION_MODELS = [
    "qwen2.5:1.5b",
    "qwen2.5:3b",
    "qwen3:1.7b",
    "qwen3:1.7b-q4_K_M",
    "qwen3-1.7b",
    "qwen3:4b",
    "qwen2.5:latest",
    "qwen3-4b-local:latest",
    "qwen3-4b-local",
]

MAX_TOKENS_SYNTHESIS = 600
MAX_TOKENS_ARC = 700
MAX_TOKENS_GENERATE = 500
MAX_TOKENS_LAYER = 400
MAX_TOKENS_GAPS = 500
MAX_TOKENS_TRANSLATE = 900

SOURCE_TYPES = {
    "news": "News & Media Evidence",
    "development": "NGO & Development Evidence",
    "community": "People's Voice",
    "official": "Official Record",
}

TRANSLATION_LANGUAGES = {
    "en": "English",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "mr": "Marathi",
    "gu": "Gujarati",
    "ur": "Urdu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "or": "Odia",
    "pa": "Punjabi",
    "as": "Assamese",
    "ne": "Nepali",
    "fr": "French",
    "es": "Spanish",
    "ar": "Arabic",
    "sw": "Swahili",
}
