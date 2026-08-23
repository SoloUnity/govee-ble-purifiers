"""Govee BLE Air Purifier integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .bluetooth import BluetoothUnavailableError
from .const import CONF_MODEL, PLATFORMS
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

type GoveeConfigEntry = ConfigEntry[GoveeDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Set up a Govee purifier from a config entry."""
    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address=entry.data[CONF_ADDRESS],
        model=Model(entry.data[CONF_MODEL]),
        name=entry.title,
    )

    try:
        await coordinator.async_start()
    except BluetoothUnavailableError as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(
            f"Unable to initialize purifier at {entry.data[CONF_ADDRESS]}"
        ) from err

    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:  # noqa: BLE001
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Unload a Govee purifier config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await entry.runtime_data.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Allow a removed purifier to be discovered again."""
    bluetooth.async_rediscover_address(hass, entry.data[CONF_ADDRESS])
