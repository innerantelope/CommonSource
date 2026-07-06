from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests


class GeminiProviderError(RuntimeError):
    """Raised when Gemini Flash and Flash-Lite cannot complete a request."""


@dataclass
class GeminiResult:
    text: str
    model: str
    latency_ms: int


class GeminiProvider:
    def __init__(self) -> None:
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.base_url = os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        ).rstrip("/")
        configured_model = os.getenv("COMMONSOURCE_GEMINI_MODEL", "").strip()
        shared_model = os.getenv("COMMONSOURCE_LLM_MODEL", "").strip()
        self.model = configured_model or shared_model or "gemini-2.5-flash"
        self.fallback_model = os.getenv(
            "COMMONSOURCE_GEMINI_FALLBACK_MODEL",
            "gemini-2.5-flash-lite",
        ).strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def models(self) -> list[str]:
        models = [self.model, self.fallback_model]
        return list(dict.fromkeys(model for model in models if model))

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    @staticmethod
    def _response_text(payload: Dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        return "\n".join(str(part.get("text", "")) for part in parts if part.get("text")).strip()

    def _generate(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        timeout: float,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> GeminiResult:
        if not self.configured:
            raise GeminiProviderError("GEMINI_API_KEY is not configured")
        generation_config: Dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        thinking_budget = int(os.getenv("COMMONSOURCE_GEMINI_THINKING_BUDGET", "0"))
        if thinking_budget >= 0 and "2.5" in model:
            generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
        if response_schema:
            generation_config.update({
                "responseMimeType": "application/json",
                "responseJsonSchema": response_schema,
            })
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        url = f"{self.base_url}/models/{quote(model, safe='')}:generateContent"
        started = time.time()
        response = requests.post(
            url,
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        )
        if not response.ok:
            detail = ""
            try:
                detail = str((response.json().get("error") or {}).get("message") or "")
            except Exception:
                detail = response.text[:300]
            raise GeminiProviderError(
                f"Gemini {model} returned HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        text = self._response_text(response.json())
        if not text:
            raise GeminiProviderError(f"Gemini {model} returned an empty response")
        return GeminiResult(
            text=text,
            model=model,
            latency_ms=int((time.time() - started) * 1000),
        )

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.2,
        timeout: float = 60,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> GeminiResult:
        errors: list[str] = []
        models = self.models()
        deadline = time.monotonic() + max(float(timeout), 1.0)
        for index, model in enumerate(models):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("Gemini timeout budget exhausted")
                break
            models_left = len(models) - index
            model_timeout = remaining if models_left == 1 else max(1.0, remaining / models_left)
            try:
                return self._generate(
                    model=model,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=model_timeout,
                    response_schema=response_schema,
                )
            except Exception as exc:
                errors.append(f"{model}: {exc}")
        raise GeminiProviderError("; ".join(errors) or "No Gemini model configured")

    def classify(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        max_tokens: int = 800,
        timeout: float = 45,
    ) -> GeminiResult:
        return self.generate(
            prompt,
            max_tokens=max_tokens,
            temperature=0.1,
            timeout=timeout,
            response_schema=schema,
        )

    def summarize(
        self,
        prompt: str,
        *,
        max_tokens: int = 500,
        timeout: float = 60,
    ) -> GeminiResult:
        return self.generate(
            prompt,
            max_tokens=max_tokens,
            temperature=0.2,
            timeout=timeout,
        )

    def health(self, timeout: float = 8) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "provider": "gemini",
            "model": self.model,
            "fallback_model": self.fallback_model,
            "configured": self.configured,
            "api_connected": False,
            "last_error": None,
        }
        if not self.configured:
            result["last_error"] = "GEMINI_API_KEY is not configured"
            return result
        try:
            url = f"{self.base_url}/models/{quote(self.model, safe='')}"
            response = requests.get(url, headers=self._headers(), timeout=timeout)
            response.raise_for_status()
            result["api_connected"] = True
        except Exception as exc:
            result["last_error"] = str(exc)
        return result
