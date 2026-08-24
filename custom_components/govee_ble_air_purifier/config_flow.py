"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, override

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from .bluetooth import BluetoothUnavailableError
from .const import CONF_MODEL, DOMAIN
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

_LOGGER = logging.getLogger(__name__)

MANUAL_DISCOVERY_SCAN_DURATION = 10.0
SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT = 10
SELECTED_ADVERTISEMENT_CHECK_INTERVAL = 0.25


@dataclass(frozen=True, slots=True)
class DiscoveredPurifier:
    """A supported connectable purifier currently known to Home Assistant."""

    address: str
    name: str
    model: str
    rssi: int


def _unique_id_from_address(address: str) -> str:
    """Return the stable normalized registry identity for an address."""
    return format_mac(address)


def _model_from_name(name: str | None) -> str | None:
    """Infer a model from the two documented purifier name families."""
    if not name:
        return None
    normalized = name.upper()
    if normalized.startswith("GVH7124"):
        return Model.H7124.value
    if normalized.startswith("IHOMENT_H7129_"):
        return Model.H7129.value
    return None


def _record_discovery(
    discoveries: dict[str, DiscoveredPurifier],
    service_info: BluetoothServiceInfoBleak,
) -> None:
    """Remember the latest valid name and signal for one purifier address."""
    model = _model_from_name(service_info.name)
    if model is None or not service_info.connectable:
        return
    discoveries[service_info.address] = DiscoveredPurifier(
        address=service_info.address,
        name=service_info.name,
        model=model,
        rssi=service_info.rssi,
    )


def _sorted_discoveries(
    discoveries: Iterable[DiscoveredPurifier],
) -> tuple[DiscoveredPurifier, ...]:
    """Order discoveries from strongest to weakest signal."""
    return tuple(
        sorted(
            discoveries,
            key=lambda device: (-device.rssi, device.name, device.address),
        )
    )


def _discovery_options(
    discoveries: tuple[DiscoveredPurifier, ...],
) -> dict[str, str]:
    """Build address-backed labels without exposing addresses to the user."""
    return {
        device.address: f"{device.name} ({'Near' if index == 0 else 'Far'})"
        for index, device in enumerate(discoveries)
    }


def _scanner_diagnostics(hass: HomeAssistant) -> list[dict[str, Any]] | None:
    """Return best-effort read-only scanner details for setup diagnostics."""
    current_scanners = getattr(bluetooth, "async_current_scanners", None)
    if current_scanners is None:
        return None
    try:
        scanners = current_scanners(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Unable to inspect Home Assistant Bluetooth scanners: %s",
            err,
            exc_info=True,
        )
        return None
    return [
        {
            "source": getattr(scanner, "source", None),
            "connectable": getattr(scanner, "connectable", None),
            "scanning": getattr(scanner, "scanning", None),
            "requested_mode": str(getattr(scanner, "requested_mode", None)),
            "current_mode": str(getattr(scanner, "current_mode", None)),
        }
        for scanner in scanners
    ]


