"""Offline coverage for live probe response validation and LLM configuration."""

import pytest

from tests.integration.test_live_rate_limits_and_inputs import (
    _agent_reply_error,
    _llm_probe_settings,
)


def test_llm_smoke_prefers_configured_openrouter_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    key, model, base = _llm_probe_settings()

    assert key.reveal() == "test-openrouter-key"
    assert model == "openrouter/free"
    assert base == "https://openrouter.ai/api"


def test_llm_smoke_respects_openrouter_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "example/model:free")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://example.test/api/v1")

    _, model, base = _llm_probe_settings()

    assert model == "example/model:free"
    assert base == "https://example.test/api/v1"


def test_llm_smoke_uses_openai_without_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com")

    key, model, base = _llm_probe_settings()

    assert key.reveal() == "test-openai-key"
    assert model == "gpt-4o-mini"
    assert base == "https://api.openai.com"


@pytest.mark.parametrize(
    "reply",
    [
        '{"ok": false}',
        '```json\n{"ok": true}\n```',
        '  ```JSON\r\n{"ok": false, "reason": "short"}\r\n```\n',
        '```\n{"ok": true}\n```',
    ],
)
def test_agent_smoke_accepts_one_json_decision(reply: str) -> None:
    assert _agent_reply_error(reply) is None


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ('Explanation\n```json\n{"ok": true}\n```', "invalid_json"),
        ('```json\n{"ok": true}\n```\nExtra', "invalid_json"),
        ('```json\n{"ok": true}\n```\n```json\n{"ok": false}\n```', "invalid_json"),
        ('```text\n{"ok": true}\n```', "invalid_json"),
        ('```json\n{"ok": true}', "invalid_json"),
        ('```json\n{"ok": true,}\n```', "invalid_json"),
        ('{"ok": true, "value": NaN}', "invalid_json"),
        ('[{"ok": true}]', "response_not_object"),
        ("```json\n{}\n```", "invalid_ok_field"),
        ('{"ok": 1}', "invalid_ok_field"),
    ],
)
def test_agent_smoke_classifies_invalid_decisions(reply: str, error: str) -> None:
    assert _agent_reply_error(reply) == error
