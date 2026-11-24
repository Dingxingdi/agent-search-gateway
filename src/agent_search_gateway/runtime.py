"""Resolved runtime assembly for the foreground daemon."""

import logging
import os
import ssl
from collections.abc import Callable, Mapping
from functools import partial
from typing import cast

import httpx

from .academic.aggregator import PaperAggregator
from .concurrency import ProviderQuotaManager
from .config import (
    ResolvedAcademicProviderConfig,
    ResolvedAgentConfig,
    ResolvedConfig,
    ResolvedInputLimits,
    ResolvedOAResolverConfig,
    ResolvedWebProviderConfig,
)
from .errors import ConfigFailure, ErrorCode
from .llm.stages import LLMStages
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
    URLFetchProvider,
)
from .providers.defaults import build_default_registry
from .providers.http import HttpJsonExecutor
from .providers.llm.openai_chat import OpenAIChatCompletionsClient
from .providers.registry import ProviderRegistry
from .rate_limit import RateGate
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
    try:
        # Match HTTPX's FILE-before-DIR precedence, including on versions that
        # silently skip missing paths. OpenSSL loads a capath lazily.
        if ca_file and not os.path.isfile(ca_file):
            raise ValueError
        if not ca_file and ca_dir:
            directories = [path for path in ca_dir.split(os.pathsep) if path]
            if not all(os.path.isdir(path) for path in directories):
                raise ValueError
            if os.pathsep in ca_dir and httpx.__version__.startswith("0.27."):
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
    if use_proxy:
        return factory()
    if direct_ssl_context is None:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, f"Missing direct TLS context for {label}")
    try:
        return factory(trust_env=False, verify=direct_ssl_context)
    except TypeError:
        raise ConfigFailure(
            ErrorCode.CONFIG_ERROR, f"HTTP client factory failed for direct connection to {label}"
        ) from None


