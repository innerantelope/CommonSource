"""
search_api.py — CommonSource search API + static file server

Serves:
  GET /              -> CommonSource demo UI (web/index.html)
  GET /api/search    -> Vector search, returns source cards
  GET /api/ask       -> RAG: search + LLM synthesis with inline citations
  GET /api/stats     -> corpus statistics
  GET /api/articles  -> browse all indexed articles

Usage:
    cd commonground/backend
    python3 search_api.py
    -> http://localhost:5050
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from document_classifier import ensure_document_metadata_tables
_translation_executor = ThreadPoolExecutor(max_workers=4)


import math
import hashlib
import json
import logging
import os
import sqlite3
import struct
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import re

from flask import Flask, g, has_request_context, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from auth_service import (
    ADMIN_ROLES,
    AuthConfigError,
    AuthError,
    authenticate_user,
    change_password,
    count_users as auth_count_users,
    create_csrf_token,
    create_password_reset_token,
    create_user,
    decode_access_token,
    get_user_by_id,
    get_user_by_email,
    issue_token_pair,
    is_access_token_revoked,
    list_users as auth_list_users,
    refresh_token_pair,
    refresh_token_user_id,
    revoke_access_token,
    reset_password,
    revoke_refresh_token,
    role_allows,
    run_migrations,
    update_user as auth_update_user,
    validate_csrf_token,
    user_count,
)
from source_classifier import classify_source
from utils.vectors import embedding_to_blob

# Upgraded retrieval stack (see app/retrieval/, app/embed.py, docs/MIGRATION-QDRANT.md)
from embed import embed_query as embed, warmup_embeddings

# Translation without Ollama
try:
    from deep_translator import GoogleTranslator
    HAS_DEEP_TRANSLATOR = True
except ImportError:
    HAS_DEEP_TRANSLATOR = False

try:
    from llm_provider import generate as provider_generate
    from llm_provider import llm_health as provider_llm_health, provider_status as llm_provider_status
    HAS_LLM_PROVIDER = True
except Exception:
    provider_generate = None
    provider_llm_health = None
    llm_provider_status = None
    HAS_LLM_PROVIDER = False

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH      = PROJECT_ROOT / "data" / "database" / "commonsource.db"
WEB_DIR      = PROJECT_ROOT / "frontend"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
PORT         = 5050
OLLAMA_BASE  = "http://localhost:11434"
OLLAMA_EMBED = "nomic-embed-text"
LOCAL_MODEL  = "paraphrase-multilingual-MiniLM-L12-v2"
# Default to gemma3:4b; can be overridden via environment variable
COMMONSOURCE_LLM_MODEL = os.getenv("COMMONSOURCE_LLM_MODEL", "gemma3:4b")

# Generation model — prioritizes gemma3:4b, falls back to fast models if unavailable
GENERATION_MODELS = [
    "gemma3:4b",         # Primary: Larger, more capable model
    "gemma3:3b",         # Secondary: Slightly smaller variant
    "qwen2.5:3b",        # Fallback: If Gemma unavailable
    "qwen3:1.7b",
    "qwen3:1.7b-q4_K_M",
    "qwen3-1.7b",
    "qwen3-1.7b:latest",
    "qwen2.5:1.5b",
    "qwen2.5:latest",
    "llama3.2:1b",
    "llama3.2:3b",
    "gemma3:1b",
    "gemma2:2b",
    "qwen3:4b",
    "qwen3-4b-local",
    "qwen3-4b-local:latest",
]

# Translation models (using deep-translator, no longer dependent on Ollama)
# Language code mapping for deep-translator (ISO 639-1 -> deep-translator code)
DEEP_TRANSLATOR_LANGS = {
    "en": "en",
    "hi": "hi",
    "bn": "bn",
    "ta": "ta",
    "te": "te",
    "mr": "mr",
    "gu": "gu",
    "ur": "ur",
    "kn": "kn",
    "ml": "ml",
    "or": "or",
    "pa": "pa",
    "as": "as",
    "ne": "ne",
    "fr": "fr",
    "es": "es",
    "ar": "ar",
    "sw": "sw",
}


def normalize_target_language_code(target_code: str) -> Optional[str]:
    """Normalize a requested language to a deep-translator language code."""
    if not target_code:
        return None
    value = target_code.strip().lower()
    if value in DEEP_TRANSLATOR_LANGS:
        return DEEP_TRANSLATOR_LANGS[value]

    for code, name in TRANSLATION_LANGUAGES.items():
        if value == name.lower():
            return code
    return None


# Token caps — keeps inference fast on small models
MAX_TOKENS_SYNTHESIS = 600
MAX_TOKENS_ARC       = 700
MAX_TOKENS_GENERATE  = 500
MAX_TOKENS_LAYER     = 400
MAX_TOKENS_GAPS      = 500
MAX_TOKENS_TRANSLATE = 300  # Reduced from 900 for faster inference

# Source type labels — used to filter and tag results per evidence layer
SOURCE_TYPES = {
    "news":        "News & Media Evidence",
    "development": "NGO & Development Evidence",
    "community":   "People's Voice",
    "official":    "Official Record",
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

OLLAMA_URL   = f"{OLLAMA_BASE}/api/embeddings"


def reexec_project_venv_if_needed() -> None:
    """Prefer the project virtualenv when the API is launched from a system Python."""
    if os.getenv("COMMONSOURCE_SKIP_VENV_REEXEC") == "1":
        return
    workspace_root = PROJECT_ROOT.parent
    if os.name == "nt":
        venv_python = workspace_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = workspace_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    current = Path(sys.executable).resolve()
    target = venv_python.resolve()
    if current == target:
        return
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.execv(str(target), [str(target), *sys.argv])

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

@app.before_request
def start_request_timer():
    from flask import g
    import time
    g.start_time = time.time()

@app.after_request
def log_request_metrics(response):
    from flask import g, request
    import time
    if hasattr(g, 'start_time') and request.endpoint in ('ask', 'ask_layered', 'arc', 'generate', 'model_test'):
        latency = time.time() - g.start_time
        model = getattr(g, 'active_model', 'unknown')
        status = response.status_code
        timeout_event = status == 504
        log.info(f"[{request.endpoint}] active_model={model} latency={latency:.3f}s timeout_event={timeout_event}")
    return response

@app.before_request
def start_request_timer():
    from flask import g, request
    import time
    g.start_time = time.time()

@app.after_request
def log_request_metrics(response):
    from flask import g, request
    import time
    if hasattr(g, 'start_time') and request.endpoint in ('ask', 'ask_layered', 'arc', 'generate', 'model_test'):
        latency = time.time() - g.start_time
        model = getattr(g, 'active_model', 'unknown')
        status = response.status_code
        timeout_event = status == 504
        log.info(f"[{request.endpoint}] active_model={model} latency={latency:.3f}s timeout_event={timeout_event}")
    return response

_local_model = None  # lazy-loaded sentence-transformers fallback
_embed_lock = __import__("threading").Lock()
_embed_ready = __import__("threading").Event()
_ollama_gen_lock = __import__("threading").Lock()
_ollama_model_cache: Dict[str, Any] = {"ts": 0.0, "models": []}
_response_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_retrieval_cache: "OrderedDict[str, tuple[float, Dict[str, Any]]]" = OrderedDict()
_cache_lock = __import__("threading").Lock()
OLLAMA_MODEL_CACHE_SECONDS = 10
OLLAMA_RESPONSE_CACHE_SECONDS = 600
OLLAMA_RESPONSE_CACHE_MAX = 64
RETRIEVAL_CACHE_SECONDS = 45
RETRIEVAL_CACHE_MAX = 32


class OllamaGenerationError(RuntimeError):
    """Raised when Ollama cannot return a usable response inside the request budget."""


# ── Embedding ─────────────────────────────────────────────────────────────────

# ── Generation ────────────────────────────────────────────────────────────────

def ollama_is_listening() -> bool:
    try:
        import socket
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.25):
            return True
    except Exception:
        return False


def get_available_model() -> Optional[str]:
    """Return the first available generation model from Ollama."""
    return get_available_ollama_model(GENERATION_MODELS)


def get_available_translation_model() -> Optional[str]:
    """Return deep-translator if available, else LOCAL fallback."""
    if HAS_DEEP_TRANSLATOR:
        return "deep-translator"
    return "LOCAL"


def get_llm_model() -> str:
    import os
    model = os.getenv("COMMONSOURCE_LLM_MODEL", "gemma3:4b")
    log.info("[LLM] Using model: %s", model)
    try:
        from flask import g
        g.active_model = model
    except Exception:
        pass
    return model


def get_available_ollama_model(candidates: List[str]) -> Optional[str]:
    if not ollama_is_listening():
        return None
    try:
        import requests as req
        now = time.time()
        cached = _ollama_model_cache.get("models") or []
        if cached and now - float(_ollama_model_cache.get("ts") or 0) < OLLAMA_MODEL_CACHE_SECONDS:
            models = cached
        else:
            r = req.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            _ollama_model_cache["models"] = models
            _ollama_model_cache["ts"] = now
        available = {name.split(":")[0] for name in models}
        available_full = set(models)
        for candidate in candidates:
            if candidate in available_full:
                return candidate
            base = candidate.split(":")[0]
            if base in available:
                # Return the actual model name
                for full in available_full:
                    if full.startswith(base):
                        return full
    except Exception as exc:
        log.debug("Could not list Ollama models: %s", exc)
    return None


def build_context(sources: List[Dict[str, Any]], limit: int = 6) -> str:
    parts = []
    for i, s in enumerate(sources[:limit], 1):
        meta = [s.get("publication") or "Unknown source"]
        if s.get("title"):    meta.append(f"Title: {s['title']}")
        if s.get("date"):     meta.append(f"Date: {s['date'][:10]}")
        if s.get("location"): meta.append(f"Location: {s['location']}")
        parts.append(
            f"[Source {i}]\n"
            f"Citation metadata, not story actors: {' | '.join(meta)}\n"
            f"Reported passage:\n{s['excerpt']}\n"
        )
    return "\n".join(parts)


def filter_entities(entities: Dict[str, Any], sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Remove citation metadata such as byline authors from extracted story entities."""
    if not entities:
        return {}

    byline_names = {
        (s.get("author") or "").strip().lower()
        for s in sources
        if (s.get("author") or "").strip()
    }
    byline_names.update({"hardnews", "hardnews bureau"})

    filtered = dict(entities)
    people = []
    for person in entities.get("people", []) or []:
        key = str(person).strip().lower()
        if not key or key in byline_names:
            continue
        people.append(person)
    filtered["people"] = people
    for key in ("organisations", "places"):
        seen = set()
        deduped = []
        for item in entities.get(key, []) or []:
            norm = str(item).strip().lower()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            deduped.append(item)
        filtered[key] = deduped
    return filtered


def get_request_timeout(default: int, *, minimum: int = 5, maximum: int = 120) -> float:
    """Read ?timeout= from the current request, bounded so workers do not hang forever."""
    try:
        raw = request.args.get("timeout", "")
    except RuntimeError:
        raw = ""
    try:
        value = float(raw) if raw else float(default)
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


def _response_cache_key(prompt: str, model: str, max_tokens: int, temperature: float) -> str:
    payload = f"{model}\n{max_tokens}\n{temperature:.3f}\n{prompt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_response_cache(key: str) -> Optional[str]:
    now = time.time()
    with _cache_lock:
        cached = _response_cache.get(key)
        if not cached:
            return None
        ts, value = cached
        if now - ts > OLLAMA_RESPONSE_CACHE_SECONDS:
            _response_cache.pop(key, None)
            return None
        _response_cache.move_to_end(key)
        return value


def _write_response_cache(key: str, value: str) -> None:
    with _cache_lock:
        _response_cache[key] = (time.time(), value)
        _response_cache.move_to_end(key)
        while len(_response_cache) > OLLAMA_RESPONSE_CACHE_MAX:
            _response_cache.popitem(last=False)


def cached_retrieve_sources(
    query: str,
    *,
    top_k: int = 8,
    min_score: float = 0.25,
    extra_sql_conditions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Small cross-endpoint cache so search, layers, timeline, and arc do not repeat work."""
    from retrieval.pipeline import retrieve_sources

    cache_key = json.dumps(
        {
            "q": query.strip().lower(),
            "top_k": top_k,
            "min_score": min_score,
            "extra": extra_sql_conditions or [],
        },
        sort_keys=True,
    )
    now = time.time()
    with _cache_lock:
        cached = _retrieval_cache.get(cache_key)
        if cached and now - cached[0] <= RETRIEVAL_CACHE_SECONDS:
            _retrieval_cache.move_to_end(cache_key)
            return json.loads(json.dumps(cached[1]))

    data = retrieve_sources(
        query,
        top_k=top_k,
        min_score=min_score,
        extra_sql_conditions=extra_sql_conditions,
        candidate_pool=max(40, top_k * 5),
    )
    with _cache_lock:
        _retrieval_cache[cache_key] = (now, data)
        _retrieval_cache.move_to_end(cache_key)
        while len(_retrieval_cache) > RETRIEVAL_CACHE_MAX:
            _retrieval_cache.popitem(last=False)
    return data


def call_ollama(
    prompt: str,
    model: str,
    max_tokens: int = 300,
    *,
    timeout: Optional[float] = None,
    temperature: float = 0.2,
    cache: bool = True,
) -> str:
    if not model:
        raise OllamaGenerationError("No generation model available")

    provider_name = os.getenv("COMMONSOURCE_LLM_PROVIDER", "gemini").strip().lower()
    if HAS_LLM_PROVIDER and provider_generate and (
        provider_name in {"gemini", "auto", "openrouter", "groq"}
        or model.lower().startswith(("gemini", "models/gemini"))
    ):
        try:
            result = provider_generate(
                prompt,
                preferred_model=model,
                max_tokens=max_tokens,
                timeout=float(timeout or 60),
                temperature=temperature,
            )
            log.info(
                "[LLM] provider=%s model=%s latency_ms=%s",
                result.provider,
                result.model,
                result.latency_ms,
            )
            return clean_generation_response(result.text)
        except Exception as exc:
            raise OllamaGenerationError(str(exc)) from exc

    import requests as req

    prompt = prepare_prompt_for_model(prompt, model)
    timeout = float(timeout or 60)
    max_tokens = max(1, int(max_tokens))
    temperature = float(temperature)
    cache_key = _response_cache_key(prompt, model, max_tokens, temperature)
    if cache and temperature <= 0.2:
        cached = _read_response_cache(cache_key)
        if cached is not None:
            log.debug("Ollama cache hit: model=%s chars=%s", model, len(prompt))
            return cached

    log.debug("Ollama call: model=%s chars=%s max_tokens=%s timeout=%ss", model, len(prompt), max_tokens, timeout)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if model.lower().startswith("qwen3"):
        payload["think"] = False
    t0 = time.time()
    acquired = False
    try:
        acquired = _ollama_gen_lock.acquire(timeout=timeout)
        if not acquired:
            raise req.exceptions.Timeout(f"Timed out waiting for LLM worker after {int(timeout)}s")
        remaining = max(1.0, timeout - (time.time() - t0))
        r = req.post(
            f"{OLLAMA_BASE}/api/generate",
            json=payload,
            timeout=remaining,
        )
        r.raise_for_status()
        response_text = r.json().get("response", "")
        cleaned = clean_generation_response(response_text)
        elapsed = time.time() - t0
        log.info("Ollama response: model=%s chars=%s elapsed=%.2fs", model, len(cleaned), elapsed)
        if not cleaned:
            raise OllamaGenerationError("Ollama returned an empty response")
        if cache and temperature <= 0.2:
            _write_response_cache(cache_key, cleaned)
        return cleaned
    except req.exceptions.Timeout as exc:
        elapsed = time.time() - t0
        log.warning("Ollama timed out: model=%s elapsed=%.2fs timeout=%ss", model, elapsed, timeout)
        raise OllamaGenerationError(f"Ollama timed out after {int(timeout)}s") from exc
    except Exception as exc:
        elapsed = time.time() - t0
        log.exception("Ollama failed: model=%s elapsed=%.2fs", model, elapsed)
        raise OllamaGenerationError(str(exc)) from exc
    finally:
        if acquired:
            _ollama_gen_lock.release()


def prepare_prompt_for_model(prompt: str, model: str) -> str:
    """Qwen3 defaults to thinking mode; disable it for UI/API responses."""
    if model.lower().startswith("qwen3") and "/no_think" not in prompt:
        return f"{prompt.rstrip()}\n\n/no_think"
    return prompt


def clean_generation_response(text: str) -> str:
    """Remove Qwen thinking traces and return the user-facing answer."""
    text = (text or "").strip()
    if not text:
        return ""

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "done thinking." in text.lower():
        text = re.split(r"done thinking\.", text, flags=re.IGNORECASE)[-1]
    text = re.sub(r"^\s*Thinking\.\.\.\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def extract_json_value(text: str) -> Any:
    """Parse a JSON object/array even when the model wraps it in fences or stray prose."""
    clean = (text or "").strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass

    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = clean.find(opener)
        end = clean.rfind(closer)
        if start >= 0 and end > start:
            candidates.append(clean[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError("No valid JSON found in model response")


def translate_with_local_model(text: str, target_lang_code: str) -> str:
    """Fallback translation using Helsinki-NLP MarianMT (lightweight) when Ollama unavailable."""
    try:
        from transformers import MarianMTModel, MarianTokenizer
        import torch

        text = (text or "").strip()
        if not text or len(text) < 5:
            return text

        # Map language codes to MarianMT model names (lightweight models)
        model_map = {
            "hi": "Helsinki-NLP/Opus-MT-en-hi",  # English -> Hindi
            "bn": "Helsinki-NLP/Opus-MT-en-bn",  # English -> Bengali
            "ta": "Helsinki-NLP/Opus-MT-en-ta",  # English -> Tamil
            "te": "Helsinki-NLP/Opus-MT-en-te",  # English -> Telugu
            "mr": "Helsinki-NLP/Opus-MT-en-mr",  # English -> Marathi
            "gu": "Helsinki-NLP/Opus-MT-en-gu",  # English -> Gujarati
            "ur": "Helsinki-NLP/Opus-MT-en-ur",  # English -> Urdu
            "kn": "Helsinki-NLP/Opus-MT-en-kn",  # English -> Kannada
            "ml": "Helsinki-NLP/Opus-MT-en-ml",  # English -> Malayalam
            "pa": "Helsinki-NLP/Opus-MT-en-pa",  # English -> Punjabi
        }

        model_name = model_map.get(target_lang_code)
        if not model_name:
            return text  # No model for this language

        tokenizer = MarianTokenizer.from_pretrained(model_name)
        model = MarianMTModel.from_pretrained(model_name)

        # Truncate if needed
        input_text = text[:500]
        inputs = tokenizer(input_text, return_tensors="pt", max_length=512, truncation=True)

        with torch.no_grad():
            translated = model.generate(**inputs, max_length=512)

        result = tokenizer.batch_decode(translated, skip_special_tokens=True)
        return result[0] if result else text
    except Exception as e:
        print(f"[Translation fallback failed: {e}]")
        return text


def translate_with_qwen(
    text: str,
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
) -> str:
    """Translate text using deep-translator or local model fallback."""
    text = (text or "").strip()
    if not text:
        return ""
    if not HAS_DEEP_TRANSLATOR or model == "LOCAL":
        lang_code = target_language[:2].lower() if target_language else "en"
        return translate_with_local_model(text, lang_code)
    try:
        if len(text) > 4999:
            text = text[:4999]
        translator = GoogleTranslator(source=source_language, target=target_language)
        return translator.translate(text)
    except Exception as e:
        log.error("deep-translator failed: %s", e)
        return text


def translate_item_text(
    text: str,
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
) -> str:
    """Translate a source item while preserving Title and Excerpt labels."""
    text = (text or "").strip()
    if not text:
        return ""

    title_match = re.search(r"^\s*Title\s*:\s*(.*?)(?:\nExcerpt\s*:|\Z)", text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
    excerpt_match = re.search(r"^\s*Excerpt\s*:\s*([\s\S]*)", text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
    title_text = title_match.group(1).strip() if title_match else ""
    excerpt_text = excerpt_match.group(1).strip() if excerpt_match else ""

    if not title_text and not excerpt_text:
        return translate_with_qwen(text, target_language, model, source_language, timeout=timeout)

    translated_title = translate_with_qwen(title_text, target_language, model, source_language, timeout=timeout) if title_text else ""
    translated_excerpt = translate_with_qwen(excerpt_text, target_language, model, source_language, timeout=timeout) if excerpt_text else ""
    return f"Title: {translated_title}\nExcerpt: {translated_excerpt}".strip()


def translate_items_batch(
    items: List[Dict[str, Any]],
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Translate multiple source cards sequentially using deep-translator."""
    prepared: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        item_text = (item.get("text") or "").strip()
        prepared.append({"id": item_id, "text": item_text[:2000]})

    if not prepared:
        return []

    translations: List[Dict[str, Any]] = []
    for p in prepared:
        if not p["text"]:
            translations.append({"id": p["id"], "translation": ""})
            continue
        translated = translate_item_text(
            p["text"], target_language, model, source_language, timeout=timeout
        )
        translations.append({"id": p["id"], "translation": translated})
    return translations


def clean_translation_response(text: str) -> str:
    """Remove model commentary and wrapper artefacts from translation output."""
    text = (text or "").strip()
    text = re.sub(r"^/\w+\s*", "", text).strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    if text.startswith('"""') and '"""' in text[3:]:
        text = text[3:text.find('"""', 3)].strip()
    text = re.split(r"\n\s*(?:The translation|Translation note|Note)\b", text, flags=re.IGNORECASE)[0]
    return text.strip().strip('"').strip()


def _parse_batch_translation_block(raw: str, item_id: str) -> str:
    """Extract one --- SOURCE id --- block from a batched model response."""
    pattern = rf"---\s*SOURCE\s*{re.escape(str(item_id))}\s*---\s*(.*?)(?=\n---\s*SOURCE\s+|\Z)"
    match = re.search(pattern, raw, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return clean_translation_response(match.group(1))
    return ""


def synthesise(query: str, sources: List[Dict[str, Any]], model: str, *, timeout: Optional[float] = None) -> tuple:
    """
    Returns (answer: str, entities: dict) from a single Qwen call.
    Entities: {people: [...], organisations: [...], places: [...]}
    """
    context = build_context(sources)
    prompt = f"""You are CommonSource, a research assistant for Indian journalism.
Using ONLY the sources below, write a connected narrative that synthesises what the sources together reveal about the topic.
Then extract named entities.

Important provenance rule:
Source authors/writers in citation metadata are not story actors. Do not include journalists, byline writers, publications, or archive metadata as people involved in the reported events unless the reported passage itself explicitly says they are involved.

Respond in valid JSON with this exact structure:
{{
  "answer": "A 4-6 paragraph narrative. Each paragraph covers a different angle or theme from the sources. Weave the sources together — do not list them one by one. Cite inline as [Source N]. End with what the coverage collectively suggests.",
  "entities": {{
    "people": ["full name", ...],
    "organisations": ["org name", ...],
    "places": ["city or region", ...]
  }}
}}

Question: {query}

Sources:
{context}

JSON response:"""

    raw = call_ollama(prompt, model, max_tokens=MAX_TOKENS_SYNTHESIS, timeout=timeout)

    # Extract JSON from response
    try:
        data = extract_json_value(raw)
        return data.get("answer", ""), filter_entities(data.get("entities", {}), sources)
    except Exception as exc:
        log.warning("Could not parse synthesis JSON; using raw answer: %s", exc)
        # Fallback: treat whole response as answer, no entities
        return raw, {}


_LAYER_PROMPTS = {
    "news": """You are analysing local journalism and community media coverage.
Using ONLY the sources below, write 2-3 paragraphs describing what ground-level reporting shows about: {query}
Focus on: incidents, voices, emerging issues, contradictions, public sentiment, and story leads.
Cite inline as [Source N]. Be factual — only use what the sources say.
If sources are insufficient, say so plainly.""",

    "development": """You are analysing development sector evidence.
Using ONLY the sources below, write 2-3 paragraphs describing what institutional evidence shows about: {query}
Focus on: programme data, delivery gaps, indicators, interventions, evaluation findings, and field records.
Cite inline as [Source N]. Be factual — only use what the sources say.
If sources are insufficient, say so plainly.""",

    "community": """You are analysing community voices and grassroots testimony.
Using ONLY the sources below, write 2-3 paragraphs capturing what communities and frontline voices are expressing about: {query}
Focus on: direct testimony, grievances, lived experience, community radio accounts, and field interviews.
Cite inline as [Source N]. Be factual — only use what the sources say.
If sources are insufficient, say so plainly.""",

    "official": """You are analysing official records and government documentation.
Using ONLY the sources below, write 2-3 paragraphs summarising what official sources show about: {query}
Focus on: scheme data, government orders, dashboards, budget documents, district plans, and parliamentary material.
Cite inline as [Source N]. Be factual — only use what the sources say.
If sources are insufficient, say so plainly.""",
}

_GAPS_PROMPT = """You are a research analyst reviewing evidence from multiple source types about: {query}

Below are summaries from different knowledge systems:

NEWS & MEDIA:
{news}

DEVELOPMENT & INSTITUTIONAL:
{development}

COMMUNITY VOICES:
{community}

OFFICIAL RECORD:
{official}

Write a structured analysis with exactly these four sections. Be concise — 2-3 sentences each.

WHERE THEY OVERLAP:
[What all or most sources agree on]

WHERE THEY DIVERGE:
[Where official records and ground-level reporting contradict each other]

WHAT IS MISSING OR UNCLEAR:
[Gaps, absences, things no source addresses, politically sensitive silences]

WHAT TO INVESTIGATE NEXT:
[Specific lines of inquiry, data to request, people to speak to, places to visit]"""


def synthesise_layer(
    query: str,
    sources: List[Dict[str, Any]],
    layer: str,
    model: str,
    *,
    timeout: Optional[float] = None,
) -> str:
    if not sources:
        return f"No {SOURCE_TYPES.get(layer, layer)} sources found for this query."
    context = build_context(sources, limit=5)
    guardrail = """Strict evidence rules:
- Use ONLY the sources below.
- Source authors/writers in citation metadata are not story actors.
- Do not describe a journalist, byline writer, publication, or archive record as an actor in the reported event unless the reported passage explicitly says they are involved.
- Do not combine people, events, victims, places, or dates across different sources.
- If a source says one person protested and another person was harmed, keep those people separate.
- Every concrete claim about an incident, person, institution, or date must cite [Source N].
- If the sources do not prove a claim, write that the available sources do not establish it."""
    prompt  = _LAYER_PROMPTS[layer].format(query=query) + f"\n\n{guardrail}\n\nSources:\n{context}\n\nAnalysis:"
    return call_ollama(prompt, model, max_tokens=MAX_TOKENS_LAYER, timeout=timeout)


def synthesise_layered_fast(
    query: str,
    layer_sources: Dict[str, List[Dict[str, Any]]],
    all_sources: List[Dict[str, Any]],
    model: str,
    *,
    timeout: Optional[float] = None,
) -> tuple:
    """All evidence layers + gaps in one Ollama call (avoids 5+ minute serial waits)."""
    def compact_layer_context(srcs: List[Dict[str, Any]]) -> str:
        lines = []
        for i, src in enumerate(srcs[:1], 1):
            title = src.get("title") or src.get("publication") or f"Source {i}"
            date = f", {src.get('date')[:10]}" if src.get("date") else ""
            excerpt = re.sub(r"\s+", " ", (src.get("excerpt") or "").strip())
            if len(excerpt) > 220:
                excerpt = excerpt[:217].rstrip() + "..."
            lines.append(f"[Source {i}] {title}{date}: {excerpt}")
        return "\n".join(lines) if lines else "(no layer-specific sources)"

    blocks: List[str] = []
    for ltype in SOURCE_TYPES:
        srcs = layer_sources.get(ltype) or all_sources[:1]
        label = SOURCE_TYPES[ltype]
        blocks.append(f"{ltype} / {label}: {compact_layer_context(srcs)}")

    prompt = f"""You are CommonSource, a research assistant for Indian community media archives.

Topic: {query}

Using ONLY the short excerpts below, write concise evidence-layer summaries.

Rules:
- Cite inline as [Source N].
- One sentence per layer.
- Keep gaps to one sentence.

Return exactly five labeled lines:
news: ...
development: ...
community: ...
official: ...
gaps: ...

{chr(10).join(blocks)}

Answer:"""
    raw = call_ollama(prompt, model, max_tokens=260, timeout=timeout)
    try:
        try:
            data = extract_json_value(raw)
            layer_texts = {
                ltype: str(data.get(ltype, "")).strip() or f"No {SOURCE_TYPES[ltype]} summary available."
                for ltype in SOURCE_TYPES
            }
            gaps = str(data.get("gaps", "")).strip() or "Gap analysis not returned by the model."
        except Exception:
            parsed: Dict[str, str] = {}
            for key in [*SOURCE_TYPES.keys(), "gaps"]:
                match = re.search(
                    rf"(?im)^\s*{re.escape(key)}\s*:\s*(.*?)(?=^\s*(?:news|development|community|official|gaps)\s*:|\Z)",
                    raw,
                    flags=re.DOTALL,
                )
                parsed[key] = clean_generation_response(match.group(1)).strip() if match else ""
            layer_texts = {
                ltype: parsed.get(ltype) or f"No {SOURCE_TYPES[ltype]} summary returned by the model."
                for ltype in SOURCE_TYPES
            }
            gaps = parsed.get("gaps") or "Gap analysis not returned by the model."
        if any(layer_texts.get(l) for l in SOURCE_TYPES):
            return layer_texts, gaps
    except Exception as exc:
        log.warning("Could not parse layered JSON; falling back to source summaries: %s", exc)

    layer_texts = {
        ltype: (
            f"{len(layer_sources.get(ltype) or [])} source(s) matched for this layer. "
            "The configured model could not summarise this layer; confirm Ollama is running and the model is installed."
        )
        for ltype in SOURCE_TYPES
    }
    return layer_texts, "Could not generate gap analysis. Try again after confirming the configured Ollama model is installed."


def synthesise_gaps(
    query: str,
    layers: Dict[str, str],
    model: str,
    *,
    timeout: Optional[float] = None,
) -> str:
    prompt = _GAPS_PROMPT.format(
        query=query,
        news=layers.get("news") or "No sources.",
        development=layers.get("development") or "No sources.",
        community=layers.get("community") or "No sources.",
        official=layers.get("official") or "No sources.",
    )
    return call_ollama(prompt, model, max_tokens=MAX_TOKENS_GAPS, timeout=timeout)


def story_arc(query: str, sources: List[Dict[str, Any]], model: str, *, timeout: Optional[float] = None) -> str:
    """Generate a chronological narrative of how coverage evolved."""
    # Sort sources by date
    dated = sorted(
        [s for s in sources if s.get("date")],
        key=lambda x: x["date"]
    )
    if not dated:
        dated = sources

    context = build_context(dated, limit=5)
    years   = sorted({s["date"][:4] for s in dated if s.get("date")})
    yr_str  = f"{years[0]}–{years[-1]}" if len(years) > 1 else years[0] if years else "unknown"

    prompt = f"""You are a senior journalist reviewing {yr_str} coverage of "{query}" from the CommonSource archive.
The sources below are ordered chronologically. Write 2 concise paragraphs tracing how this story evolved over time.
Each paragraph should cover a distinct phase or shift in the story. Weave the sources together — do not summarise each one separately.
Use transitions like "Initially...", "By [year]...", "As the story developed...". Cite inline as [Source N].
Be factual — only use what the sources say. End with a sentence on what the arc of coverage reveals.

Sources (chronological):
{context}

Narrative:"""

    return call_ollama(prompt, model, max_tokens=450, timeout=timeout)


# Embeddings: app/embed.py (embed_query aliased as embed above)

# ── Query helpers ─────────────────────────────────────────────────────────────

_STOPWORDS = {
    "tell", "me", "about", "what", "is", "are", "was", "were", "how", "who",
    "when", "where", "why", "which", "the", "a", "an", "in", "of", "and",
    "or", "to", "for", "do", "did", "has", "have", "had", "its", "it",
    "this", "that", "these", "those", "on", "at", "by", "from", "with",
    "give", "explain", "describe", "list", "find", "show", "get",
}


def extract_keywords(query: str) -> List[str]:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", query.lower())
    return [w for w in words if w not in _STOPWORDS]


def _row_value(row: Any, key: str, default: str = "") -> str:
    try:
        return row[key] or default
    except Exception:
        if isinstance(row, dict):
            return row.get(key) or default
    return default


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"\b[a-zA-Z0-9][a-zA-Z0-9'-]*\b", (text or "").lower()))


