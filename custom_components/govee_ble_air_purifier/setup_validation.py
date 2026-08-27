"""Temporary connection validation for explicit purifier setup."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .bluetooth_profile import bluetooth_settings_from_profile
from .coordinator import GoveeDataUpdateCoordinator
from .profiles import ProfileRegistry


class PurifierSetupValidator:
    """Validate a purifier without retaining setup-time runtime resources."""

    def __init__(self, hass: HomeAssistant, registry: ProfileRegistry) -> None:
        """Bind validation to one Home Assistant instance and profile snapshot."""
        self._hass = hass
        self._registry = registry

    async def async_validate(
        self,
        *,
        address: str,
        model: str,
        name: str | None,
    ) -> None:
        """Connect and complete initialization before storing the entry."""
        profile = self._registry.for_model(model)
        coordinator = GoveeDataUpdateCoordinator(
            self._hass,
            address=address,
            profile=profile,
            bluetooth_settings=bluetooth_settings_from_profile(profile),
            name=name,
        )
        try:
            await coordinator.async_start()
            await coordinator.async_wait_until_ready()
        finally:
            await coordinator.async_shutdown()
