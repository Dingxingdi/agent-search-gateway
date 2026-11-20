"""UTF-8 text and encoded request byte budgets for gateway prompts."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from .providers.contracts import ChatMessage

MAX_PREVIEW_SCAN_BYTES = 256 * 1024
TRUNCATION_MARKER = "[... truncated ...]"
_MARKER_BYTES = len(TRUNCATION_MARKER.encode())


class PreviewScanBudgetExceeded(ValueError):
    """The complete source is too large to safely prepare a debug preview."""


class InputBudgetUnfit(ValueError):
    """The input could not be encoded within the configured byte budget."""

    def __init__(self) -> None:
        super().__init__("input_budget_unfit")


@dataclass(frozen=True, slots=True)
class TextTruncation:
    text: str = field(repr=False)
    original_bytes: int
    sent_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class BodySelection:
    """Original body character indices bracketing the omitted middle."""

    message_index: int
    head_end: int
    tail_start: int


@dataclass(frozen=True, slots=True)
class PreparedPreview:
    content: bytes | None = field(repr=False)
    scan_budget_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class PreparedInput:
    content: bytes = field(repr=False)
    original_bytes: int
    input_truncated: bool
    preview_body: PreparedPreview | None = None


@dataclass(frozen=True, slots=True, eq=False)
class TruncatableMessage(Mapping[str, str]):
    """A user message with one explicitly marked, replaceable body."""

    protected_prefix: str = field(repr=False)
    body: str = field(repr=False)
    protected_suffix: str = field(default="", repr=False)

    def __iter__(self) -> Iterator[str]:
        return iter(("role", "content"))

    def __len__(self) -> int:
        return 2

    def __getitem__(self, key: str) -> str:
        if key == "role":
            return "user"
        if key == "content":
            return self.protected_prefix + self.body + self.protected_suffix
        raise KeyError(key)


PreviewProjector = Callable[
    [Sequence[ChatMessage], Sequence[ChatMessage], tuple[BodySelection, ...]],
    Sequence[ChatMessage] | None,
]
_Encoder = Callable[[Sequence[ChatMessage]], bytes]


def _utf8(text: str) -> bytes:
    try:
        return text.encode()
    except UnicodeEncodeError:
        pass
    raise InputBudgetUnfit()


def _head_tail(raw: bytes, retained_bytes: int) -> tuple[str, str]:
    head_bytes = retained_bytes * 4 // 5
    tail_offset = len(raw) - (retained_bytes - head_bytes)
    # The original was strictly encoded. Only a partial boundary code point can be dropped.
    return raw[:head_bytes].decode(errors="ignore"), raw[tail_offset:].decode(errors="ignore")


def truncate_utf8_head_tail(text: str, max_bytes: int) -> TextTruncation:
    raw = _utf8(text)
    if len(raw) <= max_bytes:
        return TextTruncation(text, len(raw), len(raw), False)
    if max_bytes < _MARKER_BYTES:
        raise InputBudgetUnfit()
    head, tail = _head_tail(raw, max_bytes - _MARKER_BYTES)
    retained = head + TRUNCATION_MARKER + tail
    return TextTruncation(retained, len(raw), len(retained.encode()), True)


def _encoded(messages: Sequence[ChatMessage], encode: _Encoder) -> bytes:
    try:
        return encode(messages)
    except Exception:
        pass
    # An encoder exception may embed unredacted content, including in its chained context.
    raise InputBudgetUnfit()


def _fit_oversize(
    messages: Sequence[ChatMessage], max_bytes: int, encode: _Encoder, original: bytes
) -> tuple[Sequence[ChatMessage], bytes, tuple[BodySelection, ...]]:
    minimum = list(messages)
    content = original
    bodies: dict[int, tuple[TruncatableMessage, bytes]] = {}
    cuts: list[BodySelection] = []
    for index, message in enumerate(messages):
        if not isinstance(message, TruncatableMessage) or not message.body:
            continue
        # JSON escapes can make a short body more expensive than the entire marker.
        minimum[index] = TruncatableMessage(
            message.protected_prefix, TRUNCATION_MARKER, message.protected_suffix
        )
        marked = _encoded(minimum, encode)
        if len(marked) < len(content):
            content = marked
            bodies[index] = (message, _utf8(message.body))
            cuts.append(BodySelection(index, 0, len(message.body)))
        else:
            minimum[index] = message
    if len(content) > max_bytes:
        raise InputBudgetUnfit()

    available = max_bytes - len(content)
    total = sum(len(raw) for _, raw in bodies.values())
    lower, upper = 0, available
    retained = upper
    best = minimum, content, tuple(cuts)
    while lower < upper:
        candidate = minimum.copy()
        selections = []
        for index, (message, raw) in bodies.items():
            share = retained * len(raw) // total
            if share >= len(raw):
                candidate[index] = message
                continue
            head, tail = _head_tail(raw, share)
            candidate[index] = TruncatableMessage(
                message.protected_prefix, head + TRUNCATION_MARKER + tail, message.protected_suffix
            )
            selections.append(BodySelection(index, len(head), len(message.body) - len(tail)))
        trial = _encoded(candidate, encode)
        if len(trial) <= max_bytes:
            if len(trial) >= len(best[1]):
                best = candidate, trial, tuple(selections)
            if len(trial) == max_bytes:
                break
            lower = retained
        else:
            upper = retained - 1
        # Each trial is measured: adding a complete body can remove its marker.
        retained = (lower + upper + 1) // 2
    return best


def fit_messages_to_budget(
    messages: Sequence[ChatMessage],
    *,
    max_bytes: int,
    encode: _Encoder,
    preview_projector: PreviewProjector | None = None,
) -> PreparedInput:
    """Fit explicit bodies to the gateway's complete JSON or role-labelled prompt encoding.

    These encoders have fixed framing and additive, nonnegative per-character costs.
    Thus each body's cheaper choice of unchanged text or marker contributes to a true
    minimum. Additional head/tail candidates are always measured with the same encoder.
    """
    original = _encoded(messages, encode)
    candidate = messages
    content = original
    selections: tuple[BodySelection, ...] = ()
    if len(original) > max_bytes:
        candidate, content, selections = _fit_oversize(messages, max_bytes, encode, original)

    preview = None
    if preview_projector is not None:
        try:
            if len(original) > MAX_PREVIEW_SCAN_BYTES:
                raise PreviewScanBudgetExceeded
            projected = preview_projector(messages, candidate, selections)
            preview = PreparedPreview(None if projected is None else encode(projected))
        except PreviewScanBudgetExceeded:
            preview = PreparedPreview(None, scan_budget_exceeded=True)
        except Exception:
            preview = PreparedPreview(None)
    return PreparedInput(content, len(original), bool(selections), preview)
