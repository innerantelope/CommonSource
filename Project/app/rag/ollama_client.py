"""Ollama generation client for Qwen."""

from __future__ import annotations

import re
import threading
from typing import List, Optional

from core.config import GENERATION_MODELS, OLLAMA_BASE, TRANSLATION_MODELS

_gen_lock = threading.Lock()


def ollama_is_listening() -> bool:
    try:
        import socket
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.25):
            return True
    except Exception:
        return False


def get_available_ollama_model(candidates: List[str]) -> Optional[str]:
    if not ollama_is_listening():
        return None
    try:
        import requests as req
        r = req.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        available = {m["name"].split(":")[0] for m in r.json().get("models", [])}
        available_full = {m["name"] for m in r.json().get("models", [])}
        for candidate in candidates:
            if candidate in available_full:
                return candidate
            base = candidate.split(":")[0]
            if base in available:
                for full in available_full:
                    if full.startswith(base):
                        return full
    except Exception:
        pass
    return None


def get_available_model() -> Optional[str]:
    return get_available_ollama_model(GENERATION_MODELS)


def get_available_translation_model() -> Optional[str]:
    return get_available_ollama_model(TRANSLATION_MODELS) or get_available_model()


def prepare_prompt_for_model(prompt: str, model: str) -> str:
    if model.lower().startswith("qwen3") and "/no_think" not in prompt:
        return f"{prompt.rstrip()}\n\n/no_think"
    return prompt


def clean_generation_response(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "done thinking." in text.lower():
        text = re.split(r"done thinking\.", text, flags=re.IGNORECASE)[-1]
    text = re.sub(r"^\s*Thinking\.\.\.\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def call_ollama(prompt: str, model: str, max_tokens: int = 300, timeout: float = 60) -> str:
    import requests as req
    prompt = prepare_prompt_for_model(prompt, model)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": max_tokens},
    }
    if model.lower().startswith("qwen3"):
        payload["think"] = False
    try:
        with _gen_lock:
            r = req.post(f"{OLLAMA_BASE}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        return clean_generation_response(r.json().get("response", ""))
    except Exception as e:
        return f"[error: {e}]"
