"""Stable facade for the pure purifier application protocol."""

from .matcher import ResponseMatcher
from .service import (
    GoveePurifierProtocol,
    initialization_requests,
    refresh_requests,
)
from .types import (
    MatchResult,
    ProtocolError,
    RequestDescriptor,
    ResponseKind,
    ResponseSpec,
)

__all__ = (
    "GoveePurifierProtocol",
    "MatchResult",
    "ProtocolError",
    "RequestDescriptor",
    "ResponseKind",
    "ResponseMatcher",
    "ResponseSpec",
    "initialization_requests",
    "refresh_requests",
)
