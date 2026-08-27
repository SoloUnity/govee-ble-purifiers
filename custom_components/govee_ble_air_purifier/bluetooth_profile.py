"""Map validated purifier profiles into integration-neutral Bluetooth settings."""

from __future__ import annotations

from .bluetooth import (
    BluetoothRuntimeSettings,
    CleanupSettings,
    ConnectionSettings,
    GattEndpoints,
    GattOperationSettings,
    RouteSelectionSettings,
)
from .profiles import DeviceProfile


def bluetooth_settings_from_profile(
    profile: DeviceProfile,
) -> BluetoothRuntimeSettings:
    """Return the exact Bluetooth values owned by a validated device profile."""
    timings = profile.timings
    return BluetoothRuntimeSettings(
        endpoints=GattEndpoints(
            service_uuid=profile.bluetooth.service_uuid,
            notify_uuid=profile.bluetooth.notify_uuid,
            write_uuid=profile.bluetooth.write_uuid,
        ),
        route_selection=RouteSelectionSettings(
            advertisement_check_interval=timings.advertisement_check_interval,
            recent_cached_advertisement_max_age=(
                timings.recent_cached_advertisement_max_age
            ),
        ),
        connection=ConnectionSettings(
            attempts=timings.connect_attempts,
            attempt_timeout=timings.connection_attempt_timeout,
            abort_timeout=timings.connection_abort_timeout,
            diagnostic_timeout=timings.connection_diagnostic_timeout,
        ),
        gatt_operations=GattOperationSettings(
            notification_subscribe_timeout=timings.notification_subscribe_timeout,
            write_timeout=timings.gatt_write_timeout,
            disconnect_timeout=timings.gatt_disconnect_timeout,
            operation_cancel_timeout=timings.gatt_operation_cancel_timeout,
        ),
        cleanup=CleanupSettings(
            stale_connection_timeout=timings.stale_connection_cleanup_timeout,
            stale_connection_check_interval=timings.stale_connection_check_interval,
        ),
    )
