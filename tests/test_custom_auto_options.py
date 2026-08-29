"""Focused tests for immutable profile-backed Custom Auto options."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    CustomAutoOptionsError,
    parse_custom_auto_options,
)
from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import get_profile_registry


@pytest.mark.parametrize(
    ("model", "boundaries"),
    [(Model.H7124, (3, 5, 9, 15)), (Model.H7129, (7, 9, 13, 19))],
)
def test_missing_options_are_disabled_and_use_only_profile_defaults(
    model: Model, boundaries: tuple[int, ...]
) -> None:
    defaults = get_profile_registry().for_model(model).custom_auto_defaults

    options = parse_custom_auto_options({}, defaults)

    assert options.enabled is False
    assert options.pm25_boundaries == boundaries
    assert options.upshift_confirmation_seconds == 3
    assert options.downshift_delays_minutes == (7, 5, 5, 5)
    with pytest.raises(AttributeError):
        options.enabled = True  # type: ignore[misc]


def test_all_profile_defaults_can_be_overridden() -> None:
    defaults = get_profile_registry().for_model(Model.H7124).custom_auto_defaults
    raw = {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [0, 1, 998, 999],
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 300,
        CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [0, 1, 1439, 1440],
    }

    options = parse_custom_auto_options(raw, defaults)

    assert options.enabled
    assert options.pm25_boundaries == (0, 1, 998, 999)
    assert options.upshift_confirmation_seconds == 300
    assert options.downshift_delays_minutes == (0, 1, 1439, 1440)
    raw[CONF_CUSTOM_AUTO_PM25_BOUNDARIES][0] = 50  # type: ignore[index]
    assert options.pm25_boundaries == (0, 1, 998, 999)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (CONF_CUSTOM_AUTO_ENABLED, 1),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, [1, 2, 3]),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, (1, 2, 3, 4)),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, [1, 2, 3, 1000]),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, [1, 2, True, 4]),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, [1, 2, 2, 4]),
        (CONF_CUSTOM_AUTO_PM25_BOUNDARIES, [2, 1, 3, 4]),
        (CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS, -1),
        (CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS, 301),
        (CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS, 1.0),
        (CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES, [0, 0, 0, 1441]),
        (CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES, [0, 0, False, 0]),
    ],
)
def test_invalid_types_ranges_lengths_and_order_are_rejected(
    key: str, value: object
) -> None:
    defaults = get_profile_registry().for_model(Model.H7129).custom_auto_defaults

    with pytest.raises(CustomAutoOptionsError):
        parse_custom_auto_options({key: value}, defaults)