def keyword_score(chunk_text: str, keywords: List[str]) -> float:
    """Fraction of query keywords present as words in a text field."""
    if not keywords:
        return 0.0
    words = _word_set(chunk_text)
    hits = sum(1 for kw in keywords if kw in words or kw.rstrip("s") in words)
    return hits / len(keywords)


def lexical_score(row: Any, query: str, keywords: List[str]) -> float:
    """Weighted keyword match across fields, with titles and places boosted."""
    if not keywords:
        return 0.0

    title = _row_value(row, "article_title")
    body = _row_value(row, "chunk_text")
    location = _row_value(row, "location")
    author = _row_value(row, "author")
    publication = _row_value(row, "publication")
    source_type = _row_value(row, "source_type")
    content_type = _row_value(row, "content_type")
    source_family = _row_value(row, "source_family")
    source_medium = _row_value(row, "source_medium")
    source_origin = _row_value(row, "source_origin")
    theme = _row_value(row, "theme")

    title_score = keyword_score(title, keywords)
    body_score = keyword_score(body, keywords)
    location_score = keyword_score(location, keywords)
    author_score = keyword_score(author, keywords)
    publication_score = keyword_score(publication, keywords)
    source_meta_score = keyword_score(
        " ".join([source_type, content_type, source_family, source_medium, source_origin, theme]),
        keywords,
    )

    q = query.lower().strip()
    phrase_bonus = 0.0
    if len(q) >= 4:
        if q in title.lower():
            phrase_bonus += 0.18
        if q in theme.lower():
            phrase_bonus += 0.16
        if q in location.lower():
            phrase_bonus += 0.12
        if q in body.lower():
            phrase_bonus += 0.08

    score = (
        0.36 * title_score +
        0.34 * body_score +
        0.16 * location_score +
        0.06 * author_score +
        0.05 * publication_score +
        0.10 * source_meta_score +
        phrase_bonus
    )
    return min(score, 1.0)


def keyword_match_count(row: Any, keywords: List[str]) -> int:
    if not keywords:
        return 0
    combined = " ".join([
        _row_value(row, "article_title"),
        _row_value(row, "chunk_text"),
        _row_value(row, "location"),
        _row_value(row, "author"),
        _row_value(row, "publication"),
        _row_value(row, "source_type"),
        _row_value(row, "content_type"),
        _row_value(row, "source_family"),
        _row_value(row, "source_medium"),
        _row_value(row, "source_origin"),
        _row_value(row, "theme"),
    ])
    words = _word_set(combined)
    return sum(1 for kw in keywords if kw in words or kw.rstrip("s") in words)


def relevance_score(row: Any, query_vec: List[float], query: str, keywords: List[str]) -> float:
    """Combine semantic recall with stronger keyword/source-field precision."""
    vec = unpack_blob(row["embedding_blob"])
    semantic = cosine(query_vec, vec)
    lexical = lexical_score(row, query, keywords)

    if keywords:
        if len(keywords) >= 2:
            required = min(2, len(keywords))
            if keyword_match_count(row, keywords) < required:
                return 0.0
        # Exact local/source matches should lead. Semantic search still helps recall.
        score = 0.45 * semantic + 0.55 * lexical
        if lexical == 0 and semantic < 0.55:
            score *= 0.65
    else:
        score = semantic

    return min(score, 1.0)


def score_row(row: Any, query_vec: Optional[List[float]], query: str, keywords: List[str]) -> float:
    """Use semantic+lexical scoring when embeddings work, otherwise keyword scoring."""
    if query_vec:
        return relevance_score(row, query_vec, query, keywords)
    return lexical_score(row, query, keywords)


def candidate_filter_sql(
    query_vec: Optional[List[float]],
    keywords: List[str],
    extra_conditions: Optional[List[str]] = None,
    fallback_limit: int = 2500,
) -> tuple[str, List[str], str]:
    conditions = list(extra_conditions or [])
    conditions.append("LENGTH(kc.chunk_text) > 80")
    params: List[str] = []

    if query_vec:
        conditions.append("kc.embedding_blob IS NOT NULL")

    keyword_terms = keywords[:6]
    if keyword_terms:
        fields = [
            "kc.chunk_text",
            "cs.article_title",
            "cs.location",
            "cs.author",
            "cs.publication",
            "cs.source_type",
            "cs.content_type",
            "cs.source_family",
            "cs.source_medium",
            "cs.source_origin",
            "cs.theme",
        ]
        keyword_clauses = []
        for keyword in keyword_terms:
            like = f"%{keyword}%"
            keyword_clauses.append("(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")")
            params.extend([like] * len(fields))
        conditions.append("(" + " OR ".join(keyword_clauses) + ")")

    # Cap candidates — scoring tens of thousands of blobs blocks the server.
    limit_sql = f"LIMIT {fallback_limit}"

    return "WHERE " + "\n          AND ".join(conditions), params, limit_sql


def result_group_key(row: Dict[str, Any]) -> str:
    publication = (row.get("publication") or "Unknown").strip()
    source_family = (row.get("source_family") or "").strip()
    theme = (row.get("theme") or "").strip()
    content_type = (row.get("content_type") or "").strip()
    if theme:
        return f"{source_family or publication}|theme:{theme}"
    if content_type:
        return f"{source_family or publication}|type:{content_type}"
    return f"{source_family or publication}|unthemed"


