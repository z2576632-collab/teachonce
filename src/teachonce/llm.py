"""LLM provider abstraction.

Production paths MUST go through :class:`LLMProvider`; tests inject
:class:`ScriptedLLM`. TeachOnce never fabricates LLM output: when no
provider is configured, calls raise :class:`LLMNotConfiguredError`.

Configuration (environment variables):
- ``TEACHCONE_LLM_API_KEY``  — required to enable real LLM calls
- ``TEACHCONE_LLM_BASE_URL`` — OpenAI-compatible endpoint
  (default ``https://api.openai.com/v1``; e.g. ``https://api.deepseek.com/v1``)
- ``TEACHCONE_LLM_MODEL``    — model name (default ``gpt-4o-mini``;
  e.g. ``deepseek-chat``)
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import LLMError, LLMNotConfiguredError, SchemaValidationError


class LLMProvider(ABC):
    """Minimal text-completion interface used by all engines."""

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> str:
        """Return the model's text response to ``system`` + ``user`` prompts."""


@dataclass
class LLMConfig:
    """Resolved LLM configuration."""

    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_s: float = 120.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LLMConfig:
        import os

        source = env if env is not None else os.environ
        return cls(
            api_key=source.get("TEACHCONE_LLM_API_KEY") or None,
            base_url=(source.get("TEACHCONE_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip(
                "/"
            ),
            model=source.get("TEACHCONE_LLM_MODEL") or "gpt-4o-mini",
        )


class OpenAICompatibleLLM(LLMProvider):
    """Real provider: POST /chat/completions against any OpenAI-compatible API.

    Works with OpenAI, DeepSeek, Ollama (``/v1``), vLLM, etc. Minimal by
    design (see docs/ARCHITECTURE.md §5 for the rationale — no heavy SDK).
    """

    def __init__(self, config: LLMConfig) -> None:
        if not config.api_key:
            raise LLMNotConfiguredError(
                "No LLM API key configured. Set TEACHCONE_LLM_API_KEY "
                "(optionally TEACHCONE_LLM_BASE_URL and TEACHCONE_LLM_MODEL), "
                "then retry. TeachOnce does not fake LLM output when no "
                "provider is configured."
            )
        self.config = config

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OpenAICompatibleLLM:
        return cls(LLMConfig.from_env(env))

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.config.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        def _post(body: dict[str, Any]) -> httpx.Response:
            try:
                return httpx.post(url, headers=headers, json=body, timeout=self.config.timeout_s)
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"LLM request to {url} failed: {exc}. Check your network / "
                    "TEACHCONE_LLM_BASE_URL."
                ) from exc

        response = _post(payload)
        if response.status_code == 400 and json_mode:
            # Some OpenAI-compatible servers reject response_format; retry
            # once without it. The prompt still demands JSON output.
            payload.pop("response_format", None)
            response = _post(payload)
        if response.status_code != 200:
            snippet = response.text[:500]
            raise LLMError(
                f"LLM API returned HTTP {response.status_code}: {snippet}. "
                f"Check TEACHCONE_LLM_API_KEY / TEACHCONE_LLM_MODEL (current "
                f"model: {self.config.model})."
            )
        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {response.text[:500]}") from exc


@dataclass
class ScriptedLLM(LLMProvider):
    """Test double — returns pre-registered responses in FIFO order.

    NOT a production provider: it exists so tests can verify pipeline
    logic (JSON parsing, schema validation, version flow, error paths)
    without calling a real API. Production code never imports this into
    a runtime path — it is constructed explicitly by tests/demo fixtures.
    """

    responses: list[str | Callable[[str, str], str]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "json_mode": json_mode,
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise LLMError(
                "ScriptedLLM ran out of scripted responses — the code under "
                "test made more LLM calls than the test scripted."
            )
        item = self.responses.pop(0)
        if callable(item):
            return str(item(system, user))
        return item


# ---------------------------------------------------------------------------
# response parsing helper
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def parse_json_response(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an LLM response.

    Handles the two common failure modes: markdown code fences and
    prose wrapped around the object. Raises
    :class:`SchemaValidationError` if no JSON object can be recovered.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = _FENCE_RE.sub("", candidate).strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
        raise SchemaValidationError(f"Expected a JSON object, got {type(parsed).__name__}.")
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise SchemaValidationError(
        f"LLM response did not contain a parseable JSON object. First 200 chars: {text[:200]!r}"
    )
