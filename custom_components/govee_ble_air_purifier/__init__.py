"""Govee BLE Air Purifier integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .bluetooth import (
    STALE_CONNECTION_CLEANUP_TIMEOUT,
    BluetoothUnavailableError,
    async_close_stale_connections,
)
from .const import CONF_MODEL, PLATFORMS
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

type GoveeConfigEntry = ConfigEntry[GoveeDataUpdateCoordinator]

_LOGGER = logging.getLogger(__name__)


async def _async_cleanup_address(address: str, *, reason: str) -> None:
    """Best-effort bounded cleanup when no runtime transport may be available."""
    try:
        async with asyncio.timeout(STALE_CONNECTION_CLEANUP_TIMEOUT):
            await async_close_stale_connections(address, reason=reason)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Best-effort stale Bluetooth cleanup failed for %s: reason=%s "
            "cause=%s",
            address,
            reason,
            err,
            exc_info=True,
        )


async def async_setup_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Set up a purifier and recover unavailable hardware in the background."""
    address = entry.data[CONF_ADDRESS]
    await _async_cleanup_address(address, reason="entry_setup")

    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address=address,
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

    async def _async_stop(_: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Unload a Govee purifier config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await entry.runtime_data.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release any surviving address-level connection after entry removal."""
    address = entry.data[CONF_ADDRESS]
    await _async_cleanup_address(address, reason="entry_removed")
