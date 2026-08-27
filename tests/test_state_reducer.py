"""Direct tests for deterministic purifier-state reduction."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.models import (
    AirQualityEvent,
    DeviceStateEvent,
    FanMode,
    FanModeEvent,
    Model,
    NightLightColorEvent,
    NightLightStateEvent,
    ProtocolCommand,
    PurifierState,
    RefreshRequestedEvent,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
    StartupFanModeEvent,
)
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile
from custom_components.govee_ble_air_purifier.state_reducer import (
    PurifierStateReducer,
)


def make_reducer(model: Model = Model.H7124) -> PurifierStateReducer:
    return PurifierStateReducer(DeviceProfile.for_model(model))


def startup_event(
    selector: int,
    *,
    mode_code: int | None = None,
    manual_level: int | None = None,
    selector_01_value: int | None = None,
    auto_parameter: int | None = None,
) -> StartupFanModeEvent:
    return StartupFanModeEvent(
        b"startup",
        selector=selector,
        mode_code=mode_code,
        manual_level=manual_level,
        level_or_configuration=selector_01_value,
        auto_parameter=auto_parameter,
    )


@pytest.mark.parametrize(
    ("mode_code", "manual_level", "expected"),
    [
        (0x01, 0x01, FanMode.LOW),
        (0x01, 0x02, FanMode.MEDIUM),
        (0x01, 0x03, FanMode.HIGH),
        (0x03, 0x00, FanMode.AUTO),
        (0x05, 0x00, FanMode.SLEEP),
        (0x07, 0x00, FanMode.TURBO),
    ],
)
def test_h7124_matched_selector_00_resolves_all_modes(
    mode_code: int,
    manual_level: int,
    expected: FanMode,
) -> None:
    reducer = make_reducer()

    result = reducer.reduce_event(
        startup_event(
            0x00,
            mode_code=mode_code,
            manual_level=manual_level,
        ),
        generation=2,
        matched_request="mode_data_00",
    )

    assert result.state_changed
    assert result.state.fan_mode is expected
    assert reducer.startup_fan_diagnostics() == {
        "last_mode_code": mode_code,
        "last_manual_level": manual_level,
        "last_selector_01_value": None,
        "last_auto_parameter": None,
        "awaiting_h7129_manual_level": False,
        "resolved_mode": expected.value,
        "resolution": f"resolved:{expected.value}",
        "generation": 2,
    }


@pytest.mark.parametrize(
    ("mode_code", "manual_level"),
    [(0x01, 0x09), (0x09, 0x00), (0x03, 0x01)],
)
def test_h7124_unknown_combinations_clear_cached_fan_mode(
    mode_code: int,
    manual_level: int,
) -> None:
    reducer = make_reducer()
    reducer.replace_state(PurifierState(fan_mode=FanMode.HIGH))

    reducer.reduce_event(
        startup_event(
            0x00,
            mode_code=mode_code,
            manual_level=manual_level,
        ),
        generation=1,
        matched_request="mode_data_00",
    )

    assert reducer.state.fan_mode is None
    assert reducer.startup_fan_diagnostics()["resolution"] == (
        f"unknown_h7124_combination:{mode_code}:{manual_level}"
    )


@pytest.mark.parametrize(
    ("level", "expected"),
    [(0x01, FanMode.LOW), (0x02, FanMode.MEDIUM), (0x03, FanMode.HIGH)],
)
def test_h7129_manual_pair_is_generation_scoped(
    level: int,
    expected: FanMode,
) -> None:
    reducer = make_reducer(Model.H7129)
    reducer.replace_state(PurifierState(fan_mode=FanMode.TURBO))

    first = reducer.reduce_event(
        startup_event(0x00, mode_code=0x01),
        generation=4,
        matched_request="mode_data_00",
    )
    assert first.state.fan_mode is None
    assert reducer.startup_fan_diagnostics()[
        "awaiting_h7129_manual_level"
    ] is True

    reducer.invalidate_connection()
    orphan = reducer.reduce_event(
        startup_event(0x01, selector_01_value=level),
        generation=5,
        matched_request="mode_data_01",
    )
    assert orphan.state.fan_mode is None
    assert reducer.startup_fan_diagnostics()[
        "awaiting_h7129_manual_level"
    ] is False

    reducer.reduce_event(
        startup_event(0x00, mode_code=0x01),
        generation=5,
        matched_request="mode_data_00",
    )
    completed = reducer.reduce_event(
        startup_event(0x01, selector_01_value=level),
        generation=5,
        matched_request="mode_data_01",
    )
    assert completed.state.fan_mode is expected
    assert reducer.startup_fan_diagnostics()["resolution"] == (
        f"resolved:{expected.value}"
    )


@pytest.mark.parametrize(
    ("mode_code", "expected"),
    [(0x03, FanMode.AUTO), (0x05, FanMode.SLEEP), (0x07, FanMode.TURBO)],
)
def test_h7129_special_modes_resolve_without_selector_01(
    mode_code: int,
    expected: FanMode,
) -> None:
    reducer = make_reducer(Model.H7129)

    reducer.reduce_event(
        startup_event(0x00, mode_code=mode_code),
        generation=3,
        matched_request="mode_data_00",
    )
    reducer.reduce_event(
        startup_event(0x01, selector_01_value=0x09),
        generation=3,
        matched_request="mode_data_01",
    )

    assert reducer.state.fan_mode is expected


def test_unmatched_startup_fragments_are_inert() -> None:
    reducer = make_reducer(Model.H7129)
    reducer.replace_state(PurifierState(fan_mode=FanMode.AUTO))

    unmatched = reducer.reduce_event(
        startup_event(0x00, mode_code=0x01),
        generation=1,
    )
    orphan = reducer.reduce_event(
        startup_event(0x01, selector_01_value=0x03),
        generation=1,
        matched_request="mode_data_01",
    )

    assert not unmatched.state_changed
    assert not orphan.state_changed
    assert reducer.state.fan_mode is FanMode.AUTO
    assert reducer.startup_fan_diagnostics()["last_mode_code"] is None


def test_physical_fan_update_supersedes_incomplete_startup_pair() -> None:
    reducer = make_reducer(Model.H7129)
    reducer.reduce_event(
        startup_event(0x00, mode_code=0x01),
        generation=7,
        matched_request="mode_data_00",
    )

    result = reducer.reduce_event(
        FanModeEvent(
            b"fan",
            mode=FanMode.TURBO,
            mode_code=0x07,
            manual_level=0,
            auto_parameter=0,
        ),
        generation=7,
    )

    assert result.state.fan_mode is FanMode.TURBO
    diagnostics = reducer.startup_fan_diagnostics()
    assert diagnostics["awaiting_h7129_manual_level"] is False
    assert diagnostics["resolution"] == "superseded_by_physical_update"
    assert diagnostics["generation"] == 7


def test_brightness_and_rgb_authority_rules_preserve_unproven_values() -> None:
    reducer = make_reducer(Model.H7129)
    reducer.replace_state(
        PurifierState(
            light_power=True,
            light_brightness=25,
            light_rgb=(10, 20, 30),
        )
    )

    reducer.reduce_event(
        NightLightStateEvent(
            b"brightness",
            power=None,
            brightness=50,
            prefix=0x3A,
            unsolicited=False,
        ),
        generation=1,
    )
    reducer.reduce_event(
        NightLightColorEvent(
            b"ack",
            red=1,
            green=2,
            blue=3,
            color_available=True,
            acknowledgement_only=True,
            prefix=0x3A,
        ),
        generation=1,
    )
    reducer.reduce_event(
        NightLightColorEvent(
            b"unavailable",
            red=None,
            green=None,
            blue=None,
            color_available=False,
            acknowledgement_only=False,
            prefix=0xAA,
        ),
        generation=1,
    )

    assert reducer.state.light_power is True
    assert reducer.state.light_brightness == 50
    assert reducer.state.light_rgb == (10, 20, 30)

    reducer.reduce_event(
        NightLightColorEvent(
            b"query",
            red=4,
            green=5,
            blue=6,
            color_available=True,
            acknowledgement_only=False,
            prefix=0xAA,
        ),
        generation=1,
    )
    assert reducer.state.light_rgb == (4, 5, 6)


def test_pm25_sentinel_clears_measurement_and_retains_filter_update() -> None:
    reducer = make_reducer()
    reducer.replace_state(PurifierState(pm25=12, filter_life=80))

    result = reducer.reduce_event(
        AirQualityEvent(
            b"air",
            status_flags=0,
            pm25_ug_m3=None,
            raw_pm25=0xFFFF,
            mode_related=0,
            unknown=0,
            filter_life=79,
            unsolicited=False,
        ),
        generation=1,
    )

    assert result.state == PurifierState(pm25=None, filter_life=79)


def test_refresh_event_returns_capability_scoped_effect() -> None:
    h7124 = make_reducer(Model.H7124)
    h7129 = make_reducer(Model.H7129)
    event = RefreshRequestedEvent(b"refresh")

    assert not h7124.reduce_event(event, generation=1).refresh_requested
    assert h7129.reduce_event(event, generation=1).refresh_requested


def test_device_state_none_does_not_clear_cached_power() -> None:
    reducer = make_reducer()
    reducer.replace_state(PurifierState(power=True))

    result = reducer.reduce_event(
        DeviceStateEvent(
            b"state",
            power=None,
            status_flags=0,
            volatile_state=0,
        ),
        generation=1,
    )

    assert not result.state_changed
    assert reducer.state.power is True


def test_confirmed_fan_command_updates_state_and_invalidates_partial_pair() -> None:
    reducer = make_reducer(Model.H7129)
    reducer.reduce_event(
        startup_event(0x00, mode_code=0x01),
        generation=8,
        matched_request="mode_data_00",
    )

    result = reducer.apply_confirmed_command(
        SetFanMode(FanMode.HIGH),
        generation=8,
    )

    assert result.state_changed
    assert result.state.fan_mode is FanMode.HIGH
    diagnostics = reducer.startup_fan_diagnostics()
    assert diagnostics["awaiting_h7129_manual_level"] is False
    assert diagnostics["resolution"] == "superseded_by_command_acknowledgement"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SetPower(True), True),
        (SetPower(False), False),
        (SetFanMode(FanMode.HIGH), True),
        (SetFanMode(FanMode.LOW), False),
        (SetNightLightPower(True), True),
        (SetNightLightPower(False), False),
        (SetNightLightBrightness(50), True),
        (SetNightLightBrightness(10), False),
        (SetNightLightColor(1, 2, 3), False),
    ],
)
def test_command_satisfaction_uses_only_authoritative_state(
    command: ProtocolCommand,
    expected: bool,
) -> None:
    reducer = make_reducer()
    reducer.replace_state(
        PurifierState(
            power=True,
            fan_mode=FanMode.HIGH,
            light_power=True,
            light_brightness=50,
            light_rgb=(1, 2, 3),
        )
    )

    assert reducer.command_is_satisfied(command) is expected
