"""Registration-order-preserving academic provider and OA resolver registries."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Requirement = Literal["none", "optional", "required"]
AcademicProviderFactory = Callable[..., object]
OAResolverFactory = Callable[..., object]


@dataclass(frozen=True, slots=True)
class AcademicProviderRegistration:
    name: str
    factory: AcademicProviderFactory
    allowed_config_keys: frozenset[str] = frozenset()
    authentication: Requirement = "none"
    contact: Requirement = "none"

    @property
    def credential_option_keys(self) -> frozenset[str]:
        return self.allowed_config_keys & {"api_url"}


@dataclass(frozen=True, slots=True)
class OAResolverRegistration:
    name: str
    factory: OAResolverFactory
    allowed_config_keys: frozenset[str] = frozenset()
    authentication: Requirement = "none"
    contact: Requirement = "none"

    @property
    def credential_option_keys(self) -> frozenset[str]:
        return self.allowed_config_keys & {"api_url"}


class AcademicProviderRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, AcademicProviderRegistration] = {}

    def register(self, registration: AcademicProviderRegistration) -> None:
        _validate_registration(registration.name, registration.authentication, registration.contact)
        if registration.name in self._registrations:
            raise ValueError(f"academic provider already registered: {registration.name}")
        self._registrations[registration.name] = registration

    def get(self, name: str) -> AcademicProviderRegistration | None:
        return self._registrations.get(name)

    def require(self, name: str) -> AcademicProviderRegistration:
        registration = self.get(name)
        if registration is None:
            raise KeyError(name)
        return registration

    def list_in_registration_order(self) -> tuple[AcademicProviderRegistration, ...]:
        return tuple(self._registrations.values())


class OAResolverRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, OAResolverRegistration] = {}

    def register(self, registration: OAResolverRegistration) -> None:
        _validate_registration(registration.name, registration.authentication, registration.contact)
        if registration.name in self._registrations:
            raise ValueError(f"OA resolver already registered: {registration.name}")
        self._registrations[registration.name] = registration

    def get(self, name: str) -> OAResolverRegistration | None:
        return self._registrations.get(name)

    def require(self, name: str) -> OAResolverRegistration:
        registration = self.get(name)
        if registration is None:
            raise KeyError(name)
        return registration

    def list_in_registration_order(self) -> tuple[OAResolverRegistration, ...]:
        return tuple(self._registrations.values())


def _validate_registration(name: str, authentication: str, contact: str) -> None:
    if not name.strip():
        raise ValueError("registration name must be non-empty")
    valid = {"none", "optional", "required"}
    if authentication not in valid:
        raise ValueError(f"invalid authentication requirement: {authentication}")
    if contact not in valid:
        raise ValueError(f"invalid contact requirement: {contact}")
