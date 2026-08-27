"""Pure data models for the Govee BLE air-purifier protocol.

This module intentionally has no Home Assistant or Bluetooth dependencies.  It
is shared by the protocol codec, the reliable client, and unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Model(StrEnum):
    """Supported purifier model values retained in config entries."""

    H7124 = "H7124"
    H7129 = "H7129"


class SecurityMode(StrEnum):
    """Closed application-channel strategy identifiers."""

    PLAINTEXT = "plaintext"
    H7129_SESSION = "h7129_session"


class FanMode(StrEnum):
    """Fan modes whose wire representation is documented."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SLEEP = "sleep"
    AUTO = "auto"
    TURBO = "turbo"


@dataclass(frozen=True, slots=True)
class PurifierState:
    """Last reported purifier state, updated from decoded protocol events.

    ``None`` means that a value has not yet been authoritatively observed.  In
    particular, fan mode must not be inferred from ``aa 01`` byte 6 and H7129's
    ``fc`` RGB response must not replace the previous color.
    """

    power: bool | None = None
    fan_mode: FanMode | None = None
    light_power: bool | None = None
    light_brightness: int | None = None
    light_rgb: tuple[int, int, int] | None = None
    pm25: int | None = None
    filter_life: int | None = None


# Commands -----------------------------------------------------------------


class Command:
    """Marker base class for typed application commands."""


@dataclass(frozen=True, slots=True)
class QueryDeviceState(Command):
    """Query power and the partially understood device-state fields."""


@dataclass(frozen=True, slots=True)
class QueryNightLightState(Command):
    """Query night-light power and brightness."""


@dataclass(frozen=True, slots=True)
class QueryNightLightColor(Command):
    """Query the stored night-light RGB color."""


@dataclass(frozen=True, slots=True)
class QueryAirQuality(Command):
    """Query PM2.5 and filter status."""


@dataclass(frozen=True, slots=True)
class SetPower(Command):
    """Set purifier power."""

    on: bool


@dataclass(frozen=True, slots=True)
class SetFanMode(Command):
    """Select a documented fan mode."""

    mode: FanMode


@dataclass(frozen=True, slots=True)
class SetNightLightPower(Command):
    """Set night-light power without changing retained brightness."""

    on: bool


@dataclass(frozen=True, slots=True)
class SetNightLightBrightness(Command):
    """Set night-light brightness to a whole-number percentage."""

    percent: int


@dataclass(frozen=True, slots=True)
class SetNightLightColor(Command):
    """Set the night-light RGB value."""

    red: int
    green: int
    blue: int


@dataclass(frozen=True, slots=True)
class RawCommand(Command):
    """A pre-built plaintext frame used by documented initialization queries."""

    frame: bytes


type ProtocolCommand = (
    QueryDeviceState
    | QueryNightLightState
    | QueryNightLightColor
    | QueryAirQuality
    | SetPower
    | SetFanMode
    | SetNightLightPower
    | SetNightLightBrightness
    | SetNightLightColor
    | RawCommand
)


# Events -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    """Base for decoded, checksum-valid application events."""

    frame: bytes


@dataclass(frozen=True, slots=True)
class DeviceStateEvent(ProtocolEvent):
    """An ``aa 01`` state response or notification."""

    power: bool | None
    status_flags: int
    volatile_state: int


@dataclass(frozen=True, slots=True)
class FanModeEvent(ProtocolEvent):
    """An authoritative unsolicited ``ee 05`` fan-mode update."""

    mode: FanMode | None
    mode_code: int
    manual_level: int
    auto_parameter: int


@dataclass(frozen=True, slots=True)
class StartupFanModeEvent(ProtocolEvent):
    """An ``aa 05`` startup or refresh response with its observed layout."""

    selector: int
    mode_code: int | None
    manual_level: int | None
    level_or_configuration: int | None
    auto_parameter: int | None


@dataclass(frozen=True, slots=True)
class NightLightStateEvent(ProtocolEvent):
    """Night-light power/brightness response, echo, or notification."""

    power: bool | None
    brightness: int
    prefix: int
    unsolicited: bool


@dataclass(frozen=True, slots=True)
class NightLightColorEvent(ProtocolEvent):
    """Night-light RGB state or acknowledgement echo.

    ``color_available`` is false for H7129's ``fc`` query response.  A
    ``3a`` event is an acknowledgement echo and must not be treated as
    independent confirmation that the displayed color changed.
    """

    red: int | None
    green: int | None
    blue: int | None
    color_available: bool
    acknowledgement_only: bool
    prefix: int


@dataclass(frozen=True, slots=True)
class AirQualityEvent(ProtocolEvent):
    """An ``aa/ee 19`` status response or notification."""

    status_flags: int
    pm25_ug_m3: int | None
    raw_pm25: int
    mode_related: int
    unknown: int
    filter_life: int
    unsolicited: bool


@dataclass(frozen=True, slots=True)
class RefreshRequestedEvent(ProtocolEvent):
    """The purifier requested the documented short refresh sweep."""


@dataclass(frozen=True, slots=True)
class NegotiationEvent(ProtocolEvent):
    """A session-negotiation frame that reached the application decoder."""

    step: int


@dataclass(frozen=True, slots=True)
class EchoEvent(ProtocolEvent):
    """A control echo without a more specific decoded representation."""

    prefix: int
    command: int


@dataclass(frozen=True, slots=True)
class UnknownEvent(ProtocolEvent):
    """A valid but currently uninterpreted frame."""

    prefix: int
    command: int


type DecodedEvent = (
    DeviceStateEvent
    | FanModeEvent
    | StartupFanModeEvent
    | NightLightStateEvent
    | NightLightColorEvent
    | AirQualityEvent
    | RefreshRequestedEvent
    | NegotiationEvent
    | EchoEvent
    | UnknownEvent
)
