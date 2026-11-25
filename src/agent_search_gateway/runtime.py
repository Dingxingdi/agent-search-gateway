"""Resolved runtime assembly for the foreground daemon."""

import asyncio
import logging
import os
import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TypeVar, cast

import httpx

from .academic.aggregator import PaperAggregator
from .concurrency import ProviderQuotaManager, join_cleanup, make_capacity_gate
from .config import (
    LLMProviderConfig,
    ResolvedAcademicProviderConfig,
    ResolvedAgentConfig,
    ResolvedConfig,
    ResolvedCredential,
    ResolvedInputLimits,
    ResolvedOAResolverConfig,
    ResolvedWebProviderConfig,
)
from .credential_pool import CredentialMember, CredentialPool
from .errors import ConfigFailure, ErrorCode
from .llm.stages import LLMStages
from .models import RetryPolicy
from .observability import log_event
from .orchestrators.fetch import FetchOrchestrator
from .orchestrators.paper import PaperSearchOrchestrator
from .orchestrators.search import SearchOrchestrator
from .paths import RuntimePaths
from .providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from .providers.academic.registry import AcademicProviderRegistry, OAResolverRegistry
from .providers.agent.antigravity_cli import run_antigravity
from .providers.contracts import (
    AcademicSearchProvider,
    AgentTextRunner,
    KeywordSearchProvider,
    LLMClient,
    OAResolver,
    PreparedLLMClient,
    ReservableFetchProvider,
    WebAdapter,
)
from .providers.defaults import build_default_registry
from .providers.http import HttpJsonExecutor
from .providers.llm.openai_chat import OpenAIChatCompletionsClient
from .providers.pooled import (
    PooledAcademicProvider,
    PooledLLMClient,
    PooledOAResolver,
    PooledWebProvider,
)
from .providers.registry import ProviderRegistry, WebProviderRegistration
from .rate_limit import make_rate_gate
from .result_writer import ResultWriter
from .scheduler.fetch import FetchScheduler
from .url_store import URLStore

HttpClientFactory = Callable[..., httpx.AsyncClient]
_DEFAULT_INPUT_LIMITS = ResolvedInputLimits()
_RESERVED_WEB_ADAPTER_KWARGS = frozenset({"name", "http_executor", "secret"})
_RESERVED_ACADEMIC_ADAPTER_KWARGS = frozenset({"name", "executor", "api_key", "contact_email"})


def _prepare_direct_ssl_context() -> ssl.SSLContext:
    ca_file = os.environ.get("SSL_CERT_FILE")
    ca_dir = os.environ.get("SSL_CERT_DIR")
    httpx_027 = httpx.__version__.startswith("0.27.")
    try:
        # HTTPX 0.27 silently skips nonregular CA files. Newer versions let
        # OpenSSL read the source, including a bundle supplied through a pipe.
        if ca_file and httpx_027 and not os.path.isfile(ca_file):
            raise ValueError
        if not ca_file and ca_dir:
            # OpenSSL resolves capath entries lazily.
            directories = [path for path in ca_dir.split(os.pathsep) if path]
            if not all(os.path.isdir(path) for path in directories):
                raise ValueError
            if os.pathsep in ca_dir and httpx_027:
                # HTTPX 0.27 falls back to certifi for a list. Seed its normal
                # TLS policy with one requested directory, then add the others.
                if not directories:
                    # An all-empty list has no trusted roots.
                    return ssl.create_default_context(capath=ca_dir)
                context = httpx.create_ssl_context(verify=directories[0], trust_env=True)
                for directory in directories[1:]:
                    context.load_verify_locations(capath=directory)
                return context
        return httpx.create_ssl_context(verify=True, trust_env=True)
    except (OSError, ValueError):
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR, "Invalid TLS CA configuration for direct HTTP providers"
        ) from None


