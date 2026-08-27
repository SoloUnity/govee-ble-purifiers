"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

import logging
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .bluetooth import BluetoothUnavailableError
from .const import CONF_MODEL, DOMAIN
from .discovery import (
    DiscoveredPurifier,
    PurifierDiscoveryService,
    unique_id_from_address,
)
from .profiles import ProfileError, ProfileRegistry, async_get_profile_registry
from .setup_validation import PurifierSetupValidator

_LOGGER = logging.getLogger(__name__)


class GoveeBleAirPurifierConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one purifier."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._manual_discoveries: tuple[DiscoveredPurifier, ...] | None = None
        self._unnamed_devices_seen = False
        self._registry: ProfileRegistry | None = None
        self._discovery: PurifierDiscoveryService | None = None
        self._validator: PurifierSetupValidator | None = None

    async def _async_load_services(self) -> bool:
        """Load profiles once and inject the setup service boundaries."""
        if self._registry is not None:
            return True
        try:
            self._registry = await async_get_profile_registry(self.hass)
        except ProfileError:
            _LOGGER.exception("Bundled purifier model profiles are invalid")
            return False
        self._discovery = PurifierDiscoveryService(self.hass, self._registry)
        self._validator = PurifierSetupValidator(self.hass, self._registry)
        return True

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a currently discovered purifier without entering an address."""
        if not await self._async_load_services():
            return self.async_abort(reason="model_profile_invalid")
        assert self._discovery is not None
        assert self._validator is not None

        if user_input is None:
            configured_ids = {
                entry.unique_id
                for entry in self._async_current_entries()
                if entry.unique_id is not None
            }
            scan_result = await self._discovery.async_discover_purifiers()
            self._manual_discoveries = tuple(
                device
                for device in scan_result.purifiers
                if unique_id_from_address(device.address) not in configured_ids
            )
            self._unnamed_devices_seen = scan_result.unnamed_devices_seen
            _LOGGER.debug(
                "Manual purifier discovery snapshot contains %d fresh, "
                "unconfigured device(s)",
                len(self._manual_discoveries),
            )

        errors: dict[str, str] = {}
        discoveries = self._manual_discoveries or ()

        if not discoveries:
            if self._unnamed_devices_seen:
                return self.async_abort(reason="devices_seen_without_supported_name")
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
                    await self._discovery.async_wait_for_selected_advertisement(
                        address=address,
                    )
                )
                if fresh_service_info is None:
                    errors["base"] = "not_discovered"
                else:
                    fresh_model = self._discovery.model_from_name(
                        fresh_service_info.name
                    )
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
                        await self.async_set_unique_id(unique_id_from_address(address))
                        self._abort_if_unique_id_configured()
                        try:
                            await self._validator.async_validate(
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

        options = self._discovery.discovery_options(discoveries)
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
