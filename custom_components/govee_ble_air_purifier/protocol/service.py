"""Facade that composes profile requests, matching, and codec behavior."""

from __future__ import annotations

from ..frame import ApplicationFrame
from ..models import DecodedEvent, ProtocolCommand
from ..profiles import DeviceProfile
from .codec import ProtocolCodec
from .matcher import ResponseMatcher
from .requests import build_request_catalog, response_for_command
from .types import RequestDescriptor

__all__ = (
    "GoveePurifierProtocol",
    "initialization_requests",
    "refresh_requests",
)


class GoveePurifierProtocol:
    """Encode commands and decode plaintext frames for one model profile."""

    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile
        self._requests = build_request_catalog(profile)
        self._codec = ProtocolCodec(self._requests)

    def initialization_requests(self) -> tuple[RequestDescriptor, ...]:
        """Return the official app's documented 23/24 request sweep."""

        return tuple(
            self._requests[name]
            for name in self.profile.protocol.initialization_order
        )

    def refresh_requests(self) -> tuple[RequestDescriptor, ...]:
        """Return the documented short sweep triggered by active-session ``ee aa``."""

        return tuple(
            self._requests[name] for name in self.profile.protocol.refresh_order
        )

    def device_state_poll(self) -> RequestDescriptor:
        """Return the sole documented steady-state three-second poll."""

        return self._requests[self.profile.protocol.periodic_request]

    @staticmethod
    def new_response_matcher(descriptor: RequestDescriptor) -> ResponseMatcher:
        return ResponseMatcher(descriptor)

    def command_request(
        self, command: ProtocolCommand, *, name: str | None = None
    ) -> RequestDescriptor:
        """Build a transaction descriptor for a typed query or control.

        Power deliberately waits for matching ``aa 01`` applied state rather
        than completing on a ``33 01`` echo. Fan mode accepts either the exact
        ``3a 05`` command acknowledgement or a matching unsolicited ``ee 05``
        mode update. A night-light RGB ``3a`` echo is the strongest documented
        response, but remains an acknowledgement rather than independent
        displayed-color confirmation.
        """

        frame = self.encode(command)
        descriptor_name = name or type(command).__name__
        return RequestDescriptor(
            descriptor_name,
            frame,
            response_for_command(command, frame),
        )

    # A short alias reads naturally at call sites constructing a transaction.
    request = command_request

    def encode(self, command: ProtocolCommand) -> bytes:
        """Encode a typed command as a checksum-valid plaintext frame."""

        return self._codec.encode(command, self.profile)

    def decode(
        self, frame: bytes | bytearray | memoryview | ApplicationFrame
    ) -> DecodedEvent:
        """Decode one checksum-valid plaintext application frame."""

        return self._codec.decode(frame, self.profile)


def initialization_requests(profile: DeviceProfile) -> tuple[RequestDescriptor, ...]:
    """Functional convenience wrapper around :class:`GoveePurifierProtocol`."""

    return GoveePurifierProtocol(profile).initialization_requests()


def refresh_requests(profile: DeviceProfile) -> tuple[RequestDescriptor, ...]:
    """Functional convenience wrapper for the short refresh sweep."""

    return GoveePurifierProtocol(profile).refresh_requests()
