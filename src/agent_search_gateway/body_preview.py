"""Safe, bounded debug previews of upstream transport bodies."""

import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from html import unescape

from .input_limits import (
    MAX_PREVIEW_SCAN_BYTES,
    TRUNCATION_MARKER,
    BodySelection,
    PreviewScanBudgetExceeded,
    TruncatableMessage,
    truncate_utf8_head_tail,
)
from .providers.contracts import ChatMessage

PREVIEW_BYTES = 8192
_MASK = "<redacted>"
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "token",
        "key",
        "idtoken",
        "sessiontoken",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "apikey",
        "xapikey",
        "privatekey",
        "credentials",
        "jwt",
        "password",
        "secret",
        "accesssecret",
        "clientsecret",
        "signature",
    }
)
_JSON_START = re.compile(
    r'\{\s*(?=["}])|\[(?!(?:\d{4}-\d{2}-\d{2}|\s*\d+\s*[-–—]\s*\d+\s*)\])'
    r'\s*(?=[\[{"\]\d-]|true\b|false\b|null\b)'
)
_NUMERIC_BRACKETS = re.compile(
    r"\[(?:[0-9]++(?:, ?[0-9]++)*+,? and [0-9]++|(?P<title>[0-9]++(?: [A-Za-z0-9]++)++))\]"
)
_MARKDOWN_DESTINATION = re.compile(r"([A-Za-z0-9._~:/?#@!&*+$,;=%-]++)\)")
_UNPARSED_QUOTE_OR_ESCAPE = re.compile(r"""["'\\]""")
_MAX_BRACKET_CHARS = 128
_MAX_DESTINATION_CHARS = 2048
_ENCODED_URL_DELIMITER = re.compile(r"%(?:23|3f|40|25)", re.IGNORECASE)
_URL_ATTRIBUTE = re.compile(
    r"""(?<![\w:-])(?:href|src|action|formaction|poster|cite)\s*=\s*"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s"'<>`]+))""",
    re.IGNORECASE,
)
_URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_MEMBER_SEPARATOR = re.compile(r"[ \r\n\t]*:[ \r\n\t]*")
_PEM_START = re.compile(r"-----BEGIN")
_PRIVATE_KEY_LABEL = r"(?P<label>(?:[A-Z0-9][A-Z0-9-]* )*PRIVATE KEY)"
_PRIVATE_KEY_HEADER = re.compile(rf"(?-i:-----BEGIN {_PRIVATE_KEY_LABEL}-----)")
_PRIVATE_KEY_FOOTER = re.compile(
    rf"(?<![^\r\n])[ \t]*-----END {_PRIVATE_KEY_LABEL}-----"
    r"""(?=[ \t]*(?:\r|\n|\Z|["'][ \t]*(?:[,;]|\r|\n|\Z)))"""
)
_TOKEN = re.compile(
    rf"(?P<json>{_JSON_START.pattern})|{_PRIVATE_KEY_HEADER.pattern}"
    r"""|(?P<url>(?:https?(?::|%3a)|(?<![\w/%\\]))(?:/|\\+/|%2f){2}[^\s<>"]+)"""
    r"""|\b(?:authorization|proxy[-_]authorization|cookie|set[-_]cookie)["']?\s*[:=][^\r\n]*"""
    r"""|\b(?:x[-_])?(?:(?:api|private)[-_]?key|access[-_]?(?:token|secret)|(?:auth|refresh|id|session)[-_]?token|client[-_]?secret|credentials|jwt|token|key|password|secret|signature)"""
    r"""["']?\s*[:=]\s*(?:(?P<structured>[\[{])|"(?:\\[\s\S]|[^"\\])*(?:"|\\?\Z)|'(?:\\[\s\S]|[^'\\])*(?:'|\\?\Z)|[^"'\s,;]+)"""
    r"""|\b(?:bearer|basic|key)[ \t]+[^\s,;]+""",
    re.IGNORECASE,
)
_DECODER = json.JSONDecoder()
_Edit = tuple[int, int, str]


def _check_scan_budget(*parts: str) -> None:
    chars = sum(map(len, parts))
    if chars <= MAX_PREVIEW_SCAN_BYTES // 4:
        return
    if chars > MAX_PREVIEW_SCAN_BYTES:
        raise PreviewScanBudgetExceeded
    size = sum(len(part) if part.isascii() else len(part.encode("utf-8")) for part in parts)
    if size > MAX_PREVIEW_SCAN_BYTES:
        raise PreviewScanBudgetExceeded


