from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def _load_local_env_files() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for env_path in (project_root / ".env", project_root / ".env.local", project_root / "app" / ".env"):
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            logging.getLogger(__name__).exception("[LLM] Could not load env file %s", env_path)


_load_local_env_files()

from llm.gemini_provider import GeminiProvider

log = logging.getLogger(__name__)


class LLMProviderError(RuntimeError):
    """Raised when no configured LLM provider can complete a request."""


@dataclass
class ProviderResult:
    text: str
    provider: str
    model: str
    latency_ms: int


def configured_provider() -> str:
    return os.getenv("COMMONSOURCE_LLM_PROVIDER", "gemini").strip().lower() or "gemini"


def _provider_order() -> list[str]:
    selected = configured_provider()
    if selected in {"gemini", "auto"}:
        providers = ["gemini", "ollama"]
    else:
        configured_fallbacks = [
            item.strip().lower()
            for item in os.getenv("COMMONSOURCE_LLM_FALLBACK_PROVIDERS", "ollama").split(",")
            if item.strip()
        ]
        providers = [selected, *configured_fallbacks]
        if selected == "gemini" and "ollama" not in providers:
            providers.append("ollama")
    return list(dict.fromkeys(providers))


def _gemini_model() -> str:
    configured = os.getenv("COMMONSOURCE_GEMINI_MODEL", "").strip()
    shared = os.getenv("COMMONSOURCE_LLM_MODEL", "").strip()
    return configured or shared or "gemini-2.5-flash"


def _qwen_model() -> str:
    return os.getenv("COMMONSOURCE_QWEN_MODEL", "qwen2.5:1.5b").strip() or "qwen2.5:1.5b"


def _model_for(provider: str, preferred_model: str) -> str:
    if provider == "gemini":
        return _gemini_model()
    if provider == "ollama":
        if preferred_model and not preferred_model.lower().startswith("gemini"):
            return preferred_model
        return _qwen_model()
    if provider == "openrouter":
        return os.getenv("COMMONSOURCE_OPENROUTER_MODEL", preferred_model).strip() or preferred_model
    if provider == "groq":
        return os.getenv("COMMONSOURCE_GROQ_MODEL", preferred_model).strip() or preferred_model
    return preferred_model


def _json_response_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content
    if isinstance(payload.get("response"), str):
        return payload["response"]
    return ""


def _openai_compatible(
    *,
    provider: str,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    if not api_key:
        raise LLMProviderError(f"{provider} API key is not configured")
    response = requests.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        timeout=timeout,
    )
    response.raise_for_status()
    text = _json_response_text(response.json()).strip()
    if not text:
        raise LLMProviderError(f"{provider} returned an empty response")
    return text


def _generate_ollama(
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
    extra_options: Optional[Dict[str, Any]],
) -> str:
    base = os.getenv("OLLAMA_BASE_URL", os.getenv("OLLAMA_BASE", "http://localhost:11434")).rstrip("/")
    options = {"temperature": temperature, "num_predict": max_tokens}
    if extra_options:
        options.update(extra_options)
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }
    if model.lower().startswith("qwen3"):
        payload["think"] = False
    response = requests.post(f"{base}/api/generate", json=payload, timeout=timeout)
    response.raise_for_status()
    text = _json_response_text(response.json()).strip()
    if not text:
        raise LLMProviderError("Ollama returned an empty response")
    return text


