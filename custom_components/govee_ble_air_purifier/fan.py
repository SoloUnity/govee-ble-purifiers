"""Fan platform for Govee BLE Air Purifier."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from . import GoveeConfigEntry
from .entity import GoveePurifierEntity
from .models import FanMode

_MANUAL_MODES: tuple[FanMode, ...] = (
    FanMode.SLEEP,
    FanMode.LOW,
    FanMode.MEDIUM,
    FanMode.HIGH,
    FanMode.TURBO,
)
_PRESET_MANUAL = "manual"
_PRESET_AUTO = "auto"
_PRESET_MODES = [_PRESET_MANUAL, _PRESET_AUTO]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the purifier fan entity."""
    if entry.runtime_data.profile.capabilities.fan:
        async_add_entities([GoveePurifierFan(entry)])


class GoveePurifierFan(GoveePurifierEntity, FanEntity):
    """Representation of the purifier fan."""

    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = len(_MANUAL_MODES)
    _attr_preset_modes = _PRESET_MODES

    def __init__(self, entry: GoveeConfigEntry) -> None:
        """Initialize the fan."""
        super().__init__(entry, "fan")
        self._remove_custom_auto_listener: Callable[[], None] | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to ownership changes that do not alter purifier state."""
        await super().async_added_to_hass()
        if (
            self.coordinator.custom_auto_snapshot is not None
            and self._remove_custom_auto_listener is None
        ):
            self._remove_custom_auto_listener = (
                self.coordinator.add_custom_auto_listener(
                    lambda _: self.async_write_ha_state()
                )
            )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe ownership refresh before entity removal."""
        if self._remove_custom_auto_listener is not None:
            self._remove_custom_auto_listener()
            self._remove_custom_auto_listener = None
        await super().async_will_remove_from_hass()

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the cached purifier power state."""
        return self._state.power

    @property
    @override
    def percentage(self) -> int | None:
        """Return the cached manual fan speed."""
        mode = self._state.fan_mode
        if mode not in _MANUAL_MODES:
            return None
        return ordered_list_item_to_percentage(_MANUAL_MODES, mode)

    @property
    @override
    def preset_mode(self) -> str | None:
        """Return the cached preset mode."""
        mode = self._state.fan_mode
        if mode in _MANUAL_MODES:
            return _PRESET_MANUAL
        if mode is FanMode.AUTO:
            snapshot = self.coordinator.custom_auto_snapshot
            if snapshot is not None and snapshot.active:
                return None
            return _PRESET_AUTO
        return None

    def _fan_mode_for_preset(self, preset_mode: str) -> FanMode:
        """Resolve an entity preset to a documented physical fan mode."""
        if preset_mode == _PRESET_AUTO:
            return FanMode.AUTO
        if preset_mode == _PRESET_MANUAL:
            mode = self._state.fan_mode
            return mode if mode in _MANUAL_MODES else FanMode.LOW
        raise ValueError(f"Unsupported preset mode: {preset_mode}")

    @override
    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the purifier, optionally selecting a mode."""
        mode: FanMode | None = None
        if percentage is not None:
            if percentage == 0:
                await self.async_turn_off()
                return
            mode = percentage_to_ordered_list_item(_MANUAL_MODES, percentage)
        elif preset_mode is not None:
            mode = self._fan_mode_for_preset(preset_mode)

        if mode is not None:
            await self._async_run_operation(
                self.coordinator.async_apply_ha_fan_mode(mode, power_on=True)
            )
            return
        await self._async_run_operation(self.coordinator.async_set_power(True))

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the purifier."""
        await self._async_run_operation(self.coordinator.async_set_power(False))

    @override
    async def async_set_percentage(self, percentage: int) -> None:
        """Set one of the five manual fan levels."""
        if percentage == 0:
            await self.async_turn_off()
            return
        mode = percentage_to_ordered_list_item(_MANUAL_MODES, percentage)
        await self._async_run_operation(
            self.coordinator.async_apply_ha_fan_mode(
                mode,
                power_on=self.is_on is False,
            )
        )

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the Auto preset or select the current/default manual level."""
        mode = self._fan_mode_for_preset(preset_mode)
        await self._async_run_operation(
            self.coordinator.async_apply_ha_fan_mode(
                mode,
                power_on=self.is_on is False,
            )
        )
