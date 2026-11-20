import io
import logging
import re
from collections.abc import Iterable

from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue


def structured_test_logger(
    name: str,
    *,
    secrets: Iterable[SecretValue] = (),
) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter(SecretRedactor(secrets)))
    logger.addHandler(handler)
    return logger, stream


# Match only the formatter's metadata prefix, respecting JSON-quoted field values.
_DEBUG_PREVIEW = re.compile(
    r'^DEBUG time=\S+ request=\S+(?: (?:provider|stage|url)=(?:"(?:\\.|[^"\\])*"|[^\s"]+))*'
    r" event=upstream_body_preview(?: |$)"
)


def without_debug_body_previews(logged: str) -> str:
    return "\n".join(line for line in logged.splitlines() if not _DEBUG_PREVIEW.match(line))
