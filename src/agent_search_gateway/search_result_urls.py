"""Local unwrapping of recognized keyword search result URLs."""

import re
from urllib.parse import parse_qsl, urlsplit


class InvalidSearchResultRedirect(ValueError):
    """A recognized wrapper has no trustworthy destination."""

    def __init__(self) -> None:
        super().__init__("recognized_invalid_redirect")


def unwrap_search_result_url(raw: str) -> str:
    """Unwrap one recognized Google goto destination."""
    raw_has_controls = bool(re.search(r"[\x00-\x1f\x7f-\x9f]", raw))
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"google.com", "www.google.com"}
        or parsed.path != "/goto"
    ):
        return raw
    if raw_has_controls or parsed.username is not None:
        raise InvalidSearchResultRedirect()
    try:
        _ = parsed.port
    except ValueError:
        raise InvalidSearchResultRedirect() from None
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.query):
        raise InvalidSearchResultRedirect()
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, errors="strict")
    except UnicodeError:
        raise InvalidSearchResultRedirect() from None
    destinations = [value for key, value in pairs if key == "url"]
    if len(destinations) != 1 or not destinations[0]:
        raise InvalidSearchResultRedirect()
    target = destinations[0]
    if re.search(r"[\x00-\x1f\x7f-\x9f]|%(?![0-9a-fA-F]{2})", target):
        raise InvalidSearchResultRedirect()
    try:
        destination = urlsplit(target)
        if destination.scheme not in {"http", "https"} or not destination.hostname:
            raise InvalidSearchResultRedirect()
        _ = destination.port
    except ValueError:
        raise InvalidSearchResultRedirect() from None
    return target
