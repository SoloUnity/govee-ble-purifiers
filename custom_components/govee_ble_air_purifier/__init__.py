"""Govee BLE Air Purifier integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .bluetooth import BluetoothUnavailableError, async_close_stale_connections
from .bluetooth.ownership import ADDRESS_OWNERSHIP
from .bluetooth_profile import bluetooth_settings_from_profile
from .const import CONF_MODEL, PLATFORMS
from .coordinator import GoveeDataUpdateCoordinator
from .profiles import ProfileError, async_get_profile_registry

type GoveeConfigEntry = ConfigEntry[GoveeDataUpdateCoordinator]

_LOGGER = logging.getLogger(__name__)


async def _async_cleanup_address(
    address: str,
    *,
    reason: str,
    timeout: float,
    cancellation_timeout: float,
) -> None:
    """Best-effort bounded cleanup when no runtime transport may be available."""
    try:
        result = await ADDRESS_OWNERSHIP.async_run_standalone_cleanup(
            address,
            lambda: async_close_stale_connections(address, reason=reason),
            timeout=timeout,
            cancellation_timeout=cancellation_timeout,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Best-effort stale Bluetooth cleanup failed for %s: reason=%s "
            "cause=%s",
            address,
            reason,
            err,
            exc_info=True,
        )
        return
    if not result["success"]:
        _LOGGER.debug(
            "Best-effort stale Bluetooth cleanup deferred or incomplete for %s: "
            "reason=%s acquired=%s retained=%s cause=%s ownership=%s",
            address,
            reason,
            result["acquired"],
            result["retained"],
            result["error"],
            result["ownership"],
        )


async def async_setup_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Set up a purifier and recover unavailable hardware in the background."""
    address = entry.data[CONF_ADDRESS]
    requested_model = entry.data[CONF_MODEL]
    try:
        registry = await async_get_profile_registry(hass)
        profile = registry.for_model(requested_model)
    except (ProfileError, ValueError) as err:
        raise ConfigEntryError(
            "Bundled purifier model profiles are invalid or the configured "
            f"model {requested_model!r} has no exact profile. Reinstall or "
            "update the integration before retrying setup."
        ) from err
    bluetooth_settings = bluetooth_settings_from_profile(profile)
    await _async_cleanup_address(
        address,
        reason="entry_setup",
        timeout=bluetooth_settings.cleanup.stale_connection_timeout,
        cancellation_timeout=(
            bluetooth_settings.gatt_operations.operation_cancel_timeout
        ),
    )

    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address=address,
        profile=profile,
        bluetooth_settings=bluetooth_settings,
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
    try:
        registry = await async_get_profile_registry(hass)
        profile = registry.for_model(entry.data[CONF_MODEL])
        settings = bluetooth_settings_from_profile(profile)
        timeout = settings.cleanup.stale_connection_timeout
        cancellation_timeout = settings.gatt_operations.operation_cancel_timeout
    except (ProfileError, KeyError, ValueError):
        # Removal must remain bounded even when the artifact that prevented
        # setup is itself invalid. This is a safety fallback, not profile data.
        timeout = 5.0
        cancellation_timeout = 1.0
    await _async_cleanup_address(
        address,
        reason="entry_removed",
        timeout=timeout,
        cancellation_timeout=cancellation_timeout,
    )
