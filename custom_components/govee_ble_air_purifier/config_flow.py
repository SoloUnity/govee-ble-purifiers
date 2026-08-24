"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

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

from .bluetooth import (
    RECENT_CACHED_ADVERTISEMENT_MAX_AGE,
    BluetoothUnavailableError,
)
from .const import CONF_MODEL, DOMAIN, INTEGRATION_NAME
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

_LOGGER = logging.getLogger(__name__)


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


async def _async_request_active_discovery_scan(hass: HomeAssistant) -> float | None:
    """Refresh AUTO scanners and return the successful sweep's start time."""
    request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
    if request_active_scan is None:
        _LOGGER.debug(
            "One-shot active Bluetooth scanning is unavailable; using only "
            "recent cached discoveries"
        )
        return None
    started_at = time.monotonic()
    try:
        await request_active_scan(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "One-shot active Bluetooth scan failed; using only recent cached "
            "discoveries: %s",
            err,
            exc_info=True,
        )
        return None
    _LOGGER.debug(
        "One-shot active Bluetooth scan completed in %.3f seconds",
        time.monotonic() - started_at,
    )
    return started_at


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
            seen_after = scan_started_at or (
                time.monotonic() - RECENT_CACHED_ADVERTISEMENT_MAX_AGE
            )
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
                    seen_after=seen_after,
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
                device = bluetooth.async_ble_device_from_address(
                    self.hass, address, connectable=True
                )
                if device is None:
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
