from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from agent_search_gateway.providers.academic.core import CoreProvider
from agent_search_gateway.providers.academic.crossref import CrossrefProvider
from agent_search_gateway.providers.academic.dblp import DblpProvider
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.academic.openalex import OpenAlexProvider
from agent_search_gateway.providers.academic.semantic_scholar import SemanticScholarProvider
from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver


def test_default_academic_registry_has_exact_order_factories_and_requirements() -> None:
    registrations = build_default_academic_registry().list_in_registration_order()
    assert [item.name for item in registrations] == [
        "arxiv",
        "semantic_scholar",
        "openalex",
        "dblp",
        "crossref",
        "core",
    ]
    assert [item.factory for item in registrations] == [
        ArxivProvider,
        SemanticScholarProvider,
        OpenAlexProvider,
        DblpProvider,
        CrossrefProvider,
        CoreProvider,
    ]
    assert [(item.authentication, item.contact) for item in registrations] == [
        ("none", "none"),
        ("optional", "none"),
        ("required", "optional"),
        ("none", "none"),
        ("none", "optional"),
        ("required", "none"),
    ]
    assert registrations[0].allowed_config_keys == frozenset(
        {"api_url", "query_fields", "max_results", "sort_by", "sort_order"}
    )
    assert all(item.allowed_config_keys == frozenset({"api_url"}) for item in registrations[1:])
    assert all(item.credential_option_keys == frozenset({"api_url"}) for item in registrations)
    assert registrations[0].allowed_config_keys - registrations[0].credential_option_keys == {
        "query_fields",
        "max_results",
        "sort_by",
        "sort_order",
    }


def test_default_oa_registry_registers_unpaywall_with_required_contact() -> None:
    registrations = build_default_oa_resolver_registry().list_in_registration_order()
    assert len(registrations) == 1
    item = registrations[0]
    assert item.name == "unpaywall"
    assert item.factory is UnpaywallResolver
    assert item.authentication == "none"
    assert item.contact == "required"
    assert item.allowed_config_keys == frozenset({"api_url"})
    assert item.credential_option_keys == frozenset({"api_url"})
