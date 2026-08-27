"""Profile-derived request catalogs and command response rules."""

from __future__ import annotations

from ..models import (
    FanMode,
    ProtocolCommand,
    QueryAirQuality,
    QueryDeviceState,
    QueryNightLightColor,
    QueryNightLightState,
    RawCommand,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
)
from ..profiles import DeviceProfile, MatcherDefinition
from .types import RequestDescriptor, ResponseKind, ResponseSpec

__all__ = ("build_request_catalog", "response_for_command")


def _prefix(prefix: bytes) -> ResponseSpec:
    return ResponseSpec(ResponseKind.PREFIX, prefix=prefix)


def _selector(prefix: bytes, selector: bytes) -> ResponseSpec:
    return ResponseSpec(ResponseKind.PREFIX_SELECTOR, prefix=prefix, selector=selector)


def _exact(frame: bytes) -> ResponseSpec:
    return ResponseSpec(ResponseKind.EXACT, exact=frame)


def _response_spec(definition: MatcherDefinition) -> ResponseSpec:
    """Translate one validated closed matcher definition to runtime rules."""

    return ResponseSpec(
        kind=ResponseKind(definition.kind),
        prefix=definition.prefix,
        selector=definition.selector,
        exact=definition.exact,
        exact_alternatives=definition.exact_alternatives,
        fragments=definition.fragments,
        allowed_prefixes=definition.allowed_prefixes,
        expected_fields=definition.expected_fields,
        allowed_values=definition.allowed_values,
    )


def build_request_catalog(profile: DeviceProfile) -> dict[str, RequestDescriptor]:
    """Build immutable runtime descriptors from one validated profile."""

    return {
        name: RequestDescriptor(
            definition.name,
            definition.frame,
            _response_spec(definition.response),
        )
        for name, definition in profile.protocol.request_catalog.items()
    }


def response_for_command(command: ProtocolCommand, frame: bytes) -> ResponseSpec:
    """Return the response-completion rule for one encoded typed command."""

    if isinstance(command, QueryDeviceState):
        return _prefix(b"\xaa\x01")
    if isinstance(command, QueryNightLightState):
        return _selector(b"\xaa\x1b", b"\x01")
    if isinstance(command, QueryNightLightColor):
        return _selector(b"\xaa\x1b", b"\x05")
    if isinstance(command, QueryAirQuality):
        return _prefix(b"\xaa\x19")
    if isinstance(command, SetPower):
        return ResponseSpec(
            ResponseKind.FIELDS,
            allowed_prefixes=(b"\xaa\x01",),
            expected_fields=((2, int(command.on)),),
        )
    if isinstance(command, SetFanMode):
        mode = FanMode(command.mode)
        if mode in (FanMode.LOW, FanMode.MEDIUM, FanMode.HIGH):
            fan_fields = ((2, frame[2]), (3, frame[3]))
        elif mode is FanMode.AUTO:
            fan_fields = ((2, frame[2]), (5, frame[5]))
        else:
            # H7129 Sleep/Turbo notifications retain an unexplained 03 in
            # byte 3 while H7124 reports 00, so only the mode byte is safe.
            fan_fields = ((2, frame[2]),)
        return ResponseSpec(
            ResponseKind.FIELDS,
            exact_alternatives=(frame,),
            allowed_prefixes=(b"\xee\x05",),
            expected_fields=fan_fields,
        )
    if isinstance(command, SetNightLightPower):
        return ResponseSpec(
            ResponseKind.FIELDS,
            allowed_prefixes=(b"\xaa\x1b", b"\x3a\x1b", b"\xee\x1b"),
            expected_fields=((2, 0x01), (3, int(command.on))),
        )
    if isinstance(command, SetNightLightBrightness):
        return ResponseSpec(
            ResponseKind.FIELDS,
            allowed_prefixes=(b"\xaa\x1b", b"\x3a\x1b", b"\xee\x1b"),
            expected_fields=((2, 0x01), (4, command.percent)),
        )
    if isinstance(command, SetNightLightColor):
        return ResponseSpec(
            ResponseKind.FIELDS,
            allowed_prefixes=(b"\x3a\x1b",),
            expected_fields=tuple((offset, frame[offset]) for offset in range(2, 7)),
        )
    if isinstance(command, RawCommand):
        return _exact(frame)
    raise TypeError(f"unsupported command type: {type(command).__name__}")
