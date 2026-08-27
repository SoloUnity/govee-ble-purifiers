"""Immutable, integration-neutral Bluetooth runtime settings."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GattEndpoints:
    """GATT service and characteristic identifiers."""

    service_uuid: str
    notify_uuid: str
    write_uuid: str


@dataclass(frozen=True, slots=True)
class RouteSelectionSettings:
    """Advertisement freshness and route-selection timing."""

    advertisement_check_interval: float
    recent_cached_advertisement_max_age: float


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    """Connector retry and bounded connection-operation timing."""

    attempts: int
    attempt_timeout: float
    abort_timeout: float
    diagnostic_timeout: float


@dataclass(frozen=True, slots=True)
class GattOperationSettings:
    """Deadlines for GATT operations and cancellation observation."""

    notification_subscribe_timeout: float
    write_timeout: float
    disconnect_timeout: float
    operation_cancel_timeout: float


@dataclass(frozen=True, slots=True)
class CleanupSettings:
    """Address-level stale-connection cleanup timing."""

    stale_connection_timeout: float
    stale_connection_check_interval: float


@dataclass(frozen=True, slots=True)
class BluetoothRuntimeSettings:
    """Complete settings shared by one route environment and GATT transport."""

    endpoints: GattEndpoints
    route_selection: RouteSelectionSettings
    connection: ConnectionSettings
    gatt_operations: GattOperationSettings
    cleanup: CleanupSettings
