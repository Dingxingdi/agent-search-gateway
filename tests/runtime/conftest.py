import logging
from collections.abc import Iterator

import pytest


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
