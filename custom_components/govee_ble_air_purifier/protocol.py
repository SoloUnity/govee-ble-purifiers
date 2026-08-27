"""Pure Govee H7124/H7129 air-purifier application protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .frame import ApplicationFrame, build_frame, validate_frame
from .models import (
    AirQualityEvent,
    DecodedEvent,
    DeviceProfile,
    DeviceStateEvent,
    EchoEvent,
    FanMode,
    FanModeEvent,
    NegotiationEvent,
    NightLightColorEvent,
    NightLightStateEvent,
    ProtocolCommand,
    QueryAirQuality,
    QueryDeviceState,
    QueryNightLightColor,
    QueryNightLightState,
    RawCommand,
    RefreshRequestedEvent,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
    StartupFanModeEvent,
    UnknownEvent,
)
from .profiles import CommandDefinition, MatcherDefinition


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


class ResponseMatcher:
    """Mutable response progress for one request transaction.

    Unrelated notifications are ignored.  Multipart metadata responses accept
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
            # The captures establish containment, but not a formal byte-offset
            # layout for this response.
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


class GoveePurifierProtocol:
    """Encode commands and decode plaintext frames for one model profile."""

    def __init__(self, profile: DeviceProfile) -> None:
        self.profile = profile
        self._requests = {
            name: RequestDescriptor(
                definition.name,
                definition.frame,
                _response_spec(definition.response),
            )
            for name, definition in profile.protocol.request_catalog.items()
        }

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
        response, but it remains an acknowledgement rather than independent
        displayed-color confirmation.
        """

        frame = self.encode(command)
        descriptor_name = name or type(command).__name__
        if isinstance(command, QueryDeviceState):
            response = _prefix(b"\xaa\x01")
        elif isinstance(command, QueryNightLightState):
            response = _selector(b"\xaa\x1b", b"\x01")
        elif isinstance(command, QueryNightLightColor):
            response = _selector(b"\xaa\x1b", b"\x05")
        elif isinstance(command, QueryAirQuality):
            response = _prefix(b"\xaa\x19")
        elif isinstance(command, SetPower):
            response = ResponseSpec(
                ResponseKind.FIELDS,
                allowed_prefixes=(b"\xaa\x01",),
                expected_fields=((2, int(command.on)),),
            )
        elif isinstance(command, SetFanMode):
            mode = FanMode(command.mode)
            if mode in (FanMode.LOW, FanMode.MEDIUM, FanMode.HIGH):
                fan_fields = ((2, frame[2]), (3, frame[3]))
            elif mode is FanMode.AUTO:
                fan_fields = ((2, frame[2]), (5, frame[5]))
            else:
                # H7129 Sleep/Turbo notifications retain an unexplained 03 in
                # byte 3 while H7124 reports 00, so only the mode byte is safe.
                fan_fields = ((2, frame[2]),)
            response = ResponseSpec(
                ResponseKind.FIELDS,
                exact_alternatives=(frame,),
                allowed_prefixes=(b"\xee\x05",),
                expected_fields=fan_fields,
            )
        elif isinstance(command, SetNightLightPower):
            response = ResponseSpec(
                ResponseKind.FIELDS,
                allowed_prefixes=(b"\xaa\x1b", b"\x3a\x1b", b"\xee\x1b"),
                expected_fields=((2, 0x01), (3, int(command.on))),
            )
        elif isinstance(command, SetNightLightBrightness):
            response = ResponseSpec(
                ResponseKind.FIELDS,
                allowed_prefixes=(b"\xaa\x1b", b"\x3a\x1b", b"\xee\x1b"),
                expected_fields=((2, 0x01), (4, command.percent)),
            )
        elif isinstance(command, SetNightLightColor):
            response = ResponseSpec(
                ResponseKind.FIELDS,
                allowed_prefixes=(b"\x3a\x1b",),
                expected_fields=tuple(
                    (offset, frame[offset]) for offset in range(2, 7)
                ),
            )
        elif isinstance(command, RawCommand):
            response = _exact(frame)
        else:  # pragma: no cover - encode() already rejects unknown commands
            raise TypeError(f"unsupported command type: {type(command).__name__}")
        return RequestDescriptor(descriptor_name, frame, response)

    # A short alias reads naturally at call sites constructing a transaction.
    request = command_request

    def encode(self, command: ProtocolCommand) -> bytes:
        """Encode a typed command as a checksum-valid plaintext frame."""

        if isinstance(command, RawCommand):
            return validate_frame(command.frame)
        if isinstance(command, QueryDeviceState):
            return self._requests["device_state"].frame
        if isinstance(command, QueryNightLightState):
            return self._requests["night_light_state"].frame
        if isinstance(command, QueryNightLightColor):
            return self._requests["night_light_color"].frame
        if isinstance(command, QueryAirQuality):
            return self._requests["air_quality"].frame
        if isinstance(command, SetPower):
            definition = self._require_command_strategy("power", "power_bool_v1")
            return build_frame(definition.prefix + bytes((int(command.on),)))
        if isinstance(command, SetFanMode):
            definition = self._require_command_strategy("fan_mode", "fan_mode_v1")
            return self._encode_fan_mode(command.mode, definition)
        if isinstance(command, SetNightLightPower):
            definition = self._require_command_strategy(
                "night_light_power", "night_light_power_v1"
            )
            return build_frame(definition.prefix + bytes((int(command.on),)))
        if isinstance(command, SetNightLightBrightness):
            definition = self._require_command_strategy(
                "night_light_brightness", "night_light_brightness_v1"
            )
            if not 1 <= command.percent <= 100:
                raise ProtocolError("night-light brightness must be from 1 through 100")
            return build_frame(definition.prefix + bytes((command.percent,)))
        if isinstance(command, SetNightLightColor):
            definition = self._require_command_strategy(
                "night_light_color", "night_light_color_v1"
            )
            components = (command.red, command.green, command.blue)
            if any(not 0 <= component <= 255 for component in components):
                raise ProtocolError("RGB components must be from 0 through 255")
            return build_frame(definition.prefix + bytes(components))
        raise TypeError(f"unsupported command type: {type(command).__name__}")

    def _require_command_strategy(
        self, command: str, expected: str
    ) -> CommandDefinition:
        """Retain a hard assertion around profile-selected Python strategies."""
        definition = self.profile.protocol.commands.get(command)
        if definition is None or definition.strategy != expected:
            raise ProtocolError(
                f"profile selected unsupported {command} strategy"
            )
        return definition

    def _encode_fan_mode(
        self,
        mode: FanMode,
        definition: CommandDefinition,
    ) -> bytes:
        try:
            mode = FanMode(mode)
        except ValueError as err:
            raise ProtocolError(f"unsupported fan mode: {mode!r}") from err
        payload: dict[FanMode, tuple[int, int, int]] = {
            FanMode.LOW: (0x01, 0x01, 0x00),
            FanMode.MEDIUM: (0x01, 0x02, 0x00),
            FanMode.HIGH: (0x01, 0x03, 0x00),
            FanMode.SLEEP: (0x05, 0x00, 0x00),
            FanMode.AUTO: (0x03, 0x00, self.profile.auto_parameter),
            FanMode.TURBO: (0x07, 0x00, 0x00),
        }
        mode_code, manual_level, auto_parameter = payload[mode]
        return build_frame(
            definition.prefix
            + bytes((mode_code, manual_level, 0x00, auto_parameter))
        )

    def decode(
        self, frame: bytes | bytearray | memoryview | ApplicationFrame
    ) -> DecodedEvent:
        """Decode one checksum-valid plaintext application frame."""

        data = (
            frame.data if isinstance(frame, ApplicationFrame) else validate_frame(frame)
        )
        prefix, command = data[:2]

        if data[:2] == b"\xaa\x01":
            return DeviceStateEvent(
                data,
                power=self._decode_bool(data[2]),
                status_flags=data[4],
                volatile_state=data[6],
            )

        if data[:2] == b"\xaa\x05":
            selector = data[2]
            return StartupFanModeEvent(
                data,
                selector=selector,
                mode_code=data[3] if selector == 0x00 else None,
                manual_level=data[4] if selector == 0x00 else None,
                level_or_configuration=data[3] if selector == 0x01 else None,
                auto_parameter=data[5] if selector == 0x03 else None,
            )

        if data[:2] == b"\xee\x05":
            return FanModeEvent(
                data,
                mode=self._decode_fan_mode(data),
                mode_code=data[2],
                manual_level=data[3],
                auto_parameter=data[5],
            )

        if command == 0x1B and data[2] == 0x01 and prefix in (0xAA, 0x3A, 0xEE):
            return NightLightStateEvent(
                data,
                power=self._decode_bool(data[3]),
                brightness=data[4],
                prefix=prefix,
                unsolicited=prefix == 0xEE,
            )

        if command == 0x1B and data[2] == 0x05 and prefix in (0xAA, 0x3A):
            available = data[3] == 0x0D
            return NightLightColorEvent(
                data,
                red=data[4] if available else None,
                green=data[5] if available else None,
                blue=data[6] if available else None,
                color_available=available,
                acknowledgement_only=prefix == 0x3A,
                prefix=prefix,
            )

        if command == 0x19 and prefix in (0xAA, 0xEE):
            raw_pm25 = (data[3] << 8) | data[4]
            return AirQualityEvent(
                data,
                status_flags=data[2],
                pm25_ug_m3=raw_pm25 if raw_pm25 <= 999 else None,
                raw_pm25=raw_pm25,
                mode_related=data[5],
                unknown=data[6],
                filter_life=data[7],
                unsolicited=prefix == 0xEE,
            )

        if data[:2] == b"\xee\xaa":
            return RefreshRequestedEvent(data)

        if prefix == 0xE7:
            return NegotiationEvent(data, step=command)

        if prefix in (0x33, 0x3A):
            return EchoEvent(data, prefix=prefix, command=command)

        return UnknownEvent(data, prefix=prefix, command=command)

    def _decode_fan_mode(self, data: bytes) -> FanMode | None:
        mode_code = data[2]
        if mode_code == 0x01:
            return {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(data[3])
        if mode_code == 0x03:
            # Only the model's default Auto parameter is documented.  Do not
            # collapse unknown Quiet/High-Efficiency variants into it.
            return FanMode.AUTO if data[5] == self.profile.auto_parameter else None
        return {0x05: FanMode.SLEEP, 0x07: FanMode.TURBO}.get(mode_code)

    @staticmethod
    def _decode_bool(value: int) -> bool | None:
        if value in (0, 1):
            return bool(value)
        return None


def initialization_requests(profile: DeviceProfile) -> tuple[RequestDescriptor, ...]:
    """Functional convenience wrapper around :class:`GoveePurifierProtocol`."""

    return GoveePurifierProtocol(profile).initialization_requests()


def refresh_requests(profile: DeviceProfile) -> tuple[RequestDescriptor, ...]:
    """Functional convenience wrapper for the short refresh sweep."""

    return GoveePurifierProtocol(profile).refresh_requests()