async def _async_discover_purifiers(
    hass: HomeAssistant,
) -> tuple[DiscoveredPurifier, ...]:
    """Collect supported purifiers during one complete setup scan window."""
    request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
    started_at = time.monotonic()
    api_available = request_active_scan is not None
    scan_error: Exception | None = None
    discoveries: dict[str, DiscoveredPurifier] = {}
    accept_callbacks = False

    def _advertisement_received(
        service_info: BluetoothServiceInfoBleak,
        *_: Any,
    ) -> None:
        # Older Home Assistant releases replay cached advertisements
        # synchronously while registering. Only callbacks delivered after
        # registration completes are evidence from this scan window.
        if not accept_callbacks:
            return
        previous = discoveries.get(service_info.address)
        _record_discovery(discoveries, service_info)
        current = discoveries.get(service_info.address)
        if current is not None and current != previous:
            _LOGGER.debug(
                "Observed supported purifier during manual scan: name=%s "
                "address=%s model=%s source=%s rssi=%s",
                current.name,
                current.address,
                current.model,
                getattr(service_info, "source", None),
                current.rssi,
            )

    callback_kwargs: dict[str, Any] = {}
    if replay_type := getattr(bluetooth, "BluetoothCallbackReplay", None):
        callback_kwargs["replay"] = replay_type.DISABLED
    cancel_callback: Callable[[], None] = bluetooth.async_register_callback(
        hass,
        _advertisement_received,
        {"connectable": True},
        bluetooth.BluetoothScanningMode.PASSIVE,
        **callback_kwargs,
    )
    accept_callbacks = True

    _LOGGER.debug(
        "Starting %.1f-second manual purifier discovery window: "
        "active_scan_api_available=%s scanners=%s",
        MANUAL_DISCOVERY_SCAN_DURATION,
        api_available,
        _scanner_diagnostics(hass),
    )

    try:
        if request_active_scan is None:
            _LOGGER.debug(
                "One-shot active Bluetooth scanning is unavailable; observing "
                "Home Assistant's shared scanner for the complete setup window"
            )
        else:
            try:
                await request_active_scan(
                    hass,
                    duration=MANUAL_DISCOVERY_SCAN_DURATION,
                )
            except Exception as err:  # noqa: BLE001
                scan_error = err
                _LOGGER.warning(
                    "Home Assistant's one-shot active Bluetooth scan failed; "
                    "continuing the shared-scanner observation window: %s",
                    err,
                    exc_info=True,
                )

        # The Home Assistant scheduler may return early when no AUTO scanner can
        # open an active window. Always own the observation deadline here so the
        # setup form cannot be populated immediately from old cache entries.
        remaining = MANUAL_DISCOVERY_SCAN_DURATION - (
            time.monotonic() - started_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)

        # Current Home Assistant updates its shared history even when repeated
        # packet content is deduplicated before callbacks. Merge that history
        # after the window while retaining any valid name seen by our callback.
        for service_info in bluetooth.async_discovered_service_info(
            hass,
            connectable=True,
        ):
            advertisement_time = getattr(service_info, "time", None)
            if not isinstance(advertisement_time, int | float):
                continue
            if advertisement_time < started_at:
                continue
            _record_discovery(discoveries, service_info)
    finally:
        cancel_callback()

    result = _sorted_discoveries(discoveries.values())
    _LOGGER.debug(
        "Manual purifier discovery window completed in %.3f seconds: "
        "active_scan_api_available=%s active_scan_error=%s scanners=%s "
        "candidates=%s",
        time.monotonic() - started_at,
        api_available,
        repr(scan_error) if scan_error is not None else None,
        _scanner_diagnostics(hass),
        [
            {
                "name": discovery.name,
                "address": discovery.address,
                "model": discovery.model,
                "rssi": discovery.rssi,
            }
            for discovery in result
        ],
    )
    return result


async def _async_wait_for_selected_advertisement(
    hass: HomeAssistant,
    *,
    address: str,
) -> BluetoothServiceInfoBleak | None:
    """Wait for fresh connectable evidence for the selected purifier."""
    started_at = time.monotonic()

    def _is_fresh(service_info: BluetoothServiceInfoBleak) -> bool:
        advertisement_time = getattr(service_info, "time", None)
        return (
            isinstance(advertisement_time, int | float)
            and advertisement_time >= started_at
        )

    _LOGGER.debug(
        "Waiting up to %d seconds for a fresh advertisement from selected "
        "purifier %s",
        SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT,
        address,
    )
    process_task = hass.async_create_task(
        bluetooth.async_process_advertisements(
            hass,
            _is_fresh,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
            SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT,
        )
    )
    deadline = started_at + SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT
    service_info: BluetoothServiceInfoBleak | None = None
    wait_error: Exception | None = None
    try:
        while service_info is None and time.monotonic() < deadline:
            if process_task.done():
                try:
                    service_info = process_task.result()
                except TimeoutError as err:
                    wait_error = err
                except Exception as err:  # noqa: BLE001
                    wait_error = err
                    _LOGGER.warning(
                        "Selected-purifier advertisement processing failed "
                        "for %s: %s",
                        address,
                        err,
                        exc_info=True,
                    )
                break

            # Home Assistant may update shared advertisement history before
            # suppressing an otherwise identical callback. Polling the cache
            # makes that refreshed timestamp valid evidence without accepting
            # registration replay from before this wait.
            cached_info = bluetooth.async_last_service_info(
                hass,
                address,
                connectable=True,
            )
            cached_time = (
                getattr(cached_info, "time", None)
                if cached_info is not None
                else None
            )
            if (
                cached_info is not None
                and isinstance(cached_time, int | float)
                and cached_time >= started_at
            ):
                service_info = cached_info
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.wait(
                {process_task},
                timeout=min(SELECTED_ADVERTISEMENT_CHECK_INTERVAL, remaining),
            )
    finally:
        if not process_task.done():
            process_task.cancel()
            with suppress(asyncio.CancelledError):
                await process_task
        elif not process_task.cancelled():
            # Retrieve any race-completed exception when cache freshness won.
            process_task.exception()

    if service_info is None:
        cached_info = bluetooth.async_last_service_info(
            hass,
            address,
            connectable=True,
        )
        cached_time = (
            getattr(cached_info, "time", None) if cached_info is not None else None
        )
        cached_age = (
            round(max(0.0, time.monotonic() - cached_time), 3)
            if isinstance(cached_time, int | float)
            else None
        )
        _LOGGER.warning(
            "Selected purifier %s was not observed during its %.1f-second "
            "freshness check; cached_advertisement_age_seconds=%s "
            "advertisement_wait_error=%s scanners=%s",
            address,
            time.monotonic() - started_at,
            cached_age,
            repr(wait_error) if wait_error is not None else None,
            _scanner_diagnostics(hass),
        )
        return None

    advertisement_time = getattr(service_info, "time", None)
    _LOGGER.debug(
        "Selected purifier %s produced a fresh advertisement after %.3f "
        "seconds: name=%s source=%s rssi=%s advertisement_age_seconds=%s",
        address,
        time.monotonic() - started_at,
        getattr(service_info, "name", None),
        getattr(service_info, "source", None),
        getattr(service_info, "rssi", None),
        (
            round(max(0.0, time.monotonic() - advertisement_time), 3)
            if isinstance(advertisement_time, int | float)
            else None
        ),
    )
    return service_info


