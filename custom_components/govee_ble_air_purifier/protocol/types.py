"""Value objects, enums, and errors for the purifier protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..frame import validate_frame

__all__ = (
    "MatchResult",
    "ProtocolError",
    "RequestDescriptor",
    "ResponseKind",
    "ResponseSpec",
)


class ProtocolError(ValueError):
    """Raised when a command cannot be represented by the protocol."""


class ResponseKind(StrEnum):
    """How a request's response is recognized and completed."""

    EXACT = "exact"
    VALUE_BYTE = "value_byte"
    ZERO_PAYLOAD = "zero_payload"
    PREFIX = "prefix"
    PREFIX_SELECTOR = "prefix_selector"
    FRAGMENTS = "fragments"
    H7129_METADATA = "h7129_metadata"
    FIELDS = "fields"


class MatchResult(StrEnum):
    """Result of offering a notification to a response matcher."""

    IGNORED = "ignored"
    ACCEPTED = "accepted"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """Immutable rules for recognizing a request's response."""

    kind: ResponseKind
    prefix: bytes = b""
    selector: bytes = b""
    exact: bytes = b""
    exact_alternatives: tuple[bytes, ...] = ()
    fragments: tuple[int, ...] = ()
    allowed_prefixes: tuple[bytes, ...] = ()
    expected_fields: tuple[tuple[int, int], ...] = ()
    allowed_values: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestDescriptor:
    """A documented plaintext request and its response-completion rule."""

    name: str
    frame: bytes
    response: ResponseSpec

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame", validate_frame(self.frame))

    @property
    def command(self) -> bytes:
        """Compatibility alias for callers that describe a request as a command."""

        return self.frame
