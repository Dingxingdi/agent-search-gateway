import logging
from collections.abc import Iterator
from typing import Any

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.credential_pool import CredentialMember
from agent_search_gateway.providers.pooled import (
    PooledAcademicProvider,
    PooledLLMClient,
    PooledOAResolver,
    PooledWebProvider,
)


@pytest.fixture(autouse=True)
def restore_runtime_logger() -> Iterator[None]:
    logger = logging.getLogger("agent_search_gateway.runtime")
    original_handlers = logger.handlers.copy()
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        yield
    finally:
        added_handlers = [
            handler for handler in logger.handlers if handler not in original_handlers
        ]
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        for handler in added_handlers:
            handler.close()


def only_member(provider: object) -> CredentialMember[Any]:
    """Return the physical member for single-credential assembly assertions."""
    assert isinstance(
        provider, (PooledWebProvider, PooledAcademicProvider, PooledOAResolver, PooledLLMClient)
    )
    (member,) = provider._pool._members
    return member


def configured_capacity(provider: object) -> CapacityGate:
    gate = only_member(provider).capacity
    assert gate is not None
    return gate
