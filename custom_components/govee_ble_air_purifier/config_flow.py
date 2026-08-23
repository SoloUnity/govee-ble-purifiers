"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

import re
from typing import Any, override

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from .bluetooth import BluetoothUnavailableError
from .const import CONF_MODEL, DOMAIN, INTEGRATION_NAME, SUPPORTED_MODELS
from .coordinator import GoveeDataUpdateCoordinator
from .models import Model

_MAC_WITHOUT_SEPARATORS = re.compile(r"^[0-9A-Fa-f]{12}$")
_MAC_WITH_SEPARATORS = re.compile(
    r"^[0-9A-Fa-f]{2}(?P<separator>[:-])"
    r"[0-9A-Fa-f]{2}(?P=separator)"
    r"[0-9A-Fa-f]{2}(?P=separator)"
    r"[0-9A-Fa-f]{2}(?P=separator)"
    r"[0-9A-Fa-f]{2}(?P=separator)[0-9A-Fa-f]{2}$"
)


def _normalize_address(value: str) -> str:
    """Validate a MAC and return Home Assistant's transport-facing form."""
    address = value.strip()
    if _MAC_WITH_SEPARATORS.fullmatch(address):
        address = address.replace("-", "").replace(":", "")
    elif not _MAC_WITHOUT_SEPARATORS.fullmatch(address):
        raise ValueError("Invalid Bluetooth address")
    return format_mac(address).upper()


def _unique_id_from_address(address: str) -> str:
    """Return the stable normalized registry identity for an address."""
    return format_mac(address)


def _model_from_name(name: str | None) -> str | None:
    """Infer a supported model only when it is present in the local name."""
    if not name:
        return None
    normalized = name.upper()
    return next((model for model in SUPPORTED_MODELS if model in normalized), None)


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

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a connectable Bluetooth discovery."""
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")
        address = discovery_info.address
        await self.async_set_unique_id(_unique_id_from_address(address))
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._discovered_model = _model_from_name(discovery_info.name)
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

        errors: dict[str, str] = {}
        if user_input is not None:
            model = self._discovered_model or user_input[CONF_MODEL]
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

        schema = vol.Schema({})
        if self._discovered_model is None:
            schema = vol.Schema({vol.Required(CONF_MODEL): vol.In(SUPPORTED_MODELS)})
        else:
            self._set_confirm_only()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=schema,
            description_placeholders={
                "name": self._discovery_info.name or INTEGRATION_NAME,
            },
            errors=errors,
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up a currently discoverable purifier manually."""
        errors: dict[str, str] = {}
        normalized_input: dict[str, Any] = user_input or {}

        if user_input is not None:
            try:
                address = _normalize_address(user_input[CONF_ADDRESS])
            except ValueError:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                device = bluetooth.async_ble_device_from_address(
                    self.hass, address, connectable=True
                )
                if device is None:
                    errors["base"] = "not_discovered"
                else:
                    model = user_input[CONF_MODEL]
                    await self.async_set_unique_id(_unique_id_from_address(address))
                    self._abort_if_unique_id_configured()
                    try:
                        await _async_validate_purifier(
                            self.hass,
                            address=address,
                            model=model,
                            name=device.name,
                        )
                    except BluetoothUnavailableError:
                        errors["base"] = "cannot_connect"
                    else:
                        return self.async_create_entry(
                            title=device.name or f"Govee {model}",
                            data={CONF_ADDRESS: address, CONF_MODEL: model},
                        )

            normalized_input = {**user_input}
            if CONF_ADDRESS not in errors:
                normalized_input[CONF_ADDRESS] = address

        address_field = (
            vol.Required(CONF_ADDRESS, default=normalized_input[CONF_ADDRESS])
            if normalized_input.get(CONF_ADDRESS)
            else vol.Required(CONF_ADDRESS)
        )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    address_field: str,
                    vol.Required(
                        CONF_MODEL,
                        default=normalized_input.get(CONF_MODEL, SUPPORTED_MODELS[0]),
                    ): vol.In(SUPPORTED_MODELS),
                }
            ),
            errors=errors,
        )
