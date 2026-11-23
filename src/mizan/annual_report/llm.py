"""AI client interface + Gemini implementation (the original's model, JSON mode). NOT exercised against the live service in the
build sandbox (no network/API key) - the batch logic is proven with a scripted fake client."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from ..core.errors import RateLimitError, TransientProviderError


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class LLMOutputError(Exception):
    """The model answered but not with usable JSON. Tokens were still spent: `usage` carries them."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage or Usage()


class LLM(Protocol):
    model: str

    def generate_json(self, prompt: str, max_output_tokens: int) -> tuple[dict, Usage]: ...


def map_llm_exception(exc: Exception) -> Exception:
    name, msg = type(exc).__name__.lower(), str(exc).lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429 or "resource_exhausted" in msg or "429" in msg or "quota" in msg or "rate limit" in msg or "ratelimit" in name:
        return RateLimitError(str(exc))
    return TransientProviderError(f"{type(exc).__name__}: {exc}")


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash"):
        self.api_key, self.model, self._client = api_key or os.environ.get("GEMINI_API_KEY"), model, None
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set")

    def generate_json(self, prompt: str, max_output_tokens: int) -> tuple[dict, Usage]:
        from google import genai
        from google.genai import types
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        try:
            r = self._client.models.generate_content(
                model=self.model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=max_output_tokens))
        except Exception as exc:
            raise map_llm_exception(exc) from exc
        um = getattr(r, "usage_metadata", None)
        usage = Usage(int(getattr(um, "prompt_token_count", 0) or 0), int(getattr(um, "candidates_token_count", 0) or 0))
        try:
            data = json.loads(r.text)
        except Exception as exc:
            raise LLMOutputError(f"invalid JSON from model: {exc}", usage) from exc
        if not isinstance(data, dict):
            raise LLMOutputError("model returned JSON that is not an object", usage)
        return data, usage