def select_diverse_results(scored: List[tuple], top_k: int, min_score: float = 0.25) -> List[tuple]:
    """Prefer relevant results while avoiding one archive/theme crowding out nearby sources."""
    selected: List[tuple] = []
    selected_ids: set[str] = set()
    group_counts: Dict[str, int] = {}
    publication_counts: Dict[str, int] = {}

    def consider(score: float, row: Dict[str, Any], *, group_cap: Optional[int], publication_cap: Optional[int]) -> None:
        if len(selected) >= top_k:
            return
        aid = row["asset_id"]
        if aid in selected_ids or score < min_score:
            return
        group = result_group_key(row)
        publication = row.get("publication") or "Unknown"
        if group_cap is not None and group_counts.get(group, 0) >= group_cap:
            return
        if publication_cap is not None and publication_counts.get(publication, 0) >= publication_cap:
            return
        selected.append((score, row))
        selected_ids.add(aid)
        group_counts[group] = group_counts.get(group, 0) + 1
        publication_counts[publication] = publication_counts.get(publication, 0) + 1

    for score, row in scored:
        consider(score, row, group_cap=3, publication_cap=max(4, top_k // 2))
    if len(selected) < top_k:
        for score, row in scored:
            consider(score, row, group_cap=5, publication_cap=None)
    if len(selected) < top_k:
        for score, row in scored:
            consider(score, row, group_cap=None, publication_cap=None)

    return selected


# ── Similarity ────────────────────────────────────────────────────────────────

_BOILERPLATE_RE = re.compile(
    r"^\s*(share|tweet|pin|email|print|post script|[\s\n|/\\·•]+)\s*$",
    re.IGNORECASE,
)


def is_boilerplate(text: str) -> bool:
    """Return True for social-share buttons, OCR/binary damage, fragments, etc."""
    stripped = text.strip()
    if len(stripped) < 40:
        return True
    control_chars = sum(1 for ch in stripped if ord(ch) < 32 and ch not in "\n\r\t")
    if control_chars:
        return True
    alnum_chars = sum(1 for ch in stripped if ch.isalnum())
    if alnum_chars < 30 or (alnum_chars / max(len(stripped), 1)) < 0.25:
        return True
    # Chunks that are exclusively social-share words separated by whitespace
    if _BOILERPLATE_RE.match(stripped):
        return True
    tokens = re.split(r"[\s\n]+", stripped)
    if all(t.lower() in {"share", "tweet", "pin", "email", "print", ""} for t in tokens):
        return True
    return False


def build_excerpt(text: str, keywords: List[str], max_chars: int = 400) -> str:
    """Return a query-centered excerpt when possible."""
    stripped = (text or "").strip()
    if not stripped:
        return ""

    lower = stripped.lower()
    positions = [lower.find(kw) for kw in keywords if kw and lower.find(kw) >= 0]
    if positions:
        first = min(positions)
        start = max(0, first - 130)
        sentence_start = max(stripped.rfind(".", 0, start), stripped.rfind("\n", 0, start))
        if sentence_start > 0 and first - sentence_start < max_chars:
            start = sentence_start + 1
        excerpt = stripped[start:start + max_chars].strip()
        if start > 0:
            excerpt = "..." + excerpt
        if start + max_chars < len(stripped):
            excerpt = excerpt.rstrip() + "..."
        return excerpt

    excerpt = stripped[:max_chars]
    last_period = excerpt.rfind(".")
    if last_period > 180:
        excerpt = excerpt[: last_period + 1]
    return excerpt


def cosine(a: List[float], b: List[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def unpack_blob(blob: bytes) -> List[float]:
    n = len(blob) // 8
    return list(struct.unpack(f"{n}d", blob))


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


_phase2a_schema_ready = False
_phase2a_schema_lock = __import__("threading").Lock()
_login_rate_lock = __import__("threading").Lock()
_login_rate_attempts: Dict[str, List[float]] = {}
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("COMMONSOURCE_LOGIN_RATE_LIMIT_WINDOW_SECONDS", "60"))
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("COMMONSOURCE_LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "8"))


def ensure_phase2a_schema() -> list[str]:
    """Apply additive Phase-2A migrations once per process."""
    global _phase2a_schema_ready
    if _phase2a_schema_ready:
        return []
    with _phase2a_schema_lock:
        if _phase2a_schema_ready:
            return []
        conn = get_conn()
        try:
            applied = run_migrations(conn, MIGRATIONS_DIR)
            if applied:
                log.info("[MIGRATION] Applied Phase-2A migrations: %s", ", ".join(applied))
            _phase2a_schema_ready = True
            return applied
        finally:
            conn.close()


def client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr or ""


def auth_error_response(exc: Exception, fallback_status: int = 500):
    status = getattr(exc, "status_code", fallback_status)
    log.warning("[AUTH] %s", exc)
    return jsonify({"error": str(exc)}), status


def login_rate_key(email: str) -> str:
    return f"{client_ip()}:{email.strip().lower()}"


def login_rate_limited(email: str) -> bool:
    now = time.time()
    key = login_rate_key(email)
    with _login_rate_lock:
        recent = [
            ts for ts in _login_rate_attempts.get(key, [])
            if now - ts < LOGIN_RATE_LIMIT_WINDOW_SECONDS
        ]
        _login_rate_attempts[key] = recent
        return len(recent) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS


def record_login_rate_failure(email: str) -> None:
    key = login_rate_key(email)
    with _login_rate_lock:
        _login_rate_attempts.setdefault(key, []).append(time.time())


def clear_login_rate(email: str) -> None:
    key = login_rate_key(email)
    with _login_rate_lock:
        _login_rate_attempts.pop(key, None)


def bearer_token() -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


def load_current_user(required: bool = True) -> Optional[Dict[str, Any]]:
    ensure_phase2a_schema()
    token = bearer_token()
    if not token:
        if required:
            raise AuthError("Authentication required", 401)
        return None
    payload = decode_access_token(token)
    conn = get_conn()
    try:
        if is_access_token_revoked(conn, payload.get("jti", "")):
            raise AuthError("Token revoked", 401)
        user = get_user_by_id(conn, payload.get("sub", ""))
        if not user:
            raise AuthError("User not found", 401)
        if not user.get("is_active"):
            raise AuthError("Account is inactive", 403)
        g.current_user = user
        return user
    finally:
        conn.close()


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            load_current_user(required=True)
        except (AuthError, AuthConfigError) as exc:
            return auth_error_response(exc)
        except Exception:
            log.exception("[AUTH] Token validation failed")
            return jsonify({"error": "Authentication failed"}), 401
        return fn(*args, **kwargs)
    return wrapper


def require_roles(*allowed_roles: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                user = load_current_user(required=True)
                if not role_allows(user["role"], allowed_roles):
                    return jsonify({"error": "Insufficient role permissions"}), 403
            except (AuthError, AuthConfigError) as exc:
                return auth_error_response(exc)
            except Exception:
                log.exception("[AUTH] Role validation failed")
                return jsonify({"error": "Authorization failed"}), 401
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_csrf(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            user = getattr(g, "current_user", None) or load_current_user(required=True)
            token = request.headers.get("X-CSRF-Token", "")
            if not validate_csrf_token(token, user["id"]):
                return jsonify({"error": "CSRF token is missing or invalid"}), 403
        except (AuthError, AuthConfigError) as exc:
            return auth_error_response(exc)
        except Exception:
            log.exception("[AUTH] CSRF validation failed")
            return jsonify({"error": "CSRF validation failed"}), 403
        return fn(*args, **kwargs)
    return wrapper


def can_manage_publisher(pub: sqlite3.Row | Dict[str, Any]) -> bool:
    user = getattr(g, "current_user", None)
    if not user:
        return False
    if user["role"] in ADMIN_ROLES:
        return True
    if user["role"] == "publisher":
        return (pub["contact_email"] or "").strip().lower() == (user["email"] or "").strip().lower()
    return False


def record_audit(
    conn: sqlite3.Connection,
    action: str,
    resource_type: str,
    resource_id: str = "",
    *,
    user_id: Optional[str] = None,
) -> None:
    user = getattr(g, "current_user", None) or {}
    actor_id = user_id if user_id is not None else user.get("id")
    conn.execute(
        """
        INSERT INTO audit_logs
          (id, user_id, action, resource_type, resource_id, timestamp, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("audit"),
            actor_id,
            action,
            resource_type,
            resource_id,
            utc_now(),
            client_ip(),
        ),
    )


def record_audit_event(action: str, resource_type: str, resource_id: str = "") -> None:
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        record_audit(conn, action, resource_type, resource_id)
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("[AUDIT] Could not record action=%s resource_type=%s", action, resource_type)
        raise
    finally:
        conn.close()


def parse_optional_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "active"}:
        return True
    if text in {"0", "false", "no", "inactive"}:
        return False
    raise ValueError("Expected a boolean value")


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone())


def current_user_publisher(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    user = getattr(g, "current_user", None)
    if not user:
        return None
    return conn.execute(
        "SELECT * FROM publishers WHERE lower(contact_email) = lower(?) ORDER BY created_at DESC LIMIT 1",
        (user["email"],),
    ).fetchone()


def ensure_publisher_profile_for_user(
    conn: sqlite3.Connection,
    user: Dict[str, Any],
    *,
    organization_name: str,
    website: str = "",
    description: str = "",
    verification_status: str = "pending",
) -> Dict[str, Any]:
    now = utc_now()
    row = conn.execute("SELECT * FROM publisher_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    if row:
        conn.execute(
            """
            UPDATE publisher_profiles
            SET organization_name = ?, website = ?, description = COALESCE(NULLIF(?, ''), description),
                verification_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (organization_name, website, description, verification_status, now, row["id"]),
        )
        return dict(conn.execute("SELECT * FROM publisher_profiles WHERE id = ?", (row["id"],)).fetchone())
    profile_id = make_id("pp")
    conn.execute(
        """
        INSERT INTO publisher_profiles
          (id, user_id, organization_name, description, website, logo_url, languages, topics,
           coverage_regions, verification_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, '', '', '', '', ?, ?, ?)
        """,
        (profile_id, user["id"], organization_name, description, website, verification_status, now, now),
    )
    return dict(conn.execute("SELECT * FROM publisher_profiles WHERE id = ?", (profile_id,)).fetchone())


def ensure_legacy_publisher_for_profile(
    conn: sqlite3.Connection,
    user: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM publishers WHERE lower(contact_email) = lower(?) OR lower(name) = lower(?) LIMIT 1",
        (user["email"], profile["organization_name"]),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE publishers
            SET name = ?, contact_email = ?, status = 'approved'
            WHERE id = ?
            """,
            (profile["organization_name"], user["email"], existing["id"]),
        )
        return dict(conn.execute("SELECT * FROM publishers WHERE id = ?", (existing["id"],)).fetchone())
    pub_id = make_id("pub")
    conn.execute(
        """
        INSERT INTO publishers
          (id, name, geography, language, contact_email, storage_mode, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'federated', 'approved', ?)
        """,
        (
            pub_id,
            profile["organization_name"],
            profile.get("coverage_regions") or "",
            (profile.get("languages") or "en").split(",")[0].strip() or "en",
            user["email"],
            utc_now(),
        ),
    )
    return dict(conn.execute("SELECT * FROM publishers WHERE id = ?", (pub_id,)).fetchone())


def chunk_text(text: str, *, max_chars: int = 900, overlap: int = 120) -> List[str]:
    """Split uploaded text into retrieval-sized chunks without changing schema."""
    clean = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    chunks: List[str] = []
    start = 0
    while start < len(clean):
        end = min(start + max_chars, len(clean))
        if end < len(clean):
            candidates = [
                clean.rfind("\n\n", start, end),
                clean.rfind(". ", start, end),
                clean.rfind(" ", start, end),
            ]
            split_at = max(candidates)
            if split_at > start + max_chars // 2:
                end = split_at + 1
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(0, end - overlap)
    return chunks


def embed_text(chunk: str) -> tuple[Optional[bytes], Optional[List[float]]]:
    """Return a SQLite embedding blob and vector for optional Qdrant indexing."""
    try:
        vec = embed(chunk)
        if not vec:
            return None, None
        return embedding_to_blob(vec), vec
    except Exception as exc:
        log.warning("Upload chunk embedding failed: %s", exc)
        return None, None


def source_path_for_response(source_path: str) -> str:
    if not source_path:
        return ""
    path = Path(source_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return str(path) if path.exists() else ""


def build_source_result(row: Dict[str, Any], score: float, excerpt: str) -> Dict[str, Any]:
    aid = row["asset_id"]
    original_url = row.get("article_url") or ""
    archive_path = source_path_for_response(row.get("source_path") or "")
    return {
        "asset_id":      aid,
        "score":         round(score, 4),
        "publication":   row.get("publication") or "Unknown",
        "author":        row.get("author") or "",
        "date":          row.get("date_published") or "",
        "location":      row.get("location") or "",
        "title":         row.get("article_title") or "",
        "excerpt":       excerpt,
        "url":           original_url,
        "archive_url":   f"/api/source/{aid}" if archive_path else "",
        "link_label":    "Open original source" if original_url else ("Open archive file" if archive_path else ""),
        "source_type":   row.get("source_type") or "news",
        "content_type":  row.get("content_type") or "",
        "source_family": row.get("source_family") or "",
        "source_medium": row.get("source_medium") or "",
        "source_origin": row.get("source_origin") or "",
        "theme":         row.get("theme") or "",
    }


def resolve_tag_ref(conn: sqlite3.Connection, tag_ref: str) -> Optional[sqlite3.Row]:
    if not tag_ref or not table_exists(conn, "tags"):
        return None
    try:
        from knowledge_layer import slugify
        slug = slugify(tag_ref)
    except Exception:
        slug = re.sub(r"[^a-z0-9]+", "-", tag_ref.lower()).strip("-")
    return conn.execute(
        "SELECT * FROM tags WHERE id = ? OR slug = ? OR lower(name) = lower(?) LIMIT 1",
        (tag_ref, slug, tag_ref),
    ).fetchone()


def resolve_entity_ref(conn: sqlite3.Connection, entity_ref: str) -> Optional[sqlite3.Row]:
    if not entity_ref or not table_exists(conn, "entities"):
        return None
    return conn.execute(
        """
        SELECT * FROM entities
        WHERE id = ? OR lower(canonical_name) = lower(?) OR lower(name) = lower(?)
        LIMIT 1
        """,
        (entity_ref, entity_ref, entity_ref),
    ).fetchone()


def article_card_rows(conn: sqlite3.Connection, where_sql: str, params: List[Any], *, limit: int, offset: int = 0) -> List[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT DISTINCT
          cs.asset_id,
          cs.publication,
          cs.author,
          cs.date_published,
          cs.location,
          cs.article_title,
          cs.article_url,
          cs.source_type,
          cs.content_type,
          cs.source_family,
          cs.source_medium,
          cs.source_origin,
          cs.theme,
          ka.source_path,
          ka.raw_text,
          ka.created_at
        FROM commonsource_articles cs
        JOIN knowledge_assets ka ON ka.id = cs.asset_id
        {where_sql}
        ORDER BY ka.created_at DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()


def article_result_from_row(row: sqlite3.Row | Dict[str, Any], query: str, score: float) -> Dict[str, Any]:
    data = dict(row)
    keywords = extract_keywords(query) if query else []
    excerpt = build_excerpt(data.get("raw_text") or data.get("article_title") or "", keywords)
    return build_source_result(data, score, excerpt)


def knowledge_filter_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    tag: str = "",
    entity: str = "",
    publisher: str = "",
    topic: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    joins: List[str] = []
    where: List[str] = []
    params: List[Any] = []
    if tag:
        tag_row = resolve_tag_ref(conn, tag)
        if not tag_row:
            return {"results": [], "count": 0}
        joins.append("JOIN article_tags at_filter ON at_filter.article_id = cs.asset_id")
        where.append("at_filter.tag_id = ?")
        params.append(tag_row["id"])
    if entity:
        entity_row = resolve_entity_ref(conn, entity)
        if not entity_row:
            return {"results": [], "count": 0}
        joins.append("JOIN article_entities ae_filter ON ae_filter.article_id = cs.asset_id")
        where.append("ae_filter.entity_id = ?")
        params.append(entity_row["id"])
    if publisher:
        where.append("lower(cs.publication) = lower(?)")
        params.append(publisher)
    if topic:
        try:
            from knowledge_layer import slugify
            topic_slug = slugify(topic)
        except Exception:
            topic_slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
        joins.append("LEFT JOIN article_tags at_topic ON at_topic.article_id = cs.asset_id")
        joins.append("LEFT JOIN tags t_topic ON t_topic.id = at_topic.tag_id")
        where.append("(lower(cs.theme) LIKE ? OR t_topic.slug = ? OR lower(t_topic.name) = lower(?))")
        params.extend([f"%{topic.lower()}%", topic_slug, topic])
    if query:
        keywords = extract_keywords(query)
        if keywords:
            terms_sql = []
            for kw in keywords[:4]:
                terms_sql.append("(lower(cs.article_title) LIKE ? OR lower(ka.raw_text) LIKE ?)")
                params.extend([f"%{kw.lower()}%", f"%{kw.lower()}%"])
            where.append("(" + " OR ".join(terms_sql) + ")")
    join_sql = "\n".join(dict.fromkeys(joins))
    where_sql = f"{join_sql} WHERE {' AND '.join(where)}" if where else join_sql
    rows = article_card_rows(conn, where_sql, params, limit=limit)
    results = [article_result_from_row(row, query, max(0.25, 1.0 - idx * 0.03)) for idx, row in enumerate(rows)]
    return {"results": results, "count": len(results)}


def related_articles_for_asset(conn: sqlite3.Connection, asset_id: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    if not table_exists(conn, "article_tags") or not table_exists(conn, "article_entities"):
        return []
    rows = conn.execute(
        """
        WITH source_tags AS (
          SELECT tag_id FROM article_tags WHERE article_id = ?
        ),
        source_entities AS (
          SELECT entity_id FROM article_entities WHERE article_id = ?
        ),
        tag_scores AS (
          SELECT article_id, COUNT(*) * 1.0 AS tag_score
          FROM article_tags
          WHERE tag_id IN (SELECT tag_id FROM source_tags) AND article_id != ?
          GROUP BY article_id
        ),
        entity_scores AS (
          SELECT article_id, COUNT(*) * 1.4 AS entity_score
          FROM article_entities
          WHERE entity_id IN (SELECT entity_id FROM source_entities) AND article_id != ?
          GROUP BY article_id
        )
        SELECT
          cs.asset_id,
          cs.publication,
          cs.author,
          cs.date_published,
          cs.location,
          cs.article_title,
          cs.article_url,
          cs.source_type,
          cs.content_type,
          cs.source_family,
          cs.source_medium,
          cs.source_origin,
          cs.theme,
          ka.source_path,
          ka.raw_text,
          COALESCE(ts.tag_score, 0) + COALESCE(es.entity_score, 0) AS related_score
        FROM commonsource_articles cs
        JOIN knowledge_assets ka ON ka.id = cs.asset_id
        LEFT JOIN tag_scores ts ON ts.article_id = cs.asset_id
        LEFT JOIN entity_scores es ON es.article_id = cs.asset_id
        WHERE cs.asset_id != ? AND (COALESCE(ts.tag_score, 0) + COALESCE(es.entity_score, 0)) > 0
        ORDER BY related_score DESC, ka.created_at DESC
        LIMIT ?
        """,
        (asset_id, asset_id, asset_id, asset_id, asset_id, limit),
    ).fetchall()
    return [article_result_from_row(row, "", float(row["related_score"])) for row in rows]



def _citation_clean(value):
    val_str = str(value if value else '').strip()
    return re.sub(r'\s+', ' ', val_str).strip(' .')

def _citation_year(value):
    match = re.search(r'\b(19|20)\d{2}\b', value if value else '')
    if match:
        return match.group(0)
    return 'n.d.'

def _citation_date(value):
    if not value:
        return 'n.d.'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except Exception:
        parts = str(value).split('T')
        return parts[0] if parts[0] else 'n.d.'

def generate_document_citations(doc):
    source_detail = doc.get('source_detail') or {}
    title = _citation_clean(doc.get('title'))
    if not title:
        title = 'Untitled'

    author = _citation_clean(source_detail.get('author'))
    if not author:
        author = _citation_clean(source_detail.get('publication'))

    source = _citation_clean(doc.get('source'))
    if not source:
        source = 'CommonSource Archive'

    date_value = doc.get('upload_date') or ''
    year = _citation_year(str(date_value))
    date_text = _citation_date(str(date_value))

    url = _citation_clean(source_detail.get('url'))
    if not url:
        url = f"{request.host_url.rstrip('/')}/document/{doc.get('document_id')}"

    apa_author = author if author else source
    mla_author = f"{author}. " if author else ''
    chicago_author = f"{author}. " if author else ''

    apa = f"{apa_author}. ({year}). {title}. {source}. {url}"
    mla = f'{mla_author}"{title}." {source}, {date_text}, {url}.'
    chicago = f'{chicago_author}"{title}." {source}. {date_text}. {url}.'

    return {
        'apa': apa,
        'mla': mla,
        'chicago': chicago
    }

def citation_payload_for_document(conn, document_id):
    try:
        from knowledge_layer import ensure_knowledge_tables
        ensure_knowledge_tables(conn)
    except Exception:
        pass
    cached = None
    if table_exists(conn, 'document_citations'):
        cached = conn.execute('SELECT apa, mla, chicago, updated_at FROM document_citations WHERE document_id = ?', (document_id,)).fetchone()
    if cached and cached['apa'] and cached['mla'] and cached['chicago']:
        return {
            'document_id': document_id,
            'citations': {
                'apa': cached['apa'],
                'mla': cached['mla'],
                'chicago': cached['chicago']
            },
            'updated_at': cached['updated_at']
        }
    doc = document_detail_payload(conn, document_id)
    if not doc:
        return None
    citations = generate_document_citations(doc)
    if table_exists(conn, 'document_citations'):
        conn.execute("""
            INSERT OR REPLACE INTO document_citations (document_id, apa, mla, chicago, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (document_id, citations['apa'], citations['mla'], citations['chicago'], utc_now()))
    return {
        'document_id': document_id,
        'citations': citations,
        'updated_at': utc_now()
    }

def document_exists(conn, document_id):
    return bool(conn.execute('SELECT 1 FROM knowledge_assets WHERE id = ? LIMIT 1', (document_id,)).fetchone())

def document_summaries_for_ids(conn, document_ids):
    ordered_ids = [doc_id for doc_id in document_ids if doc_id]
    if not ordered_ids:
        return []
    placeholders = ','.join('?' for _ in ordered_ids)
    query = f"""
        SELECT
          ka.id AS document_id,
          COALESCE(dm.title, cs.article_title, ka.title, ka.id) AS title,
          COALESCE(dm.language, cs.language, '') AS language,
          COALESCE(dm.document_type, cs.content_type, '') AS document_type,
          COALESCE(cs.publication, dm.source, 'CommonSource Archive') AS source,
          COALESCE(dm.import_date, cs.date_published, cs.created_at, ka.created_at, '') AS upload_date,
          (
            SELECT dc.category
            FROM document_categories dc
            WHERE dc.document_id = ka.id
            ORDER BY dc.confidence_score DESC, dc.category
            LIMIT 1
          ) AS category,
          (
            SELECT group_concat(tag, '|')
            FROM (
              SELECT dt.tag
              FROM document_tags dt
              WHERE dt.document_id = ka.id
              ORDER BY dt.confidence_score DESC, dt.tag
              LIMIT 8
            )
          ) AS tags,
          substr(COALESCE(ka.raw_text, ''), 1, 260) AS excerpt
        FROM knowledge_assets ka
        LEFT JOIN commonsource_articles cs ON cs.asset_id = ka.id
        LEFT JOIN document_metadata dm ON dm.document_id = ka.id
        WHERE ka.id IN ({placeholders})
    """
    rows = conn.execute(query, ordered_ids).fetchall()
    by_id = {row['document_id']: row for row in rows}
    summaries = []
    for document_id in ordered_ids:
        row = by_id.get(document_id)
        if not row:
            continue
        summaries.append({
            'document_id': row['document_id'],
            'title': row['title'] if row['title'] else 'Untitled',
            'category': row['category'] if row['category'] else '',
            'tags': [tag for tag in str(row['tags'] if row['tags'] else '').split('|') if tag],
            'language': row['language'] if row['language'] else '',
            'document_type': row['document_type'] if row['document_type'] else '',
            'source': row['source'] if row['source'] else 'CommonSource Archive',
            'upload_date': row['upload_date'] if row['upload_date'] else '',
            'excerpt': row['excerpt'] if row['excerpt'] else ''
        })
    return summaries

def bookmark_id_for_document(conn, user_id, document_id):
    row = conn.execute(
        'SELECT id FROM bookmarks WHERE user_id = ? AND document_id = ?',
        (user_id, document_id)
    ).fetchone()
    if row:
        return row['id']
    return ''

def embedding_scores_for_candidates(conn, asset_id, candidate_ids):
    if not candidate_ids:
        return {}
    try:
        source = conn.execute("""
            SELECT embedding_blob
            FROM knowledge_chunks
            WHERE asset_id = ? AND embedding_blob IS NOT NULL
            ORDER BY chunk_index ASC
            LIMIT 1
        """, (asset_id,)).fetchone()
        if not source or not source['embedding_blob']:
            return {}
        source_vec = unpack_blob(source['embedding_blob'])
    except Exception:
        return {}
    placeholders = ','.join('?' for _ in candidate_ids)
    query = f"""
        SELECT asset_id, embedding_blob
        FROM knowledge_chunks
        WHERE asset_id IN ({placeholders}) AND embedding_blob IS NOT NULL
        ORDER BY asset_id, chunk_index ASC
    """
    rows = conn.execute(query, candidate_ids).fetchall()
    scores = {}
    seen_counts = {}
    for row in rows:
        candidate_id = row['asset_id']
        if seen_counts.get(candidate_id, 0) >= 2:
            continue
        seen_counts[candidate_id] = seen_counts.get(candidate_id, 0) + 1
        try:
            score = cosine(source_vec, unpack_blob(row['embedding_blob']))
            scores[candidate_id] = max(scores.get(candidate_id, 0.0), score)
        except Exception:
            pass
    return scores

def related_documents_for_asset(conn, asset_id, limit=8):
    if not table_exists(conn, 'document_categories') or not table_exists(conn, 'document_tags'):
        return related_articles_for_asset(conn, asset_id, limit=limit)
    query = """
        WITH source_categories AS (
          SELECT category FROM document_categories WHERE document_id = ?
        ),
        source_tags AS (
          SELECT tag FROM document_tags WHERE document_id = ?
        ),
        category_scores AS (
          SELECT document_id, COUNT(*) * 0.65 AS score
          FROM document_categories
          WHERE category IN (SELECT category FROM source_categories) AND document_id != ?
          GROUP BY document_id
        ),
        tag_scores AS (
          SELECT document_id, COUNT(*) * 0.35 AS score
          FROM document_tags
          WHERE tag IN (SELECT tag FROM source_tags) AND document_id != ?
          GROUP BY document_id
        ),
        combined AS (
          SELECT document_id, score FROM category_scores
          UNION ALL
          SELECT document_id, score FROM tag_scores
        ),
        ranked AS (
          SELECT document_id, SUM(score) AS metadata_score
          FROM combined
          GROUP BY document_id
          ORDER BY metadata_score DESC
          LIMIT 24
        )
        SELECT
          ranked.document_id,
          ranked.metadata_score,
          COALESCE(dm.title, cs.article_title, ka.title, ranked.document_id) AS title,
          (
            SELECT dc.category
            FROM document_categories dc
            WHERE dc.document_id = ranked.document_id
            ORDER BY dc.confidence_score DESC, dc.category
            LIMIT 1
          ) AS category
        FROM ranked
        JOIN knowledge_assets ka ON ka.id = ranked.document_id
        LEFT JOIN commonsource_articles cs ON cs.asset_id = ranked.document_id
        LEFT JOIN document_metadata dm ON dm.document_id = ranked.document_id
        ORDER BY ranked.metadata_score DESC, ka.created_at DESC
    """
    rows = conn.execute(query, (asset_id, asset_id, asset_id, asset_id)).fetchall()
    candidate_ids = [row['document_id'] for row in rows]
    embedding_scores = embedding_scores_for_candidates(conn, asset_id, candidate_ids)
    related = []
    for row in rows:
        embedding_score = max(0.0, embedding_scores.get(row['document_id'], 0.0))
        metadata_score = float(row['metadata_score'] if row['metadata_score'] else 0)
        relevance = metadata_score + embedding_score * 0.8
        related.append({
            'document_id': row['document_id'],
            'title': row['title'] if row['title'] else 'Untitled',
            'category': row['category'] if row['category'] else '',
            'relevance_score': round(relevance, 3)
        })
    related.sort(key=lambda item: item['relevance_score'], reverse=True)
    if related:
        return related[:limit]

    result = []
    for item in related_articles_for_asset(conn, asset_id, limit=limit):
        result.append({
            'document_id': item.get('asset_id'),
            'title': item.get('title') if item.get('title') else 'Untitled',
            'category': item.get('category') if item.get('category') else '',
            'relevance_score': item.get('score') if item.get('score') else 0
        })
    return result

def recommend_documents_for_user(conn, user_id, limit, exclude_document_id):
    params = [user_id, user_id, user_id, exclude_document_id, limit * 4]
    query1 = """
        WITH user_docs AS (
          SELECT document_id FROM bookmarks WHERE user_id = ?
          UNION
          SELECT cd.document_id
          FROM collection_documents cd
          JOIN collections c ON c.id = cd.collection_id
          WHERE c.user_id = ?
          UNION
          SELECT document_id FROM reading_history WHERE user_id = ?
        ),
        user_categories AS (
          SELECT DISTINCT category FROM document_categories
          WHERE document_id IN (SELECT document_id FROM user_docs)
        ),
        user_tags AS (
          SELECT DISTINCT tag FROM document_tags
          WHERE document_id IN (SELECT document_id FROM user_docs)
        ),
        category_scores AS (
          SELECT document_id, COUNT(*) * 0.65 AS score
          FROM document_categories
          WHERE category IN (SELECT category FROM user_categories)
          GROUP BY document_id
        ),
        tag_scores AS (
          SELECT document_id, COUNT(*) * 0.35 AS score
          FROM document_tags
          WHERE tag IN (SELECT tag FROM user_tags)
          GROUP BY document_id
        ),
        combined AS (
          SELECT document_id, score FROM category_scores
          UNION ALL
          SELECT document_id, score FROM tag_scores
        )
        SELECT ka.id AS document_id, COALESCE(SUM(combined.score), 0.1) AS relevance_score
        FROM knowledge_assets ka
        LEFT JOIN combined ON combined.document_id = ka.id
        WHERE ka.id NOT IN (SELECT document_id FROM user_docs)
          AND (? = '' OR ka.id != ?)
        GROUP BY ka.id
        HAVING relevance_score > 0.1
        ORDER BY relevance_score DESC, ka.created_at DESC
        LIMIT ?
    """
    rows = conn.execute(query1, [user_id, user_id, user_id, exclude_document_id, exclude_document_id, limit * 4]).fetchall()
    if not rows:
        query2 = """
            SELECT ka.id AS document_id, 0.1 AS relevance_score
            FROM knowledge_assets ka
            WHERE ka.id NOT IN (
              SELECT document_id FROM reading_history WHERE user_id = ?
              UNION SELECT document_id FROM bookmarks WHERE user_id = ?
            )
            AND (? = '' OR ka.id != ?)
            ORDER BY ka.created_at DESC
            LIMIT ?
        """
        rows = conn.execute(query2, (user_id, user_id, exclude_document_id, exclude_document_id, limit)).fetchall()
    scores = {
        row['document_id']: round(float(row['relevance_score'] if row['relevance_score'] else 0), 3)
        for row in rows
    }
    summaries = document_summaries_for_ids(conn, [row['document_id'] for row in rows])
    for item in summaries:
        item['relevance_score'] = scores.get(item['document_id'], 0)
    return summaries[:limit]

def document_term_rows(conn, table_name, document_id, value_column, limit=20):
    if not table_exists(conn, table_name):
        return []
    query = f"""
        SELECT {value_column} AS value, confidence_score
        FROM {table_name}
        WHERE document_id = ?
        ORDER BY confidence_score DESC, {value_column}
        LIMIT ?
    """
    rows = conn.execute(query, (document_id, limit)).fetchall()
    result = []
    for row in rows:
        if row['value']:
            result.append({
                'name': row['value'],
                'confidence_score': round(float(row['confidence_score'] if row['confidence_score'] else 0), 4)
            })
    return result

def document_entity_rows(conn, document_id, limit=40):
    if table_exists(conn, 'document_entities'):
        query = """
            SELECT e.id, e.name, e.entity_type, de.entity_type AS display_type,
                   de.confidence, de.mentions
            FROM document_entities de
            JOIN entities e ON e.id = de.entity_id
            WHERE de.document_id = ?
            ORDER BY de.confidence DESC, de.mentions DESC, e.name
            LIMIT ?
        """
        rows = conn.execute(query, (document_id, limit)).fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row['id'],
                'name': row['name'],
                'entity_type': row['entity_type'],
                'legacy_entity_type': row['display_type'] if row['display_type'] else row['entity_type'],
                'confidence': round(float(row['confidence'] if row['confidence'] else 0), 4),
                'mentions': int(row['mentions'] if row['mentions'] else 0)
            })
        return result
    elif table_exists(conn, 'article_entities'):
        query = """
            SELECT e.id, e.name, e.entity_type, ae.confidence
            FROM article_entities ae
            JOIN entities e ON e.id = ae.entity_id
            WHERE ae.article_id = ?
            ORDER BY ae.confidence DESC, e.name
            LIMIT ?
        """
        rows = conn.execute(query, (document_id, limit)).fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row['id'],
                'name': row['name'],
                'entity_type': row['entity_type'],
                'legacy_entity_type': row['entity_type'],
                'confidence': round(float(row['confidence'] if row['confidence'] else 0), 4),
                'mentions': 1
            })
        return result
    return []

def full_content_for_document(conn, document_id, raw_text):
    if raw_text and raw_text.strip():
        return raw_text.strip()
    if not table_exists(conn, 'knowledge_chunks'):
        return ""
    query = """
        SELECT chunk_text
        FROM knowledge_chunks
        WHERE asset_id = ?
        ORDER BY chunk_index ASC, id ASC
    """
    chunks = conn.execute(query, (document_id,)).fetchall()
    return '\n\n'.join(row['chunk_text'].strip() for row in chunks if row['chunk_text'] and row['chunk_text'].strip()).strip()

def parse_json_object(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception:
        return {}

def document_detail_payload(conn, document_id):
    query = """
        SELECT
          ka.id AS document_id,
          ka.title AS asset_title,
          ka.source_type AS asset_source_type,
          ka.source_path,
          ka.raw_text,
          ka.metadata_json AS asset_metadata_json,
          ka.created_at AS asset_created_at,
          cs.publication,
          cs.author,
          cs.date_published,
          cs.location,
          cs.article_title,
          cs.article_url,
          cs.created_at AS article_created_at,
          cs.source_type AS article_source_type,
          cs.content_type,
          cs.theme,
          cs.collection,
          cs.language AS article_language,
          cs.source_family,
          cs.source_medium,
          cs.source_origin,
          dm.title AS metadata_title,
          dm.filename,
          dm.language AS metadata_language,
          dm.document_type,
          dm.word_count,
          dm.chunk_count,
          dm.import_date,
          dm.source AS metadata_source,
          dm.content_hash,
          dm.metadata_json AS document_metadata_json
        FROM knowledge_assets ka
        LEFT JOIN commonsource_articles cs ON cs.asset_id = ka.id
        LEFT JOIN document_metadata dm ON dm.document_id = ka.id
        WHERE ka.id = ?
        LIMIT 1
    """
    row = conn.execute(query, (document_id,)).fetchone()
    if not row:
        return None

    full_content = full_content_for_document(conn, row['document_id'], row['raw_text'] if row['raw_text'] else '')
    asset_metadata = parse_json_object(row['asset_metadata_json'] if row['asset_metadata_json'] else '')
    document_metadata = parse_json_object(row['document_metadata_json'] if row['document_metadata_json'] else '')

    categories = document_term_rows(conn, 'document_categories', row['document_id'], 'category', limit=20)
    tags = document_term_rows(conn, 'document_tags', row['document_id'], 'tag', limit=40)
    keywords = document_term_rows(conn, 'document_keywords', row['document_id'], 'keyword', limit=40)
    entities = document_entity_rows(conn, row['document_id'], limit=40)

    source_path = source_path_for_response(row['source_path'] if row['source_path'] else '')

    if row['metadata_title']:
        title = row['metadata_title']
    elif row['article_title']:
        title = row['article_title']
    elif row['asset_title']:
        title = row['asset_title']
    elif document_metadata.get('title'):
        title = document_metadata.get('title')
    elif asset_metadata.get('title'):
        title = asset_metadata.get('title')
    else:
        title = 'Untitled'

    word_count = int(row['word_count'] if row['word_count'] else 0)
    if not word_count and full_content:
        word_count = len(re.findall(r'\S+', full_content))

    if row['import_date']:
        upload_date = row['import_date']
    elif row['date_published']:
        upload_date = row['date_published']
    elif row['article_created_at']:
        upload_date = row['article_created_at']
    elif row['asset_created_at']:
        upload_date = row['asset_created_at']
    else:
        upload_date = ''

    if row['publication']:
        source_label = row['publication']
    elif row['metadata_source']:
        source_label = row['metadata_source']
    elif row['source_origin']:
        source_label = row['source_origin']
    else:
        source_label = 'CommonSource Archive'

    payload = {
        'document_id': row['document_id'],
        'title': title,
        'full_content': full_content,
        'category': categories[0]['name'] if categories else '',
        'categories': categories,
        'tags': [item['name'] for item in tags],
        'tag_details': tags,
        'keywords': [item['name'] for item in keywords],
        'keyword_details': keywords,
        'entities': entities,
        'language': row['metadata_language'] if row['metadata_language'] else (row['article_language'] if row['article_language'] else (document_metadata.get('language') if document_metadata.get('language') else '')),
        'document_type': row['document_type'] if row['document_type'] else (row['content_type'] if row['content_type'] else (document_metadata.get('document_type') if document_metadata.get('document_type') else '')),
        'source': source_label,
        'source_detail': {
            'publication': row['publication'] if row['publication'] else '',
            'author': row['author'] if row['author'] else '',
            'url': row['article_url'] if row['article_url'] else '',
            'archive_url': f'/api/source/{row["document_id"]}' if source_path else '',
            'filename': row['filename'] if row['filename'] else (Path(source_path).name if source_path else ''),
            'source_type': row['article_source_type'] if row['article_source_type'] else (row['asset_source_type'] if row['asset_source_type'] else ''),
            'source_family': row['source_family'] if row['source_family'] else '',
            'source_medium': row['source_medium'] if row['source_medium'] else '',
            'source_origin': row['source_origin'] if row['source_origin'] else '',
            'theme': row['theme'] if row['theme'] else '',
            'collection': row['collection'] if row['collection'] else ''
        },
        'upload_date': upload_date,
        'word_count': word_count,
        'chunk_count': int(row['chunk_count'] if row['chunk_count'] else 0)
    }

    content_hash = row['content_hash'] if row['content_hash'] else (asset_metadata.get('content_hash') if asset_metadata.get('content_hash') else '')
    download_url = f'/api/document/{row["document_id"]}/download' if source_path else ''
    related_docs = related_documents_for_asset(conn, row['document_id'], limit=6)

    payload.update({
        'content_hash': content_hash,
        'download_url': download_url,
        'related_documents': related_docs
    })
    return payload

def record_reading_history(conn, user_id, document_id):
    conn.execute(
        """
        INSERT INTO reading_history (id, user_id, document_id, viewed_at)
        VALUES (?, ?, ?, ?)
        """,
        (make_id('rh'), user_id, document_id, utc_now())
    )

def optional_current_user():
    """Return the authenticated user when a valid bearer token is present."""
    if not has_request_context() or not bearer_token():
        return None
    try:
        return load_current_user(required=False)
    except Exception:
        return None

def split_text_for_translation(text: str, max_chars: int = 4200) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    units: List[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.!?।])\s+", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                split_at = sentence.rfind(" ", 0, max_chars)
                if split_at < max_chars // 2:
                    split_at = max_chars
                units.append(sentence[:split_at].strip())
                sentence = sentence[split_at:].strip()
            if sentence:
                units.append(sentence)
    chunks: List[str] = []
    current: List[str] = []
    current_length = 0
    for unit in units:
        added = len(unit) + (2 if current else 0)
        if current and current_length + added > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(unit)
        current_length += len(unit) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks

def translate_text_with_timeout(
    text: str,
    target_language: str,
    model: str,
    source_language: str,
    *,
    timeout: float,
) -> str:
    future = _translation_executor.submit(
        translate_with_qwen,
        text,
        target_language,
        model,
        source_language,
        timeout=timeout,
        strict=True,
    )
    try:
        return future.result(timeout=max(1.0, timeout))
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"Translation timed out after {int(timeout)}s") from exc

def translate_with_qwen(
    text: str,
    target_language: str,
    model: str,
    source_language: str = "auto",
    *,
    timeout: Optional[float] = None,
    strict: bool = False,
) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if not HAS_DEEP_TRANSLATOR or model == "LOCAL":
        lang_code = target_language[:2].lower() if target_language else "en"
        return translate_with_local_model(text, lang_code)
    try:
        if len(text) > 4999:
            text = text[:4999]
        translator = GoogleTranslator(source=source_language, target=target_language)
        return translator.translate(text)
    except Exception as e:
        if source_language != "auto":
            try:
                translator = GoogleTranslator(source="auto", target=target_language)
                return translator.translate(text)
            except Exception as retry_exc:
                log.error("deep-translator failed after auto-detect retry: %s", retry_exc)
                e = retry_exc
        else:
            log.error("deep-translator failed: %s", e)
        if strict:
            raise RuntimeError(f"Translation provider failed: {e}") from e
        return text

def translate_full_document(
    title: str,
    content: str,
    target_language: str,
    model: str,
    source_language: str,
    *,
    timeout: float,
) -> Tuple[str, str, int, Optional[str]]:
    chunks = split_text_for_translation(content)
    if not chunks:
        return title, "", 0, None
    deadline = time.monotonic() + max(timeout, 1.0)
    translated_chunks: List[str] = []
    translated_title = title
    failed_chunks = 0
    timed_out = False
    if title:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
        else:
            try:
                translated_title = translate_text_with_timeout(
                    title,
                    target_language,
                    model,
                    source_language,
                    timeout=min(30.0, remaining),
                )
            except Exception as exc:
                log.warning("Document title translation failed; using original title: %s", exc)
    for chunk in chunks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            translated_chunks.append(chunk)
            continue
        try:
            translated_chunks.append(
                translate_text_with_timeout(
                    chunk,
                    target_language,
                    model,
                    source_language,
                    timeout=min(45.0, remaining),
                )
            )
        except Exception as exc:
            failed_chunks += 1
            log.warning("Document translation chunk failed; using original chunk: %s", exc)
            translated_chunks.append(chunk)
    warning = None
    if timed_out:
        warning = "Translation timed out for part of the document; untranslated sections are shown in the original language."
    elif failed_chunks:
        warning = f"Translation skipped {failed_chunks} section(s); untranslated sections are shown in the original language."
    return translated_title, "\n\n".join(translated_chunks), len(chunks), warning

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/document/<document_id>")
def document_page(document_id: str):
    return send_from_directory(str(WEB_DIR), "document.html")

@app.route("/api/document/<document_id>")
def document_detail(document_id: str):
    ensure_phase2a_schema()
    user = optional_current_user()
    conn = get_conn()
    try:
        ensure_document_metadata_tables(conn)
        payload = document_detail_payload(conn, document_id)
        if not payload:
            return jsonify({"error": "Document not found"}), 404
        if user:
            record_reading_history(conn, user['id'], document_id)
            payload['bookmark_id'] = bookmark_id_for_document(conn, user['id'], document_id)
            payload['is_bookmarked'] = bool(payload['bookmark_id'])
            payload['recommended_documents'] = recommend_documents_for_user(conn, user['id'], limit=6, exclude_document_id=document_id)
            conn.commit()
        return jsonify(payload)
    except Exception as exc:
        conn.rollback()
        log.exception("Document detail failed for %s", document_id)
        return jsonify({"error": f"Document detail failed: {exc}"}), 500
    finally:
        conn.close()

@app.route("/api/document/<document_id>/translate", methods=["POST"])
def document_translate(document_id: str):
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    target_code = normalize_target_language_code(
        str(data.get("target") or data.get("target_language") or "").strip()
    )
    if not target_code:
        return jsonify({"error": "A supported target language is required"}), 400
    target_name = TRANSLATION_LANGUAGES.get(target_code, target_code)
    timeout = get_request_timeout(180, maximum=300)
    model = get_available_translation_model()
    if not model:
        return jsonify({"error": "No translation provider is available"}), 503

    conn = get_conn()
    try:
        payload = document_detail_payload(conn, document_id)
        if not payload:
            return jsonify({"error": "Document not found"}), 404
        content = (payload.get("full_content") or "").strip()
        title = (payload.get("title") or "").strip()
        if not content:
            return jsonify({"error": "This document has no readable text to translate"}), 422
        max_chars = max(10000, int(os.getenv("COMMONSOURCE_TRANSLATION_MAX_DOCUMENT_CHARS", "250000")))
        if len(content) > max_chars:
            return jsonify({
                "error": f"Document is too large for interactive translation ({len(content):,} characters; limit {max_chars:,})"
            }), 413

        source_language_value = str(payload.get("language") or "").strip().lower()
        source_language = {
            "english": "en",
            "hindi": "hi",
            "bengali": "bn",
            "tamil": "ta",
            "telugu": "te",
            "marathi": "mr",
            "gujarati": "gu",
            "urdu": "ur",
            "kannada": "kn",
            "malayalam": "ml",
            "odia": "or",
            "punjabi": "pa",
        }.get(source_language_value, "auto")
        if source_language == target_code:
            return jsonify({
                "document_id": document_id,
                "target": target_code,
                "target_language": target_name,
                "translated_title": title,
                "translated_content": content,
                "provider": "original",
                "model": "original",
                "chunk_count": 0,
                "cached": True,
            })

        source_hash = hashlib.sha256(f"{title}\0{content}".encode("utf-8")).hexdigest()
        cached = conn.execute(
            """
            SELECT translated_title, translated_content, provider, model, chunk_count
            FROM document_translations
            WHERE document_id = ? AND target_language = ? AND source_hash = ?
            """,
            (document_id, target_code, source_hash),
        ).fetchone()
        if cached:
            return jsonify({
                "document_id": document_id,
                "target": target_code,
                "target_language": target_name,
                "translated_title": cached["translated_title"],
                "translated_content": cached["translated_content"],
                "provider": cached["provider"],
                "model": cached["model"],
                "chunk_count": int(cached["chunk_count"] or 0),
                "cached": True,
            })

        translated_title, translated_content, chunk_count, translation_warning = translate_full_document(
            title,
            content,
            target_code,
            model,
            source_language,
            timeout=timeout,
        )
        if not translated_content:
            raise RuntimeError("Translation provider returned empty document text")
        if not translation_warning:
            now = utc_now()
            conn.execute(
                """
                INSERT INTO document_translations (
                    document_id, target_language, source_hash, translated_title,
                    translated_content, provider, model, chunk_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, target_language) DO UPDATE SET
                    source_hash = excluded.source_hash,
                    translated_title = excluded.translated_title,
                    translated_content = excluded.translated_content,
                    provider = excluded.provider,
                    model = excluded.model,
                    chunk_count = excluded.chunk_count,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    target_code,
                    source_hash,
                    translated_title,
                    translated_content,
                    "deep-translator" if model == "deep-translator" else "local",
                    model,
                    chunk_count,
                    now,
                    now,
                ),
            )
            conn.commit()
        return jsonify({
            "document_id": document_id,
            "target": target_code,
            "target_language": target_name,
            "translated_title": translated_title,
            "translated_content": translated_content,
            "provider": "deep-translator" if model == "deep-translator" else "local",
            "model": model,
            "chunk_count": chunk_count,
            "cached": False,
            "warning": translation_warning,
        })
    except TimeoutError as exc:
        conn.rollback()
        log.warning("Full document translation timed out document=%s target=%s: %s", document_id, target_code, exc)
        return jsonify({"error": str(exc)}), 504
    except Exception as exc:
        conn.rollback()
        log.exception("Full document translation failed document=%s target=%s", document_id, target_code)
        return jsonify({"error": f"Document translation failed: {exc}"}), 502
    finally:
        conn.close()

@app.route("/api/document/<document_id>/citations")
def document_citations(document_id: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        payload = citation_payload_for_document(conn, document_id)
        if not payload:
            return jsonify({"error": "Document not found"}), 404
        conn.commit()
        return jsonify(payload)
    except Exception as exc:
        conn.rollback()
        log.exception("Citation generation failed for %s", document_id)
        return jsonify({"error": f"Citation generation failed: {exc}"}), 500
    finally:
        conn.close()

@app.route("/api/document/<document_id>/download")
def document_download(document_id: str):
    conn = get_conn()
    try:
        row = conn.execute('SELECT source_path FROM knowledge_assets WHERE id = ?', (document_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Document not found"}), 404
        path = source_path_for_response(row['source_path'] if row['source_path'] else '')
        if not path:
            return jsonify({"error": "Source file missing"}), 404
        return send_file(path, as_attachment=True, download_name=Path(path).name)
    except Exception:
        conn.close()
        raise


@app.route("/")
def index():
    return send_from_directory(str(WEB_DIR), "landing.html")

@app.route("/search")
def search_app():
    return send_from_directory(str(WEB_DIR), "index.html")

@app.route("/join")
def join_page():
    return send_from_directory(str(WEB_DIR), "join.html")

@app.route("/governance")
def governance_page():
    return send_from_directory(str(WEB_DIR), "governance.html")


@app.route("/users")
def users_page():
    return send_from_directory(str(WEB_DIR), "users.html")


@app.route("/profile")
def profile_page():
    return send_from_directory(str(WEB_DIR), "profile.html")


@app.route("/publisher/profile")
def publisher_profile_page():
    return send_from_directory(str(WEB_DIR), "publisher-profile.html")


@app.route("/publisher/dashboard")
def publisher_dashboard_page():
    return send_from_directory(str(WEB_DIR), "publisher-dashboard.html")


@app.route("/admin/publisher-applications")
def publisher_applications_page():
    return send_from_directory(str(WEB_DIR), "publisher-applications.html")


@app.route("/moderation")
def moderation_page():
    return send_from_directory(str(WEB_DIR), "moderation.html")


@app.route("/entity/<entity_id>")
def entity_page(entity_id: str):
    return send_from_directory(str(WEB_DIR), "entity.html")


@app.route("/topic/<path:topic>")
def topic_page(topic: str):
    return send_from_directory(str(WEB_DIR), "topic.html")


@app.route("/tag/<tag_id>")
def tag_page(tag_id: str):
    return send_from_directory(str(WEB_DIR), "tag.html")


@app.route("/trending")
def trending_page():
    return send_from_directory(str(WEB_DIR), "trending.html")


@app.route("/login")
def login_page():
    return send_from_directory(str(WEB_DIR), "login.html")


@app.route("/register")
def register_page():
    return send_from_directory(str(WEB_DIR), "register.html")


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    """Create a platform user. The first user bootstraps as super_admin."""
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    conn = get_conn()
    try:
        role = "super_admin" if user_count(conn) == 0 else "reader"
        user = create_user(conn, name=name, email=email, password=password, role=role)
        tokens = issue_token_pair(conn, user, ip_address=client_ip())
        record_audit(conn, "Registration", "user", user["id"], user_id=user["id"])
        conn.commit()
        log.info("[AUTH] Registered user id=%s role=%s", user["id"], user["role"])
        return jsonify({
            "user": user,
            **tokens,
            "message": "Account created successfully.",
        }), 201
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Email is already registered"}), 409
    except (AuthError, AuthConfigError) as exc:
        conn.rollback()
        return auth_error_response(exc)
    except Exception as exc:
        conn.rollback()
        log.exception("[AUTH] Registration failed")
        return jsonify({"error": "Registration failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Validate credentials. Token issuing is layered in the JWT flow."""
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if login_rate_limited(email):
        return jsonify({"error": "Too many login attempts. Try again shortly."}), 429
    conn = get_conn()
    try:
        user = authenticate_user(conn, email, password)
        tokens = issue_token_pair(conn, user, ip_address=client_ip())
        record_audit(conn, "Login", "user", user["id"], user_id=user["id"])
        conn.commit()
        clear_login_rate(email)
        log.info("[AUTH] Login accepted for user_id=%s", user["id"])
        return jsonify({"user": user, **tokens, "message": "Login successful"}), 200
    except (AuthError, AuthConfigError) as exc:
        record_login_rate_failure(email)
        if getattr(exc, "commit", False):
            conn.commit()
        else:
            conn.rollback()
        return auth_error_response(exc)
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Login failed")
        return jsonify({"error": "Login failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/me")
@require_auth
def auth_me():
    return jsonify({"user": g.current_user}), 200


@app.route("/api/auth/profile")
@require_auth
def auth_profile():
    return jsonify({"user": g.current_user}), 200


@app.route("/api/auth/csrf")
@require_auth
def auth_csrf():
    return jsonify({"csrf_token": create_csrf_token(g.current_user["id"])}), 200


@app.route("/api/auth/refresh", methods=["POST"])
def auth_refresh():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token") or ""
    conn = get_conn()
    try:
        user, tokens = refresh_token_pair(conn, refresh_token, ip_address=client_ip())
        conn.commit()
        log.info("[AUTH] Refresh token rotated for user_id=%s", user["id"])
        return jsonify({"user": user, **tokens}), 200
    except (AuthError, AuthConfigError) as exc:
        conn.rollback()
        return auth_error_response(exc)
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Refresh failed")
        return jsonify({"error": "Refresh failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token") or ""
    access_payload = None
    access_token = bearer_token()
    if access_token:
        try:
            access_payload = decode_access_token(access_token)
        except Exception:
            access_payload = None
    conn = get_conn()
    try:
        user_id = refresh_token_user_id(conn, refresh_token) or (access_payload or {}).get("sub")
        revoked = revoke_refresh_token(conn, refresh_token)
        access_revoked = revoke_access_token(conn, access_payload) if access_payload else False
        if user_id:
            record_audit(conn, "Logout", "user", user_id, user_id=user_id)
        conn.commit()
        log.info("[AUTH] Logout processed refresh_revoked=%s access_revoked=%s", revoked, access_revoked)
        return jsonify({"message": "Logged out", "revoked": revoked, "access_revoked": access_revoked}), 200
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Logout failed")
        return jsonify({"error": "Logout failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/password-reset/request", methods=["POST"])
def auth_password_reset_request():
    """Create a reset token when the account exists; always return a generic response."""
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    conn = get_conn()
    try:
        user_row = get_user_by_email(conn, email)
        reset_token = create_password_reset_token(conn, email, ip_address=client_ip())
        if user_row:
            record_audit(conn, "Password Reset Requested", "user", user_row["id"], user_id=user_row["id"])
        conn.commit()
        response = {
            "message": "If the account exists, a password reset link has been prepared.",
        }
        if reset_token and os.getenv("COMMONSOURCE_DEV_RESET_TOKEN") == "1":
            response["reset_token"] = reset_token
        return jsonify(response), 200
    except (AuthError, AuthConfigError) as exc:
        conn.rollback()
        return auth_error_response(exc)
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Password reset request failed")
        return jsonify({"error": "Password reset request failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/password-reset/confirm", methods=["POST"])
def auth_password_reset_confirm():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    reset_token = data.get("token") or data.get("reset_token") or ""
    password = data.get("password") or data.get("new_password") or ""
    conn = get_conn()
    try:
        user = reset_password(conn, reset_token, password)
        record_audit(conn, "Password Reset Completed", "user", user["id"], user_id=user["id"])
        conn.commit()
        log.info("[AUTH] Password reset completed for user_id=%s", user["id"])
        return jsonify({"user": user, "message": "Password reset successful"}), 200
    except (AuthError, AuthConfigError) as exc:
        conn.rollback()
        return auth_error_response(exc, fallback_status=400)
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Password reset confirmation failed")
        return jsonify({"error": "Password reset failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/change-password", methods=["POST"])
@require_auth
@require_csrf
def auth_change_password():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or data.get("password") or ""
    conn = get_conn()
    try:
        user = change_password(conn, g.current_user["id"], current_password, new_password)
        access_token = bearer_token()
        if access_token:
            try:
                revoke_access_token(conn, decode_access_token(access_token))
            except Exception:
                pass
        record_audit(conn, "Password Changed", "user", user["id"], user_id=user["id"])
        conn.commit()
        return jsonify({"user": user, "message": "Password changed. Please log in again."}), 200
    except (AuthError, AuthConfigError) as exc:
        conn.rollback()
        return auth_error_response(exc)
    except Exception:
        conn.rollback()
        log.exception("[AUTH] Password change failed")
        return jsonify({"error": "Password change failed"}), 500
    finally:
        conn.close()


@app.route("/api/auth/activity")
@require_auth
def auth_activity():
    ensure_phase2a_schema()
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 25)), 1), 100)
    except ValueError:
        return jsonify({"error": "page and per_page must be integers"}), 400
    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE user_id = ?",
            (g.current_user["id"],),
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, user_id, action, resource_type, resource_id, timestamp, ip_address
            FROM audit_logs
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (g.current_user["id"], per_page, offset),
        ).fetchall()
        return jsonify({
            "activity": [dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
        }), 200
    finally:
        conn.close()


@app.route("/api/source/<asset_id>")
def source_file(asset_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT source_path FROM knowledge_assets WHERE id = ?",
        (asset_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Source not found"}), 404
    path = source_path_for_response(row["source_path"] or "")
    if not path:
        return jsonify({"error": "Source file missing"}), 404
    return send_file(path, as_attachment=False)


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    tag_filter = (request.args.get("tag") or "").strip()
    entity_filter = (request.args.get("entity") or "").strip()
    publisher_filter = (request.args.get("publisher") or "").strip()
    topic_filter = (request.args.get("topic") or "").strip()
    try:
        top_k = min(max(int(request.args.get("k", 8)), 1), 20)
    except ValueError:
        top_k = 8
    has_filters = any([tag_filter, entity_filter, publisher_filter, topic_filter])
    if not query and not has_filters:
        return jsonify({"error": "No query or filter provided"}), 400
    try:
        if has_filters:
            ensure_phase2a_schema()
            conn = get_conn()
            try:
                filtered = knowledge_filter_search(
                    conn,
                    query=query,
                    tag=tag_filter,
                    entity=entity_filter,
                    publisher=publisher_filter,
                    topic=topic_filter,
                    limit=top_k,
                )
            finally:
                conn.close()
            return jsonify({
                "query": query,
                "count": filtered["count"],
                "results": filtered["results"],
                "retrieval_backend": "knowledge-filter",
                "filters": {
                    "tag": tag_filter,
                    "entity": entity_filter,
                    "publisher": publisher_filter,
                    "topic": topic_filter,
                },
            })
        data = cached_retrieve_sources(query, top_k=top_k)
        if data.get("error"):
            return jsonify(data), 400
        return jsonify({
            "query": data["query"],
            "count": data["count"],
            "results": data["results"],
            "retrieval_backend": data.get("retrieval_backend"),
        })
    except Exception as exc:
        log.exception("Search failed for query=%r", query)
        return jsonify({"error": f"Search failed: {exc}"}), 500



@app.route("/api/debug/model-test")
def model_test():
    import time
    prompt = request.args.get("prompt", "Say 'Hello, CommonSource!'").strip()
    timeout = get_request_timeout(30, maximum=60)
    model = get_llm_model()
    if not model:
        return jsonify({"error": "No generation model configured"}), 503
    try:
        response = call_ollama(prompt, model, max_tokens=50, timeout=timeout, cache=False)
        return jsonify({"model": model, "response": response, "status": "success"})
    except OllamaGenerationError as exc:
        status = 504 if "timed out" in str(exc).lower() else 502
        return jsonify({"model": model, "error": str(exc), "status": "error"}), status
    except Exception as exc:
        return jsonify({"model": model, "error": str(exc), "status": "error"}), 500



@app.route("/api/knowledge/reindex", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def knowledge_reindex():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    asset_id = (data.get("asset_id") or "").strip()
    try:
        limit = min(max(int(data.get("limit", request.args.get("limit", 100))), 1), 500)
        offset = max(int(data.get("offset", request.args.get("offset", 0))), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    conn = get_conn()
    try:
        where = "WHERE cs.asset_id = ?" if asset_id else ""
        params = [asset_id] if asset_id else []
        rows = conn.execute(
            f"""
            SELECT cs.*, ka.raw_text, ka.metadata_json
            FROM commonsource_articles cs
            JOIN knowledge_assets ka ON ka.id = cs.asset_id
            {where}
            ORDER BY cs.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        from knowledge_layer import process_article_knowledge
        processed = []
        for row in rows:
            metadata: Dict[str, Any] = {}
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            result = process_article_knowledge(
                conn,
                article_id=row["asset_id"],
                title=row["article_title"] or "",
                text=row["raw_text"] or "",
                publication=row["publication"] or "",
                metadata=metadata,
            )
            processed.append({
                "asset_id": row["asset_id"],
                "entities": result["entity_count"],
                "tags": result["tag_count"],
            })
        record_audit(conn, "Entity Extraction", "knowledge", asset_id or "batch")
        record_audit(conn, "Tag Generation", "knowledge", asset_id or "batch")
        conn.commit()
        return jsonify({"processed": processed, "count": len(processed), "limit": limit, "offset": offset}), 200
    except Exception as exc:
        conn.rollback()
        log.exception("Knowledge reindex failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/entities")
def entities_list():
    ensure_phase2a_schema()
    q = (request.args.get("q") or "").strip().lower()
    entity_type = (request.args.get("type") or "").strip().upper()
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    if entity_type and entity_type not in {"PERSON", "ORG", "GPE", "LOC", "EVENT", "TOPIC"}:
        return jsonify({"error": "Invalid entity type"}), 400
    conn = get_conn()
    try:
        if not table_exists(conn, "entities"):
            return jsonify({"entities": [], "count": 0})
        where: List[str] = []
        params: List[Any] = []
        if q:
            where.append("(lower(e.name) LIKE ? OR lower(e.canonical_name) LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if entity_type:
            where.append("e.entity_type = ?")
            params.append(entity_type)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            f"""
            SELECT e.*, COUNT(ae.article_id) AS article_count
            FROM entities e
            LEFT JOIN article_entities ae ON ae.entity_id = e.id
            {where_sql}
            GROUP BY e.id
            ORDER BY article_count DESC, e.name
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return jsonify({"entities": [dict(row) for row in rows], "count": len(rows)})
    finally:
        conn.close()


@app.route("/api/entity/<entity_id>")
def entity_detail(entity_id: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        entity = resolve_entity_ref(conn, entity_id)
        if not entity:
            return jsonify({"error": "Entity not found"}), 404
        publishers = conn.execute(
            """
            SELECT cs.publication, COUNT(*) AS count
            FROM article_entities ae
            JOIN commonsource_articles cs ON cs.asset_id = ae.article_id
            WHERE ae.entity_id = ?
            GROUP BY cs.publication
            ORDER BY count DESC, cs.publication
            LIMIT 20
            """,
            (entity["id"],),
        ).fetchall()
        article_count = conn.execute(
            "SELECT COUNT(*) FROM article_entities WHERE entity_id = ?",
            (entity["id"],),
        ).fetchone()[0]
        return jsonify({"entity": dict(entity), "article_count": article_count, "publishers": [dict(row) for row in publishers]})
    finally:
        conn.close()


@app.route("/api/entity/<entity_id>/articles")
def entity_articles(entity_id: str):
    ensure_phase2a_schema()
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    conn = get_conn()
    try:
        entity = resolve_entity_ref(conn, entity_id)
        if not entity:
            return jsonify({"error": "Entity not found"}), 404
        rows = article_card_rows(
            conn,
            "JOIN article_entities ae ON ae.article_id = cs.asset_id WHERE ae.entity_id = ?",
            [entity["id"]],
            limit=limit,
            offset=offset,
        )
        return jsonify({"entity": dict(entity), "articles": [article_result_from_row(row, "", 1.0) for row in rows]})
    finally:
        conn.close()


@app.route("/api/entity/<entity_id>/related")
def entity_related(entity_id: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        entity = resolve_entity_ref(conn, entity_id)
        if not entity:
            return jsonify({"error": "Entity not found"}), 404
        rows = conn.execute(
            """
            SELECT er.relationship_type, er.weight, e.*
            FROM entity_relationships er
            JOIN entities e ON e.id = er.target_entity_id
            WHERE er.source_entity_id = ?
            UNION
            SELECT er.relationship_type, er.weight, e.*
            FROM entity_relationships er
            JOIN entities e ON e.id = er.source_entity_id
            WHERE er.target_entity_id = ?
            ORDER BY weight DESC
            LIMIT 30
            """,
            (entity["id"], entity["id"]),
        ).fetchall()
        return jsonify({"entity": dict(entity), "related": [dict(row) for row in rows]})
    finally:
        conn.close()


@app.route("/api/tags")
def tags_list():
    ensure_phase2a_schema()
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    conn = get_conn()
    try:
        if not table_exists(conn, "tags"):
            return jsonify({"tags": [], "count": 0})
        where = "WHERE lower(t.name) LIKE ? OR t.slug LIKE ?" if q else ""
        params: List[Any] = [f"%{q}%", f"%{q}%"] if q else []
        rows = conn.execute(
            f"""
            SELECT t.*, COUNT(at.article_id) AS article_count
            FROM tags t
            LEFT JOIN article_tags at ON at.tag_id = t.id
            {where}
            GROUP BY t.id
            ORDER BY article_count DESC, t.name
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return jsonify({"tags": [dict(row) for row in rows], "count": len(rows)})
    finally:
        conn.close()


@app.route("/api/tag/<tag_id>")
def tag_detail(tag_id: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        tag = resolve_tag_ref(conn, tag_id)
        if not tag:
            return jsonify({"error": "Tag not found"}), 404
        article_count = conn.execute(
            "SELECT COUNT(*) FROM article_tags WHERE tag_id = ?",
            (tag["id"],),
        ).fetchone()[0]
        return jsonify({"tag": dict(tag), "article_count": article_count})
    finally:
        conn.close()


@app.route("/api/tag/<tag_id>/articles")
def tag_articles(tag_id: str):
    ensure_phase2a_schema()
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    conn = get_conn()
    try:
        tag = resolve_tag_ref(conn, tag_id)
        if not tag:
            return jsonify({"error": "Tag not found"}), 404
        rows = article_card_rows(
            conn,
            "JOIN article_tags at ON at.article_id = cs.asset_id WHERE at.tag_id = ?",
            [tag["id"]],
            limit=limit,
            offset=offset,
        )
        return jsonify({"tag": dict(tag), "articles": [article_result_from_row(row, "", 1.0) for row in rows]})
    finally:
        conn.close()


@app.route("/api/tag/<tag_id>/publishers")
def tag_publishers(tag_id: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        tag = resolve_tag_ref(conn, tag_id)
        if not tag:
            return jsonify({"error": "Tag not found"}), 404
        rows = conn.execute(
            """
            SELECT cs.publication, COUNT(*) AS article_count
            FROM article_tags at
            JOIN commonsource_articles cs ON cs.asset_id = at.article_id
            WHERE at.tag_id = ?
            GROUP BY cs.publication
            ORDER BY article_count DESC, cs.publication
            LIMIT 30
            """,
            (tag["id"],),
        ).fetchall()
        return jsonify({"tag": dict(tag), "publishers": [dict(row) for row in rows]})
    finally:
        conn.close()


@app.route("/api/topic/<path:topic>")
def topic_detail(topic: str):
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        filtered = knowledge_filter_search(conn, query="", topic=topic, limit=30)
        publishers: Dict[str, int] = {}
        for result in filtered["results"]:
            pub = result.get("publication") or "Unknown"
            publishers[pub] = publishers.get(pub, 0) + 1
        return jsonify({
            "topic": topic,
            "articles": filtered["results"],
            "publishers": [{"publication": name, "article_count": count} for name, count in publishers.items()],
            "related_topics": trending_topics(conn, limit=12),
        })
    finally:
        conn.close()


def trending_topics(conn: sqlite3.Connection, *, limit: int = 20) -> List[Dict[str, Any]]:
    if not table_exists(conn, "tags"):
        return []
    rows = conn.execute(
        """
        SELECT t.name, t.slug, COUNT(*) AS article_count, MAX(ka.created_at) AS latest_article_at
        FROM tags t
        JOIN article_tags at ON at.tag_id = t.id
        JOIN knowledge_assets ka ON ka.id = at.article_id
        GROUP BY t.id
        ORDER BY article_count DESC, latest_article_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


@app.route("/api/trending/topics")
def trending_topics_api():
    ensure_phase2a_schema()
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    conn = get_conn()
    try:
        topics = trending_topics(conn, limit=limit)
        entities = []
        if table_exists(conn, "entities"):
            entities = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT e.id, e.name, e.entity_type, COUNT(*) AS article_count
                    FROM entities e
                    JOIN article_entities ae ON ae.entity_id = e.id
                    GROUP BY e.id
                    ORDER BY article_count DESC, e.name
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
        return jsonify({"topics": topics, "entities": entities})
    finally:
        conn.close()


@app.route("/api/articles/<asset_id>/related")
def related_articles(asset_id: str):
    ensure_phase2a_schema()
    try:
        limit = min(max(int(request.args.get("limit", 8)), 1), 30)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    conn = get_conn()
    try:
        article = conn.execute("SELECT asset_id FROM commonsource_articles WHERE asset_id = ?", (asset_id,)).fetchone()
        if not article:
            return jsonify({"error": "Article not found"}), 404
        return jsonify({"asset_id": asset_id, "related": related_articles_for_asset(conn, asset_id, limit=limit)})
    finally:
        conn.close()


@app.route("/api/publisher/analytics")
@require_roles("super_admin", "admin", "publisher")
def publisher_analytics():
    ensure_phase2a_schema()
    publisher_id = (request.args.get("publisher_id") or "").strip()
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        if publisher_id:
            publisher = conn.execute("SELECT * FROM publishers WHERE id = ?", (publisher_id,)).fetchone()
            if not publisher:
                return jsonify({"error": "Publisher not found"}), 404
            if g.current_user["role"] == "publisher" and not can_manage_publisher(publisher):
                return jsonify({"error": "You can only view your own analytics"}), 403
        else:
            publisher = current_user_publisher(conn)
            if not publisher:
                return jsonify({"error": "No publisher account linked to current user"}), 404
        top_tags = []
        top_entities = []
        if table_exists(conn, "tags"):
            top_tags = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT t.name, t.slug, COUNT(*) AS count
                    FROM article_tags at
                    JOIN tags t ON t.id = at.tag_id
                    JOIN commonsource_articles cs ON cs.asset_id = at.article_id
                    WHERE lower(cs.publication) = lower(?)
                    GROUP BY t.id
                    ORDER BY count DESC, t.name
                    LIMIT 20
                    """,
                    (publisher["name"],),
                ).fetchall()
            ]
        if table_exists(conn, "entities"):
            top_entities = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT e.id, e.name, e.entity_type, COUNT(*) AS count
                    FROM article_entities ae
                    JOIN entities e ON e.id = ae.entity_id
                    JOIN commonsource_articles cs ON cs.asset_id = ae.article_id
                    WHERE lower(cs.publication) = lower(?)
                    GROUP BY e.id
                    ORDER BY count DESC, e.name
                    LIMIT 20
                    """,
                    (publisher["name"],),
                ).fetchall()
            ]
        feed_count = conn.execute(
            "SELECT COUNT(*) FROM rss_feeds WHERE publisher_id = ? AND deleted_at IS NULL",
            (publisher["id"],),
        ).fetchone()[0]
        article_count = conn.execute(
            "SELECT COUNT(*) FROM commonsource_articles WHERE lower(publication) = lower(?)",
            (publisher["name"],),
        ).fetchone()[0]
        dashboard = knowledge_filter_search(conn, query="", publisher=publisher["name"], limit=100)
        topic_counts: Dict[str, int] = {}
        for result in dashboard["results"]:
            for topic_value in re.split(r"[,;|]", result.get("theme") or ""):
                topic_value = topic_value.strip()
                if topic_value:
                    topic_counts[topic_value] = topic_counts.get(topic_value, 0) + 1
        return jsonify({
            "publisher": dict(publisher),
            "article_count": article_count,
            "feed_count": feed_count,
            "top_tags": top_tags,
            "top_entities": top_entities,
            "top_topics": [
                {"name": name, "count": count}
                for name, count in sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:20]
            ],
        })
    finally:
        conn.close()


@app.route("/api/ask")
def ask():
    """RAG endpoint: vector search -> LLM synthesis -> cited answer + source cards."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    timeout = get_request_timeout(60, maximum=120)

    # 1. Embed + search (reuse search logic; keyword fallback if offline)
    query_vec = embed(query)
    keywords = extract_keywords(query)

    conn = get_conn()
    where_sql, params, limit_sql = candidate_filter_sql(query_vec, keywords)
    rows = conn.execute(f"""
        SELECT kc.asset_id, kc.chunk_text, kc.embedding_blob,
               cs.publication, cs.author, cs.date_published,
               cs.location, cs.article_title, cs.article_url,
               cs.source_type, cs.content_type, cs.source_family, cs.source_medium, cs.source_origin, cs.theme,
               ka.source_path
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        LEFT JOIN knowledge_assets ka ON ka.id = kc.asset_id
        {where_sql}
        {limit_sql}
    """, params).fetchall()
    conn.close()

    scored = []
    for row in rows:
        try:
            if is_boilerplate(row["chunk_text"]):
                continue
            score = score_row(row, query_vec, query, keywords)
            scored.append((score, dict(row)))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)

    sources: List[Dict[str, Any]] = []
    for score, row in select_diverse_results(scored, 6, min_score=0.25):
        excerpt = build_excerpt(row["chunk_text"], keywords)
        sources.append(build_source_result(row, score, excerpt))

    if not sources:
        return jsonify({"query": query, "answer": "No relevant sources found.", "sources": [], "model": None})

    # 2. Find a generation model
    model = get_llm_model()
    if not model:
        # Fall back to search-only — still useful
        return jsonify({
            "query":   query,
            "answer":  None,
            "sources": sources,
            "model":   None,
            "warning": "Configured LLM model is empty. Set COMMONSOURCE_LLM_MODEL.",
        })

    # 3. Synthesise + extract entities
    try:
        answer, entities = synthesise(query, sources, model, timeout=timeout)
    except OllamaGenerationError as exc:
        return jsonify({
            "query": query,
            "answer": None,
            "entities": {},
            "sources": sources,
            "model": model,
            "warning": str(exc),
        }), 504 if "timed out" in str(exc).lower() else 502

    return jsonify({
        "query":    query,
        "answer":   answer,
        "entities": entities,
        "sources":  sources,
        "model":    model,
    })


@app.route("/api/ask/layered")
def ask_layered():
    """
    Two-layer evidence response (optimized for speed):
      news, development + gaps/contradictions synthesis.
    Each layer is synthesised independently from sources tagged with that source_type.
    Falls back to all sources when a layer has no tagged content.
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    timeout = get_request_timeout(90, maximum=120)

    try:
        data = cached_retrieve_sources(query, top_k=20, min_score=0.20)
        all_sources = data.get("results") or []
        if not all_sources:
            return jsonify({
                "query": query,
                "model": None,
                "layers": {},
                "gaps": "",
                "all_sources": [],
                "warning": "No relevant sources found for evidence layers.",
            })

        layer_sources: Dict[str, List[Dict[str, Any]]] = {k: [] for k in SOURCE_TYPES}
        for src in all_sources:
            stype = src.get("source_type") or "news"
            if stype in layer_sources and len(layer_sources[stype]) < 4:
                layer_sources[stype].append(src)

        def fallback_summary(ltype: str) -> str:
            srcs = layer_sources.get(ltype) or all_sources[:3]
            if not srcs:
                return f"No {SOURCE_TYPES[ltype]} sources found for this query."
            snippets = []
            for i, src in enumerate(srcs[:3], 1):
                title = src.get("title") or src.get("publication") or f"Source {i}"
                date = f", {src.get('date')[:10]}" if src.get("date") else ""
                excerpt = (src.get("excerpt") or "").strip()
                if len(excerpt) > 180:
                    excerpt = excerpt[:177].rstrip() + "..."
                snippets.append(f"[Source {i}] {title}{date}: {excerpt}")
            return f"{len(srcs)} relevant source(s) matched this layer. " + " ".join(snippets)

        model = get_llm_model()
        warning = None
        try:
            if model:
                layer_texts, gaps = synthesise_layered_fast(
                    query, layer_sources, all_sources, model, timeout=timeout
                )
            else:
                raise OllamaGenerationError("No generation model available in Ollama")
        except OllamaGenerationError as exc:
            warning = str(exc)
            log.warning("Layered synthesis unavailable for query=%r: %s", query, exc)
            layer_texts = {ltype: fallback_summary(ltype) for ltype in SOURCE_TYPES}
            gaps = (
                "WHERE THEY OVERLAP: Review the retrieved source cards for repeated claims and places.\n\n"
                "WHERE THEY DIVERGE: Compare official or institutional records against news and community accounts.\n\n"
                "WHAT IS MISSING OR UNCLEAR: Model synthesis was unavailable, so gaps should be verified manually.\n\n"
                "WHAT TO INVESTIGATE NEXT: Open the cited sources below and follow up with primary documents and local voices."
            )

        return jsonify({
            "query": query,
            "model": model,
            "layers": {
                ltype: {
                    "label": SOURCE_TYPES[ltype],
                    "summary": layer_texts.get(ltype) or fallback_summary(ltype),
                    "sources": layer_sources.get(ltype) or all_sources[:3],
                }
                for ltype in SOURCE_TYPES
            },
            "gaps": gaps,
            "all_sources": all_sources,
            "warning": warning,
            "retrieval_backend": data.get("retrieval_backend"),
        })
    except Exception as exc:
        log.exception("Evidence layers failed for query=%r", query)
        return jsonify({"error": f"Evidence layers failed: {exc}"}), 500


@app.route("/api/arc")
def arc():
    """Story arc: how did coverage of this topic evolve over time?"""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    timeout = get_request_timeout(90, maximum=120)

    def fallback_arc(srcs: List[Dict[str, Any]], reason: str = "") -> str:
        dated = sorted(srcs, key=lambda s: s.get("date") or "")
        if not dated:
            return "No dated sources were found, so CommonSource could not trace a story arc for this query."
        first_year = (dated[0].get("date") or "")[:4] or "the earliest coverage"
        last_year = (dated[-1].get("date") or "")[:4] or "the latest coverage"
        source_lines = []
        for i, src in enumerate(dated[:6], 1):
            title = src.get("title") or src.get("publication") or f"Source {i}"
            date = src.get("date") or "undated"
            excerpt = (src.get("excerpt") or "").strip()
            if len(excerpt) > 170:
                excerpt = excerpt[:167].rstrip() + "..."
            source_lines.append(f"[Source {i}] {date}: {title}. {excerpt}")
        suffix = f"\n\nModel note: {reason}" if reason else ""
        return (
            f"Coverage for \"{query}\" runs from {first_year} to {last_year} in the retrieved archive set. "
            "The available sources suggest a progression best verified by reading the dated records in order.\n\n"
            + "\n".join(source_lines)
            + suffix
        )

    try:
        data = cached_retrieve_sources(
            query,
            top_k=12,
            min_score=0.20,
            extra_sql_conditions=["cs.date_published != ''", "cs.date_published IS NOT NULL"],
        )
        sources = sorted(data.get("results") or [], key=lambda s: s.get("date") or "")[:10]
        if not sources:
            return jsonify({
                "query": query,
                "narrative": "No dated sources were found for this query.",
                "timeline": {},
                "sources": [],
                "model": None,
            })

        model = get_llm_model()
        try:
            if not model:
                raise OllamaGenerationError("No generation model available in Ollama")
            narrative = story_arc(query, sources, model, timeout=timeout)
            warning = None
        except OllamaGenerationError as exc:
            log.warning("Story arc synthesis unavailable for query=%r: %s", query, exc)
            narrative = fallback_arc(sources, str(exc))
            warning = str(exc)

        from collections import Counter
        year_counts: Counter = Counter()
        for s in sources:
            if s.get("date") and len(s["date"]) >= 4:
                year_counts[s["date"][:4]] += 1

        return jsonify({
            "query": query,
            "narrative": narrative,
            "timeline": dict(sorted(year_counts.items())),
            "sources": sources,
            "model": model,
            "warning": warning,
            "retrieval_backend": data.get("retrieval_backend"),
        })
    except Exception as exc:
        log.exception("Story arc failed for query=%r", query)
        return jsonify({"error": f"Story arc failed: {exc}"}), 500


@app.route("/api/timeline")
def timeline():
    """Year-by-year article counts for a query (fast, no LLM)."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400

    query_vec = embed(query)
    keywords = extract_keywords(query)

    conn = get_conn()
    where_sql, params, limit_sql = candidate_filter_sql(query_vec, keywords)
    rows = conn.execute(f"""
        SELECT kc.asset_id, kc.chunk_text, kc.embedding_blob, cs.date_published,
               cs.author, cs.publication, cs.source_type, cs.content_type,
               cs.source_family, cs.source_medium, cs.source_origin, cs.theme,
               cs.location, cs.article_title
        FROM knowledge_chunks kc
        LEFT JOIN commonsource_articles cs ON cs.asset_id = kc.asset_id
        {where_sql}
        {limit_sql}
    """, params).fetchall()
    conn.close()

    from collections import Counter, defaultdict
    seen: set = set()
    year_counts:   Counter               = Counter()
    year_authors:  dict[str, set]        = defaultdict(set)

    for row in rows:
        aid = row["asset_id"]
        if aid in seen: continue
        try:
            if is_boilerplate(row["chunk_text"]):
                continue
            score = score_row(row, query_vec, query, keywords)
            if keywords and keyword_match_count(row, keywords) < min(2, len(keywords)):
                continue
            if score < 0.35:
                continue
            seen.add(aid)
            date = (row["date_published"] or "")[:4]
            if date.isdigit() and len(date) == 4:
                year_counts[date] += 1
                if row["author"]:
                    year_authors[date].add(row["author"])
        except Exception:
            continue

    return jsonify({
        "query":   query,
        "by_year": {
            yr: {"count": cnt, "authors": list(year_authors[yr])}
            for yr, cnt in sorted(year_counts.items())
        },
        "total": sum(year_counts.values()),
    })


@app.route("/api/generate", methods=["POST"])
def generate():
    """Raw generation endpoint for the Script Writer."""
    data      = request.get_json(silent=True) or {}
    prompt    = data.get("prompt", "").strip()
    try:
        max_tok = min(max(int(data.get("max_tokens", MAX_TOKENS_GENERATE)), 1), MAX_TOKENS_GENERATE)
    except (TypeError, ValueError):
        max_tok = MAX_TOKENS_GENERATE
    timeout   = get_request_timeout(90, maximum=120)

    if not prompt:
        return jsonify({"error": "No prompt"}), 400

    model = get_llm_model()
    if not model:
        return jsonify({"error": "No generation model available in Ollama"}), 503

    try:
        if not (HAS_LLM_PROVIDER and provider_generate):
            return jsonify({"error": "No generation provider available"}), 503
        result = provider_generate(
            prompt,
            preferred_model=model,
            max_tokens=max_tok,
            timeout=timeout,
            temperature=0.7,
        )
        return jsonify({"response": result.text, "model": result.model})
    except OllamaGenerationError as exc:
        status = 504 if "timed out" in str(exc).lower() else 502
        return jsonify({"error": str(exc)}), status
    except Exception as exc:
        log.exception("Script writer generation failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/translate", methods=["POST"])
def translate():
    """Translation endpoint using deep-translator or local fallback."""
    data = request.get_json(silent=True) or {}
    target_code = (data.get("target") or data.get("target_language") or "hi").strip()
    target_language = TRANSLATION_LANGUAGES.get(target_code.lower(), data.get("target_language_name") or target_code)
    target_lang_code = normalize_target_language_code(target_code)
    source_language = (data.get("source") or "auto").strip()
    text = (data.get("text") or "").strip()
    items = data.get("items") or []
    timeout = get_request_timeout(90, maximum=120)

    if not text and not items:
        return jsonify({"error": "No text or items provided"}), 400
    if len(text) > 6000:
        return jsonify({"error": "Text too long for one translation request"}), 400
    if items and (not isinstance(items, list) or len(items) > 12):
        return jsonify({"error": "items must be a list of up to 12 entries"}), 400
    if not target_lang_code:
        return jsonify({"error": f"Unsupported target language: {target_code}"}), 400

    model = get_available_translation_model()
    if not model:
        return jsonify({"error": "No translation model available"}), 503

    try:
        if text:
            translated = translate_with_qwen(
                text, target_lang_code, model, source_language, timeout=timeout
            )
            return jsonify({
                "model": model,
                "target": target_code,
                "target_language": target_language,
                "translated_text": translated,
                "translation": translated,
                "status": "success",
            })

        translations = translate_items_batch(
            items, target_lang_code, model, source_language, timeout=timeout
        )

        return jsonify({
            "model": model,
            "target": target_code,
            "target_language": target_language,
            "translated_text": None,
            "translations": translations,
            "status": "success",
        })
    except OllamaGenerationError as exc:
        status = 504 if "timed out" in str(exc).lower() else 502
        return jsonify({"error": str(exc), "model": model}), status
    except Exception as exc:
        log.exception("Translation failed")
        return jsonify({"error": str(exc), "model": model}), 500


@app.route("/api/models")
def models():
    """List available Ollama models."""
    try:
        import requests as req
        r = req.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        available = [m["name"] for m in r.json().get("models", [])]
        gen_model = get_available_model()
        translation_model = get_available_translation_model()
        qwen_ready = bool(gen_model)
        setup_hint = None
        if not qwen_ready:
            setup_hint = (
                "Qwen not found in Ollama. Install with: ollama pull qwen2.5:1.5b "
                "(then restart this API). Ensure Ollama is running."
            )
        return jsonify({
            "models": available,
            "generation_model": gen_model,
            "translation_model": translation_model,
            "embed_model": OLLAMA_EMBED,
            "ollama_running": ollama_is_listening(),
            "qwen_ready": qwen_ready,
            "setup_hint": setup_hint,
        })
    except Exception as e:
        return jsonify({"error": str(e), "models": []})


@app.route("/api/health")
def app_health():
    """Lightweight application liveness check for Docker and load balancers."""
    return jsonify({"status": "healthy", "service": "commonsource"}), 200


@app.route("/api/health/models")
def model_health():
    """Detailed model/Ollama health for the operator health page."""
    started = time.time()
    report: Dict[str, Any] = {
        "ollama_base": OLLAMA_BASE,
        "ollama_running": False,
        "models": [],
        "generation_model": None,
        "translation_model": None,
        "qwen_ready": False,
        "generation_candidates": GENERATION_MODELS,
        "translation_candidates": TRANSLATION_MODELS,
        "error": None,
        "seconds": 0.0,
    }
    try:
        report["ollama_running"] = ollama_is_listening()
        if report["ollama_running"]:
            import requests as req
            tags_started = time.time()
            r = req.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            _ollama_model_cache["models"] = models
            _ollama_model_cache["ts"] = time.time()
            report["models"] = models
            report["tag_seconds"] = round(time.time() - tags_started, 3)
        else:
            report["error"] = "Ollama is not reachable on localhost:11434"

        generation_model = get_available_model()
        translation_ollama = get_available_ollama_model(TRANSLATION_MODELS)
        report["generation_model"] = generation_model
        report["translation_model"] = translation_ollama or "LOCAL"
        report["qwen_ready"] = bool(
            (generation_model and "qwen" in generation_model.lower())
            or (translation_ollama and "qwen" in translation_ollama.lower())
        )
        report["seconds"] = round(time.time() - started, 3)
        return jsonify(report)
    except Exception as exc:
        log.exception("Model health check failed")
        report["error"] = str(exc)
        report["seconds"] = round(time.time() - started, 3)
        return jsonify(report), 200


@app.route("/api/llm/health")
@app.route("/api/health/llm")
def llm_health_api():
    """Report configured LLM provider, active model, fallback, and API availability."""
    timeout = get_request_timeout(8, minimum=2, maximum=30)
    configured = os.getenv("COMMONSOURCE_LLM_PROVIDER", "gemini").strip().lower() or "gemini"
    fallback_provider = os.getenv("COMMONSOURCE_LLM_FALLBACK_PROVIDERS", "ollama").split(",", 1)[0].strip() or "ollama"
    model = (
        os.getenv("COMMONSOURCE_GEMINI_MODEL")
        or os.getenv("COMMONSOURCE_LLM_MODEL")
        or "gemini-2.5-flash"
    ).strip()
    report: Dict[str, Any] = {
        "configured_provider": configured,
        "active_provider": None,
        "provider": configured,
        "model": model,
        "fallback_provider": fallback_provider,
        "fallback_available": False,
        "api_available": False,
        "configured": False,
        "status": "unhealthy",
        "error": None,
    }
    try:
        if HAS_LLM_PROVIDER and provider_llm_health and llm_provider_status:
            status = llm_provider_status(model)
            health = provider_llm_health(timeout=timeout)
            provider_order = status.get("provider_order") or [configured]
            fallback_available = bool(health.get("fallback_available"))
            api_available = bool(health.get("api_connected") or fallback_available)
            active_provider = configured if health.get("api_connected") else (health.get("fallback_provider") if fallback_available else None)
            report.update({
                "active_provider": active_provider,
                "provider_order": provider_order,
                "models": status.get("models", {}),
                "model": health.get("model") or model,
                "fallback_provider": health.get("fallback_provider") or fallback_provider,
                "fallback_model": health.get("fallback_model"),
                "fallback_available": fallback_available,
                "api_available": api_available,
                "configured": bool(health.get("configured")),
                "last_error": health.get("last_error"),
                "status": "healthy" if api_available else "unhealthy",
            })
        else:
            report.update({
                "active_provider": configured if os.getenv("GEMINI_API_KEY") else None,
                "api_available": bool(os.getenv("GEMINI_API_KEY")),
                "configured": bool(os.getenv("GEMINI_API_KEY")),
                "error": "llm_provider module is not available",
                "status": "healthy" if os.getenv("GEMINI_API_KEY") else "unhealthy",
            })
        return jsonify(report), 200 if report["status"] == "healthy" else 503
    except Exception as exc:
        log.exception("LLM health endpoint failed")
        report["error"] = str(exc)
        return jsonify(report), 500


@app.route("/api/stats")
def stats():
    conn = get_conn()
    articles  = conn.execute("SELECT COUNT(*) FROM commonsource_articles").fetchone()[0]
    chunks    = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    embedded  = conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL").fetchone()[0]
    authors   = conn.execute("SELECT COUNT(DISTINCT author) FROM commonsource_articles WHERE author != ''").fetchone()[0]
    source_types = {
        row["source_type"] or "news": row["count"]
        for row in conn.execute(
            "SELECT source_type, COUNT(*) AS count FROM commonsource_articles GROUP BY source_type"
        ).fetchall()
    }
    content_types = {
        row["content_type"] or "unspecified": row["count"]
        for row in conn.execute(
            "SELECT content_type, COUNT(*) AS count FROM commonsource_articles GROUP BY content_type"
        ).fetchall()
    }
    source_families = {
        row["source_family"] or "unknown": row["count"]
        for row in conn.execute(
            "SELECT source_family, COUNT(*) AS count FROM commonsource_articles GROUP BY source_family"
        ).fetchall()
    }
    source_media = {
        row["source_medium"] or "unknown": row["count"]
        for row in conn.execute(
            "SELECT source_medium, COUNT(*) AS count FROM commonsource_articles GROUP BY source_medium"
        ).fetchall()
    }
    source_origins = {
        row["source_origin"] or "unknown": row["count"]
        for row in conn.execute(
            "SELECT source_origin, COUNT(*) AS count FROM commonsource_articles GROUP BY source_origin"
        ).fetchall()
    }
    date_range = conn.execute(
        "SELECT MIN(date_published), MAX(date_published) FROM commonsource_articles WHERE date_published != ''"
    ).fetchone()
    conn.close()
    return jsonify({
        "articles":    articles,
        "chunks":      chunks,
        "embedded":    embedded,
        "coverage":    f"{embedded / chunks * 100:.1f}%" if chunks else "0%",
        "authors":     authors,
        "date_from":   date_range[0],
        "date_to":     date_range[1],
        "publication": "CommonSource",
        "source_types": source_types,
        "content_types": content_types,
        "source_families": source_families,
        "source_media": source_media,
        "source_origins": source_origins,
    })


@app.route("/api/corpus/stats")
def corpus_stats_page():
    """Detailed corpus stats for the operator-facing statistics page."""
    conn = get_conn()
    try:
        articles = conn.execute("SELECT COUNT(*) FROM commonsource_articles").fetchone()[0]
        assets = conn.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL"
        ).fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(date_published), MAX(date_published) FROM commonsource_articles WHERE date_published != ''"
        ).fetchone()

        def counts(sql: str) -> Dict[str, int]:
            return {
                (row["label"] or "unknown"): int(row["count"])
                for row in conn.execute(sql).fetchall()
            }

        top_publications = [
            dict(r) for r in conn.execute(
                """
                SELECT publication AS label, COUNT(*) AS count
                FROM commonsource_articles
                GROUP BY publication
                ORDER BY count DESC, publication
                LIMIT 12
                """
            ).fetchall()
        ]
        yearly_counts = [
            dict(r) for r in conn.execute(
                """
                SELECT substr(date_published, 1, 4) AS year, COUNT(*) AS count
                FROM commonsource_articles
                WHERE date_published != ''
                GROUP BY substr(date_published, 1, 4)
                ORDER BY year
                """
            ).fetchall()
        ]
        recent_uploads = [
            dict(r) for r in conn.execute(
                """
                SELECT asset_id, publication, article_title, source_type, language, created_at
                FROM commonsource_articles
                WHERE source_origin = 'upload'
                ORDER BY created_at DESC
                LIMIT 8
                """
            ).fetchall()
        ]
        return jsonify({
            "articles": articles,
            "assets": assets,
            "chunks": chunks,
            "embedded": embedded,
            "coverage": f"{embedded / chunks * 100:.1f}%" if chunks else "0%",
            "date_from": date_range[0],
            "date_to": date_range[1],
            "source_types": counts("SELECT source_type AS label, COUNT(*) AS count FROM commonsource_articles GROUP BY source_type"),
            "content_types": counts("SELECT content_type AS label, COUNT(*) AS count FROM commonsource_articles GROUP BY content_type"),
            "source_families": counts("SELECT source_family AS label, COUNT(*) AS count FROM commonsource_articles GROUP BY source_family"),
            "source_media": counts("SELECT source_medium AS label, COUNT(*) AS count FROM commonsource_articles GROUP BY source_medium"),
            "source_origins": counts("SELECT source_origin AS label, COUNT(*) AS count FROM commonsource_articles GROUP BY source_origin"),
            "top_publications": top_publications,
            "yearly_counts": yearly_counts,
            "recent_uploads": recent_uploads,
        })
    except Exception as exc:
        log.exception("Corpus stats page failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/articles")
def articles():
    limit  = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    author = request.args.get("author", "")
    conn   = get_conn()
    if author:
        rows = conn.execute(
            "SELECT * FROM commonsource_articles WHERE author LIKE ? ORDER BY date_published DESC LIMIT ? OFFSET ?",
            (f"%{author}%", limit, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM commonsource_articles ORDER BY date_published DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Publisher & feed management ───────────────────────────────────────────────

def publisher_profile_response(conn: sqlite3.Connection) -> Dict[str, Any]:
    user = g.current_user
    profile = conn.execute("SELECT * FROM publisher_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    publisher = current_user_publisher(conn)
    pending_app = conn.execute(
        """
        SELECT * FROM publisher_applications
        WHERE user_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user["id"],),
    ).fetchone()
    return {
        "profile": row_to_dict(profile),
        "publisher": row_to_dict(publisher),
        "pending_application": row_to_dict(pending_app),
    }


@app.route("/api/publisher/profile", methods=["GET"])
@require_auth
def publisher_profile_get():
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        return jsonify(publisher_profile_response(conn)), 200
    finally:
        conn.close()


@app.route("/api/publisher/profile", methods=["PUT"])
@app.route("/publisher/profile", methods=["PUT"])
@require_roles("super_admin", "admin", "publisher")
@require_csrf
def publisher_profile_put():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    org = (data.get("organization_name") or "").strip()
    if not org:
        return jsonify({"error": "organization_name is required"}), 400
    requested_status = (data.get("verification_status") or "pending").strip()
    if requested_status not in {"pending", "verified", "rejected", "suspended"}:
        return jsonify({"error": "Invalid verification_status"}), 400

    fields = {
        "organization_name": org,
        "description": (data.get("description") or "").strip(),
        "website": (data.get("website") or "").strip(),
        "logo_url": (data.get("logo_url") or "").strip(),
        "languages": (data.get("languages") or "").strip(),
        "topics": (data.get("topics") or "").strip(),
        "coverage_regions": (data.get("coverage_regions") or "").strip(),
        "verification_status": requested_status,
    }
    conn = get_conn()
    try:
        now = utc_now()
        existing = conn.execute(
            "SELECT id, verification_status FROM publisher_profiles WHERE user_id = ?",
            (g.current_user["id"],),
        ).fetchone()
        if g.current_user["role"] not in ADMIN_ROLES:
            fields["verification_status"] = existing["verification_status"] if existing else "pending"
        if existing:
            conn.execute(
                """
                UPDATE publisher_profiles
                SET organization_name = ?, description = ?, website = ?, logo_url = ?, languages = ?,
                    topics = ?, coverage_regions = ?, verification_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fields["organization_name"], fields["description"], fields["website"],
                    fields["logo_url"], fields["languages"], fields["topics"],
                    fields["coverage_regions"], fields["verification_status"], now, existing["id"],
                ),
            )
            profile_id = existing["id"]
        else:
            profile_id = make_id("pp")
            conn.execute(
                """
                INSERT INTO publisher_profiles
                  (id, user_id, organization_name, description, website, logo_url, languages, topics,
                   coverage_regions, verification_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id, g.current_user["id"], fields["organization_name"], fields["description"],
                    fields["website"], fields["logo_url"], fields["languages"], fields["topics"],
                    fields["coverage_regions"], fields["verification_status"], now, now,
                ),
            )
        record_audit(conn, "Publisher Profile Updated", "publisher_profile", profile_id)
        conn.commit()
        profile = conn.execute("SELECT * FROM publisher_profiles WHERE id = ?", (profile_id,)).fetchone()
        return jsonify({"profile": dict(profile), "message": "Publisher profile saved"}), 200
    except Exception as exc:
        conn.rollback()
        log.exception("Publisher profile update failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/publisher/dashboard")
@require_roles("super_admin", "admin", "publisher")
def publisher_dashboard():
    ensure_phase2a_schema()
    publisher_id = (request.args.get("publisher_id") or "").strip()
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        user = g.current_user
        if publisher_id:
            publisher = conn.execute("SELECT * FROM publishers WHERE id = ?", (publisher_id,)).fetchone()
            if not publisher:
                return jsonify({"error": "Publisher not found"}), 404
            if user["role"] == "publisher" and not can_manage_publisher(publisher):
                return jsonify({"error": "You can only view your own publisher dashboard"}), 403
        else:
            publisher = current_user_publisher(conn)
            if not publisher:
                return jsonify({"error": "No publisher account linked to current user"}), 404

        profile = conn.execute(
            """
            SELECT pp.*
            FROM publisher_profiles pp
            JOIN users u ON u.id = pp.user_id
            WHERE lower(u.email) = lower(?)
            LIMIT 1
            """,
            (publisher["contact_email"],),
        ).fetchone()
        feed_count = conn.execute(
            "SELECT COUNT(*) FROM rss_feeds WHERE publisher_id = ? AND deleted_at IS NULL",
            (publisher["id"],),
        ).fetchone()[0]
        article_count = conn.execute(
            "SELECT COUNT(*) FROM commonsource_articles WHERE lower(publication) = lower(?)",
            (publisher["name"],),
        ).fetchone()[0]
        recent_uploads = conn.execute(
            """
            SELECT asset_id, article_title, source_type, source_origin, created_at
            FROM commonsource_articles
            WHERE lower(publication) = lower(?)
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (publisher["name"],),
        ).fetchall()
        recent_activity = conn.execute(
            """
            SELECT al.action, al.resource_type, al.resource_id, al.timestamp, u.email AS user_email
            FROM audit_logs al
            LEFT JOIN users u ON u.id = al.user_id
            WHERE al.resource_id IN (
                SELECT id FROM rss_feeds WHERE publisher_id = ?
                UNION
                SELECT asset_id FROM commonsource_articles WHERE lower(publication) = lower(?)
            )
            OR lower(u.email) = lower(?)
            ORDER BY al.timestamp DESC
            LIMIT 10
            """,
            (publisher["id"], publisher["name"], publisher["contact_email"]),
        ).fetchall()
        topic_counts: Dict[str, int] = {}
        for row in conn.execute(
            "SELECT theme FROM commonsource_articles WHERE lower(publication) = lower(?) AND theme != ''",
            (publisher["name"],),
        ).fetchall():
            for topic in re.split(r"[,;|]", row["theme"] or ""):
                topic = topic.strip()
                if topic:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1
        top_topics = [
            {"name": name, "count": count}
            for name, count in sorted(topic_counts.items(), key=lambda item: item[1], reverse=True)[:10]
        ]
        top_tags: List[Dict[str, Any]] = []
        if table_exists(conn, "article_tags"):
            top_tags = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT t.name, t.slug, COUNT(*) AS count
                    FROM article_tags at
                    JOIN tags t ON t.id = at.tag_id
                    JOIN commonsource_articles cs ON cs.asset_id = at.article_id
                    WHERE lower(cs.publication) = lower(?)
                    GROUP BY t.id
                    ORDER BY count DESC, t.name
                    LIMIT 10
                    """,
                    (publisher["name"],),
                ).fetchall()
            ]
        return jsonify({
            "publisher": dict(publisher),
            "profile": row_to_dict(profile),
            "verification_status": (profile["verification_status"] if profile else publisher["status"]),
            "feed_count": feed_count,
            "article_count": article_count,
            "recent_uploads": [dict(row) for row in recent_uploads],
            "recent_activity": [dict(row) for row in recent_activity],
            "top_topics": top_topics,
            "top_tags": top_tags,
        }), 200
    except Exception as exc:
        log.exception("Publisher dashboard failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/publisher/apply", methods=["POST"])
@require_roles("reader", "reviewer", "publisher")
@require_csrf
def publisher_apply():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    org = (data.get("organization_name") or "").strip()
    website = (data.get("website") or "").strip()
    reason = (data.get("reason") or "").strip()
    if not org:
        return jsonify({"error": "organization_name is required"}), 400
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT id FROM publisher_applications WHERE user_id = ? AND status = 'pending'",
            (g.current_user["id"],),
        ).fetchone()
        if pending:
            return jsonify({"error": "Publisher application already pending", "id": pending["id"]}), 409
        app_id = make_id("pa")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO publisher_applications
              (id, user_id, organization_name, website, reason, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (app_id, g.current_user["id"], org, website, reason, now, now),
        )
        record_audit(conn, "Publisher Application Submitted", "publisher_application", app_id)
        conn.commit()
        application = conn.execute("SELECT * FROM publisher_applications WHERE id = ?", (app_id,)).fetchone()
        return jsonify({"application": dict(application), "message": "Publisher application submitted"}), 201
    except Exception as exc:
        conn.rollback()
        log.exception("Publisher application failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/admin/publisher-applications")
@require_roles("super_admin", "admin")
def admin_publisher_applications():
    ensure_phase2a_schema()
    status = (request.args.get("status") or "").strip()
    search = (request.args.get("search") or "").strip().lower()
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 25)), 1), 100)
    except ValueError:
        return jsonify({"error": "page and per_page must be integers"}), 400
    where: List[str] = []
    params: List[Any] = []
    if status:
        if status not in {"pending", "approved", "rejected"}:
            return jsonify({"error": "Invalid status"}), 400
        where.append("pa.status = ?")
        params.append(status)
    if search:
        where.append("(lower(pa.organization_name) LIKE ? OR lower(u.email) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM publisher_applications pa JOIN users u ON u.id = pa.user_id {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT pa.*, u.name AS user_name, u.email AS user_email, u.role AS user_role,
                   reviewer.email AS reviewed_by_email
            FROM publisher_applications pa
            JOIN users u ON u.id = pa.user_id
            LEFT JOIN users reviewer ON reviewer.id = pa.reviewed_by
            {where_sql}
            ORDER BY pa.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        return jsonify({
            "applications": [dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
        })
    finally:
        conn.close()


def _review_publisher_application(app_id: str, status: str) -> tuple[Dict[str, Any], int]:
    if status not in {"approved", "rejected"}:
        return {"error": "Invalid application status"}, 400
    data = request.get_json(silent=True) or {}
    notes = (data.get("review_notes") or data.get("notes") or "").strip()
    conn = get_conn()
    try:
        app_row = conn.execute("SELECT * FROM publisher_applications WHERE id = ?", (app_id,)).fetchone()
        if not app_row:
            return {"error": "Application not found"}, 404
        if app_row["status"] != "pending":
            return {"error": "Application has already been reviewed"}, 409
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (app_row["user_id"],)).fetchone()
        if not user_row:
            return {"error": "Applicant user not found"}, 404
        now = utc_now()
        conn.execute(
            """
            UPDATE publisher_applications
            SET status = ?, review_notes = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, notes, g.current_user["id"], now, now, app_id),
        )
        profile_status = "verified" if status == "approved" else "rejected"
        profile = ensure_publisher_profile_for_user(
            conn,
            dict(user_row),
            organization_name=app_row["organization_name"],
            website=app_row["website"],
            description=app_row["reason"],
            verification_status=profile_status,
        )
        publisher = None
        if status == "approved":
            conn.execute(
                "UPDATE users SET role = 'publisher', updated_at = ? WHERE id = ?",
                (now, app_row["user_id"]),
            )
            publisher = ensure_legacy_publisher_for_profile(conn, dict(user_row), profile)
            record_audit(conn, "Publisher Approved", "publisher_application", app_id)
        else:
            record_audit(conn, "Publisher Rejected", "publisher_application", app_id)
        conn.commit()
        application = conn.execute("SELECT * FROM publisher_applications WHERE id = ?", (app_id,)).fetchone()
        return {
            "application": dict(application),
            "profile": profile,
            "publisher": publisher,
            "message": f"Publisher application {status}",
        }, 200
    except Exception as exc:
        conn.rollback()
        log.exception("Publisher application review failed")
        return {"error": str(exc)}, 500
    finally:
        conn.close()


@app.route("/api/admin/publisher-applications/<app_id>/approve", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_approve_publisher_application(app_id: str):
    result, status = _review_publisher_application(app_id, "approved")
    return jsonify(result), status


@app.route("/api/admin/publisher-applications/<app_id>/reject", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_reject_publisher_application(app_id: str):
    result, status = _review_publisher_application(app_id, "rejected")
    return jsonify(result), status


@app.route("/api/reports", methods=["POST"])
@require_auth
@require_csrf
def report_article():
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    article_id = (data.get("article_id") or data.get("asset_id") or "").strip()
    reason = (data.get("reason") or "").strip()
    details = (data.get("details") or "").strip()
    if not article_id:
        return jsonify({"error": "article_id is required"}), 400
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    conn = get_conn()
    try:
        article = conn.execute(
            "SELECT asset_id FROM commonsource_articles WHERE asset_id = ?",
            (article_id,),
        ).fetchone()
        if not article:
            return jsonify({"error": "Article not found"}), 404
        existing = conn.execute(
            """
            SELECT id FROM reports
            WHERE article_id = ? AND reporter_id = ? AND status = 'pending'
            """,
            (article_id, g.current_user["id"]),
        ).fetchone()
        if existing:
            return jsonify({"error": "You already have a pending report for this article", "id": existing["id"]}), 409
        report_id = make_id("report")
        now = utc_now()
        conn.execute(
            """
            INSERT INTO reports
              (id, article_id, reporter_id, reason, details, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (report_id, article_id, g.current_user["id"], reason, details, now, now),
        )
        record_audit(conn, "Moderation Report Submitted", "report", report_id)
        conn.commit()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return jsonify({"report": dict(report), "message": "Report submitted"}), 201
    except Exception as exc:
        conn.rollback()
        log.exception("Article report failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/moderation/reports")
@require_roles("super_admin", "admin", "reviewer")
def moderation_reports():
    ensure_phase2a_schema()
    status = (request.args.get("status") or "").strip().lower()
    search = (request.args.get("search") or "").strip().lower()
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 25)), 1), 100)
    except ValueError:
        return jsonify({"error": "page and per_page must be integers"}), 400
    if status and status not in {"pending", "resolved", "dismissed", "escalated"}:
        return jsonify({"error": "Invalid report status"}), 400
    where: List[str] = []
    params: List[Any] = []
    if status:
        where.append("r.status = ?")
        params.append(status)
    if search:
        where.append("(lower(r.reason) LIKE ? OR lower(r.details) LIKE ? OR lower(cs.article_title) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    offset = (page - 1) * per_page
    conn = get_conn()
    try:
        total = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM reports r
            JOIN users reporter ON reporter.id = r.reporter_id
            LEFT JOIN commonsource_articles cs ON cs.asset_id = r.article_id
            {where_sql}
            """,
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT
              r.*,
              reporter.email AS reporter_email,
              reviewer.email AS reviewed_by_email,
              cs.article_title,
              cs.publication,
              cs.article_url
            FROM reports r
            JOIN users reporter ON reporter.id = r.reporter_id
            LEFT JOIN users reviewer ON reviewer.id = r.reviewed_by
            LEFT JOIN commonsource_articles cs ON cs.asset_id = r.article_id
            {where_sql}
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        return jsonify({
            "reports": [dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
        })
    except Exception as exc:
        log.exception("Could not list moderation reports")
        return jsonify({"error": str(exc), "reports": []}), 500
    finally:
        conn.close()


def _moderate_report(report_id: str, status: str) -> tuple[Dict[str, Any], int]:
    if status not in {"resolved", "dismissed", "escalated"}:
        return {"error": "Invalid moderation status"}, 400
    data = request.get_json(silent=True) or {}
    notes = (data.get("notes") or "").strip()
    conn = get_conn()
    try:
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            return {"error": "Report not found"}, 404
        if report["status"] != "pending":
            return {"error": "Report has already been reviewed"}, 409
        now = utc_now()
        conn.execute(
            """
            UPDATE reports
            SET status = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, g.current_user["id"], now, now, report_id),
        )
        action_id = make_id("mod")
        conn.execute(
            """
            INSERT INTO moderation_actions
              (id, report_id, actor_id, action, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action_id, report_id, g.current_user["id"], status, notes, now),
        )
        record_audit(conn, f"Moderation Report {status.title()}", "report", report_id)
        conn.commit()
        updated = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return {
            "report": dict(updated),
            "moderation_action_id": action_id,
            "message": f"Report {status}",
        }, 200
    except Exception as exc:
        conn.rollback()
        log.exception("Moderation action failed")
        return {"error": str(exc)}, 500
    finally:
        conn.close()


@app.route("/api/moderation/reports/<report_id>/resolve", methods=["POST"])
@require_roles("super_admin", "admin", "reviewer")
@require_csrf
def resolve_report(report_id: str):
    result, status = _moderate_report(report_id, "resolved")
    return jsonify(result), status


@app.route("/api/moderation/reports/<report_id>/dismiss", methods=["POST"])
@require_roles("super_admin", "admin", "reviewer")
@require_csrf
def dismiss_report(report_id: str):
    result, status = _moderate_report(report_id, "dismissed")
    return jsonify(result), status


@app.route("/api/moderation/reports/<report_id>/escalate", methods=["POST"])
@require_roles("super_admin", "admin", "reviewer")
@require_csrf
def escalate_report(report_id: str):
    result, status = _moderate_report(report_id, "escalated")
    return jsonify(result), status


def _init_publisher_tables():
    conn = get_conn()
    init_publisher_tables(conn)
    conn.close()


def init_publisher_tables(conn: sqlite3.Connection) -> None:
    """Ensure publisher workflow tables exist using the established Phase-1 schema."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS publishers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            geography TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'en',
            contact_email TEXT NOT NULL DEFAULT '',
            storage_mode TEXT NOT NULL DEFAULT 'federated',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_publishers_status ON publishers(status);
        CREATE INDEX IF NOT EXISTS idx_publishers_email ON publishers(contact_email);

        CREATE TABLE IF NOT EXISTS rss_feeds (
            id TEXT PRIMARY KEY,
            publisher_id TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            feed_name TEXT NOT NULL DEFAULT '',
            last_polled_at TEXT,
            last_item_hash TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            FOREIGN KEY(publisher_id) REFERENCES publishers(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_rss_feeds_publisher ON rss_feeds(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_rss_feeds_status ON rss_feeds(status);
        CREATE INDEX IF NOT EXISTS idx_rss_feeds_url ON rss_feeds(feed_url);
        """
    )
    conn.commit()


def get_feed_with_publisher(conn: sqlite3.Connection, feed_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          f.*,
          p.name AS publisher_name,
          p.contact_email AS publisher_contact_email,
          p.status AS publisher_status
        FROM rss_feeds f
        JOIN publishers p ON p.id = f.publisher_id
        WHERE f.id = ? AND f.deleted_at IS NULL
        """,
        (feed_id,),
    ).fetchone()


@app.route("/api/publisher/register", methods=["POST"])
def publisher_register():
    """Register a new publisher. Status starts as 'pending' until reviewed."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or data.get("publication_name") or "").strip()
    geo = (data.get("geography") or data.get("location") or "").strip()
    lang = (data.get("language") or data.get("languages") or "en").strip()
    email = (data.get("contact_email") or data.get("email") or "").strip().lower()
    mode = (data.get("storage_mode") or "federated").strip().lower()

    if not name:
        return jsonify({"error": "Publication name is required"}), 400
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "A valid contact email is required"}), 400
    if mode not in ("federated", "hosted"):
        return jsonify({"error": "storage_mode must be 'federated' or 'hosted'"}), 400

    conn = get_conn()
    try:
        init_publisher_tables(conn)
        existing = conn.execute(
            """
            SELECT id, status
            FROM publishers
            WHERE lower(name) = lower(?) OR lower(contact_email) = lower(?)
            """,
            (name, email),
        ).fetchone()
        if existing:
            return jsonify({
                "error": "Publisher already registered",
                "id": existing["id"],
                "status": existing["status"],
            }), 409

        pub_id = make_id("pub")
        conn.execute(
            """
            INSERT INTO publishers
              (id, name, geography, language, contact_email, storage_mode, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (pub_id, name, geo, lang, email, mode, utc_now()),
        )
        conn.commit()
        log.info("Publisher registered: id=%s name=%s email=%s", pub_id, name, email)
        return jsonify({
            "id": pub_id,
            "name": name,
            "status": "pending",
            "message": "Registration received. We'll review and activate your account within 48 hours.",
        }), 201
    except Exception as exc:
        conn.rollback()
        log.exception("Publisher registration failed")
        return jsonify({"error": f"Publisher registration failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/feed/add", methods=["POST"])
@require_roles("super_admin", "admin", "publisher")
@require_csrf
def feed_add():
    """Add an RSS/podcast feed for an approved publisher."""
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    publisher_id = (data.get("publisher_id") or "").strip()
    feed_url = (data.get("feed_url") or "").strip()
    feed_name = (data.get("feed_name") or "").strip()

    if not publisher_id or not feed_url:
        return jsonify({"error": "publisher_id and feed_url are required"}), 400
    parsed = urlparse(feed_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"error": "feed_url must be a valid http(s) URL"}), 400

    conn = get_conn()
    try:
        init_publisher_tables(conn)

        pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (publisher_id,)).fetchone()
        if not pub:
            return jsonify({"error": "Publisher not found"}), 404
        if pub["status"] != "approved":
            return jsonify({"error": "Publisher must be approved before feeds can be added"}), 403
        if not can_manage_publisher(pub):
            return jsonify({"error": "You can only manage feeds for your own publisher account"}), 403

        existing = conn.execute(
            "SELECT id FROM rss_feeds WHERE lower(feed_url) = lower(?)",
            (feed_url,),
        ).fetchone()
        if existing:
            return jsonify({"error": "Feed already registered", "id": existing["id"]}), 409

        feed_id = make_id("feed")
        now = utc_now()
        conn.execute(
            """INSERT INTO rss_feeds (id, publisher_id, feed_url, feed_name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (feed_id, publisher_id, feed_url, feed_name or parsed.netloc, now, now),
        )
        record_audit(conn, "Feed Created", "feed", feed_id)
        conn.commit()
        log.info("Feed registered: id=%s publisher_id=%s url=%s", feed_id, publisher_id, feed_url)
        return jsonify({
            "id": feed_id,
            "publisher_id": publisher_id,
            "feed_url": feed_url,
            "feed_name": feed_name or parsed.netloc,
            "status": "active",
            "message": "Feed registered. First poll will run within 24 hours.",
        }), 201
    except Exception as exc:
        conn.rollback()
        log.exception("Feed registration failed")
        return jsonify({"error": f"Feed registration failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/feeds")
@require_roles("super_admin", "admin", "publisher")
def feeds_list():
    """List registered feeds with publisher metadata for the management UI."""
    ensure_phase2a_schema()
    status = (request.args.get("status") or "").strip().lower()
    publisher_id = (request.args.get("publisher_id") or "").strip()
    include_deleted = request.args.get("include_deleted", "").strip().lower() in {"1", "true", "yes"}
    allowed = {"active", "paused", "disabled", "error"}
    if status and status not in allowed:
        return jsonify({"error": f"status must be one of: {', '.join(sorted(allowed))}"}), 400
    if include_deleted and getattr(g, "current_user", {}).get("role") not in ADMIN_ROLES:
        return jsonify({"error": "Only admins can include deleted feeds"}), 403

    conn = get_conn()
    try:
        init_publisher_tables(conn)
        where: List[str] = [] if include_deleted else ["f.deleted_at IS NULL"]
        params: List[Any] = []
        user = getattr(g, "current_user", None)
        if user and user["role"] == "publisher":
            owner_rows = conn.execute(
                "SELECT id FROM publishers WHERE lower(contact_email) = lower(?)",
                (user["email"],),
            ).fetchall()
            owner_ids = [row["id"] for row in owner_rows]
            if not owner_ids:
                return jsonify({"feeds": []})
            if publisher_id and publisher_id not in owner_ids:
                return jsonify({"error": "You can only view feeds for your own publisher account"}), 403
        if status:
            where.append("f.status = ?")
            params.append(status)
        if publisher_id:
            where.append("f.publisher_id = ?")
            params.append(publisher_id)
        elif user and user["role"] == "publisher":
            where.append(f"f.publisher_id IN ({','.join('?' for _ in owner_ids)})")
            params.extend(owner_ids)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        rows = conn.execute(
            f"""
            SELECT
              f.id,
              f.publisher_id,
              f.feed_url,
              f.feed_name,
              f.status,
              f.last_polled_at,
              f.last_item_hash,
              f.created_at,
              f.updated_at,
              f.deleted_at,
              p.name AS publisher_name,
              p.status AS publisher_status
            FROM rss_feeds f
            LEFT JOIN publishers p ON p.id = f.publisher_id
            {where_sql}
            ORDER BY f.created_at DESC
            """,
            params,
        ).fetchall()
        return jsonify({"feeds": [dict(r) for r in rows]})
    except Exception as exc:
        log.exception("Could not list feeds")
        return jsonify({"error": str(exc), "feeds": []}), 500
    finally:
        conn.close()


@app.route("/api/feeds/<feed_id>", methods=["PUT"])
@require_roles("super_admin", "admin", "publisher")
@require_csrf
def feed_update(feed_id: str):
    """Edit a feed while enforcing publisher ownership."""
    ensure_phase2a_schema()
    data = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}
    if "feed_url" in data:
        feed_url = (data.get("feed_url") or "").strip()
        parsed = urlparse(feed_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return jsonify({"error": "feed_url must be a valid http(s) URL"}), 400
        updates["feed_url"] = feed_url
    if "feed_name" in data:
        updates["feed_name"] = (data.get("feed_name") or "").strip()
    if "status" in data:
        status = (data.get("status") or "").strip().lower()
        if status not in {"active", "paused", "disabled", "error"}:
            return jsonify({"error": "Invalid feed status"}), 400
        updates["status"] = status
    if not updates:
        return jsonify({"error": "No editable feed fields supplied"}), 400

    conn = get_conn()
    try:
        init_publisher_tables(conn)
        feed = get_feed_with_publisher(conn, feed_id)
        if not feed:
            return jsonify({"error": "Feed not found"}), 404
        pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (feed["publisher_id"],)).fetchone()
        if not pub or not can_manage_publisher(pub):
            return jsonify({"error": "You can only edit feeds for your own publisher account"}), 403
        if "feed_url" in updates:
            duplicate = conn.execute(
                "SELECT id FROM rss_feeds WHERE lower(feed_url) = lower(?) AND id != ? AND deleted_at IS NULL",
                (updates["feed_url"], feed_id),
            ).fetchone()
            if duplicate:
                return jsonify({"error": "Feed URL already registered", "id": duplicate["id"]}), 409
        assignments = [f"{field} = ?" for field in updates]
        params = list(updates.values())
        assignments.append("updated_at = ?")
        params.append(utc_now())
        params.append(feed_id)
        conn.execute(
            f"UPDATE rss_feeds SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        record_audit(conn, "Feed Updated", "feed", feed_id)
        conn.commit()
        updated = get_feed_with_publisher(conn, feed_id)
        return jsonify({"feed": dict(updated), "message": "Feed updated"}), 200
    except Exception as exc:
        conn.rollback()
        log.exception("Feed update failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/feeds/<feed_id>", methods=["DELETE"])
@require_roles("super_admin", "admin", "publisher")
@require_csrf
def feed_delete(feed_id: str):
    """Soft-delete a feed while preserving historical articles."""
    ensure_phase2a_schema()
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        feed = get_feed_with_publisher(conn, feed_id)
        if not feed:
            return jsonify({"error": "Feed not found"}), 404
        pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (feed["publisher_id"],)).fetchone()
        if not pub or not can_manage_publisher(pub):
            return jsonify({"error": "You can only delete feeds for your own publisher account"}), 403
        now = utc_now()
        conn.execute(
            "UPDATE rss_feeds SET status = 'disabled', deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, feed_id),
        )
        record_audit(conn, "Feed Deleted", "feed", feed_id)
        conn.commit()
        return jsonify({"id": feed_id, "message": "Feed deleted"}), 200
    except Exception as exc:
        conn.rollback()
        log.exception("Feed delete failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/feeds/poll", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def feeds_poll():
    """Trigger a poll of all active feeds. Called by cron or manually."""
    try:
        from app.Ingestion.ingest_rss import poll_all_feeds  # type: ignore
    except BaseException as first_exc:
        log.debug("Primary feed worker import failed: %s", first_exc)
        try:
            from Ingestion.ingest_rss import poll_all_feeds  # type: ignore
        except BaseException as exc:
            log.warning("Feed polling unavailable: %s", exc)
            return jsonify({
                "error": "Feed polling worker is not available in this build",
                "feeds_polled": 0,
                "articles_added": 0,
                "detail": [],
            }), 501

    try:
        results = poll_all_feeds(DB_PATH)
        total_added = sum(r.get("added", 0) for r in results)
        record_audit_event("Feeds Poll Requested", "feed", "all")
        return jsonify({"feeds_polled": len(results), "articles_added": total_added, "detail": results})
    except Exception as exc:
        log.exception("Feed polling failed")
        return jsonify({"error": f"Feed polling failed: {exc}", "detail": []}), 500


@app.route("/api/ingest/upload", methods=["POST"])
@require_roles("super_admin", "admin", "publisher")
@require_csrf
def ingest_upload():
    """
    Archive upload endpoint. Accepts PDF, DOCX, or TXT files.
    Requires publisher_id in form data.
    """
    ensure_phase2a_schema()
    publisher_id = request.form.get("publisher_id", "").strip()
    if not publisher_id:
        return jsonify({"error": "publisher_id is required"}), 400

    conn = get_conn()
    try:
        init_publisher_tables(conn)

        pub = conn.execute("SELECT * FROM publishers WHERE id = ?", (publisher_id,)).fetchone()
        if not pub:
            return jsonify({"error": "Publisher not found"}), 404
        if pub["status"] != "approved":
            return jsonify({"error": "Publisher must be approved before articles can be uploaded"}), 403
        if not can_manage_publisher(pub):
            return jsonify({"error": "You can only upload for your own publisher account"}), 403

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        uploaded = request.files["file"]
        filename = Path(uploaded.filename or "upload.txt").name
        suffix = Path(filename).suffix.lower()
        raw_bytes = uploaded.read()
        if not raw_bytes:
            return jsonify({"error": "Uploaded file is empty"}), 422
        if len(raw_bytes) > 15 * 1024 * 1024:
            return jsonify({"error": "Uploaded file is too large; limit is 15 MB"}), 413

        try:
            if suffix == ".txt":
                text = raw_bytes.decode("utf-8", errors="replace")
            elif suffix == ".docx":
                import io
                from docx import Document
                doc = Document(io.BytesIO(raw_bytes))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            elif suffix == ".pdf":
                import io
                import fitz  # PyMuPDF
                doc = fitz.open(stream=raw_bytes, filetype="pdf")
                text = "\n".join(page.get_text() for page in doc)
            else:
                return jsonify({"error": f"Unsupported file type: {suffix}. Use .pdf, .docx, or .txt"}), 400
        except ImportError as exc:
            return jsonify({"error": f"Required parser is not installed: {exc}"}), 501
        except Exception as exc:
            return jsonify({"error": f"Could not read file: {exc}"}), 422

        text = text.strip()
        if len(text) < 100:
            return jsonify({"error": "File appears to be empty or unreadable"}), 422

        content_hash = hashlib.sha1(raw_bytes).hexdigest()
        existing = conn.execute(
            "SELECT id FROM knowledge_assets WHERE source_sha1 = ?", (content_hash,)
        ).fetchone()
        if existing:
            return jsonify({"message": "File already indexed", "asset_id": existing["id"], "duplicate": True}), 200

        title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Uploaded article"
        asset_id = make_id("asset")
        article_id = make_id("cs")
        source_profile = classify_source(
            {
                "publication": pub["name"],
                "source_type": request.form.get("source_type", "").strip(),
                "content_type": request.form.get("content_type", "").strip(),
                "source_family": request.form.get("source_family", "").strip(),
                "source_medium": request.form.get("source_medium", "").strip(),
                "source_origin": request.form.get("source_origin", "").strip() or "upload",
                "theme": request.form.get("theme", "").strip(),
                "collection": request.form.get("collection", "").strip(),
                "language": request.form.get("language", pub["language"] if "language" in pub.keys() else "").strip(),
            },
            path=filename,
            publication=pub["name"],
            default_source_type=request.form.get("source_type", "").strip() or "development",
            source_origin="upload",
        )
        asset_metadata = {
            "publication": pub["name"],
            "source_type": source_profile["source_type"],
            "content_type": source_profile["content_type"],
            "source_family": source_profile["source_family"],
            "source_medium": source_profile["source_medium"],
            "source_origin": source_profile["source_origin"],
            "theme": source_profile["theme"],
            "collection": source_profile["collection"],
            "language": source_profile["language"],
            "sha1": content_hash,
            "source_profile": source_profile,
            "source": "upload",
        }

        now = utc_now()
        conn.execute(
            """INSERT INTO knowledge_assets
               (id, title, source_type, source_path, source_sha1, raw_text, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                asset_id,
                title,
                source_profile["source_type"],
                filename,
                content_hash,
                text,
                json.dumps(asset_metadata, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            """INSERT INTO commonsource_articles
               (id, asset_id, publication, author, date_published, location, article_title, article_url,
                source_type, content_type, source_family, source_medium, source_origin,
                theme, collection, language, source_profile_json, created_at)
               VALUES (?, ?, ?, '', '', '', ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article_id,
                asset_id,
                pub["name"],
                title,
                source_profile["source_type"],
                source_profile["content_type"],
                source_profile["source_family"],
                source_profile["source_medium"],
                source_profile["source_origin"],
                source_profile["theme"],
                source_profile["collection"],
                source_profile["language"],
                json.dumps(source_profile, ensure_ascii=False),
                now,
            ),
        )

        chunks = [c for c in chunk_text(text) if len(c.strip()) >= 40]
        qdrant_rows: List[Dict[str, Any]] = []
        qdrant_vectors: List[List[float]] = []
        embedded_chunks = 0
        for i, chunk in enumerate(chunks):
            blob, vec = embed_text(chunk)
            chunk_row_id = make_id("chunk")
            chunk_public_id = make_id("ck")
            if blob:
                embedded_chunks += 1
            conn.execute(
                """INSERT INTO knowledge_chunks
                   (id, asset_id, chunk_index, chunk_id, chunk_text, token_estimate,
                    embedding_blob, embedding_model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk_row_id,
                    asset_id,
                    i,
                    chunk_public_id,
                    chunk,
                    len(chunk.split()),
                    blob,
                    LOCAL_MODEL if blob else None,
                    utc_now(),
                ),
            )
            if vec:
                qdrant_rows.append({
                    "chunk_row_id": chunk_row_id,
                    "asset_id": asset_id,
                    "chunk_index": i,
                    "chunk_text": chunk,
                    "embedding_blob": blob,
                    "publication": pub["name"],
                    "author": "",
                    "date_published": "",
                    "location": "",
                    "article_title": title,
                    "article_url": "",
                    "source_type": source_profile["source_type"],
                    "content_type": source_profile["content_type"],
                    "source_family": source_profile["source_family"],
                    "source_medium": source_profile["source_medium"],
                    "source_origin": source_profile["source_origin"],
                    "theme": source_profile["theme"],
                    "source_path": filename,
                })
                qdrant_vectors.append(vec)

        knowledge_result = {"entity_count": 0, "tag_count": 0, "relationship_count": 0}
        try:
            from knowledge_layer import process_article_knowledge
            knowledge_result = process_article_knowledge(
                conn,
                article_id=asset_id,
                title=title,
                text=text,
                publication=pub["name"],
                metadata=asset_metadata,
            )
            record_audit(conn, "Entity Extraction", "article", asset_id)
            record_audit(conn, "Tag Generation", "article", asset_id)
        except Exception as exc:
            log.warning("Upload knowledge layer processing skipped for asset_id=%s: %s", asset_id, exc)

        record_audit(conn, "Article Uploaded", "article", asset_id)
        conn.commit()
        qdrant_indexed = 0
        if qdrant_rows:
            try:
                from retrieval.qdrant_store import ensure_collection, upsert_chunks_batch
                if ensure_collection():
                    qdrant_indexed = upsert_chunks_batch(qdrant_rows, qdrant_vectors)
            except Exception as exc:
                log.warning("Upload indexed in SQLite but Qdrant upsert failed: %s", exc)

        log.info(
            "Uploaded article indexed: asset_id=%s publisher_id=%s chunks=%s embedded=%s qdrant=%s",
            asset_id, publisher_id, len(chunks), embedded_chunks, qdrant_indexed,
        )
        return jsonify({
            "asset_id": asset_id,
            "article_id": article_id,
            "title": title,
            "chunks": len(chunks),
            "embedded_chunks": embedded_chunks,
            "qdrant_indexed": qdrant_indexed,
            "knowledge": knowledge_result,
            "publisher": pub["name"],
            "source_profile": source_profile,
            "message": "File ingested and indexed successfully.",
        }), 201
    except Exception as exc:
        conn.rollback()
        log.exception("Article upload failed")
        return jsonify({"error": f"Article upload failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/publishers")
def list_publishers():
    """List approved publishers."""
    conn  = get_conn()
    try:
        init_publisher_tables(conn)
        rows  = conn.execute(
            "SELECT id, name, geography, language, storage_mode, created_at FROM publishers WHERE status = 'approved' ORDER BY name"
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/dashboard")
@require_roles("super_admin", "admin")
def admin_dashboard():
    """Aggregate admin counts in one bounded DB pass for the dashboard UI."""
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        corpus = {
            "articles": conn.execute("SELECT COUNT(*) FROM commonsource_articles").fetchone()[0],
            "assets": conn.execute("SELECT COUNT(*) FROM knowledge_assets").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0],
            "embedded": conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL"
            ).fetchone()[0],
            "uploads": conn.execute(
                "SELECT COUNT(*) FROM commonsource_articles WHERE source_origin = 'upload'"
            ).fetchone()[0],
        }
        publishers = {
            (row["status"] or "unknown"): row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM publishers GROUP BY status"
            ).fetchall()
        }
        feeds = {
            (row["status"] or "unknown"): row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM rss_feeds GROUP BY status"
            ).fetchall()
        }
        user_rows = conn.execute(
            "SELECT role, is_active, COUNT(*) AS count FROM users GROUP BY role, is_active"
        ).fetchall()
        users = {"total": 0, "active": 0, "inactive": 0, "by_role": {}}
        for row in user_rows:
            count = row["count"]
            role = row["role"] or "unknown"
            users["total"] += count
            if row["is_active"]:
                users["active"] += count
            else:
                users["inactive"] += count
            users["by_role"][role] = users["by_role"].get(role, 0) + count
        pending = conn.execute(
            """
            SELECT id, name, geography, language, contact_email, storage_mode, status, created_at
            FROM publishers
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()
        recent_uploads = conn.execute(
            """
            SELECT asset_id, publication, article_title, source_type, language, created_at
            FROM commonsource_articles
            WHERE source_origin = 'upload'
            ORDER BY created_at DESC
            LIMIT 5
            """
        ).fetchall()
        recent_audits = conn.execute(
            """
            SELECT al.action, al.resource_type, al.resource_id, al.timestamp, u.email AS user_email
            FROM audit_logs al
            LEFT JOIN users u ON u.id = al.user_id
            ORDER BY al.timestamp DESC
            LIMIT 5
            """
        ).fetchall()
        return jsonify({
            "corpus": corpus,
            "publishers": publishers,
            "feeds": feeds,
            "users": users,
            "pending_publishers": [dict(r) for r in pending],
            "recent_uploads": [dict(r) for r in recent_uploads],
            "recent_audits": [dict(r) for r in recent_audits],
            "services": {
                "ollama_running": ollama_is_listening(),
            },
        })
    except Exception as exc:
        log.exception("Could not build admin dashboard")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/admin/publishers")
@require_roles("super_admin", "admin")
def admin_publishers():
    """List publishers for approval/admin workflows."""
    status = (request.args.get("status") or "").strip().lower()
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        params: List[Any] = []
        where = ""
        if status:
            where = "WHERE p.status = ?"
            params.append(status)
        rows = conn.execute(
            f"""
            SELECT
                p.id, p.name, p.geography, p.language, p.contact_email,
                p.storage_mode, p.status, p.created_at,
                COUNT(DISTINCT f.id) AS feeds,
                COUNT(DISTINCT a.asset_id) AS articles
            FROM publishers p
            LEFT JOIN rss_feeds f ON f.publisher_id = p.id
            LEFT JOIN commonsource_articles a ON lower(a.publication) = lower(p.name)
            {where}
            GROUP BY p.id
            ORDER BY
                CASE p.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                p.created_at DESC
            """,
            params,
        ).fetchall()
        return jsonify({"publishers": [dict(r) for r in rows]})
    except Exception as exc:
        log.exception("Could not list admin publishers")
        return jsonify({"error": str(exc), "publishers": []}), 500
    finally:
        conn.close()


def _set_publisher_status(pub_id: str, status: str) -> tuple[Dict[str, Any], int]:
    if status not in {"approved", "rejected", "pending"}:
        return {"error": "Invalid publisher status"}, 400
    conn = get_conn()
    try:
        init_publisher_tables(conn)
        row = conn.execute("SELECT * FROM publishers WHERE id = ?", (pub_id,)).fetchone()
        if not row:
            return {"error": "Publisher not found"}, 404
        conn.execute("UPDATE publishers SET status = ? WHERE id = ?", (status, pub_id))
        action = {
            "approved": "Publisher Approved",
            "rejected": "Publisher Rejected",
            "pending": "Publisher Marked Pending",
        }[status]
        record_audit(conn, action, "publisher", pub_id)
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM publishers WHERE id = ?", (pub_id,)).fetchone())
        log.info("Publisher status changed: id=%s status=%s", pub_id, status)
        return {"publisher": updated, "message": f"Publisher {status}"}, 200
    except Exception as exc:
        conn.rollback()
        log.exception("Could not update publisher status")
        return {"error": str(exc)}, 500
    finally:
        conn.close()


@app.route("/api/admin/publishers/<pub_id>/approve", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_approve_publisher(pub_id: str):
    result, status = _set_publisher_status(pub_id, "approved")
    return jsonify(result), status


@app.route("/api/admin/publishers/<pub_id>/reject", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_reject_publisher(pub_id: str):
    result, status = _set_publisher_status(pub_id, "rejected")
    return jsonify(result), status


@app.route("/api/admin/publishers/<pub_id>/pending", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_mark_publisher_pending(pub_id: str):
    result, status = _set_publisher_status(pub_id, "pending")
    return jsonify(result), status


@app.route("/api/admin/users")
@require_roles("super_admin", "admin")
def admin_users():
    ensure_phase2a_schema()
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", request.args.get("limit", 25))), 1), 100)
        active = parse_optional_bool(request.args.get("active"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    search = (request.args.get("search") or "").strip()
    role = (request.args.get("role") or "").strip()
    sort = (request.args.get("sort") or "created_desc").strip()
    if sort not in {"created_desc", "created_asc", "email_asc", "email_desc"}:
        return jsonify({"error": "Invalid sort"}), 400
    offset = (page - 1) * per_page

    conn = get_conn()
    try:
        total = auth_count_users(conn, search=search, role=role, active=active)
        users = auth_list_users(
            conn,
            search=search,
            role=role,
            active=active,
            limit=per_page,
            offset=offset,
            sort=sort,
        )
        return jsonify({"users": users, "page": page, "per_page": per_page, "total": total, "sort": sort})
    except AuthError as exc:
        return auth_error_response(exc)
    except Exception as exc:
        log.exception("Could not list users")
        return jsonify({"error": str(exc), "users": []}), 500
    finally:
        conn.close()


@app.route("/api/admin/users/<user_id>", methods=["PATCH"])
@require_roles("super_admin", "admin")
@require_csrf
def admin_update_user(user_id: str):
    ensure_phase2a_schema()
    actor = g.current_user
    data = request.get_json(silent=True) or {}
    requested_role = data.get("role") if "role" in data else None
    requested_name = data.get("name") if "name" in data else None
    try:
        requested_active = parse_optional_bool(data.get("is_active")) if "is_active" in data else None
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    conn = get_conn()
    try:
        before = conn.execute(
            "SELECT id, name, email, role, is_active, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not before:
            return jsonify({"error": "User not found"}), 404
        if before["role"] == "super_admin" and actor["role"] != "super_admin":
            return jsonify({"error": "Only a super_admin can modify a super_admin account"}), 403
        if requested_role in {"super_admin", "admin"} and actor["role"] != "super_admin":
            return jsonify({"error": "Only a super_admin can assign admin roles"}), 403
        if user_id == actor["id"] and (requested_role is not None or requested_active is False):
            return jsonify({"error": "You cannot change your own role or deactivate your own account"}), 400

        updated = auth_update_user(
            conn,
            user_id,
            name=requested_name,
            role=requested_role,
            is_active=requested_active,
        )
        if requested_active is False and before["is_active"]:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?",
                (utc_now(), user_id),
            )

        if requested_role is not None and requested_role != before["role"]:
            record_audit(conn, "User Role Changed", "user", user_id)
        if requested_active is not None and int(bool(requested_active)) != int(before["is_active"]):
            record_audit(conn, "User Activated" if requested_active else "User Suspended", "user", user_id)
        if requested_name is not None and requested_name.strip() != before["name"]:
            record_audit(conn, "User Updated", "user", user_id)
        conn.commit()
        log.info("[AUTH] User updated id=%s by=%s", user_id, actor["id"])
        return jsonify({"user": updated, "message": "User updated"}), 200
    except AuthError as exc:
        conn.rollback()
        return auth_error_response(exc)
    except Exception as exc:
        conn.rollback()
        log.exception("Could not update user")
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/admin/audit-logs")
@require_roles("super_admin", "admin")
def admin_audit_logs():
    ensure_phase2a_schema()
    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", request.args.get("limit", 25))), 1), 100)
    except ValueError:
        return jsonify({"error": "page and per_page must be integers"}), 400
    search = (request.args.get("search") or "").strip().lower()
    action = (request.args.get("action") or "").strip()
    resource_type = (request.args.get("resource_type") or "").strip()
    user_id = (request.args.get("user_id") or "").strip()
    sort = (request.args.get("sort") or "timestamp_desc").strip()
    if sort not in {"timestamp_desc", "timestamp_asc"}:
        return jsonify({"error": "Invalid sort"}), 400
    order_sql = "al.timestamp ASC" if sort == "timestamp_asc" else "al.timestamp DESC"
    offset = (page - 1) * per_page

    where: List[str] = []
    params: List[Any] = []
    if search:
        where.append("(lower(al.action) LIKE ? OR lower(al.resource_type) LIKE ? OR lower(u.email) LIKE ?)")
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern])
    if action:
        where.append("al.action = ?")
        params.append(action)
    if resource_type:
        where.append("al.resource_type = ?")
        params.append(resource_type)
    if user_id:
        where.append("al.user_id = ?")
        params.append(user_id)
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    conn = get_conn()
    try:
        total = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM audit_logs al
            LEFT JOIN users u ON u.id = al.user_id
            {where_sql}
            """,
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT
                al.id,
                al.user_id,
                al.action,
                al.resource_type,
                al.resource_id,
                al.timestamp,
                al.ip_address,
                u.name AS user_name,
                u.email AS user_email,
                u.role AS user_role
            FROM audit_logs al
            LEFT JOIN users u ON u.id = al.user_id
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()
        return jsonify({
            "audit_logs": [dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "sort": sort,
        })
    except Exception as exc:
        log.exception("Could not list audit logs")
        return jsonify({"error": str(exc), "audit_logs": []}), 500
    finally:
        conn.close()


# ── Debug Endpoints ───────────────────────────────────────────────────────────

@app.route("/api/debug/db")
def debug_db():
    """Debug endpoint: database statistics."""
    conn = get_conn()
    try:
        articles = conn.execute("SELECT COUNT(*) FROM commonsource_articles").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL").fetchone()[0]
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        return jsonify({
            "articles": articles,
            "chunks": chunks,
            "embedded_chunks": embedded,
            "tables": [t[0] for t in tables],
            "db_path": str(DB_PATH)
        })
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/retrieval")
def debug_retrieval():
    """Debug endpoint: trace retrieval pipeline."""
    from retrieval.pipeline import retrieve_sources
    from retrieval.qdrant_store import is_qdrant_available, qdrant_health

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    import time
    start = time.time()

    # Embedding
    embed_start = time.time()
    query_vec = embed(query)
    embed_time = time.time() - embed_start

    embedding_size = len(query_vec) if query_vec else 0

    # Qdrant check
    qdrant_available = is_qdrant_available()
    qdrant = qdrant_health()

    # Retrieval
    retrieve_start = time.time()
    result = retrieve_sources(query, top_k=8)
    retrieve_time = time.time() - retrieve_start

    total_time = time.time() - start

    return jsonify({
        "query": query,
        "embedding_size": embedding_size,
        "embedding_time_seconds": round(embed_time, 3),
        "qdrant_available": qdrant_available,
        "qdrant": qdrant,
        "retrieval_backend": result.get("retrieval_backend"),
        "retrieval_time_seconds": round(retrieve_time, 3),
        "total_time_seconds": round(total_time, 3),
        "candidate_count": len(result.get("results", [])),
        "final_results": result.get("count", 0),
        "sample_results": [
            {
                "title": r.get("title", "")[:60],
                "publication": r.get("publication"),
                "score": r.get("score")
            }
            for r in result.get("results", [])[:3]
        ],
        "error": result.get("error")
    })


# ── Start ─────────────────────────────────────────────────────────────────────

@app.route("/api/debug/model-test")
def debug_model_test():
    """Send a tiny prompt to the configured LLM and report latency."""
    model = get_llm_model()
    started = time.time()
    try:
        response = call_ollama(
            "Reply with exactly: CommonSource model healthy.",
            model,
            max_tokens=32,
            timeout=get_request_timeout(60, maximum=120),
            temperature=0.0,
            cache=False,
        )
        latency_ms = int((time.time() - started) * 1000)
        return jsonify({
            "model": model,
            "latency_ms": latency_ms,
            "status": "healthy",
        })
    except OllamaGenerationError as exc:
        latency_ms = int((time.time() - started) * 1000)
        status = 504 if "timed out" in str(exc).lower() else 502
        return jsonify({
            "model": model,
            "latency_ms": latency_ms,
            "status": "unhealthy",
            "error": str(exc),
        }), status
    except Exception as exc:
        log.exception("Model test failed")
        latency_ms = int((time.time() - started) * 1000)
        return jsonify({
            "model": model,
            "latency_ms": latency_ms,
            "status": "unhealthy",
            "error": str(exc),
        }), 500


@app.route("/api/retrieval/diagnostics")
def retrieval_diagnostics():
    """Operator-facing retrieval trace with bounded candidate and timing details."""
    from retrieval.keyword import extract_keywords
    from retrieval.pipeline import retrieve_sources
    from retrieval.qdrant_store import ann_search, is_qdrant_available, qdrant_health
    from retrieval.scoring import is_boilerplate, score_row
    from retrieval.sqlite_retriever import fetch_candidate_rows

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    try:
        top_k = min(max(int(request.args.get("k", 8)), 1), 20)
    except ValueError:
        top_k = 8
    try:
        candidate_limit = min(max(int(request.args.get("candidate_limit", 2500)), 50), 5000)
    except ValueError:
        candidate_limit = 2500

    started = time.time()
    try:
        keywords = extract_keywords(query)
        embed_started = time.time()
        query_vec = embed(query)
        embed_seconds = time.time() - embed_started

        qdrant_started = time.time()
        qdrant_available = bool(query_vec and is_qdrant_available())
        qdrant_hits = ann_search(query_vec, limit=max(top_k * 4, 20)) if qdrant_available and query_vec else []
        qdrant_seconds = time.time() - qdrant_started

        sqlite_started = time.time()
        rows = fetch_candidate_rows(query_vec, keywords, limit=candidate_limit)
        sqlite_seconds = time.time() - sqlite_started

        scored = []
        for row in rows:
            try:
                if is_boilerplate(row.get("chunk_text") or ""):
                    continue
                scored.append((score_row(row, query_vec, query, keywords), row))
            except Exception:
                continue
        scored.sort(key=lambda item: item[0], reverse=True)

        final_started = time.time()
        final = retrieve_sources(query, top_k=top_k)
        final_seconds = time.time() - final_started

        return jsonify({
            "query": query,
            "keywords": keywords,
            "embedding": {
                "available": bool(query_vec),
                "size": len(query_vec) if query_vec else 0,
                "seconds": round(embed_seconds, 3),
            },
            "qdrant": {
                "available": qdrant_available,
                "hits": len(qdrant_hits),
                "seconds": round(qdrant_seconds, 3),
                "health": qdrant_health(),
            },
            "sqlite": {
                "candidate_limit": candidate_limit,
                "candidates": len(rows),
                "scored": len(scored),
                "seconds": round(sqlite_seconds, 3),
            },
            "final": {
                "backend": final.get("retrieval_backend"),
                "count": final.get("count", 0),
                "seconds": round(final_seconds, 3),
                "results": final.get("results", []),
            },
            "sample_scored": [
                {
                    "asset_id": row.get("asset_id"),
                    "title": row.get("article_title"),
                    "publication": row.get("publication"),
                    "score": round(score, 4),
                }
                for score, row in scored[:8]
            ],
            "total_seconds": round(time.time() - started, 3),
        })
    except Exception as exc:
        log.exception("Retrieval diagnostics failed")
        return jsonify({"error": str(exc), "query": query}), 500


@app.route("/api/qdrant/health")
@app.route("/api/health/qdrant")
def qdrant_health_api():
    """Operational Qdrant health endpoint for production readiness checks."""
    from retrieval.qdrant_store import qdrant_health

    try:
        report = qdrant_health()
        status = 200 if report.get("available") else 503
        if not report.get("configured"):
            status = 200
        return jsonify(report), status
    except Exception as exc:
        log.exception("Qdrant health endpoint failed")
        return jsonify({"available": False, "error": str(exc)}), 500


@app.route("/api/qdrant/ensure", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def qdrant_ensure_api():
    """Create the configured Qdrant collection when it is missing."""
    from retrieval.qdrant_store import ensure_collection_report

    recreate = request.args.get("recreate", "false").lower() in ("1", "true", "yes")
    report = ensure_collection_report(recreate=recreate)
    if report.get("ensured"):
        record_audit_event(
            "Qdrant Collection Ensured",
            "qdrant",
            str(report.get("collection") or report.get("collection_name") or "commonsource"),
        )
    return jsonify(report), 200 if report.get("ensured") else 503


@app.route("/api/qdrant/index", methods=["POST"])
@require_roles("super_admin", "admin")
@require_csrf
def qdrant_index_api():
    """Index embedded SQLite chunks into Qdrant in bounded batches."""
    from retrieval.qdrant_store import index_sqlite_chunks

    try:
        data = request.get_json(silent=True) or {}
        limit = min(max(int(data.get("limit", request.args.get("limit", 1000))), 1), 5000)
        offset = max(int(data.get("offset", request.args.get("offset", 0))), 0)
        recreate = str(data.get("recreate", request.args.get("recreate", "false"))).lower() in ("1", "true", "yes")
    except (TypeError, ValueError):
        return jsonify({"error": "limit and offset must be integers"}), 400

    try:
        result = index_sqlite_chunks(limit=limit, offset=offset, recreate=recreate)
        if result.get("ok"):
            record_audit_event("Qdrant Index Started", "qdrant", "chunks")
        return jsonify(result), 200 if result.get("ok") else 503
    except Exception as exc:
        log.exception("Qdrant index endpoint failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    import threading

    reexec_project_venv_if_needed()

    threading.Thread(target=warmup_embeddings, daemon=True).start()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Run ingest_commonsource.py first.")
        raise SystemExit(1)

    conn = get_conn()
    articles = conn.execute("SELECT COUNT(*) FROM commonsource_articles").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE embedding_blob IS NOT NULL").fetchone()[0]
    conn.close()

    print(f"\nCommonSource Search API")
    print(f"  Articles : {articles}")
    print(f"  Embedded : {embedded} chunks")
    print(f"  DB       : {DB_PATH}")
    print(f"\n  -> http://localhost:{PORT}\n")

    debug_enabled = os.getenv("COMMONSOURCE_FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=PORT, debug=debug_enabled, use_reloader=debug_enabled, threaded=True)
