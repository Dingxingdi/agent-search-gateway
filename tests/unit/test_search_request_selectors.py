from dataclasses import FrozenInstanceError, fields

import pytest

from agent_search_gateway.models import KeywordSearchRequest, LLMSearchRequest, PaperSearchRequest


def test_keyword_request_has_optional_provider() -> None:
    assert [field.name for field in fields(KeywordSearchRequest)] == ["query", "provider"]
    assert KeywordSearchRequest("query").provider is None
    assert KeywordSearchRequest("query", provider="exa").provider == "exa"


@pytest.mark.parametrize(
    ("request_type", "field_names"),
    [
        (PaperSearchRequest, ["query", "provider"]),
        (LLMSearchRequest, ["prompt", "scope", "provider", "model"]),
    ],
)
def test_search_request_selector_fields(
    request_type: type[PaperSearchRequest] | type[LLMSearchRequest], field_names: list[str]
) -> None:
    assert [field.name for field in fields(request_type)] == field_names
    assert request_type("text").provider is None
    assert request_type("text", provider="  Alias  ").provider == "  Alias  "


@pytest.mark.parametrize("provider", [None, "openai_main"])
@pytest.mark.parametrize("model", [None, "gpt-5"])
def test_llm_selectors_are_independent(provider: str | None, model: str | None) -> None:
    request = LLMSearchRequest("prompt", "paper", provider=provider, model=model)
    assert (request.prompt, request.scope, request.provider, request.model) == (
        "prompt",
        "paper",
        provider,
        model,
    )


@pytest.mark.parametrize(
    "request_type", [KeywordSearchRequest, PaperSearchRequest, LLMSearchRequest]
)
def test_selectors_remain_frozen_and_slotted(
    request_type: type[KeywordSearchRequest] | type[PaperSearchRequest] | type[LLMSearchRequest],
) -> None:
    request = request_type("text", provider="exa")
    with pytest.raises(FrozenInstanceError):
        request.provider = "other"  # type: ignore[misc]  # Exercise frozen assignment at runtime.
    assert not hasattr(request, "__dict__")


def test_model_is_not_normalized_by_request() -> None:
    request = LLMSearchRequest("prompt", model="  ")
    assert request.model == "  "
    with pytest.raises(FrozenInstanceError):
        request.model = "other"  # type: ignore[misc]  # Exercise frozen assignment at runtime.
