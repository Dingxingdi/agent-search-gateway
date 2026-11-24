"""Registration-order-preserving web provider registry."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .contracts import ProviderCapabilities

WebStage = Literal["search", "fetch"]
WebProviderFactory = Callable[..., object]


@dataclass(frozen=True, slots=True)
class WebProviderRegistration:
    name: str
    capabilities: ProviderCapabilities
    factory: WebProviderFactory
    allowed_config_keys: frozenset[str]
    requires_api_key: bool = True
    supports_optional_api_key: bool = False
    validate_config: Callable[[Mapping[str, object], bool], None] | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, WebProviderRegistration] = {}

    def register(self, registration: WebProviderRegistration) -> None:
        if registration.name in self._registrations:
            raise ValueError(f"provider already registered: {registration.name}")
        self._registrations[registration.name] = registration

    def get(self, name: str) -> WebProviderRegistration | None:
        return self._registrations.get(name)

    def require(self, name: str) -> WebProviderRegistration:
        registration = self.get(name)
        if registration is None:
            raise KeyError(name)
        return registration

    def capabilities(self, name: str) -> ProviderCapabilities:
        return self.require(name).capabilities

    def list_in_registration_order(self) -> tuple[WebProviderRegistration, ...]:
        return tuple(self._registrations.values())

    def for_stage(self, stage: WebStage) -> tuple[WebProviderRegistration, ...]:
        if stage == "search":
            return tuple(item for item in self._registrations.values() if item.capabilities.search)
        return tuple(item for item in self._registrations.values() if item.capabilities.fetch)
