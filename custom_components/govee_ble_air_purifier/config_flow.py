"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
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
from .const import CONF_MODEL, DOMAIN, INTEGRATION_NAME
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

_LOGGER = logging.getLogger(__name__)

MANUAL_DISCOVERY_SCAN_DURATION = 10.0
SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT = 10


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


def _discover_purifiers(
    service_infos: Iterable[BluetoothServiceInfoBleak],
    *,
    seen_after: float | None = None,
) -> tuple[DiscoveredPurifier, ...]:
    """Return supported discoveries ordered from strongest to weakest signal."""
    discovered = []
    for service_info in service_infos:
        model = _model_from_name(service_info.name)
        if model is None or not service_info.connectable:
            continue
        advertisement_time = getattr(service_info, "time", None)
        if seen_after is not None and (
            not isinstance(advertisement_time, int | float)
            or advertisement_time < seen_after
        ):
            advertisement_age = (
                round(max(0.0, time.monotonic() - advertisement_time), 3)
                if isinstance(advertisement_time, int | float)
                else None
            )
            _LOGGER.debug(
                "Excluding stale purifier from manual setup: name=%s "
                "address=%s advertisement_age_seconds=%s",
                service_info.name,
                service_info.address,
                advertisement_age,
            )
            continue
        discovered.append(
            DiscoveredPurifier(
                address=service_info.address,
                name=service_info.name,
                model=model,
                rssi=service_info.rssi,
            )
        )
    return tuple(
        sorted(
            discovered,
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


async def _async_request_active_discovery_scan(hass: HomeAssistant) -> float:
    """Observe the shared scanner for a full setup discovery window."""
    request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
    started_at = time.monotonic()
    api_available = request_active_scan is not None
    scan_error: Exception | None = None
    _LOGGER.debug(
        "Starting %.1f-second manual purifier discovery window: "
        "active_scan_api_available=%s scanners=%s",
        MANUAL_DISCOVERY_SCAN_DURATION,
        api_available,
        _scanner_diagnostics(hass),
    )

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
    remaining = MANUAL_DISCOVERY_SCAN_DURATION - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)

    _LOGGER.debug(
        "Manual purifier discovery window completed in %.3f seconds: "
        "active_scan_api_available=%s active_scan_error=%s scanners=%s",
        time.monotonic() - started_at,
        api_available,
        repr(scan_error) if scan_error is not None else None,
        _scanner_diagnostics(hass),
    )
    return started_at


async def _async_wait_for_selected_advertisement(
    hass: HomeAssistant,
    *,
    address: str,
) -> bool:
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
    try:
        service_info = await bluetooth.async_process_advertisements(
            hass,
            _is_fresh,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
            SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT,
        )
    except TimeoutError:
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
            "freshness check; cached_advertisement_age_seconds=%s scanners=%s",
            address,
            time.monotonic() - started_at,
            cached_age,
            _scanner_diagnostics(hass),
        )
        return False

    advertisement_time = getattr(service_info, "time", None)
    _LOGGER.debug(
        "Selected purifier %s produced a fresh advertisement after %.3f "
        "seconds: source=%s rssi=%s advertisement_age_seconds=%s",
        address,
        time.monotonic() - started_at,
        getattr(service_info, "source", None),
        getattr(service_info, "rssi", None),
        (
            round(max(0.0, time.monotonic() - advertisement_time), 3)
            if isinstance(advertisement_time, int | float)
            else None
        ),
    )
    return True


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
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_model: str | None = None
        self._manual_discoveries: tuple[DiscoveredPurifier, ...] | None = None

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a connectable Bluetooth discovery."""
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")
        model = _model_from_name(discovery_info.name)
        if model is None:
            return self.async_abort(reason="unsupported_device")
        address = discovery_info.address
        await self.async_set_unique_id(_unique_id_from_address(address))
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._discovered_model = model
        self.context["title_placeholders"] = {
            "name": discovery_info.name or INTEGRATION_NAME
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a purifier found by Bluetooth."""
        if self._discovery_info is None:
            return self.async_abort(reason="discovery_expired")
        if self._discovered_model is None:
            return self.async_abort(reason="unsupported_device")

        errors: dict[str, str] = {}
        if user_input is not None:
            model = self._discovered_model
            address = self._discovery_info.address
            try:
                await _async_validate_purifier(
                    self.hass,
                    address=address,
                    model=model,
                    name=self._discovery_info.name,
                )
            except BluetoothUnavailableError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=self._discovery_info.name or f"Govee {model}",
                    data={CONF_ADDRESS: address, CONF_MODEL: model},
                )

        self._set_confirm_only()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._discovery_info.name or INTEGRATION_NAME,
            },
            errors=errors,
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a currently discovered purifier without entering an address."""
        if user_input is None:
            scan_started_at = await _async_request_active_discovery_scan(self.hass)
            configured_ids = {
                entry.unique_id
                for entry in self._async_current_entries()
                if entry.unique_id is not None
            }
            self._manual_discoveries = tuple(
                device
                for device in _discover_purifiers(
                    bluetooth.async_discovered_service_info(
                        self.hass,
                        connectable=True,
                    ),
                    seen_after=scan_started_at,
                )
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
                fresh_advertisement = (
                    await _async_wait_for_selected_advertisement(
                        self.hass,
                        address=address,
                    )
                )
                if not fresh_advertisement:
                    errors["base"] = "not_discovered"
                else:
                    await self.async_set_unique_id(_unique_id_from_address(address))
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
