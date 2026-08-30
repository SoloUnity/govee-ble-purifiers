"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

import logging
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector

from .bluetooth import BluetoothUnavailableError
from .const import CONF_MODEL, DOMAIN
from .custom_auto_options import (
    CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    CustomAutoOptions,
    CustomAutoOptionsError,
    parse_custom_auto_options,
)
from .discovery import (
    DiscoveredPurifier,
    PurifierDiscoveryService,
    unique_id_from_address,
)
from .profiles import ProfileError, ProfileRegistry, async_get_profile_registry
from .setup_validation import PurifierSetupValidator

_LOGGER = logging.getLogger(__name__)

CONF_PM25_BOUNDARY_SLEEP_LOW = "pm25_boundary_sleep_low"
CONF_PM25_BOUNDARY_LOW_MEDIUM = "pm25_boundary_low_medium"
CONF_PM25_BOUNDARY_MEDIUM_HIGH = "pm25_boundary_medium_high"
CONF_PM25_BOUNDARY_HIGH_TURBO = "pm25_boundary_high_turbo"
CONF_DOWNSHIFT_LOW_SLEEP = "downshift_low_sleep_minutes"
CONF_DOWNSHIFT_MEDIUM_LOW = "downshift_medium_low_minutes"
CONF_DOWNSHIFT_HIGH_MEDIUM = "downshift_high_medium_minutes"
CONF_DOWNSHIFT_TURBO_HIGH = "downshift_turbo_high_minutes"

_BOUNDARY_FIELDS = (
    CONF_PM25_BOUNDARY_SLEEP_LOW,
    CONF_PM25_BOUNDARY_LOW_MEDIUM,
    CONF_PM25_BOUNDARY_MEDIUM_HIGH,
    CONF_PM25_BOUNDARY_HIGH_TURBO,
)
_DOWNSHIFT_FIELDS = (
    CONF_DOWNSHIFT_LOW_SLEEP,
    CONF_DOWNSHIFT_MEDIUM_LOW,
    CONF_DOWNSHIFT_HIGH_MEDIUM,
    CONF_DOWNSHIFT_TURBO_HIGH,
)


def _whole_number(value: object) -> object:
    """Restore integral selector output without hiding invalid fractions."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _number_selector() -> object:
    """Return a numeric box while leaving semantic bounds to the parser."""
    return vol.All(
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        _whole_number,
    )


def _enable_schema(enabled: bool) -> vol.Schema:
    """Build the opt-in step schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_CUSTOM_AUTO_ENABLED,
                description={"suggested_value": enabled},
            ): (
                selector.BooleanSelector()
            )
        }
    )


def _settings_schema(options: CustomAutoOptions) -> vol.Schema:
    """Build scalar form fields from profile-backed effective options."""
    fields: dict[vol.Marker, object] = {}
    for key, value in zip(
        _BOUNDARY_FIELDS, options.pm25_boundaries, strict=True
    ):
        fields[vol.Required(key, default=value)] = _number_selector()
    fields[
        vol.Required(
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
            default=options.upshift_confirmation_seconds,
        )
    ] = _number_selector()
    for key, value in zip(
        _DOWNSHIFT_FIELDS, options.downshift_delays_minutes, strict=True
    ):
        fields[vol.Required(key, default=value)] = _number_selector()
    return vol.Schema(fields)


def _normalize_settings(user_input: dict[str, Any]) -> dict[str, object]:
    """Convert scalar form values to the shared stored option shape."""
    return {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [
            user_input[key] for key in _BOUNDARY_FIELDS
        ],
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: user_input[
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS
        ],
        CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [
            user_input[key] for key in _DOWNSHIFT_FIELDS
        ],
    }


