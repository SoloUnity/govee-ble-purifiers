"""Govee BLE Air Purifier integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac

from .bluetooth import BluetoothUnavailableError, async_close_stale_connections
from .bluetooth.ownership import ADDRESS_OWNERSHIP
from .bluetooth_profile import bluetooth_settings_from_profile
from .const import CONF_MODEL, DOMAIN, PLATFORMS
from .coordinator import GoveeDataUpdateCoordinator
from .custom_auto_memory import CustomAutoMemory
from .custom_auto_options import (
    CustomAutoOptions,
    CustomAutoOptionsError,
    parse_custom_auto_options,
)
from .profiles import ProfileError, async_get_profile_registry

type GoveeConfigEntry = ConfigEntry[GoveeDataUpdateCoordinator]

_LOGGER = logging.getLogger(__name__)

_CONFIG_ENTRY_CLEAR_ATTEMPTS = 3
_CONFIG_ENTRY_CLEAR_BACKOFF = (0.01, 0.05)
_CUSTOM_AUTO_CLEAR_STATE = f"{DOMAIN}.custom_auto_required_clear"


@dataclass(slots=True)
class _RequiredCustomAutoClear:
    """Track one entry's required and durably served clear generations."""

    generation: int = 0
    served_generation: int = 0
    task: asyncio.Task[None] | None = None
    error: Exception | None = None


@dataclass(slots=True)
class _CustomAutoClearState:
    """Integration-lifetime owner for unloaded-entry safety clears."""

    entries: dict[str, _RequiredCustomAutoClear] = field(default_factory=dict)
    listener_installed: bool = False
    remove_listener: Callable[[], None] | None = None


def _custom_auto_clear_state(hass: HomeAssistant) -> _CustomAutoClearState:
    """Return the stable integration-owned required-clear state."""
    return hass.data.setdefault(_CUSTOM_AUTO_CLEAR_STATE, _CustomAutoClearState())


async def _async_clear_required_generation(
    hass: HomeAssistant,
    entry_id: str,
    required: _RequiredCustomAutoClear,
) -> None:
    """Serve required generations with one bounded budget per generation."""
    try:
        while required.served_generation < required.generation:
            target_generation = required.generation
            for attempt in range(_CONFIG_ENTRY_CLEAR_ATTEMPTS):
                try:
                    await CustomAutoMemory(hass, entry_id).async_clear()
                except Exception as err:  # noqa: BLE001
                    if required.generation != target_generation:
                        break
                    if attempt + 1 == _CONFIG_ENTRY_CLEAR_ATTEMPTS:
                        required.error = err
                        _LOGGER.error(
                            "Could not clear Custom Auto memory for disabled "
                            "config entry %s after bounded retries: %s",
                            entry_id,
                            type(err).__name__,
                        )
                        return
                    await asyncio.sleep(_CONFIG_ENTRY_CLEAR_BACKOFF[attempt])
                else:
                    # This verified clear completed after every generation now
                    # visible on the event loop, so it safely serves all of them.
                    required.served_generation = required.generation
                    required.error = None
                    break
    finally:
        if required.task is asyncio.current_task():
            required.task = None


def _schedule_required_custom_auto_clear(
    hass: HomeAssistant,
    entry_id: str,
    required: _RequiredCustomAutoClear,
) -> asyncio.Task[None]:
    """Own one deduplicated clear task for an entry."""
    task = required.task
    if task is not None and not task.done():
        return task
    task = hass.async_create_task(
        _async_clear_required_generation(hass, entry_id, required),
        f"clear disabled {DOMAIN} Custom Auto memory {entry_id}",
    )
    required.task = task
    return task


