"""One-attempt opt-in probes. Report only safe metrics, even when an upstream fails."""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.config import ResolvedRatePolicy
from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.input_limits import MAX_PREVIEW_SCAN_BYTES
from agent_search_gateway.llm.prompts import judge_messages
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from agent_search_gateway.providers.academic.openalex import OpenAlexProvider
from agent_search_gateway.providers.academic.semantic_scholar import SemanticScholarProvider
from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver
from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity
from agent_search_gateway.providers.contracts import AcademicSearchProvider
from agent_search_gateway.providers.http import HttpJsonExecutor, HttpStatusFailure
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from agent_search_gateway.rate_limit import RateGate

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live probes require WEB_SEARCH_RUN_INTEGRATION=1",
)
_POLICY = RetryPolicy(1, 0, 0, 60)
_TIME = re.compile(r"^[A-Z]+ time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z ")
_QUERY = "Attention Is All You Need"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def _llm_probe_settings() -> tuple[SecretValue, str, str]:
    if os.environ.get("OPENROUTER_API_KEY"):
        return (
            SecretValue(_required("OPENROUTER_API_KEY")),
            os.environ.get("OPENROUTER_MODEL") or "openrouter/free",
            os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api",
        )
    return (
        SecretValue(_required("OPENAI_API_KEY")),
        os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_URL")
        or "https://api.openai.com",
    )