def _private_key_footer_ends(text: str, start: int) -> dict[str, deque[int]]:
    ends: dict[str, deque[int]] = {}
    for footer in _PRIVATE_KEY_FOOTER.finditer(text, start):
        ends.setdefault(footer["label"], deque()).append(footer.end())
    return ends


def _sensitive_key(key: str) -> bool:
    return key.lower().replace("_", "").replace("-", "") in _SENSITIVE_KEYS


def _url_field(key: str) -> bool:
    return (
        key.lower() in {"url", "uri", "href", "src", "link"}
        or key.lower().endswith(("_url", "-url", "_uri", "-uri"))
        or key.endswith(("Url", "URL", "Uri", "URI"))
    )


def _relative_url_edit(value: str) -> _Edit | None:
    """Strip private components only from a value already identified as a URL."""
    if value.lstrip().startswith("//") or _URI_SCHEME.match(value.lstrip()):
        return None
    delimiter = re.search(r"[?#]", value)
    path = value[: delimiter.start()] if delimiter else value
    if _ENCODED_URL_DELIMITER.search(path):
        return 0, len(value), _MASK
    return (delimiter.start(), len(value), "") if delimiter else None


def _masked_slice(text: str, edits: Sequence[_Edit], left: int, right: int) -> str:
    """Apply only intersections with this retained slice, in original coordinates."""
    parts: list[str] = []
    cursor = left
    for start, end, replacement in edits:
        if end <= left:
            continue
        if start >= right:
            break
        parts.append(text[cursor : max(start, left)])
        if start < left or end > right:
            # Never reconstruct a replacement from the omitted middle. Preserve JSON
            # string delimiters only when the retained range actually contained them.
            quoted = replacement.startswith('"') and text[start] == '"' and text[end - 1] == '"'
            replacement = ('"' if quoted and left <= start else "") + _MASK
            if quoted and end <= right:
                replacement += '"'
        parts.append(replacement)
        cursor = min(end, right)
    parts.append(text[cursor:right])
    return "".join(parts)


@dataclass(slots=True)
class _ProseScan:
    numeric_seen: bool = False
    ambiguous_gap: bool = False

    def check_gap(self, text: str, start: int, end: int) -> None:
        if not self.ambiguous_gap and _UNPARSED_QUOTE_OR_ESCAPE.search(text, start, end):
            self.ambiguous_gap = True


