"""Constants for the Govee BLE Air Purifier integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

from .models import Model

DOMAIN: Final = "govee_ble_air_purifier"

CONF_MODEL: Final = "model"

MANUFACTURER: Final = "Govee"
INTEGRATION_NAME: Final = "Govee BLE Air Purifier"

SUPPORTED_MODELS: Final[tuple[str, ...]] = tuple(model.value for model in Model)

PLATFORMS: Final[list[Platform]] = [
    Platform.FAN,
    Platform.LIGHT,
    Platform.SENSOR,
]
