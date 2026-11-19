"""Built-in academic discovery and open-access resolver registrations."""

from .arxiv import ArxivProvider
from .core import CoreProvider
from .crossref import CrossrefProvider
from .dblp import DblpProvider
from .openalex import OpenAlexProvider
from .registry import (
    AcademicProviderRegistration,
    AcademicProviderRegistry,
    OAResolverRegistration,
    OAResolverRegistry,
)
from .semantic_scholar import SemanticScholarProvider
from .unpaywall import UnpaywallResolver

_ALLOWED_OPTIONS = frozenset({"api_url"})


def build_default_academic_registry() -> AcademicProviderRegistry:
    registry = AcademicProviderRegistry()
    registrations = (
        AcademicProviderRegistration("arxiv", ArxivProvider, _ALLOWED_OPTIONS, "none", "none"),
        AcademicProviderRegistration(
            "semantic_scholar",
            SemanticScholarProvider,
            _ALLOWED_OPTIONS,
            "optional",
            "none",
        ),
        AcademicProviderRegistration(
            "openalex", OpenAlexProvider, _ALLOWED_OPTIONS, "required", "optional"
        ),
        AcademicProviderRegistration("dblp", DblpProvider, _ALLOWED_OPTIONS, "none", "none"),
        AcademicProviderRegistration(
            "crossref", CrossrefProvider, _ALLOWED_OPTIONS, "none", "optional"
        ),
        AcademicProviderRegistration("core", CoreProvider, _ALLOWED_OPTIONS, "required", "none"),
    )
    for registration in registrations:
        registry.register(registration)
    return registry


def build_default_oa_resolver_registry() -> OAResolverRegistry:
    registry = OAResolverRegistry()
    registry.register(
        OAResolverRegistration(
            "unpaywall",
            UnpaywallResolver,
            _ALLOWED_OPTIONS,
            "none",
            "required",
        )
    )
    return registry
