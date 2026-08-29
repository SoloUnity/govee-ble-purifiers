"""Switch platform for integration-managed Custom Auto policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import override

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoveeConfigEntry
from .custom_auto_controller import CustomAutoSnapshot
from .entity import GoveePurifierEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Custom Auto only when its controller exists."""
    if entry.runtime_data.custom_auto_controller is not None:
        async_add_entities([GoveePurifierCustomAutoSwitch(entry)])


class GoveePurifierCustomAutoSwitch(GoveePurifierEntity, SwitchEntity):
    """Expose cached Custom Auto ownership."""

    _attr_translation_key = "custom_auto"

    def __init__(self, entry: GoveeConfigEntry) -> None:
        """Initialize the switch with a stable purifier-scoped identity."""
        super().__init__(entry, "custom_auto")
        self._remove_custom_auto_listener: Callable[[], None] | None = None

    @property
    @override
    def is_on(self) -> bool:
        """Return active intent, including while power-suspended."""
        snapshot = self.coordinator.custom_auto_snapshot
        return snapshot is not None and snapshot.active

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to controller-only cached state changes."""
        await super().async_added_to_hass()
        self._remove_custom_auto_listener = (
            self.coordinator.add_custom_auto_listener(
                self._handle_custom_auto_state
            )
        )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Remove the controller listener before entity teardown."""
        if self._remove_custom_auto_listener is not None:
            self._remove_custom_auto_listener()
            self._remove_custom_auto_listener = None
        await super().async_will_remove_from_hass()

    def _handle_custom_auto_state(self, snapshot: CustomAutoSnapshot) -> None:
        """Publish a controller snapshot change without doing Bluetooth I/O."""
        self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: object) -> None:
        """Activate Custom Auto policy ownership."""
        await self._async_run_operation(
            self.coordinator.async_activate_custom_auto()
        )

    @override
    async def async_turn_off(self, **kwargs: object) -> None:
        """Deactivate Custom Auto and perform the coordinator handoff."""
        await self._async_run_operation(
            self.coordinator.async_deactivate_custom_auto()
        )