@dataclass(frozen=True)
class BodyScrubber:
    redact: Callable[[str], str]
    registered_spans: Callable[[str], Iterable[tuple[int, int]]]
    safe_url: Callable[[str], str]

    def _json_edits(self, text: str, start: int, end: int, scan: _ProseScan) -> Iterator[_Edit]:
        """Walk validated JSON string tokens using original offsets, including duplicate keys."""
        cursor = start
        url_value_at = -1
        while (opening := text.find('"', cursor, end)) >= 0:
            value, cursor = _DECODER.raw_decode(text, opening)
            separator = _MEMBER_SEPARATOR.match(text, cursor)
            if separator:
                if _sensitive_key(value):
                    _, cursor = _DECODER.raw_decode(text, separator.end())
                    yield separator.end(), cursor, json.dumps(_MASK)
                    continue
                if _url_field(value):
                    url_value_at = separator.end()

            edits = self._collect_edits(value, scan)
            if opening == url_value_at and (url_edit := _relative_url_edit(value)) is not None:
                edits = self._merge([*edits, url_edit])
            if text[opening + 1 : cursor - 1] == value:
                # Unescaped string content shares exact source offsets with its JSON token.
                for left, right, replacement in edits:
                    yield (
                        opening + 1 + left,
                        opening + 1 + right,
                        json.dumps(replacement, ensure_ascii=False)[1:-1],
                    )
            else:
                safe = self.redact(_masked_slice(value, edits, 0, len(value)))
                if safe != value:
                    yield opening, cursor, json.dumps(safe, ensure_ascii=False)

    def _numeric_prose_end(
        self, text: str, start: int, edits: list[_Edit], scan: _ProseScan
    ) -> int | None:
        if text[start] != "[" or text[start - 1 : start] == "\\":
            return None
        fragment = _NUMERIC_BRACKETS.match(text, start, start + _MAX_BRACKET_CHARS)
        if fragment is None:
            return None
        if not text.startswith("(", fragment.end()):
            return fragment.end() if fragment["title"] is None else None

        offset = fragment.end() + 1
        target = _MARKDOWN_DESTINATION.match(text, offset, offset + _MAX_DESTINATION_CHARS + 1)
        if target is None:
            raise ValueError("ambiguous Markdown destination")

        if title := fragment["title"]:
            for left, right, replacement in self._collect_edits(title, scan):
                edits.append((start + 1 + left, start + 1 + right, replacement))
        url = target[1]
        if ("&" in url and unescape(url) != url) or (
            _URI_SCHEME.match(url) and not url.lower().startswith(("http://", "https://"))
        ):
            edits.append((offset, target.end(1), _MASK))
        else:
            url_edits = self._collect_edits(url, scan)
            if relative := _relative_url_edit(url):
                url_edits.append(relative)
            for left, right, replacement in url_edits:
                edits.append((offset + left, offset + right, replacement))
        return target.end()

    def _edits(self, text: str) -> list[_Edit]:
        scan = _ProseScan()
        edits = self._collect_edits(text, scan)
        if scan.numeric_seen and scan.ambiguous_gap:
            raise ValueError("ambiguous quoted or escaped text beside numeric prose")
        return edits

    def _collect_edits(self, text: str, scan: _ProseScan) -> list[_Edit]:
        _check_scan_budget(text)
        edits = [(a, b, _MASK) for a, b in self.registered_spans(text)]
        for attribute in _URL_ATTRIBUTE.finditer(text):
            group = attribute.lastindex
            assert group is not None
            url = attribute[group]
            decoded = unescape(url) if "&" in url else url
            if decoded != url and any(char in decoded for char in "?#@"):
                # Decoded separators have no reliable offsets in the raw attribute.
                edits.append((*attribute.span(group), _MASK))
                continue
            edit = _relative_url_edit(url)
            if edit is not None:
                left, right, replacement = edit
                offset = attribute.start(group)
                edits.append((offset + left, offset + right, replacement))
        first = len(text) - len(text.lstrip())
        cursor = 0
        if text[first : first + 1] == '"':
            try:
                decoded, cursor = _DECODER.raw_decode(text, first)
            except json.JSONDecodeError:
                # A plain document may start with a quote. Ambiguous escaped JSON or
                # encoded separators must never fall back to a plaintext credential scan.
                if re.search(r'[\[{]|\\[utrnbf"/]', text):
                    raise
            else:
                separator = _MEMBER_SEPARATOR.match(text, cursor)
                if separator and _sensitive_key(decoded):
                    start_value = separator.end()
                    if _PRIVATE_KEY_HEADER.match(text, start_value):
                        # The plaintext scanner below handles the entire PEM span.
                        cursor = start_value
                    else:
                        _, cursor = _DECODER.raw_decode(text, start_value)
                        if cursor < len(text) and text[cursor] not in " \t\r\n,}]":
                            raise ValueError("ambiguous credential boundary")
                        edits.append((start_value, cursor, json.dumps(_MASK)))
                else:
                    decoded_edits = self._collect_edits(decoded, scan)
                    safe = self.redact(_masked_slice(decoded, decoded_edits, 0, len(decoded)))
                    if decoded != safe:
                        edits.append((first, cursor, json.dumps(safe, ensure_ascii=False)))
        footer_ends: dict[str, deque[int]] | None = None
        # Match the earliest token. A plaintext credential can enclose JSON.
        while match := _TOKEN.search(text, cursor):
            # Inspect each gap once. Complete JSON and recognized credential values are
            # consumed by their own handlers and are outside these ranges.
            scan.check_gap(text, cursor, match.start())
            start, cursor = match.span()
            if match["json"]:
                prose_end = self._numeric_prose_end(text, start, edits, scan)
                if prose_end is not None:
                    scan.numeric_seen = True
                    cursor = prose_end
                    continue
                _, cursor = _DECODER.raw_decode(text, start)
                edits.extend(self._json_edits(text, start, cursor, scan))
                continue
            url = match["url"]
            replacement = _MASK
            if match["structured"]:
                _, cursor = _DECODER.raw_decode(text, match.start("structured"))
            elif url:
                # Keep a closing HTML attribute quote outside the sanitized URL.
                if url.endswith("'") and text[start - 1 : start] == "'":
                    cursor -= 1
                    url = url[:-1]
                if not _ENCODED_URL_DELIMITER.search(url) and not any(self.registered_spans(url)):
                    if url.startswith("//"):
                        replacement = self.safe_url("https:" + url).removeprefix("https:")
                    else:
                        replacement = self.safe_url(url)
            if not match["structured"]:
                # A credential or URL token can consume the first word of the PEM header.
                marker = _PEM_START.search(text, start, cursor)
                while marker:
                    if header := _PRIVATE_KEY_HEADER.match(text, marker.start()):
                        replacement = _MASK
                        if footer_ends is None:
                            footer_ends = _private_key_footer_ends(text, header.end())
                        ends = footer_ends.get(header["label"])
                        # Headers are visited in source order, even when a block extends the token.
                        while ends and ends[0] <= header.end():
                            ends.popleft()
                        if not ends:
                            cursor = len(text)
                            break
                        cursor = max(cursor, ends.popleft())
                    marker = _PEM_START.search(text, marker.end(), cursor)
            if replacement != text[start:cursor]:
                edits.append((start, cursor, replacement))

        scan.check_gap(text, cursor, len(text))
        return self._merge(edits)

    def _merge(self, edits: list[_Edit]) -> list[_Edit]:
        merged: list[_Edit] = []
        for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], -edit[1])):
            if merged and start < merged[-1][1]:
                previous_start, previous_end, previous_replacement = merged[-1]
                if end > previous_end:
                    merged[-1] = (previous_start, end, _MASK)
                else:
                    merged[-1] = (previous_start, previous_end, self.redact(previous_replacement))
            else:
                merged.append((start, end, replacement))
        return merged

    def scrub(self, text: str) -> str:
        return self.redact(_masked_slice(text, self._edits(text), 0, len(text)))

    def project(
        self,
        original: Sequence[ChatMessage],
        candidate: Sequence[ChatMessage],
        selections: tuple[BodySelection, ...],
    ) -> Sequence[ChatMessage]:
        cuts = {cut.message_index: cut for cut in selections}
        projected = []
        for index, message in enumerate(candidate):
            source = original[index]
            if isinstance(source, TruncatableMessage):
                cut = cuts.get(index)
                prefix = len(source.protected_prefix)
                if prefix or source.protected_suffix:
                    _check_scan_budget(
                        source.protected_prefix, source.body, source.protected_suffix
                    )
                body_edits = self._edits(source.body)
                full = source["content"]
                if prefix or source.protected_suffix:
                    # A protected label can introduce a body credential. Keep both the
                    # standalone body's structure and spans from its surrounding context.
                    edits = self._edits(full)
                    edits.extend((a + prefix, b + prefix, value) for a, b, value in body_edits)
                    edits = self._merge(edits)
                else:
                    edits = body_edits
                if cut is None:
                    content = _masked_slice(full, edits, 0, len(full))
                else:
                    content = _masked_slice(full, edits, 0, prefix + cut.head_end)
                    content += TRUNCATION_MARKER
                    content += _masked_slice(full, edits, prefix + cut.tail_start, len(full))
                projected.append({"role": "user", "content": content})
            else:
                projected.append(
                    {
                        self.redact(k): _MASK if _sensitive_key(k) else self.scrub(v)
                        for k, v in message.items()
                    }
                )
        return projected


