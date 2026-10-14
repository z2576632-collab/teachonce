"""LLM layer tests: config resolution, ScriptedLLM contract, JSON parsing.

No real API calls happen anywhere in this suite.
"""

from __future__ import annotations

import pytest

from teachonce.errors import LLMError, LLMNotConfiguredError, SchemaValidationError
from teachonce.llm import (
    LLMConfig,
    OpenAICompatibleLLM,
    ScriptedLLM,
    parse_json_response,
)

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_from_env_defaults():
    cfg = LLMConfig.from_env(env={})
    assert cfg.api_key is None
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.model == "gpt-4o-mini"


def test_config_from_env_custom():
    cfg = LLMConfig.from_env(
        env={
            "TEACHCONE_LLM_API_KEY": "sk-test",
            "TEACHCONE_LLM_BASE_URL": "https://api.deepseek.com/v1/",
            "TEACHCONE_LLM_MODEL": "deepseek-chat",
        }
    )
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://api.deepseek.com/v1"  # trailing slash trimmed
    assert cfg.model == "deepseek-chat"


def test_openai_compatible_requires_key():
    with pytest.raises(LLMNotConfiguredError) as exc:
        OpenAICompatibleLLM(LLMConfig(api_key=None))
    # Error message must tell the user how to fix it.
    assert "TEACHCONE_LLM_API_KEY" in str(exc.value)


# ---------------------------------------------------------------------------
# ScriptedLLM
# ---------------------------------------------------------------------------


def test_scripted_llm_returns_in_order_and_records_calls():
    llm = ScriptedLLM(["first", "second"])
    assert llm.complete("sys", "u1") == "first"
    assert llm.complete("sys", "u2", json_mode=True) == "second"
    assert len(llm.calls) == 2
    assert llm.calls[1]["json_mode"] is True


def test_scripted_llm_callable_response():
    llm = ScriptedLLM([lambda system, user: f"echo:{user}"])
    assert llm.complete("s", "hello") == "echo:hello"


def test_scripted_llm_exhausted_raises():
    llm = ScriptedLLM(["only one"])
    llm.complete("s", "u")
    with pytest.raises(LLMError) as exc:
        llm.complete("s", "u")
    assert "ran out of scripted responses" in str(exc.value)


# ---------------------------------------------------------------------------
# parse_json_response
# ---------------------------------------------------------------------------


def test_parse_plain_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    text = '```json\n{"a": 1}\n```'
    assert parse_json_response(text) == {"a": 1}


def test_parse_json_with_surrounding_prose():
    text = 'Sure! Here is the result:\n{"a": 1}\nHope that helps.'
    assert parse_json_response(text) == {"a": 1}


def test_parse_invalid_raises():
    with pytest.raises(SchemaValidationError):
        parse_json_response("no json here")


def test_parse_top_level_array_rejected():
    with pytest.raises(SchemaValidationError):
        parse_json_response("[1, 2, 3]")
