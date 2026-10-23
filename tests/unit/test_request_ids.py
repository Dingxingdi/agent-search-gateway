import asyncio
import re
from pathlib import Path

import pytest

from agent_search_gateway.request_ids import (
    RequestIdRegistry,
    bind_request_id,
    current_request_id,
    generate_request_id,
    result_filename,
    validate_request_id,
)


def test_generate_validate_and_bind_request_id() -> None:
    generated = generate_request_id()
    assert re.fullmatch(r"[0-9a-f]{8}", generated)
    assert validate_request_id("a1b2c3d4") == "a1b2c3d4"
    assert current_request_id() is None

    with bind_request_id("11111111"):
        assert current_request_id() == "11111111"
        with bind_request_id("22222222"):
            assert current_request_id() == "22222222"
        assert current_request_id() == "11111111"

    assert current_request_id() is None


@pytest.mark.parametrize("value", ["", "ABCDEF12", "123", "123456789", "zzzzzzzz"])
def test_validate_request_id_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="request ID"):
        validate_request_id(value)


def test_bind_request_id_resets_after_exception() -> None:
    with pytest.raises(RuntimeError, match="boom"), bind_request_id("11111111"):
        raise RuntimeError("boom")
    assert current_request_id() is None


@pytest.mark.asyncio
async def test_request_id_context_is_isolated_across_child_tasks() -> None:
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    observed: dict[str, tuple[str | None, str | None]] = {}

    async def child() -> str | None:
        await asyncio.sleep(0)
        return current_request_id()

    async def worker(name: str, request_id: str, release: asyncio.Event) -> None:
        with bind_request_id(request_id):
            before = current_request_id()
            child_value = await asyncio.create_task(child())
            await release.wait()
            observed[name] = (before, child_value)

    first = asyncio.create_task(worker("first", "11111111", release_first))
    second = asyncio.create_task(worker("second", "22222222", release_second))
    await asyncio.sleep(0)
    release_second.set()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first, second)

    assert observed == {
        "first": ("11111111", "11111111"),
        "second": ("22222222", "22222222"),
    }
    assert current_request_id() is None


def test_registry_rejects_active_collision_and_releases_for_reuse(tmp_path: Path) -> None:
    values = iter(["11111111", "11111111", "22222222", "11111111"])
    registry = RequestIdRegistry(tmp_path, factory=values.__next__)

    with registry.reserve(may_write_search_result=False) as first:
        assert first == "11111111"
        with registry.reserve(may_write_search_result=False) as second:
            assert second == "22222222"

    with registry.reserve(may_write_search_result=False) as reused:
        assert reused == "11111111"


def test_search_reservation_rejects_existing_result_files(tmp_path: Path) -> None:
    (tmp_path / "keyword-33333333.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "llm-44444444.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "paper-55555555.jsonl").write_text("", encoding="utf-8")
    values = iter(["33333333", "44444444", "55555555", "66666666"])
    registry = RequestIdRegistry(tmp_path, factory=values.__next__)

    with registry.reserve(may_write_search_result=True) as request_id:
        assert request_id == "66666666"


def test_fetch_reservation_ignores_existing_result_files(tmp_path: Path) -> None:
    (tmp_path / "keyword-33333333.jsonl").write_text("", encoding="utf-8")
    registry = RequestIdRegistry(tmp_path, factory=lambda: "33333333")

    with registry.reserve(may_write_search_result=False) as request_id:
        assert request_id == "33333333"


def test_registry_rejects_malformed_factory_output(tmp_path: Path) -> None:
    registry = RequestIdRegistry(tmp_path, factory=lambda: "invalid")
    with (
        pytest.raises(RuntimeError, match="invalid request ID"),
        registry.reserve(may_write_search_result=False),
    ):
        pass


def test_registry_has_bounded_exhaustion(tmp_path: Path) -> None:
    registry = RequestIdRegistry(tmp_path, factory=lambda: "11111111", max_attempts=2)
    with (
        registry.reserve(may_write_search_result=False),
        pytest.raises(RuntimeError, match="unable to reserve"),
        registry.reserve(may_write_search_result=False),
    ):
        pass


def test_registry_wraps_exhausted_factory(tmp_path: Path) -> None:
    values = iter(())
    registry = RequestIdRegistry(tmp_path, factory=values.__next__)
    with (
        pytest.raises(RuntimeError, match="request ID factory failed"),
        registry.reserve(may_write_search_result=False),
    ):
        pass


def test_result_filename_is_exact_and_validated() -> None:
    assert result_filename("keyword", "a1b2c3d4") == "keyword-a1b2c3d4.jsonl"
    assert result_filename("llm", "11111111") == "llm-11111111.jsonl"
    assert result_filename("paper", "22222222") == "paper-22222222.jsonl"

    with pytest.raises(ValueError, match="result kind"):
        result_filename("other", "11111111")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="request ID"):
        result_filename("keyword", "bad")


def test_agent_result_filename_uses_request_id() -> None:
    assert result_filename("agent", "a1b2c3d4") == "agent-a1b2c3d4.jsonl"


def test_agent_result_collision_reserves_a_new_id(tmp_path: Path) -> None:
    (tmp_path / "agent-33333333.jsonl").write_text("existing\n", encoding="utf-8")
    values = iter(["33333333", "44444444"])
    registry = RequestIdRegistry(tmp_path, factory=values.__next__)
    with registry.reserve(may_write_search_result=True) as request_id:
        assert request_id == "44444444"
