"""Response matching and multipart response progress."""

from __future__ import annotations

from ..frame import ApplicationFrame, validate_frame
from .types import MatchResult, RequestDescriptor, ResponseKind, ResponseSpec

__all__ = ("ResponseMatcher",)


class ResponseMatcher:
    """Mutable response progress for one request transaction.

    Unrelated notifications are ignored. Multipart metadata responses accept
    only their documented fragment sequence; an immediate duplicate of the
    most recently accepted fragment is tolerated without advancing progress.
    """

    def __init__(self, descriptor: RequestDescriptor) -> None:
        self.descriptor = descriptor
        self._frames: list[bytes] = []
        self._fragment_index = 0
        self._complete = False

    @property
    def frames(self) -> tuple[bytes, ...]:
        return tuple(self._frames)

    @property
    def complete(self) -> bool:
        return self._complete

    def feed(
        self, frame: bytes | bytearray | memoryview | ApplicationFrame
    ) -> MatchResult:
        """Offer a plaintext frame to this transaction."""

        if self._complete:
            return MatchResult.IGNORED
        data = (
            frame.data if isinstance(frame, ApplicationFrame) else validate_frame(frame)
        )
        spec = self.descriptor.response

        if data in spec.exact_alternatives:
            matched = True
        elif spec.kind is ResponseKind.EXACT:
            matched = data == spec.exact
        elif spec.kind is ResponseKind.VALUE_BYTE:
            matched = (
                data[:2] == spec.prefix
                and data[2] in spec.allowed_values
                and not any(data[3:19])
            )
        elif spec.kind is ResponseKind.ZERO_PAYLOAD:
            matched = data[:2] == spec.prefix and not any(data[2:19])
        elif spec.kind is ResponseKind.PREFIX:
            matched = data.startswith(spec.prefix)
        elif spec.kind is ResponseKind.PREFIX_SELECTOR:
            matched = (
                data.startswith(spec.prefix)
                and data[len(spec.prefix) : len(spec.prefix) + len(spec.selector)]
                == spec.selector
            )
        elif spec.kind is ResponseKind.H7129_METADATA:
            # Captures establish containment, but not a formal byte-offset layout.
            matched = data[:2] == b"\xab\x00" and b"\x02\x02\x00\x01" in data[2:19]
        elif spec.kind is ResponseKind.FIELDS:
            matched = any(
                data.startswith(prefix) for prefix in spec.allowed_prefixes
            ) and all(data[offset] == value for offset, value in spec.expected_fields)
        else:
            return self._feed_fragment(data, spec)

        if not matched:
            return MatchResult.IGNORED
        self._frames.append(data)
        self._complete = True
        return MatchResult.COMPLETE

    def _feed_fragment(self, data: bytes, spec: ResponseSpec) -> MatchResult:
        if data[0] != 0xAB or not spec.fragments:
            return MatchResult.IGNORED
        fragment = data[1]
        expected = spec.fragments[self._fragment_index]
        if fragment == expected:
            self._frames.append(data)
            self._fragment_index += 1
            if self._fragment_index == len(spec.fragments):
                self._complete = True
                return MatchResult.COMPLETE
            return MatchResult.ACCEPTED
        if (
            self._fragment_index
            and fragment == spec.fragments[self._fragment_index - 1]
        ):
            return MatchResult.ACCEPTED
        return MatchResult.IGNORED
