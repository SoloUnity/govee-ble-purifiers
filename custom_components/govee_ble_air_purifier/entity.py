"""Shared entities for Govee BLE Air Purifier."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GoveeConfigEntry
from .const import DOMAIN
from .coordinator import GoveeDataUpdateCoordinator


class GoveePurifierEntity(CoordinatorEntity[GoveeDataUpdateCoordinator]):
    """Base class for a cached purifier entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: GoveeConfigEntry,
        entity_key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry.runtime_data)
        address = entry.data[CONF_ADDRESS]
        stable_address = format_mac(address)
        self._attr_unique_id = f"{stable_address}_{entity_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, stable_address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer=self.coordinator.profile.identity.manufacturer,
            model=self.coordinator.profile.model.value,
            name=entry.title,
        )

    @property
    def available(self) -> bool:
        """Return unavailable during expected Bluetooth recovery without errors."""
        return super().available and self.coordinator.client_available

    async def _async_run_operation(self, operation: Awaitable[None]) -> None:
        """Run a control operation and translate connection failures."""
        try:
            await operation
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_failed",
            ) from err

    @property
    def _state(self) -> Any:
        """Return the coordinator's cached device state."""
        return self.coordinator.data
