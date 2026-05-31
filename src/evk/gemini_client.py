"""Gemini client — supports Vertex AI (production) and Gemini Developer API (local).

Every call is: temperature <= 0.1 by default, schema-enforced (for structured
output), retried 3x with bounded exponential backoff on transient failures.
Schema validation is **not** retried — a malformed response is a hard fail.

Selection logic:

1. If `GOOGLE_API_KEY` is set → use Gemini Developer API (free tier, no GCP).
2. Else if `EVK_MODE=production` → use Vertex AI with `GOOGLE_CLOUD_PROJECT`.
3. Else → caller should use `evk.stubs.StubGemini` (handled by the agent factory).
"""

from __future__ import annotations

from typing import TypeVar

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from evk.config import get_settings
from evk.logging import logger

T = TypeVar("T", bound=BaseModel)

# Per production mandate: deterministic output for classification & matching.
_DEFAULT_TEMPERATURE = 0.1
_MAX_ATTEMPTS = 3


def _strip_unsupported_schema_keys(obj: object) -> object:
    """Recursively remove JSON-Schema keys unsupported by the Gemini Developer API.

    The Gemini Developer API rejects ``additionalProperties``, ``$schema``, and
    ``title`` at the top level, even though they are valid JSON Schema.  Pydantic's
    ``.model_json_schema()`` emits them automatically, so we strip before sending.
    """
    _UNSUPPORTED = {"additionalProperties", "$schema", "title", "$defs"}
    if isinstance(obj, dict):
        return {
            k: _strip_unsupported_schema_keys(v)
            for k, v in obj.items()
            if k not in _UNSUPPORTED
        }
    if isinstance(obj, list):
        return [_strip_unsupported_schema_keys(item) for item in obj]
    return obj


class GeminiError(RuntimeError):
    """Raised when Gemini produces an unusable response (schema invalid / empty)."""


class _TransientGeminiError(RuntimeError):
    """Internal marker for retry-worthy failures."""


class GeminiClient:
    """Wraps the unified google-genai SDK, auto-routing to Dev API or Vertex AI."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        if settings.google_api_key:
            self._client = genai.Client(api_key=settings.google_api_key)
            self._backend = "gemini_dev_api"
        else:
            self._client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
            self._backend = "vertex_ai"
        logger.bind(backend=self._backend, model=settings.gemini_model).debug("gemini.init")

    # --- public ------------------------------------------------------------

    def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_output_tokens: int = 2048,
    ) -> str:
        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        return _call_with_retry(
            self._client,
            model=self._settings.gemini_model,
            contents=prompt,
            config=config,
        )

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_output_tokens: int = 4096,
    ) -> T:
        # Build a cleaned schema dict — the Gemini Developer API rejects
        # additionalProperties, $schema, title, and $defs even though they are
        # valid JSON Schema.  Pydantic emits them; we strip before sending.
        raw_schema = schema.model_json_schema()
        clean_schema = _strip_unsupported_schema_keys(raw_schema)
        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=clean_schema,
        )
        raw = _call_with_retry(
            self._client,
            model=self._settings.gemini_model,
            contents=prompt,
            config=config,
        )
        # Gemini may truncate mid-JSON if the response hits the token limit.
        # Attempt a lightweight repair before validation.
        repaired = _repair_truncated_json(raw)
        try:
            return schema.model_validate_json(repaired)
        except ValidationError as exc:
            # Schema failures are not retried: deterministic input + deterministic
            # model at T=0.1 means a second shot would produce the same garbage.
            logger.bind(raw=raw[:1000], schema=schema.__name__).error(
                "gemini.structured_validation_failed"
            )
            raise GeminiError(f"Gemini returned invalid JSON for {schema.__name__}: {exc}") from exc

    def generate_embedding(self, text: str) -> list[float]:
        """Generate a text embedding using the Gemini embeddings API."""
        try:
            response = self._client.models.embed_content(
                model="models/text-embedding-004",
                contents=text,
            )
            return list(response.embeddings[0].values)
        except Exception as exc:
            logger.bind(error=str(exc)).warning("gemini.embedding_failed")
            return []

    def healthcheck(self) -> bool:
        """Cheap liveness ping — ~10 tokens, used by /healthz."""
        try:
            _ = self.generate_text(prompt="ping", max_output_tokens=4, temperature=0.0)
            return True
        except Exception:  # pragma: no cover — liveness probe
            logger.exception("gemini.healthcheck_failed")
            return False


@retry(
    reraise=True,
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(_TransientGeminiError),
)
def _call_with_retry(
    client: genai.Client,
    *,
    model: str,
    contents: str,
    config: genai_types.GenerateContentConfig,
) -> str:
    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:
        # All SDK exceptions are treated as transient — quotas, 5xx, timeouts.
        logger.bind(error=type(exc).__name__).warning("gemini.call_failed_retrying")
        raise _TransientGeminiError(str(exc)) from exc
    text = getattr(response, "text", None)
    if not text:
        raise _TransientGeminiError("empty response")
    return text


def _repair_truncated_json(text: str) -> str:
    """Best-effort repair of JSON truncated mid-response by a token limit.

    Gemini occasionally cuts output mid-string when the response exceeds
    ``max_output_tokens``.  We try to close any dangling structure so Pydantic
    can at least parse what arrived.  If the JSON is already valid this is a
    no-op.
    """
    import json as _json

    text = text.strip()
    try:
        _json.loads(text)
        return text  # already valid
    except _json.JSONDecodeError:
        pass

    # Close unclosed strings, arrays, and objects.
    # Strategy: count open/close brackets while tracking string context.
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    # If we're mid-string, close it first.
    suffix = '"' if in_string else ""
    suffix += "".join(reversed(stack))

    repaired = text + suffix
    try:
        _json.loads(repaired)
        return repaired
    except _json.JSONDecodeError:
        return text  # give up; let Pydantic surface the real error


__all__ = ["GeminiClient", "GeminiError"]
