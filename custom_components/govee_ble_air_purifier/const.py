"""Constants for the Govee BLE Air Purifier integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "govee_ble_air_purifier"

CONF_MODEL: Final = "model"

PLATFORMS: Final[list[Platform]] = [
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]