def _create_http_client(
    factory: HttpClientFactory,
    *,
    use_proxy: bool,
    direct_ssl_context: ssl.SSLContext | None,
    label: str,
) -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=20)
    if use_proxy:
        return factory(limits=limits)
    if direct_ssl_context is None:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, f"Missing direct TLS context for {label}")
    try:
        return factory(trust_env=False, verify=direct_ssl_context, limits=limits)
    except TypeError:
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR, f"HTTP client factory failed for direct connection to {label}"
        ) from None


class _RuntimeResources:
    """Owns the physical HTTP clients allocated during runtime assembly."""

    def __init__(self) -> None:
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._closing: asyncio.Task[None] | None = None

    def register(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        self._clients[id(client)] = client
        return client

    async def aclose(self) -> None:
        if self._closing is None or (self._closing.done() and self._clients):
            self._closing = asyncio.create_task(self._close_clients())
        await join_cleanup(self._closing)

    async def _close_clients(self) -> None:
        while self._clients:
            _, client = self._clients.popitem()
            try:
                await client.aclose()
            except Exception:
                log_event(logging.getLogger(__name__), logging.ERROR, "runtime_client_close_failed")


ProviderConfig = (
    ResolvedWebProviderConfig
    | ResolvedAcademicProviderConfig
    | ResolvedOAResolverConfig
    | LLMProviderConfig
)
Adapter = TypeVar("Adapter")


@dataclass
class _HTTPAssembly:
    resources: _RuntimeResources
    factory: HttpClientFactory
    direct_ssl_context: ssl.SSLContext | None
    retry: RetryPolicy

    async def build_pool(
        self,
        namespace: str,
        provider: ProviderConfig,
        create_adapter: Callable[[ResolvedCredential, HttpJsonExecutor], Adapter],
    ) -> tuple[CredentialPool[Adapter], Adapter]:
        members: list[CredentialMember[Adapter]] = []
        for credential in provider.credentials:
            capacity = make_capacity_gate(
                credential.max_concurrency, provider=provider.name, quota_kind=namespace
            )
            rate = make_rate_gate(credential.rate_policy)
            client = self.resources.register(
                _create_http_client(
                    self.factory,
                    use_proxy=provider.use_proxy,
                    direct_ssl_context=self.direct_ssl_context,
                    label=f"{namespace} provider {provider.name}",
                )
            )
            executor = HttpJsonExecutor(
                client, self.retry, provider_name=provider.name, rate_gate=rate
            )
            adapter = create_adapter(credential, executor)
            members.append(CredentialMember(adapter, capacity, rate))
            # Startup can be cancelled between physical allocations.
            await asyncio.sleep(0)
        if not members:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Missing credentials for {namespace} provider {provider.name}",
            )
        pool = CredentialPool(
            namespace,
            provider.name,
            members,
            strategy=provider.credential_strategy,
            request_timeout_seconds=self.retry.request_timeout_seconds,
            error_code=ErrorCode.LLM_STAGE_FAILED
            if namespace == "llm"
            else ErrorCode.ALL_PROVIDERS_FAILED,
        )
        return pool, members[0].adapter


def _vendor_adapter(
    factory: Callable[..., object],
    label: str,
    options: Mapping[str, object],
    reserved: frozenset[str],
    **kwargs: object,
) -> object:
    conflicts = reserved.intersection(options)
    if conflicts:
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR,
            f"Reserved config key(s) for {label}: {', '.join(sorted(conflicts))}",
        )
    try:
        return factory(**kwargs, **options)
    except TypeError as exc:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, f"Invalid configuration for {label}") from exc


def _web_adapter(
    registration: WebProviderRegistration,
    credential: ResolvedCredential,
    executor: HttpJsonExecutor,
) -> WebAdapter:
    kwargs: dict[str, object] = {"name": registration.name, "http_executor": executor}
    if credential.api_key is not None and (
        registration.requires_api_key or registration.supports_optional_api_key
    ):
        kwargs["secret"] = credential.api_key
    elif registration.requires_api_key:
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR, f"Invalid enabled web provider: {registration.name}"
        )
    return cast(
        WebAdapter,
        _vendor_adapter(
            registration.factory,
            f"web provider {registration.name}",
            credential.options,
            _RESERVED_WEB_ADAPTER_KWARGS,
            **kwargs,
        ),
    )