def generate_text(
    prompt: str,
    *,
    preferred_model: str = "",
    max_tokens: int = 300,
    timeout: float = 60,
    temperature: float = 0.2,
    extra_options: Optional[Dict[str, Any]] = None,
    response_schema: Optional[Dict[str, Any]] = None,
) -> ProviderResult:
    errors: list[str] = []
    providers = _provider_order()
    deadline = time.monotonic() + max(float(timeout), 1.0)
    for index, provider in enumerate(providers):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("request timeout budget exhausted")
            break
        providers_left = len(providers) - index
        provider_timeout = remaining if providers_left == 1 else max(1.0, remaining / providers_left)
        model = _model_for(provider, preferred_model)
        started = time.time()
        try:
            if provider == "gemini":
                result = GeminiProvider().generate(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=provider_timeout,
                    response_schema=response_schema,
                )
                log.info("[LLM] provider=gemini model=%s latency_ms=%s", result.model, result.latency_ms)
                return ProviderResult(result.text, "gemini", result.model, result.latency_ms)
            if provider == "ollama":
                text = _generate_ollama(model, prompt, max_tokens, temperature, provider_timeout, extra_options)
            elif provider == "openrouter":
                text = _openai_compatible(
                    provider="OpenRouter",
                    url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
                    api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
                    model=model,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=provider_timeout,
                    headers={
                        "HTTP-Referer": os.getenv("COMMONSOURCE_PUBLIC_URL", "http://localhost:5050"),
                        "X-Title": "CommonSource",
                    },
                )
            elif provider == "groq":
                text = _openai_compatible(
                    provider="Groq",
                    url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1/chat/completions"),
                    api_key=os.getenv("GROQ_API_KEY", "").strip(),
                    model=model,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=provider_timeout,
                )
            else:
                raise LLMProviderError(f"Unsupported LLM provider: {provider}")
            latency_ms = int((time.time() - started) * 1000)
            log.info("[LLM] provider=%s model=%s latency_ms=%s", provider, model, latency_ms)
            return ProviderResult(text=text, provider=provider, model=model, latency_ms=latency_ms)
        except requests.exceptions.Timeout as exc:
            errors.append(f"{provider}: timed out after {int(provider_timeout)}s")
            log.warning("[LLM] %s timed out: %s", provider, exc)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            log.warning("[LLM] %s failed; trying fallback: %s", provider, exc)
    raise LLMProviderError("; ".join(errors) or "No LLM provider configured")


def generate(prompt: str, **kwargs: Any) -> ProviderResult:
    return generate_text(prompt, **kwargs)


def classify(
    prompt: str,
    *,
    schema: Dict[str, Any],
    preferred_model: str = "",
    max_tokens: int = 800,
    timeout: float = 45,
) -> ProviderResult:
    return generate_text(
        prompt,
        preferred_model=preferred_model,
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=0.1,
        response_schema=schema,
    )


def summarize(
    prompt: str,
    *,
    preferred_model: str = "",
    max_tokens: int = 500,
    timeout: float = 60,
) -> ProviderResult:
    return generate_text(
        prompt,
        preferred_model=preferred_model,
        max_tokens=max_tokens,
        timeout=timeout,
        temperature=0.2,
    )


def _qwen_fallback_available(timeout: float = 3) -> bool:
    try:
        base = os.getenv("OLLAMA_BASE_URL", os.getenv("OLLAMA_BASE", "http://localhost:11434")).rstrip("/")
        response = requests.get(f"{base}/api/tags", timeout=timeout)
        response.raise_for_status()
        available = [str(item.get("name") or "") for item in response.json().get("models", [])]
        expected = _qwen_model()
        return any(name == expected or name.split(":", 1)[0] == expected.split(":", 1)[0] for name in available)
    except Exception:
        return False


def llm_health(timeout: float = 8) -> Dict[str, Any]:
    gemini = GeminiProvider().health(timeout=timeout)
    return {
        "provider": configured_provider(),
        "model": _gemini_model(),
        "api_connected": bool(gemini.get("api_connected")),
        "configured": bool(gemini.get("configured")),
        "gemini_fallback_model": gemini.get("fallback_model"),
        "fallback_available": _qwen_fallback_available(),
        "fallback_provider": "ollama",
        "fallback_model": _qwen_model(),
        "last_error": gemini.get("last_error"),
    }


def provider_status(preferred_model: str = "") -> Dict[str, Any]:
    providers = _provider_order()
    return {
        "selected_provider": configured_provider(),
        "provider_order": providers,
        "models": {provider: _model_for(provider, preferred_model) for provider in providers},
        "configured": {
            "gemini": bool(os.getenv("GEMINI_API_KEY")),
            "ollama": True,
            "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
            "groq": bool(os.getenv("GROQ_API_KEY")),
        },
    }