async def _async_settle_required_custom_auto_clear(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Join or perform every required clear before setup can restore memory."""
    required = _custom_auto_clear_state(hass).entries.get(entry_id)
    if required is None or required.served_generation >= required.generation:
        return
    task = _schedule_required_custom_auto_clear(hass, entry_id, required)
    await asyncio.shield(task)
    if required.served_generation < required.generation:
        raise ConfigEntryError(
            "Could not safely clear remembered Custom Auto state before setup"
        ) from required.error


async def async_setup(hass: HomeAssistant, _: dict) -> bool:
    """Install integration-level safety cleanup for disabled unloaded entries."""
    state = _custom_auto_clear_state(hass)
    if state.listener_installed:
        return True

    @callback
    def _registry_updated(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return
        registry_entry = er.async_get(hass).async_get(entity_id)
        if (
            registry_entry is None
            or registry_entry.platform != DOMAIN
            or not registry_entry.unique_id.endswith("_custom_auto")
            or registry_entry.config_entry_id is None
        ):
            return
        entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
        if entry is None:
            return
        changes = event.data.get("changes")
        disabled_by_config_entry_now = (
            entry.disabled_by is not None
            and registry_entry.disabled_by
            is er.RegistryEntryDisabler.CONFIG_ENTRY
            and isinstance(changes, dict)
            and "disabled_by" in changes
            and changes["disabled_by"]
            is not er.RegistryEntryDisabler.CONFIG_ENTRY
        )
        reenabled_after_config_entry_disable = (
            entry.disabled_by is None
            and registry_entry.disabled_by is None
            and isinstance(changes, dict)
            and changes.get("disabled_by")
            is er.RegistryEntryDisabler.CONFIG_ENTRY
        )
        if (
            not disabled_by_config_entry_now
            and not reenabled_after_config_entry_disable
        ):
            return
        entry_id = entry.entry_id
        required = state.entries.setdefault(
            entry_id, _RequiredCustomAutoClear()
        )
        required.generation += 1
        _schedule_required_custom_auto_clear(hass, entry_id, required)

    state.remove_listener = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_updated
    )
    state.listener_installed = True
    return True


def _remove_custom_auto_switch_registry_entry(
    hass: HomeAssistant, address: str
) -> None:
    """Remove only the stable Custom Auto switch registry entry, if present."""
    registry = er.async_get(hass)
    unique_id = f"{format_mac(address)}_custom_auto"
    entity_id = registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, unique_id
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


def _custom_auto_registry_entry(hass: HomeAssistant, address: str):
    """Return the stable Custom Auto registry entry, if it exists."""
    registry = er.async_get(hass)
    unique_id = f"{format_mac(address)}_custom_auto"
    entity_id = registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id)
    return registry.async_get(entity_id) if entity_id is not None else None


def _custom_auto_registry_allowed(hass: HomeAssistant, address: str) -> bool:
    """Return whether the stable switch exists, is enabled, and is visible."""
    registry_entry = _custom_auto_registry_entry(hass, address)
    return (
        registry_entry is not None
        and registry_entry.disabled_by is None
        and registry_entry.hidden_by is None
    )


async def _async_resolve_custom_auto_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> CustomAutoOptions:
    """Resolve an entry's exact profile and validate all mutable options."""
    requested_model = entry.data[CONF_MODEL]
    try:
        registry = await async_get_profile_registry(hass)
        profile = registry.for_model(requested_model)
        return parse_custom_auto_options(
            entry.options, profile.custom_auto_defaults
        )
    except CustomAutoOptionsError as err:
        raise ConfigEntryError(
            "Stored Custom Auto options are invalid for configured model "
            f"{requested_model!r}: {err}. Correct the integration options "
            "before retrying"
        ) from err
    except (ProfileError, KeyError, ValueError) as err:
        raise ConfigEntryError(
            "Bundled purifier model profiles are invalid or the configured "
            f"model {requested_model!r} has no exact profile. Reinstall or "
            "update the integration before retrying setup."
        ) from err


async def _async_options_updated(
    hass: HomeAssistant, entry: GoveeConfigEntry
) -> None:
    """Validate options, yield active ownership, and atomically reload once."""
    try:
        new_options = await _async_resolve_custom_auto_options(hass, entry)
        coordinator = entry.runtime_data
        old_controller = coordinator.custom_auto_controller
        disabling = old_controller is not None and not new_options.enabled

        if disabling:
            await coordinator.async_disable_custom_auto_and_clear()
        elif old_controller is not None and old_controller.snapshot.active:
            try:
                await coordinator.async_quiesce_custom_auto()
            except Exception:  # noqa: BLE001
                # An options flow performs this fallible handoff before commit.
                # This path is defensive for other options writers: persisted
                # values must still reload so storage and runtime converge.
                _LOGGER.exception(
                    "Custom Auto handoff failed after options were persisted "
                    "for purifier model %s; continuing with reload",
                    entry.data.get(CONF_MODEL, "unknown"),
                )

        reloaded = await hass.config_entries.async_reload(entry.entry_id)
        if not reloaded:
            raise ConfigEntryError(
                "Could not reload purifier after validating updated Custom Auto "
                "options"
            )

        if disabling:
            _remove_custom_auto_switch_registry_entry(
                hass, entry.data[CONF_ADDRESS]
            )
    except Exception:
        _LOGGER.exception(
            "Failed to apply updated Custom Auto options for purifier model %s",
            entry.data.get(CONF_MODEL, "unknown"),
        )
        raise


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
    await _async_settle_required_custom_auto_clear(hass, entry.entry_id)
    try:
        registry = await async_get_profile_registry(hass)
        profile = registry.for_model(requested_model)
        custom_auto_options = parse_custom_auto_options(
            entry.options, profile.custom_auto_defaults
        )
    except CustomAutoOptionsError as err:
        raise ConfigEntryError(
            "Stored Custom Auto options are invalid for configured model "
            f"{requested_model!r}: {err}. Correct the integration options "
            "before retrying"
        ) from err
    except (ProfileError, ValueError) as err:
        raise ConfigEntryError(
            "Bundled purifier model profiles are invalid or the configured "
            f"model {requested_model!r} has no exact profile. Reinstall or "
            "update the integration before retrying setup."
        ) from err
    custom_auto_memory = CustomAutoMemory(hass, entry.entry_id)
    registry_allowed = custom_auto_options.enabled and _custom_auto_registry_allowed(
        hass, address
    )
    if custom_auto_options.enabled and registry_allowed:
        restore_custom_auto = await custom_auto_memory.async_load_active()
    else:
        await custom_auto_memory.async_clear()
        restore_custom_auto = False
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
        custom_auto_options=custom_auto_options,
        custom_auto_memory=custom_auto_memory,
        restore_custom_auto=restore_custom_auto,
        custom_auto_registry_allowed=registry_allowed,
    )

    remove_registry_listener: Callable[[], None] | None = None
    try:
        if custom_auto_options.enabled:

            @callback
            def _registry_updated(_: Event) -> None:
                coordinator.set_custom_auto_registry_allowed(
                    _custom_auto_registry_allowed(hass, address)
                )

            remove_registry_subscription = hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_updated
            )
            registry_listener_removed = False

            def _remove_registry_listener() -> None:
                nonlocal registry_listener_removed
                if registry_listener_removed:
                    return
                registry_listener_removed = True
                remove_registry_subscription()

            remove_registry_listener = _remove_registry_listener
            coordinator.set_custom_auto_registry_listener_remover(
                remove_registry_listener
            )
            current_registry_allowed = _custom_auto_registry_allowed(hass, address)
            coordinator.set_custom_auto_registry_allowed(current_registry_allowed)
            if not current_registry_allowed and registry_allowed:
                await custom_auto_memory.async_clear()
        await coordinator.async_start()
    except BluetoothUnavailableError as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(
            f"Unable to initialize purifier at {entry.data[CONF_ADDRESS]}"
        ) from err
    except BaseException as setup_error:
        if remove_registry_listener is not None:
            remove_registry_listener()
        try:
            await coordinator.async_shutdown()
        except BaseException as cleanup_error:
            raise setup_error from cleanup_error
        raise

    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:  # noqa: BLE001
        await coordinator.async_shutdown()
        raise

    if not custom_auto_options.enabled:
        _remove_custom_auto_switch_registry_entry(hass, address)

    async def _async_stop(_: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GoveeConfigEntry) -> bool:
    """Unload a Govee purifier config entry."""
    cleanup_error: Exception | None = None
    if getattr(entry, "disabled_by", None) is not None:
        try:
            await entry.runtime_data.async_disable_custom_auto_and_clear()
        except Exception as err:  # noqa: BLE001
            cleanup_error = err
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await entry.runtime_data.async_shutdown()
    if cleanup_error is not None:
        raise cleanup_error
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release any surviving address-level connection after entry removal."""
    address = entry.data[CONF_ADDRESS]
    timeout = 5.0
    cancellation_timeout = 1.0
    memory_error: BaseException | None = None
    try:
        await CustomAutoMemory(hass, entry.entry_id).async_clear()
    except BaseException as err:  # cancellation must not skip address cleanup
        memory_error = err
    try:
        registry = await async_get_profile_registry(hass)
        profile = registry.for_model(entry.data[CONF_MODEL])
        settings = bluetooth_settings_from_profile(profile)
        timeout = settings.cleanup.stale_connection_timeout
        cancellation_timeout = settings.gatt_operations.operation_cancel_timeout
    except (ProfileError, KeyError, ValueError):
        # Removal must remain bounded even when the artifact that prevented
        # setup is itself invalid. This is a safety fallback, not profile data.
        pass
    finally:
        cleanup_task = asyncio.create_task(
            _async_cleanup_address(
                address,
                reason="entry_removed",
                timeout=timeout,
                cancellation_timeout=cancellation_timeout,
            )
        )
        cleanup_cancellation: asyncio.CancelledError | None = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as err:
                cleanup_cancellation = err
                continue
        cleanup_task.result()
        if memory_error is None and cleanup_cancellation is not None:
            memory_error = cleanup_cancellation
    if memory_error is not None:
        raise memory_error
