"""Typed purifier command encoding and application-event decoding."""

from __future__ import annotations

from collections.abc import Mapping

from ..frame import ApplicationFrame, build_frame, validate_frame
from ..models import (
    AirQualityEvent,
    DecodedEvent,
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
from ..profiles import CommandDefinition, DeviceProfile
from .types import ProtocolError, RequestDescriptor

__all__ = ("ProtocolCodec",)


class ProtocolCodec:
    """Encode commands and decode plaintext frames for one model profile."""

    def __init__(
        self,
        requests: Mapping[str, RequestDescriptor],
    ) -> None:
        self._requests = requests

    def encode(self, command: ProtocolCommand, profile: DeviceProfile) -> bytes:
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
            definition = self._require_command_strategy(
                profile, "power", "power_bool_v1"
            )
            return build_frame(definition.prefix + bytes((int(command.on),)))
        if isinstance(command, SetFanMode):
            definition = self._require_command_strategy(
                profile, "fan_mode", "fan_mode_v1"
            )
            return self._encode_fan_mode(command.mode, definition, profile)
        if isinstance(command, SetNightLightPower):
            definition = self._require_command_strategy(
                profile, "night_light_power", "night_light_power_v1"
            )
            return build_frame(definition.prefix + bytes((int(command.on),)))
        if isinstance(command, SetNightLightBrightness):
            definition = self._require_command_strategy(
                profile,
                "night_light_brightness",
                "night_light_brightness_v1",
            )
            if not 1 <= command.percent <= 100:
                raise ProtocolError("night-light brightness must be from 1 through 100")
            return build_frame(definition.prefix + bytes((command.percent,)))
        if isinstance(command, SetNightLightColor):
            definition = self._require_command_strategy(
                profile, "night_light_color", "night_light_color_v1"
            )
            components = (command.red, command.green, command.blue)
            if any(not 0 <= component <= 255 for component in components):
                raise ProtocolError("RGB components must be from 0 through 255")
            return build_frame(definition.prefix + bytes(components))
        raise TypeError(f"unsupported command type: {type(command).__name__}")

    @staticmethod
    def _require_command_strategy(
        profile: DeviceProfile, command: str, expected: str
    ) -> CommandDefinition:
        """Retain a hard assertion around profile-selected Python strategies."""

        definition = profile.protocol.commands.get(command)
        if definition is None or definition.strategy != expected:
            raise ProtocolError(f"profile selected unsupported {command} strategy")
        return definition

    def _encode_fan_mode(
        self,
        mode: FanMode,
        definition: CommandDefinition,
        profile: DeviceProfile,
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
            FanMode.AUTO: (0x03, 0x00, profile.auto_parameter),
            FanMode.TURBO: (0x07, 0x00, 0x00),
        }
        mode_code, manual_level, auto_parameter = payload[mode]
        return build_frame(
            definition.prefix + bytes((mode_code, manual_level, 0x00, auto_parameter))
        )

    def decode(
        self,
        frame: bytes | bytearray | memoryview | ApplicationFrame,
        profile: DeviceProfile,
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
                mode=self._decode_fan_mode(data, profile),
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

    @staticmethod
    def _decode_fan_mode(data: bytes, profile: DeviceProfile) -> FanMode | None:
        mode_code = data[2]
        if mode_code == 0x01:
            return {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(data[3])
        if mode_code == 0x03:
            # Only the model's default Auto parameter is documented. Do not
            # collapse unknown Quiet/High-Efficiency variants into it.
            return (
                FanMode.AUTO
                if data[5] == profile.auto_parameter
                else None
            )
        return {0x05: FanMode.SLEEP, 0x07: FanMode.TURBO}.get(mode_code)

    @staticmethod
    def _decode_bool(value: int) -> bool | None:
        if value in (0, 1):
            return bool(value)
        return None