def _article(cap: int) -> str:
    sentence = "This synthetic article describes how rain is measured. "
    head = sentence * ((cap * 4 // 5 + 2048) // len(sentence))
    tail = sentence * ((cap // 5 + 2048) // len(sentence))
    return "ARTICLE_START " + head + "DroppedMiddle" + tail + " ARTICLE_END"


@dataclass
class _Observation:
    provider: str
    auth: str
    cap: int = 0
    log: StringIO = field(default_factory=StringIO, repr=False)
    attempts: int = 0
    status: int | None = None
    sent: int = 0
    bearer: bool = False
    retained: bool = False
    valid: bool = False
    invalid_response_category: str = "response_contract"

    async def request(self, request: httpx.Request) -> None:
        self.attempts += 1
        self.sent = len(request.content)
        if self.provider == "openalex":
            self.bearer = (
                request.headers.get("Authorization", "").startswith("Bearer ")
                and "api_key" not in request.url.params
            )
        if self.cap:
            self.retained = (
                all(
                    mark in request.content
                    for mark in (b"ARTICLE_START", b"ARTICLE_END", b"[... truncated ...]")
                )
                and b"DroppedMiddle" not in request.content
            )

    async def response(self, response: httpx.Response) -> None:
        self.status = response.status_code


@contextmanager
def _probe(obs: _Observation, secrets: tuple[SecretValue, ...]) -> Iterator[None]:
    handler = logging.StreamHandler(obs.log)
    handler.setFormatter(KeyValueFormatter(SecretRedactor(secrets)))
    category = "success"
    with pytest.MonkeyPatch.context() as patch:
        project = logging.getLogger("agent_search_gateway")
        previous_level = project.level
        project.setLevel(logging.DEBUG)
        for name in ("httpx", "httpcore", "agent_search_gateway"):
            logger = logging.getLogger(name)
            patch.setattr(
                logger, "handlers", [handler] if logger is project else [logging.NullHandler()]
            )
            patch.setattr(logger, "propagate", False)
        try:
            yield
        except HttpStatusFailure:
            category = "http_status"
        except ExecutionFailure as exc:
            safe_reasons = {
                "rate_limit_wait_timeout",
                "timeout",
                "nonzero_exit",
                "input_budget_unfit",
                "invalid_json",
            }
            category = (
                exc.reason
                if exc.reason in safe_reasons and exc.reason is not None
                else exc.code.value
            )
        except Exception:
            category = "unexpected"
        finally:
            project.setLevel(previous_level)

    output = obs.log.getvalue()
    lines = output.splitlines()
    previews = [line for line in lines if " event=upstream_body_preview " in line]
    preview = previews[0] if len(previews) == 1 else ""
    sent_match = re.search(r"\brequest_body_sent_bytes=(\d+)\b", preview)
    original_match = re.search(r"\brequest_body_original_bytes=(\d+)\b", preview)
    original_bytes = int(original_match[1]) if original_match else 0
    sent_bytes = int(sent_match[1]) if sent_match else 0
    clipped = " request_input_truncated=true" in preview
    response_match = re.search(r"\bresponse_body_bytes=(\d+)\b", preview)
    response_budget_omitted = (
        response_match is not None
        and int(response_match[1]) > MAX_PREVIEW_SCAN_BYTES
        and " response_preview_truncated=true" in preview
    )
    time_valid = bool(lines) and all(_TIME.match(line) for line in lines)
    diagnostic_count = sum(" event=agent_process_diagnostic " in line for line in lines)
    redacted = all(secret.reveal() not in output for secret in secrets)
    size_ok = not obs.cap or (
        obs.retained
        and clipped
        and " request_body_preview=null" not in preview
        and " request_preview_truncated=true" in preview
        and 0 < sent_bytes <= obs.cap < original_bytes
    )
    valid = (
        obs.valid
        and obs.attempts == 1
        and len(previews) == 1
        and "request_body_preview=" in preview
        and "response_body_preview=" in preview
        and ("response_body_preview=null" not in preview or response_budget_omitted)
        and time_valid
        and redacted
        and size_ok
        and sent_bytes == obs.sent
        and (obs.provider != "agent_large_input" or obs.attempts == diagnostic_count)
        and (obs.provider != "openalex" or obs.bearer)
    )
    waits = [
        int(n)
        for line in lines
        if " event=rate_limit_" in line
        for n in re.findall(r"\bwaited_ms=(\d+)\b", line)
    ]
    if category == "success" and not valid:
        category = obs.invalid_response_category
    print(
        json.dumps(
            {
                "provider": obs.provider,
                "auth": obs.auth,
                "expected_attempts": 1,
                "attempts": obs.attempts,
                "status": obs.status,
                "rate_wait_ms": sum(waits) if obs.provider != "agent_large_input" else None,
                "input_original_bytes": original_bytes,
                "input_sent_bytes": obs.sent,
                "preview_sent_bytes_match": sent_bytes == obs.sent,
                "platform": sys.platform if obs.provider == "agent_large_input" else None,
                "input_truncated": clipped,
                "previews": len(previews),
                "utc_time": time_valid,
                "secrets_redacted": redacted,
                "category": category,
            }
        )
    )
    if category != "success":
        pytest.fail(
            f"{obs.provider}: {category}; attempts={obs.attempts}, HTTP={obs.status}", pytrace=False
        )


@pytest.mark.parametrize("provider", ["openalex", "arxiv", "semantic_scholar", "unpaywall"])
async def test_live_academic(provider: str) -> None:
    secrets: tuple[SecretValue, ...] = ()
    if provider == "openalex":
        secrets = (SecretValue(_required("OPENALEX_API_KEY")),)
    elif provider == "unpaywall":
        secrets = (SecretValue(_required("UNPAYWALL_CONTACT_EMAIL")),)
    elif provider == "semantic_scholar" and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
        secrets = (SecretValue(_required("SEMANTIC_SCHOLAR_API_KEY")),)
    auth = {
        "openalex": "bearer",
        "unpaywall": "contact",
        "semantic_scholar": "key" if secrets else "anonymous",
        "arxiv": "anonymous",
    }[provider]
    obs = _Observation(provider, auth)
    with _probe(obs, secrets):
        async with httpx.AsyncClient(
            event_hooks={"request": [obs.request], "response": [obs.response]}
        ) as client:
            executor = HttpJsonExecutor(
                client,
                _POLICY,
                provider_name=provider,
                rate_gate=RateGate(ResolvedRatePolicy(min_interval_seconds=3)),
            )
            if provider == "unpaywall":
                resolution = await UnpaywallResolver(executor, contact_email=secrets[0]).resolve(
                    "10.1038/nature14539"
                )
                obs.valid = resolution is not None and isinstance(resolution.is_open_access, bool)
            else:
                adapter: AcademicSearchProvider
                if provider == "openalex":
                    adapter = OpenAlexProvider(executor, api_key=secrets[0])
                elif provider == "arxiv":
                    adapter = ArxivProvider(executor)
                else:
                    adapter = SemanticScholarProvider(
                        executor, api_key=secrets[0] if secrets else None
                    )
                papers = await adapter.search(_QUERY)
                obs.valid = bool(papers) and all(
                    paper.title and paper.source_id for paper in papers
                )


async def test_live_llm_large_input() -> None:
    key, model, base = _llm_probe_settings()
    obs = _Observation("llm_large_input", "bearer", cap=131072)
    with _probe(obs, (key,)):
        async with httpx.AsyncClient(
            event_hooks={"request": [obs.request], "response": [obs.response]}
        ) as client:
            executor = HttpJsonExecutor(client, _POLICY, provider_name="live_llm")
            llm = OpenAIChatCompletionsClient(
                name="live_llm",
                api_url=base.rstrip("/").removesuffix("/v1"),
                secret=key,
                executor=executor,
                retry_policy=_POLICY,
            )
            result = await llm.complete_json(
                LLMInvocation("live_llm", model), judge_messages(_article(obs.cap))
            )
            obs.valid = isinstance(result.get("ok"), bool)


def _agent_reply_error(reply: str) -> str | None:
    """The Agent returns text, so a complete Markdown JSON block is also a decision."""
    text = reply.strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence[1]

    def reject_constant(value: str) -> None:
        raise ValueError("Nonstandard JSON constant")

    try:
        result = json.loads(text, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return "invalid_json"
    if not isinstance(result, dict):
        return "response_not_object"
    if not isinstance(result.get("ok"), bool):
        return "invalid_ok_field"
    return None


async def test_live_agent_large_input(monkeypatch: pytest.MonkeyPatch) -> None:
    command = shutil.which("agy")
    if command is None:
        pytest.skip("agy is not installed")
    obs = _Observation("agent_large_input", "installed_session", cap=98304)
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    pass_env = tuple(
        name for upper in proxy_names for name in (upper, upper.lower()) if name in os.environ
    )
    real_spawn = asyncio.create_subprocess_exec

    async def observed_spawn(program: str, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        obs.attempts += 1
        prompt = args[args.index("-p") + 1]
        obs.sent = len(prompt.encode("utf-8"))
        obs.retained = (
            all(mark in prompt for mark in ("ARTICLE_START", "ARTICLE_END", "[... truncated ...]"))
            and "DroppedMiddle" not in prompt
        )
        return await real_spawn(program, *args, **kwargs)

    with _probe(obs, ()):
        monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_spawn)
        reply = await run_antigravity(
            None,
            judge_messages(_article(obs.cap)),
            provider_name="live_agent",
            command=command,
            timeout_seconds=120,
            pass_env=pass_env,
            quota=CapacityGate(1),
        )
        error = _agent_reply_error(reply)
        obs.valid = error is None
        if error is not None:
            obs.invalid_response_category = error
