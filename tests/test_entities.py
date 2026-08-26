"""Tests for cached Home Assistant entity mappings."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_ADDRESS
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
    coordinator.client_available = True
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


@pytest.mark.parametrize(
    ("mode", "percentage", "preset"),
    [
        (FanMode.SLEEP, 20, "manual"),
        (FanMode.LOW, 40, "manual"),
        (FanMode.MEDIUM, 60, "manual"),
        (FanMode.HIGH, 80, "manual"),
        (FanMode.TURBO, 100, "manual"),
        (FanMode.AUTO, None, "auto"),
        (None, None, None),
    ],
)
def test_fan_maps_physical_modes_to_entity_state(
    mode: FanMode | None,
    percentage: int | None,
    preset: str | None,
) -> None:
    """Every physical mode and unknown state has a stable UI mapping."""
    entry, _ = _entry_and_coordinator(PurifierState(power=True, fan_mode=mode))
    fan = GoveePurifierFan(entry)

    assert fan.is_on is True
    assert fan.percentage == percentage
    assert fan.preset_mode == preset


def test_fan_advertises_five_speeds_and_manual_auto_presets() -> None:
    """The entity advertises the complete, stably ordered UI contract."""
    entry, _ = _entry_and_coordinator(PurifierState())
    fan = GoveePurifierFan(entry)

    assert fan.speed_count == 5
    assert fan.preset_modes == ["manual", "auto"]


@pytest.mark.parametrize(
    ("percentage", "expected"),
    [
        (20, FanMode.SLEEP),
        (40, FanMode.LOW),
        (60, FanMode.MEDIUM),
        (80, FanMode.HIGH),
        (100, FanMode.TURBO),
        (1, FanMode.SLEEP),
        (21, FanMode.LOW),
        (41, FanMode.MEDIUM),
        (61, FanMode.HIGH),
        (81, FanMode.TURBO),
    ],
)
async def test_fan_maps_percentages_to_physical_modes(
    percentage: int, expected: FanMode
) -> None:
    """Canonical and non-canonical percentages use ordered-list conversion."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=True))
    fan = GoveePurifierFan(entry)

    await fan.async_set_percentage(percentage)

    coordinator.async_set_power.assert_not_awaited()
    coordinator.async_set_fan_mode.assert_awaited_once_with(expected)


async def test_fan_zero_percentage_powers_off() -> None:
    """Zero percent is the Home Assistant power-off request."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=True))
    fan = GoveePurifierFan(entry)

    await fan.async_set_percentage(0)

    coordinator.async_set_power.assert_awaited_once_with(False)
    coordinator.async_set_fan_mode.assert_not_awaited()


async def test_fan_powers_on_before_setting_a_level() -> None:
    """An off purifier is powered before its physical level is selected."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=False))
    fan = GoveePurifierFan(entry)
    calls: list[tuple[str, bool | FanMode]] = []
    coordinator.async_set_power.side_effect = lambda on: calls.append(("power", on))
    coordinator.async_set_fan_mode.side_effect = lambda mode: calls.append(
        ("mode", mode)
    )

    await fan.async_set_percentage(100)

    assert calls == [("power", True), ("mode", FanMode.TURBO)]


@pytest.mark.parametrize(
    "mode",
    [FanMode.SLEEP, FanMode.LOW, FanMode.MEDIUM, FanMode.HIGH, FanMode.TURBO],
)
async def test_manual_preset_preserves_current_level(mode: FanMode) -> None:
    """Manual reapplies whichever physical manual level is already active."""
    entry, coordinator = _entry_and_coordinator(
        PurifierState(power=True, fan_mode=mode)
    )
    fan = GoveePurifierFan(entry)

    await fan.async_set_preset_mode("manual")

    coordinator.async_set_fan_mode.assert_awaited_once_with(mode)


@pytest.mark.parametrize("mode", [FanMode.AUTO, None])
async def test_manual_preset_falls_back_to_low(mode: FanMode | None) -> None:
    """Manual from Auto or unknown selects the approved Low fallback."""
    entry, coordinator = _entry_and_coordinator(
        PurifierState(power=True, fan_mode=mode)
    )
    fan = GoveePurifierFan(entry)

    await fan.async_set_preset_mode("manual")

    coordinator.async_set_fan_mode.assert_awaited_once_with(FanMode.LOW)


async def test_auto_preset_selects_hardware_auto() -> None:
    """Auto remains the purifier's physical Auto mode."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=True))
    fan = GoveePurifierFan(entry)

    await fan.async_set_preset_mode("auto")

    coordinator.async_set_fan_mode.assert_awaited_once_with(FanMode.AUTO)


@pytest.mark.parametrize("method", ["set_preset", "turn_on"])
async def test_unsupported_preset_is_rejected_before_coordinator_call(
    method: str,
) -> None:
    """Invalid presets cannot power or otherwise control the purifier."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=False))
    fan = GoveePurifierFan(entry)

    with pytest.raises(ValueError, match="Unsupported preset mode: sleep"):
        if method == "set_preset":
            await fan.async_set_preset_mode("sleep")
        else:
            await fan.async_turn_on(preset_mode="sleep")

    coordinator.async_set_power.assert_not_awaited()
    coordinator.async_set_fan_mode.assert_not_awaited()


async def test_turn_on_percentage_takes_precedence_over_preset() -> None:
    """A percentage wins when Home Assistant supplies both mode arguments."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=False))
    fan = GoveePurifierFan(entry)
    calls: list[tuple[str, bool | FanMode]] = []
    coordinator.async_set_power.side_effect = lambda on: calls.append(("power", on))
    coordinator.async_set_fan_mode.side_effect = lambda mode: calls.append(
        ("mode", mode)
    )

    await fan.async_turn_on(percentage=20, preset_mode="auto")

    assert calls == [("power", True), ("mode", FanMode.SLEEP)]


async def test_turn_on_with_preset_powers_on_before_mode() -> None:
    """Preset-only turn-on resolves and applies the preset after power."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=False))
    fan = GoveePurifierFan(entry)
    calls: list[tuple[str, bool | FanMode]] = []
    coordinator.async_set_power.side_effect = lambda on: calls.append(("power", on))
    coordinator.async_set_fan_mode.side_effect = lambda mode: calls.append(
        ("mode", mode)
    )

    await fan.async_turn_on(preset_mode="auto")

    assert calls == [("power", True), ("mode", FanMode.AUTO)]


async def test_turn_on_without_mode_only_powers_on() -> None:
    """A plain turn-on does not alter the cached fan mode."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=False))
    fan = GoveePurifierFan(entry)

    await fan.async_turn_on()

    coordinator.async_set_power.assert_awaited_once_with(True)
    coordinator.async_set_fan_mode.assert_not_awaited()


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


def test_entities_become_unavailable_during_quiet_bluetooth_recovery() -> None:
    """Expected link recovery does not require a coordinator update error."""
    entry, coordinator = _entry_and_coordinator(PurifierState(power=True))
    fan = GoveePurifierFan(entry)

    assert fan.available

    coordinator.client_available = False
    assert not fan.available
