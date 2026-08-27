"""Tests for purifier-profile to Bluetooth-settings adaptation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    BluetoothRuntimeSettings,
    CleanupSettings,
    ConnectionSettings,
    GattEndpoints,
    GattOperationSettings,
    RouteSelectionSettings,
)
from custom_components.govee_ble_air_purifier.bluetooth_profile import (
    bluetooth_settings_from_profile,
)
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile, Model


@pytest.mark.parametrize("model", [Model.H7124, Model.H7129])
def test_profile_mapping_copies_every_bluetooth_consumed_value(model: Model) -> None:
    """Both supported models map every consumed value without transformation."""
    profile = DeviceProfile.for_model(model)
    timings = profile.timings

    assert bluetooth_settings_from_profile(profile) == BluetoothRuntimeSettings(
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


def test_bluetooth_settings_are_immutable_and_slotted() -> None:
    """Runtime settings cannot be mutated or gain integration-specific fields."""
    settings = bluetooth_settings_from_profile(DeviceProfile.for_model(Model.H7129))

    with pytest.raises(FrozenInstanceError):
        settings.connection.attempts = 4  # type: ignore[misc]
    assert not hasattr(settings, "__dict__")
    assert not hasattr(settings.connection, "__dict__")
