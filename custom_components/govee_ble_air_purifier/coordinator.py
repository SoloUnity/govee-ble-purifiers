"""Home Assistant coordinator for a connected Govee BLE air purifier."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bluetooth import GattTransport, HomeAssistantBluetoothEnvironment
from .client import PurifierClientError, ReliablePurifierClient
from .models import (
    DeviceProfile,
    FanMode,
    Model,
    ProtocolCommand,
    PurifierState,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
)
from .protocol import GoveePurifierProtocol

_LOGGER = logging.getLogger(__name__)


class GoveeDataUpdateCoordinator(DataUpdateCoordinator[PurifierState]):
    """Distribute the client's cached push state to Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        address: str,
        model: Model,
        name: str | None = None,
    ) -> None:
        self.address = address
        self.model = Model(model)
        self.profile = DeviceProfile.for_model(self.model)
        self.name = name or f"Govee {self.model.value}"

        super().__init__(
            hass,
            _LOGGER,
            name=f"{self.name} Bluetooth",
            update_interval=None,
        )
        self.data = PurifierState()
        self._shutdown = False
        self._client_available = False

        environment = HomeAssistantBluetoothEnvironment(hass, address)
        transport = GattTransport(name=self.name)
        protocol = GoveePurifierProtocol(self.profile)
        self.client = ReliablePurifierClient(
            environment=environment,
            transport=transport,
            protocol=protocol,
            profile=self.profile,
            state_callback=self._state_updated,
            availability_callback=self._availability_updated,
        )

    async def async_start(self) -> None:
        """Start persistent Bluetooth recovery in the background."""
        await self.client.async_start()

    async def async_wait_until_ready(self) -> None:
        """Wait for essential state during explicit setup validation."""
        await self.client.async_wait_until_ready()

    @property
    def client_available(self) -> bool:
        """Return whether essential initialization completed and is connected."""
        return self._client_available

    async def async_shutdown(self) -> None:
        """Cancel recovery and close any active BLE connection."""
        if self._shutdown:
            return
        self._shutdown = True
        await self.client.async_shutdown()
        await super().async_shutdown()

    async def _async_update_data(self) -> PurifierState:
        """Return cached push state without introducing another poll."""
        if not self.client.is_ready:
            raise UpdateFailed("Purifier Bluetooth connection is not ready")
        return self.client.state

    async def async_set_power(self, on: bool) -> None:
        """Set purifier power and wait for matching applied state."""
        await self._async_execute(SetPower(bool(on)))

    async def async_set_fan_mode(self, mode: FanMode) -> None:
        """Set a documented fan mode and wait for its exact acknowledgement."""
        await self._async_execute(SetFanMode(FanMode(mode)))

    async def async_set_light_power(self, on: bool) -> None:
        """Set night-light power."""
        await self._async_execute(SetNightLightPower(bool(on)))

    async def async_set_light_brightness(self, percent: int) -> None:
        """Set night-light brightness from one through one hundred percent."""
        await self._async_execute(SetNightLightBrightness(percent))

    async def async_set_light_rgb(self, rgb: tuple[int, int, int]) -> None:
        """Set the night-light RGB color."""
        red, green, blue = rgb
        await self._async_execute(SetNightLightColor(red, green, blue))

    async def _async_execute(self, command: ProtocolCommand) -> None:
        try:
            await self.client.async_execute(command)
        except (PurifierClientError, ValueError) as err:
            _LOGGER.error(
                "Purifier control failed: name=%s command=%s error=%s",
                self.name,
                repr(command),
                err,
            )
            raise HomeAssistantError(
                f"Unable to apply {type(command).__name__} to {self.name}: {err}"
            ) from err

    def _state_updated(self, state: PurifierState) -> None:
        # During (re)initialization, collect state privately until the full
        # startup sequence has been attempted and essential state is known.
        # Exhausted secondary requests do not prevent later publication.
        if self.client.is_ready:
            self.async_set_updated_data(state)

    def _availability_updated(self, available: bool, error: Exception | None) -> None:
        self._client_available = available
        if available:
            self.async_set_updated_data(self.client.state)
            return
        if error is not None and not isinstance(error, ConnectionError | TimeoutError):
            _LOGGER.error(
                "Unexpected purifier recovery failure: name=%s error=%s",
                self.name,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            _LOGGER.debug(
                "Purifier is unavailable while Bluetooth recovery continues: "
                "name=%s error=%s",
                self.name,
                str(error) if error is not None else None,
            )
        self.async_update_listeners()