def _llm_adapter(
    name: str, config: ResolvedConfig, credential: ResolvedCredential, executor: HttpJsonExecutor
) -> PreparedLLMClient:
    api_url = credential.options["api_url"]
    if not isinstance(api_url, str) or credential.api_key is None:
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR, f"Invalid configuration for LLM provider {name}"
        )
    return OpenAIChatCompletionsClient(
        name=name,
        api_url=api_url,
        secret=credential.api_key,
        executor=executor,
        request_body_bytes=config.input_limits.llm_request_body_bytes,
        retry_policy=config.retry,
    )


def _academic_adapter(
    factory: Callable[..., object],
    label: str,
    credential: ResolvedCredential,
    executor: HttpJsonExecutor,
) -> object:
    kwargs: dict[str, object] = {"executor": executor}
    if credential.api_key is not None:
        kwargs["api_key"] = credential.api_key
    if credential.contact_email is not None:
        kwargs["contact_email"] = credential.contact_email
    return _vendor_adapter(
        factory, label, credential.options, _RESERVED_ACADEMIC_ADAPTER_KWARGS, **kwargs
    )


class Runtime:
    """Owns one in-memory gateway runtime and its transport clients."""

    def __init__(
        self,
        *,
        quotas: ProviderQuotaManager,
        web_search_providers: tuple[KeywordSearchProvider, ...],
        web_fetch_providers: tuple[ReservableFetchProvider, ...],
        academic_search_providers: tuple[AcademicSearchProvider, ...],
        oa_resolver: OAResolver | None,
        llm_clients: Mapping[str, LLMClient],
        store: URLStore,
        search_orchestrator: SearchOrchestrator,
        paper_search_orchestrator: PaperSearchOrchestrator,
        fetch_orchestrator: FetchOrchestrator,
        resources: _RuntimeResources,
    ) -> None:
        self.quotas = quotas
        self.web_search_providers = web_search_providers
        self.web_fetch_providers = web_fetch_providers
        self.academic_search_providers = academic_search_providers
        self.oa_resolver = oa_resolver
        self.llm_clients = dict(llm_clients)
        self.store = store
        self.search_orchestrator = search_orchestrator
        self.paper_search_orchestrator = paper_search_orchestrator
        self.fetch_orchestrator = fetch_orchestrator
        self._resources = resources

    @classmethod
    async def build(
        cls,
        config: ResolvedConfig,
        paths: RuntimePaths,
        *,
        registry: ProviderRegistry | None = None,
        academic_registry: AcademicProviderRegistry | None = None,
        oa_resolver_registry: OAResolverRegistry | None = None,
        http_client_factory: HttpClientFactory = httpx.AsyncClient,
    ) -> "Runtime":
        provider_registry = registry or build_default_registry()
        academic_provider_registry = academic_registry or build_default_academic_registry()
        resolver_registry = oa_resolver_registry or build_default_oa_resolver_registry()
        enabled_web = tuple(
            item for item in config.web.providers if item.enable_search or item.enable_fetch
        )
        enabled_academic = tuple(item for item in config.academic.providers if item.enabled)
        needs_direct = (
            any(not item.use_proxy for item in enabled_web)
            or any(not item.use_proxy for item in enabled_academic)
            or (config.oa_resolver is not None and not config.oa_resolver.use_proxy)
            or any(not item.use_proxy for item in config.llm.providers)
        )
        direct_ssl_context = _prepare_direct_ssl_context() if needs_direct else None
        quotas = ProviderQuotaManager(
            web_limits={},
            llm_limits={},
            agent_limits={item.name: item.max_concurrency for item in config.agent.providers},
        )
        resources = _RuntimeResources()
        assembly = _HTTPAssembly(resources, http_client_factory, direct_ssl_context, config.retry)
        try:
            agent_runners = cls._build_agent_runners(config.agent, quotas, config.input_limits)
            web_search, web_fetch = await cls._build_web_providers(
                enabled_web, provider_registry, config.web.fetch_order, assembly
            )
            academic_search = await cls._build_academic_providers(
                enabled_academic, academic_provider_registry, assembly
            )
            oa_resolver = await cls._build_oa_resolver(
                config.oa_resolver, resolver_registry, assembly
            )
            llm_clients = await cls._build_llm_clients(config, assembly)
            stages = LLMStages(
                llm_clients,
                judge=config.llm.judge,
                safety=config.llm.safety,
                content_clean=config.llm.content_clean,
                focus_summary=config.llm.focus_summary,
                agent_runners=agent_runners,
            )
            store = URLStore()
            result_writer = ResultWriter(paths.results_dir)
            paper_aggregator = PaperAggregator(
                (
                    *(provider.name for provider in academic_search),
                    *(f"llm:{item.provider}" for item in config.llm.search_invocations),
                    *(f"agent:{item.provider}" for item in config.agent.search_invocations),
                )
            )
            search_orchestrator = SearchOrchestrator(
                keyword_providers=web_search,
                llm_invocations=config.llm.search_invocations,
                agent_invocations=config.agent.search_invocations,
                stages=stages,
                store=store,
                result_writer=result_writer,
                paper_aggregator=paper_aggregator,
                paper_resolver=oa_resolver,
            )
            paper_search_orchestrator = PaperSearchOrchestrator(
                providers=academic_search,
                aggregator=paper_aggregator,
                resolver=oa_resolver,
                store=store,
                result_writer=result_writer,
            )
            fetch_orchestrator = FetchOrchestrator(
                store=store,
                scheduler=FetchScheduler(
                    web_fetch,
                    quotas,
                    stages,
                    request_timeout_seconds=config.retry.request_timeout_seconds,
                ),
                stages=stages,
            )
            runtime = cls(
                quotas=quotas,
                web_search_providers=web_search,
                web_fetch_providers=web_fetch,
                academic_search_providers=academic_search,
                oa_resolver=oa_resolver,
                llm_clients=llm_clients,
                store=store,
                search_orchestrator=search_orchestrator,
                paper_search_orchestrator=paper_search_orchestrator,
                fetch_orchestrator=fetch_orchestrator,
                resources=resources,
            )
            log_event(
                logging.getLogger(__name__),
                logging.DEBUG,
                "runtime_built",
                web_use_proxy=",".join(
                    f"{item.name}:{str(item.use_proxy).lower()}" for item in enabled_web
                )
                or "-",
                academic_use_proxy=",".join(
                    f"{item.name}:{str(item.use_proxy).lower()}" for item in enabled_academic
                )
                or "-",
                oa_use_proxy=(
                    f"{config.oa_resolver.name}:{str(config.oa_resolver.use_proxy).lower()}"
                    if config.oa_resolver is not None
                    else "-"
                ),
                llm_use_proxy=",".join(
                    f"{item.name}:{str(item.use_proxy).lower()}" for item in config.llm.providers
                )
                or "-",
                web_providers=",".join(item.name for item in enabled_web) or "-",
                web_provider_count=len(enabled_web),
                llm_providers=",".join(item.name for item in config.llm.providers) or "-",
                llm_provider_count=len(config.llm.providers),
                academic_providers=",".join(item.name for item in enabled_academic) or "-",
                academic_provider_count=len(enabled_academic),
                oa_resolver=config.oa_resolver.name if config.oa_resolver is not None else "-",
            )
            return runtime
        except BaseException:
            # Finish cleanup before propagating the original startup exception.
            with suppress(asyncio.CancelledError):
                await resources.aclose()
            raise

    @staticmethod
    async def _build_web_providers(
        providers: tuple[ResolvedWebProviderConfig, ...],
        registry: ProviderRegistry,
        fetch_order: tuple[str, ...],
        assembly: _HTTPAssembly,
    ) -> tuple[tuple[KeywordSearchProvider, ...], tuple[ReservableFetchProvider, ...]]:
        search: list[KeywordSearchProvider] = []
        fetch: dict[str, ReservableFetchProvider] = {}
        for provider in providers:
            registration = registry.get(provider.name)
            if registration is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR, f"Invalid enabled web provider: {provider.name}"
                )

            create = partial(_web_adapter, registration)
            pool, _ = await assembly.build_pool("web", provider, create)
            facade = PooledWebProvider(pool)
            if provider.enable_search and registration.capabilities.search:
                search.append(facade)
            if provider.enable_fetch and registration.capabilities.fetch:
                fetch[provider.name] = facade
        ordered = dict.fromkeys((*fetch_order, *fetch))
        return tuple(search), tuple(fetch[name] for name in ordered if name in fetch)

    @staticmethod
    async def _build_academic_providers(
        providers: tuple[ResolvedAcademicProviderConfig, ...],
        registry: AcademicProviderRegistry,
        assembly: _HTTPAssembly,
    ) -> tuple[AcademicSearchProvider, ...]:
        search: list[AcademicSearchProvider] = []
        for provider in providers:
            registration = registry.get(provider.name)
            if registration is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR, f"Invalid enabled academic provider: {provider.name}"
                )
            create = partial(
                _academic_adapter, registration.factory, f"academic provider {provider.name}"
            )
            pool, _ = await assembly.build_pool(
                "academic",
                provider,
                cast(
                    Callable[[ResolvedCredential, HttpJsonExecutor], AcademicSearchProvider], create
                ),
            )
            search.append(PooledAcademicProvider(pool))
        return tuple(search)

    @staticmethod
    async def _build_oa_resolver(
        provider: ResolvedOAResolverConfig | None,
        registry: OAResolverRegistry,
        assembly: _HTTPAssembly,
    ) -> OAResolver | None:
        if provider is None:
            return None
        registration = registry.get(provider.name)
        if registration is None:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR, f"Invalid enabled OA resolver: {provider.name}"
            )
        create = partial(_academic_adapter, registration.factory, f"OA resolver {provider.name}")
        pool, _ = await assembly.build_pool(
            "oa",
            provider,
            cast(Callable[[ResolvedCredential, HttpJsonExecutor], OAResolver], create),
        )
        return PooledOAResolver(pool)

    @staticmethod
    def _build_agent_runners(
        config: ResolvedAgentConfig,
        quotas: ProviderQuotaManager,
        input_limits: ResolvedInputLimits = _DEFAULT_INPUT_LIMITS,
    ) -> dict[str, AgentTextRunner]:
        runners: dict[str, AgentTextRunner] = {}
        for provider in config.providers:
            if provider.harness != "antigravity":
                raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Unsupported Agent harness")
            command = provider.command
            if "/" in command:
                command = os.path.abspath(command)
            runners[provider.name] = partial(
                run_antigravity,
                provider_name=provider.name,
                command=command,
                timeout_seconds=provider.timeout_seconds,
                pass_env=provider.pass_env,
                quota=quotas.get_agent(provider.name),
                prompt_max_bytes=input_limits.agent_prompt_bytes,
            )
        return runners

    @staticmethod
    async def _build_llm_clients(
        config: ResolvedConfig, assembly: _HTTPAssembly
    ) -> dict[str, LLMClient]:
        clients: dict[str, LLMClient] = {}
        for provider in config.llm.providers:
            create = partial(_llm_adapter, provider.name, config)
            pool, first = await assembly.build_pool("llm", provider, create)
            # All physical clients use the alias protocol and input budget.
            prepare = cast(OpenAIChatCompletionsClient, first).prepare_request
            clients[provider.name] = PooledLLMClient(pool, prepare)
        return clients

    async def aclose(self) -> None:
        await self._resources.aclose()

    def __repr__(self) -> str:
        resolver_name = self.oa_resolver.name if self.oa_resolver is not None else "-"
        return (
            "Runtime("
            f"web_search={len(self.web_search_providers)}, "
            f"web_fetch={len(self.web_fetch_providers)}, "
            f"academic_search={len(self.academic_search_providers)}, "
            f"oa_resolver={resolver_name}, "
            f"llm_clients={len(self.llm_clients)}"
            ")"
        )