async def _async_validate_purifier(
    hass: HomeAssistant,
    *,
    address: str,
    model: str,
    name: str | None,
) -> None:
    """Connect and complete initialization before storing the entry."""
    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address=address,
        model=Model(model),
        name=name,
    )
    try:
        await coordinator.async_start()
    finally:
        await coordinator.async_shutdown()


class GoveeBleAirPurifierConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one purifier."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._manual_discoveries: tuple[DiscoveredPurifier, ...] | None = None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a currently discovered purifier without entering an address."""
        if user_input is None:
            configured_ids = {
                entry.unique_id
                for entry in self._async_current_entries()
                if entry.unique_id is not None
            }
            self._manual_discoveries = tuple(
                device
                for device in await _async_discover_purifiers(self.hass)
                if _unique_id_from_address(device.address) not in configured_ids
            )
            _LOGGER.debug(
                "Manual purifier discovery snapshot contains %d fresh, "
                "unconfigured device(s)",
                len(self._manual_discoveries),
            )

        errors: dict[str, str] = {}
        discoveries = self._manual_discoveries or ()

        if not discoveries:
            return self.async_abort(reason="no_devices_found")

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = next(
                (device for device in discoveries if device.address == address),
                None,
            )
            if discovery is None:
                errors["base"] = "not_discovered"
            else:
                fresh_service_info = (
                    await _async_wait_for_selected_advertisement(
                        self.hass,
                        address=address,
                    )
                )
                if fresh_service_info is None:
                    errors["base"] = "not_discovered"
                else:
                    fresh_model = _model_from_name(fresh_service_info.name)
                    if fresh_model is not None and fresh_model != discovery.model:
                        _LOGGER.warning(
                            "Selected purifier %s changed advertised model "
                            "during setup: listed_model=%s fresh_model=%s",
                            address,
                            discovery.model,
                            fresh_model,
                        )
                        errors["base"] = "not_discovered"
                    else:
                        if fresh_model is not None:
                            discovery = DiscoveredPurifier(
                                address=address,
                                name=fresh_service_info.name,
                                model=fresh_model,
                                rssi=fresh_service_info.rssi,
                            )
                        await self.async_set_unique_id(
                            _unique_id_from_address(address)
                        )
                        self._abort_if_unique_id_configured()
                        try:
                            await _async_validate_purifier(
                                self.hass,
                                address=address,
                                model=discovery.model,
                                name=discovery.name,
                            )
                        except BluetoothUnavailableError:
                            errors["base"] = "cannot_connect"
                        else:
                            return self.async_create_entry(
                                title=discovery.name,
                                data={
                                    CONF_ADDRESS: address,
                                    CONF_MODEL: discovery.model,
                                },
                            )

        options = _discovery_options(discoveries)
        address_field = vol.Required(CONF_ADDRESS)
        if user_input is not None and user_input[CONF_ADDRESS] in options:
            address_field = vol.Required(
                CONF_ADDRESS,
                default=user_input[CONF_ADDRESS],
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({address_field: vol.In(options)}),
            errors=errors,
        )