class Runtime:
    """Owns one in-memory gateway runtime and its transport clients."""

    def __init__(
        self,
        *,
        quotas: ProviderQuotaManager,
        web_search_providers: tuple[KeywordSearchProvider, ...],
        web_fetch_providers: tuple[URLFetchProvider, ...],
        academic_search_providers: tuple[AcademicSearchProvider, ...],
        oa_resolver: OAResolver | None,
        llm_clients: Mapping[str, LLMClient],
        store: URLStore,
        search_orchestrator: SearchOrchestrator,
        paper_search_orchestrator: PaperSearchOrchestrator,
        fetch_orchestrator: FetchOrchestrator,
        web_http_executors: tuple[HttpJsonExecutor, ...],
        academic_http_executors: tuple[HttpJsonExecutor, ...],
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
        self._web_http_executors = web_http_executors
        self._academic_http_executors = academic_http_executors
        self._closed = False

    @classmethod
    def build(
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
            web_limits={item.name: item.max_concurrency for item in enabled_web},
            llm_limits={item.name: item.max_concurrency for item in config.llm.providers},
            academic_limits={item.name: item.max_concurrency for item in enabled_academic},
            agent_limits={item.name: item.max_concurrency for item in config.agent.providers},
        )
        agent_runners = cls._build_agent_runners(config.agent, quotas, config.input_limits)
        web_search, web_fetch, web_executors = cls._build_web_providers(
            enabled_web,
            provider_registry,
            config,
            quotas,
            http_client_factory,
            direct_ssl_context,
        )
        academic_search, academic_executors = cls._build_academic_providers(
            enabled_academic,
            academic_provider_registry,
            config,
            quotas,
            http_client_factory,
            direct_ssl_context,
        )
        oa_resolver, resolver_executors = cls._build_oa_resolver(
            config.oa_resolver,
            resolver_registry,
            config,
            quotas,
            http_client_factory,
            direct_ssl_context,
        )
        llm_clients = cls._build_llm_clients(
            config, quotas, http_client_factory, direct_ssl_context
        )
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
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=result_writer,
            paper_aggregator=paper_aggregator,
            paper_resolver=oa_resolver,
        )
        paper_search_orchestrator = PaperSearchOrchestrator(
            providers=academic_search,
            quotas=quotas,
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
            web_http_executors=web_executors,
            academic_http_executors=(*academic_executors, *resolver_executors),
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
            web_limits=",".join(f"{item.name}:{item.max_concurrency}" for item in enabled_web)
            or "-",
            llm_limits=",".join(
                f"{item.name}:{item.max_concurrency}" for item in config.llm.providers
            )
            or "-",
            academic_limits=",".join(
                f"{item.name}:{item.max_concurrency}" for item in enabled_academic
            )
            or "-",
        )
        return runtime

    @classmethod
    def _build_web_providers(
        cls,
        providers: tuple[ResolvedWebProviderConfig, ...],
        registry: ProviderRegistry,
        config: ResolvedConfig,
        quotas: ProviderQuotaManager,
        http_client_factory: HttpClientFactory,
        direct_ssl_context: ssl.SSLContext | None,
    ) -> tuple[
        tuple[KeywordSearchProvider, ...],
        tuple[URLFetchProvider, ...],
        tuple[HttpJsonExecutor, ...],
    ]:
        search: list[KeywordSearchProvider] = []
        fetch: list[URLFetchProvider] = []
        executors: list[HttpJsonExecutor] = []
        for provider_config in providers:
            registration = registry.get(provider_config.name)
            if registration is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid enabled web provider: {provider_config.name}",
                )
            credential_kwargs: dict[str, object] = {}
            private_value = provider_config.secret
            if registration.requires_api_key and private_value is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid enabled web provider: {provider_config.name}",
                )
            if private_value is not None and (
                registration.requires_api_key or registration.supports_optional_api_key
            ):
                credential_kwargs["secret"] = private_value
            reserved = set(provider_config.options) & _RESERVED_WEB_ADAPTER_KWARGS
            if reserved:
                names = ", ".join(sorted(reserved))
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Reserved config key(s) for web provider {provider_config.name}: {names}",
                )
            gate = RateGate(provider_config.rate_policy)
            quotas.register_rate("web", provider_config.name, gate)
            executor = HttpJsonExecutor(
                _create_http_client(
                    http_client_factory,
                    use_proxy=provider_config.use_proxy,
                    direct_ssl_context=direct_ssl_context,
                    label=f"web provider {provider_config.name}",
                ),
                config.retry,
                provider_name=provider_config.name,
                rate_gate=gate,
            )
            kwargs: dict[str, object] = {
                "name": provider_config.name,
                "http_executor": executor,
            }
            kwargs.update(credential_kwargs)
            kwargs.update(provider_config.options)
            try:
                adapter = registration.factory(**kwargs)
            except TypeError as exc:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid configuration for web provider {provider_config.name}",
                ) from exc
            executors.append(executor)
            if provider_config.enable_search:
                search.append(cast(KeywordSearchProvider, adapter))
            if provider_config.enable_fetch:
                fetch.append(cast(URLFetchProvider, adapter))
        return tuple(search), tuple(fetch), tuple(executors)

    @classmethod
    def _build_academic_providers(
        cls,
        providers: tuple[ResolvedAcademicProviderConfig, ...],
        registry: AcademicProviderRegistry,
        config: ResolvedConfig,
        quotas: ProviderQuotaManager,
        http_client_factory: HttpClientFactory,
        direct_ssl_context: ssl.SSLContext | None,
    ) -> tuple[tuple[AcademicSearchProvider, ...], tuple[HttpJsonExecutor, ...]]:
        search: list[AcademicSearchProvider] = []
        executors: list[HttpJsonExecutor] = []
        for provider_config in providers:
            registration = registry.get(provider_config.name)
            if registration is None:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid enabled academic provider: {provider_config.name}",
                )
            reserved = set(provider_config.options) & _RESERVED_ACADEMIC_ADAPTER_KWARGS
            if reserved:
                names = ", ".join(sorted(reserved))
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Reserved config key(s) for academic provider {provider_config.name}: {names}",
                )
            gate = RateGate(provider_config.rate_policy)
            quotas.register_rate("academic", provider_config.name, gate)
            executor = HttpJsonExecutor(
                _create_http_client(
                    http_client_factory,
                    use_proxy=provider_config.use_proxy,
                    direct_ssl_context=direct_ssl_context,
                    label=f"academic provider {provider_config.name}",
                ),
                config.retry,
                provider_name=provider_config.name,
                rate_gate=gate,
            )
            kwargs: dict[str, object] = {"executor": executor}
            if provider_config.api_key is not None:
                kwargs["api_key"] = provider_config.api_key
            if provider_config.contact_email is not None:
                kwargs["contact_email"] = provider_config.contact_email
            kwargs.update(provider_config.options)
            try:
                adapter = registration.factory(**kwargs)
            except TypeError as exc:
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Invalid configuration for academic provider {provider_config.name}",
                ) from exc
            search.append(cast(AcademicSearchProvider, adapter))
            executors.append(executor)
        return tuple(search), tuple(executors)

    @classmethod
    def _build_oa_resolver(
        cls,
        resolver_config: ResolvedOAResolverConfig | None,
        registry: OAResolverRegistry,
        config: ResolvedConfig,
        quotas: ProviderQuotaManager,
        http_client_factory: HttpClientFactory,
        direct_ssl_context: ssl.SSLContext | None,
    ) -> tuple[OAResolver | None, tuple[HttpJsonExecutor, ...]]:
        if resolver_config is None:
            return None, ()
        registration = registry.get(resolver_config.name)
        if registration is None:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Invalid enabled OA resolver: {resolver_config.name}",
            )
        reserved = set(resolver_config.options) & _RESERVED_ACADEMIC_ADAPTER_KWARGS
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Reserved config key(s) for OA resolver {resolver_config.name}: {names}",
            )
        gate = RateGate(resolver_config.rate_policy)
        quotas.register_rate("oa", resolver_config.name, gate)
        executor = HttpJsonExecutor(
            _create_http_client(
                http_client_factory,
                use_proxy=resolver_config.use_proxy,
                direct_ssl_context=direct_ssl_context,
                label=f"OA resolver {resolver_config.name}",
            ),
            config.retry,
            provider_name=resolver_config.name,
            rate_gate=gate,
        )
        kwargs: dict[str, object] = {"executor": executor}
        if resolver_config.api_key is not None:
            kwargs["api_key"] = resolver_config.api_key
        if resolver_config.contact_email is not None:
            kwargs["contact_email"] = resolver_config.contact_email
        kwargs.update(resolver_config.options)
        try:
            adapter = registration.factory(**kwargs)
        except TypeError as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Invalid configuration for OA resolver {resolver_config.name}",
            ) from exc
        return cast(OAResolver, adapter), (executor,)

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
    def _build_llm_clients(
        config: ResolvedConfig,
        quotas: ProviderQuotaManager,
        http_client_factory: HttpClientFactory,
        direct_ssl_context: ssl.SSLContext | None,
    ) -> dict[str, LLMClient]:
        clients: dict[str, LLMClient] = {}
        for provider_config in config.llm.providers:
            private_value = provider_config.secret
            executor = HttpJsonExecutor(
                _create_http_client(
                    http_client_factory,
                    use_proxy=provider_config.use_proxy,
                    direct_ssl_context=direct_ssl_context,
                    label=f"LLM provider {provider_config.name}",
                ),
                config.retry,
                provider_name=provider_config.name,
            )
            clients[provider_config.name] = OpenAIChatCompletionsClient(
                name=provider_config.name,
                api_url=provider_config.api_url,
                secret=private_value,
                executor=executor,
                quota=quotas.get_llm(provider_config.name),
                request_body_bytes=config.input_limits.llm_request_body_bytes,
                retry_policy=config.retry,
            )
        return clients

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for executor in self._web_http_executors:
            await executor.aclose()
        for executor in self._academic_http_executors:
            await executor.aclose()
        for client in self.llm_clients.values():
            await client.aclose()

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
