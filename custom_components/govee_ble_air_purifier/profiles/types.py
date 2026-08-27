"""Immutable runtime values produced from bundled model profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..models import FanMode, Model, SecurityMode
from .errors import ProfileSelectionError


class SupportStatus(StrEnum):
    """Closed support-state values exposed in diagnostics."""

    BASELINE = "baseline"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    manufacturer: str
    model: Model | None
    display_name: str
    support_status: SupportStatus
    advertised_name_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BluetoothProfile:
    service_uuid: str
    notify_uuid: str
    write_uuid: str


@dataclass(frozen=True, slots=True)
class NegotiationPolicy:
    attempts: int
    retry_interval: float
    phase_timeout: float
    step_delay: float


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    strategy: SecurityMode
    first_application_delay: float
    negotiation: NegotiationPolicy | None


@dataclass(frozen=True, slots=True)
class MatcherDefinition:
    kind: str
    prefix: bytes = b""
    selector: bytes = b""
    exact: bytes = b""
    exact_alternatives: tuple[bytes, ...] = ()
    fragments: tuple[int, ...] = ()
    allowed_prefixes: tuple[bytes, ...] = ()
    expected_fields: tuple[tuple[int, int], ...] = ()
    allowed_values: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestDefinition:
    name: str
    frame: bytes
    response: MatcherDefinition


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    strategy: str
    prefix: bytes


@dataclass(frozen=True, slots=True)
class ProtocolProfile:
    codec: str
    checksum: str
    frame_size: int
    auto_parameter: int
    startup_mode_strategy: str
    commands: Mapping[str, CommandDefinition]
    request_catalog: Mapping[str, RequestDefinition]
    initialization_order: tuple[str, ...]
    refresh_order: tuple[str, ...]
    essential_request: str
    periodic_request: str


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    power: bool
    fan: bool
    light: bool
    pm25: bool
    filter_life: bool
    unsolicited_updates: bool
    refresh: bool

    def as_dict(self) -> dict[str, bool]:
        """Return safe primitive capability diagnostics."""
        return {
            "power": self.power,
            "fan": self.fan,
            "light": self.light,
            "pm25": self.pm25,
            "filter_life": self.filter_life,
            "unsolicited_updates": self.unsolicited_updates,
            "refresh": self.refresh,
        }


@dataclass(frozen=True, slots=True)
class TimingProfile:
    connect_attempts: int
    connection_attempt_timeout: float
    connection_abort_timeout: float
    connection_diagnostic_timeout: float
    notification_subscribe_timeout: float
    gatt_write_timeout: float
    gatt_disconnect_timeout: float
    gatt_operation_cancel_timeout: float
    stale_connection_cleanup_timeout: float
    stale_connection_check_interval: float
    advertisement_check_interval: float
    recent_cached_advertisement_max_age: float
    fresh_advertisement_timeout: float
    transaction_timeout: float
    initialization_attempts: int
    essential_initialization_max_batches: int
    initialization_retry_delay: float
    periodic_poll_attempts: int
    refresh_attempts: int
    command_send_attempts: int
    command_deadline: float
    startup_timeout: float
    between_request_delay: float
    poll_interval: float
    initial_poll_delay: float
    backoff_initial: float
    backoff_max: float
    backoff_reset_after: float
    recent_advertisement_backoff_max: float
    advertisement_settle_delay: float
    recovery_storm_window: float
    recovery_storm_failure_threshold: int
    recovery_storm_advertisement_threshold: int
    recovery_storm_initial_floor: float
    recovery_storm_max_floor: float

    def as_dict(self) -> dict[str, int | float]:
        """Return safe effective timing diagnostics."""
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class CustomAutoDefaults:
    modes: tuple[FanMode, ...]
    pm25_boundaries: tuple[int, ...]
    upshift_confirmation_seconds: int
    downshift_delays_minutes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """One complete, immutable, validated effective model profile."""

    schema_version: int
    profile_id: str
    lineage: tuple[str, ...]
    source_basename: str
    fingerprint: str
    identity: IdentityProfile
    bluetooth: BluetoothProfile
    channel: ChannelProfile
    protocol: ProtocolProfile
    capabilities: CapabilityProfile
    timings: TimingProfile
    custom_auto_defaults: CustomAutoDefaults

    @property
    def model(self) -> Model:
        """Return the exact supported model; roots cannot enter runtime."""
        if self.identity.model is None:
            raise ProfileSelectionError(
                f"baseline profile {self.profile_id!r} has no supported model"
            )
        return self.identity.model

    @property
    def security(self) -> SecurityMode:
        """Compatibility property for channel selection."""
        return self.channel.strategy

    @property
    def auto_parameter(self) -> int:
        """Compatibility property for protocol command assembly."""
        return self.protocol.auto_parameter

    def diagnostic_snapshot(
        self, *, requested_model: str | None = None
    ) -> dict[str, Any]:
        """Return safe profile metadata without device identity or raw JSON."""
        return {
            "requested_model": requested_model,
            "profile_id": self.profile_id,
            "lineage": self.lineage,
            "schema_version": self.schema_version,
            "support_status": self.identity.support_status.value,
            "security_strategy": self.channel.strategy.value,
            "source_basename": self.source_basename,
            "fingerprint": self.fingerprint,
            "capabilities": self.capabilities.as_dict(),
            "request_names": tuple(self.protocol.request_catalog),
            "request_count": len(self.protocol.request_catalog),
            "initialization_request_count": len(self.protocol.initialization_order),
            "refresh_request_count": len(self.protocol.refresh_order),
            "timings": self.timings.as_dict(),
            "service_uuid": self.bluetooth.service_uuid,
            "notify_uuid": self.bluetooth.notify_uuid,
            "write_uuid": self.bluetooth.write_uuid,
        }

    @classmethod
    def for_model(cls, model: Model | str) -> DeviceProfile:
        """Compatibility loader backed by the immutable bundled registry."""
        from .registry import get_profile_registry

        return get_profile_registry().for_model(model)