def _settings_error(error: CustomAutoOptionsError) -> str:
    """Map parser failures to stable localized flow errors."""
    message = str(error)
    if "strictly ascending" in message:
        return "boundaries_not_ascending"
    if "must be an integer" in message:
        return "invalid_integer"
    if "must be between" in message:
        return "value_out_of_range"
    return "invalid_custom_auto_settings"


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
        self._pending_title: str | None = None
        self._pending_data: dict[str, str] | None = None

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
                            self._pending_title = discovery.name
                            self._pending_data = {
                                CONF_ADDRESS: address,
                                CONF_MODEL: discovery.model,
                            }
                            return await self.async_step_enable_custom_auto()

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

    async def async_step_enable_custom_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the optional policy after purifier validation."""
        if user_input is not None:
            if user_input.get(CONF_CUSTOM_AUTO_ENABLED, False):
                return await self.async_step_custom_auto_settings()
            return self._async_create_pending_entry(
                {CONF_CUSTOM_AUTO_ENABLED: False}
            )
        return self.async_show_form(
            step_id="enable_custom_auto",
            data_schema=_enable_schema(False),
        )

    async def async_step_custom_auto_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate profile-backed Custom Auto settings."""
        assert self._pending_data is not None
        assert self._registry is not None
        defaults = self._registry.for_model(
            self._pending_data[CONF_MODEL]
        ).custom_auto_defaults
        effective = parse_custom_auto_options(
            {CONF_CUSTOM_AUTO_ENABLED: True}, defaults
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized = _normalize_settings(user_input)
            try:
                parse_custom_auto_options(normalized, defaults)
            except CustomAutoOptionsError as err:
                errors["base"] = _settings_error(err)
            else:
                return self._async_create_pending_entry(normalized)
        return self.async_show_form(
            step_id="custom_auto_settings",
            data_schema=_settings_schema(effective),
            errors=errors,
        )

    def _async_create_pending_entry(
        self, options: dict[str, object]
    ) -> ConfigFlowResult:
        """Complete setup once with immutable identity in data."""
        assert self._pending_title is not None
        assert self._pending_data is not None
        return self.async_create_entry(
            title=self._pending_title,
            data=self._pending_data,
            options=options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> GoveeBleAirPurifierOptionsFlow:
        """Return the Custom Auto options flow."""
        return GoveeBleAirPurifierOptionsFlow()


class GoveeBleAirPurifierOptionsFlow(OptionsFlow):
    """Edit per-entry Custom Auto options."""

    def _async_install_setup_error_repair_listener(self) -> None:
        """Reload once after valid options repair an entry that could not set up."""
        if self.config_entry.state is not ConfigEntryState.SETUP_ERROR:
            return

        remove_listener = None

        async def _async_reload_after_persist(hass, entry: ConfigEntry) -> None:
            assert remove_listener is not None
            remove_listener()
            hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id),
                f"reload repaired {DOMAIN} entry {entry.entry_id}",
            )

        remove_listener = self.config_entry.add_update_listener(
            _async_reload_after_persist
        )

    async def _async_handoff_active_controller(self) -> bool:
        """Yield an active loaded controller before options are persisted."""
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return True
        coordinator = entry.runtime_data
        controller = coordinator.custom_auto_controller
        if controller is None or not controller.snapshot.active:
            return True
        try:
            await coordinator.async_deactivate_custom_auto()
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Could not hand off active Custom Auto before saving options "
                "for purifier model %s",
                entry.data.get(CONF_MODEL, "unknown"),
            )
            try:
                # Activation establishes a fresh-sample barrier, so a failed
                # pre-commit edit cannot resume from stale cached PM2.5.
                await coordinator.async_activate_custom_auto()
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Could not reactivate Custom Auto after an options handoff "
                    "failure for purifier model %s",
                    entry.data.get(CONF_MODEL, "unknown"),
                )
            return False
        return True

    async def _async_effective_options(
        self,
    ) -> tuple[CustomAutoOptions, bool]:
        """Resolve current values, recovering invalid storage to profile defaults."""
        registry = await async_get_profile_registry(self.hass)
        defaults = registry.for_model(
            self.config_entry.data[CONF_MODEL]
        ).custom_auto_defaults
        try:
            return (
                parse_custom_auto_options(self.config_entry.options, defaults),
                False,
            )
        except CustomAutoOptionsError:
            return parse_custom_auto_options({}, defaults), True

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the feature toggle before any settings."""
        effective, stored_options_invalid = await self._async_effective_options()
        if user_input is not None:
            if user_input.get(CONF_CUSTOM_AUTO_ENABLED, False):
                return await self.async_step_custom_auto_settings()
            disabled_options = {CONF_CUSTOM_AUTO_ENABLED: False}
            if disabled_options == self.config_entry.options:
                return self.async_create_entry(title="", data=disabled_options)
            if not await self._async_handoff_active_controller():
                return self.async_show_form(
                    step_id="init",
                    data_schema=_enable_schema(effective.enabled),
                    errors={"base": "custom_auto_handoff_failed"},
                )
            self._async_install_setup_error_repair_listener()
            return self.async_create_entry(title="", data=disabled_options)
        return self.async_show_form(
            step_id="init",
            data_schema=_enable_schema(effective.enabled),
            errors=(
                {"base": "stored_options_invalid"}
                if stored_options_invalid
                else {}
            ),
        )

    async def async_step_custom_auto_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit complete normalized settings or report stable errors."""
        effective, stored_options_invalid = await self._async_effective_options()
        defaults = (
            await async_get_profile_registry(self.hass)
        ).for_model(self.config_entry.data[CONF_MODEL]).custom_auto_defaults
        errors: dict[str, str] = (
            {"base": "stored_options_invalid"}
            if stored_options_invalid and user_input is None
            else {}
        )
        if user_input is not None:
            normalized = _normalize_settings(user_input)
            try:
                replacement = parse_custom_auto_options(normalized, defaults)
            except CustomAutoOptionsError as err:
                errors["base"] = _settings_error(err)
            else:
                if normalized == self.config_entry.options:
                    return self.async_create_entry(title="", data=normalized)
                if not await self._async_handoff_active_controller():
                    return self.async_show_form(
                        step_id="custom_auto_settings",
                        data_schema=_settings_schema(replacement),
                        errors={"base": "custom_auto_handoff_failed"},
                    )
                self._async_install_setup_error_repair_listener()
                return self.async_create_entry(title="", data=normalized)
        return self.async_show_form(
            step_id="custom_auto_settings",
            data_schema=_settings_schema(effective),
            errors=errors,
        )
