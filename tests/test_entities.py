"""Tests for cached Home Assistant entity mappings."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_ADDRESS
from homeassistant.util.percentage import ordered_list_item_to_percentage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_air_purifier.const import CONF_MODEL, DOMAIN
from custom_components.govee_ble_air_purifier.fan import GoveePurifierFan
from custom_components.govee_ble_air_purifier.light import GoveePurifierLight
from custom_components.govee_ble_air_purifier.models import FanMode, PurifierState
from custom_components.govee_ble_air_purifier.sensor import (
    SENSORS,
    GoveePurifierSensor,
)


def _entry_and_coordinator(
    state: PurifierState,
) -> tuple[MockConfigEntry, MagicMock]:
    coordinator = MagicMock()
    coordinator.data = state
    coordinator.last_update_success = True
    coordinator.async_set_power = AsyncMock()
    coordinator.async_set_fan_mode = AsyncMock()
    coordinator.async_set_light_power = AsyncMock()
    coordinator.async_set_light_brightness = AsyncMock()
    coordinator.async_set_light_rgb = AsyncMock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bedroom purifier",
        unique_id="aa:bb:cc:dd:ee:ff",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7129"},
    )
    entry.runtime_data = coordinator
    return entry, coordinator


async def test_fan_maps_manual_speeds_and_presets() -> None:
    """Manual levels use percentages while special modes use presets."""
    entry, coordinator = _entry_and_coordinator(
        PurifierState(power=True, fan_mode=FanMode.MEDIUM)
    )
    fan = GoveePurifierFan(entry)

    assert fan.is_on is True
    assert fan.percentage == ordered_list_item_to_percentage(
        (FanMode.LOW, FanMode.MEDIUM, FanMode.HIGH), FanMode.MEDIUM
    )
    assert fan.preset_mode is None

    coordinator.data = replace(coordinator.data, fan_mode=FanMode.AUTO)
    assert fan.percentage is None
    assert fan.preset_mode == "auto"

    await fan.async_set_percentage(100)
    coordinator.async_set_fan_mode.assert_awaited_once_with(FanMode.HIGH)

    await fan.async_set_preset_mode("sleep")
    coordinator.async_set_fan_mode.assert_awaited_with(FanMode.SLEEP)


async def test_light_maps_percent_brightness_and_rgb() -> None:
    """The light converts only at the Home Assistant boundary."""
    entry, coordinator = _entry_and_coordinator(
        PurifierState(
            light_power=True,
            light_brightness=50,
            light_rgb=(12, 34, 56),
        )
    )
    light = GoveePurifierLight(entry)

    assert light.is_on is True
    assert light.brightness == 128
    assert light.rgb_color == (12, 34, 56)

    await light.async_turn_on(brightness=255, rgb_color=(1, 2, 3))
    coordinator.async_set_light_brightness.assert_awaited_once_with(100)
    coordinator.async_set_light_rgb.assert_awaited_once_with((1, 2, 3))
    coordinator.async_set_light_power.assert_awaited_once_with(True)


def test_sensors_read_cached_state_only() -> None:
    """Sensor properties expose coordinator state without Bluetooth I/O."""
    entry, _ = _entry_and_coordinator(PurifierState(pm25=7, filter_life=73))
    sensors = {
        description.key: GoveePurifierSensor(entry, description)
        for description in SENSORS
    }

    assert sensors["pm25"].native_value == 7
    assert sensors["filter_life"].native_value == 73
