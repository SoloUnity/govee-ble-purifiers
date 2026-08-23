"""Light platform for the Govee purifier night light."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeConfigEntry
from .entity import GoveePurifierEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the purifier night-light entity."""
    async_add_entities([GoveePurifierLight(entry)])


class GoveePurifierLight(GoveePurifierEntity, LightEntity):
    """Representation of the purifier night light."""

    _attr_translation_key = "night_light"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(self, entry: GoveeConfigEntry) -> None:
        """Initialize the light."""
        super().__init__(entry, "night_light")

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the cached light power state."""
        return self._state.light_power

    @property
    @override
    def brightness(self) -> int | None:
        """Return cached brightness in Home Assistant's 0-255 scale."""
        percentage = self._state.light_brightness
        if percentage is None:
            return None
        return round(percentage * 255 / 100)

    @property
    @override
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the cached RGB color."""
        return self._state.light_rgb

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the night light and apply requested attributes."""
        if ATTR_RGB_COLOR in kwargs:
            await self._async_run_operation(
                self.coordinator.async_set_light_rgb(tuple(kwargs[ATTR_RGB_COLOR]))
            )

        if ATTR_BRIGHTNESS in kwargs:
            percentage = max(1, min(100, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)))
            await self._async_run_operation(
                self.coordinator.async_set_light_brightness(percentage)
            )

        await self._async_run_operation(self.coordinator.async_set_light_power(True))

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the night light."""
        await self._async_run_operation(self.coordinator.async_set_light_power(False))