def limit_preview(text: str, render: Callable[[str], str]) -> tuple[str, bool]:
    if len(text) <= PREVIEW_BYTES and len(render(text).encode()) <= PREVIEW_BYTES:
        return text, False
    # The marker contains whitespace, so the physical field always has two quotes.
    capped = truncate_utf8_head_tail(text, PREVIEW_BYTES - 2)
    if capped.truncated and len(render(capped.text).encode()) <= PREVIEW_BYTES:
        return capped.text, True

    def edge(chars: Iterator[str], budget: int) -> str:
        result: list[str] = []
        for char in chars:
            # Exact UTF-8 cost inside a JSON string, excluding its outer quotes.
            if char in '"\\\b\f\n\r\t':
                cost = 2
            elif ord(char) < 32:
                cost = 6
            else:
                cost = len(char.encode())
            if cost > budget:
                break
            budget -= cost
            result.append(char)
        return "".join(result)

    available = PREVIEW_BYTES - len(render(TRUNCATION_MARKER).encode())
    head_budget = available * 4 // 5
    value = edge(iter(text), head_budget) + TRUNCATION_MARKER
    value += edge(reversed(text), available - head_budget)[::-1]
    if len(render(value).encode()) > PREVIEW_BYTES:
        raise ValueError("invalid preview field budget")
    return value, True
