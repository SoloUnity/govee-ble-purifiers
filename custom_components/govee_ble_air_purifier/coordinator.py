"""Home Assistant coordinator for a connected Govee BLE air purifier."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bluetooth import (
    BluetoothRuntimeSettings,
    GattTransport,
    HomeAssistantBluetoothEnvironment,
)
from .client import PurifierClientError, ReliablePurifierClient
from .custom_auto_controller import CustomAutoController, CustomAutoSnapshot
from .custom_auto_options import CustomAutoOptions, parse_custom_auto_options
from .models import (
    FanMode,
    ProtocolCommand,
    PurifierState,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
)
from .observations import (
    AirQualityObservation,
    CommandOrigin,
    FanModeObservation,
    PurifierObservation,
)
from .profiles import DeviceProfile
from .protocol import GoveePurifierProtocol

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CustomAutoHandoffSnapshot:
    """Cached, bounded switch-off handoff outcome."""

    state: str = "idle"
    error_type: str | None = None
    reason: str | None = None


class GoveeDataUpdateCoordinator(DataUpdateCoordinator[PurifierState]):
    """Distribute the client's cached push state to Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        address: str,
        profile: DeviceProfile,
        bluetooth_settings: BluetoothRuntimeSettings,
        name: str | None = None,
        custom_auto_options: CustomAutoOptions | None = None,
    ) -> None:
        self.address = address
        self.profile = profile
        self.bluetooth_settings = bluetooth_settings
        self.model = profile.model
        self.name = name or profile.identity.display_name

        super().__init__(
            hass,
            _LOGGER,
            name=f"{self.name} Bluetooth",
            update_interval=None,
        )
        self.data = PurifierState()
        self._shutdown = False
        self._client_available = False
        self._observation_listeners: set[
            Callable[[PurifierObservation], object]
        ] = set()
        self._publishing_observation = False
        self._started = False
        self._custom_auto_observation_remove: Callable[[], None] | None = None
        self._custom_auto_state_remove: Callable[[], None] | None = None
        self._custom_auto_listeners: set[
            Callable[[CustomAutoSnapshot], None]
        ] = set()
        self._custom_auto_handoff = CustomAutoHandoffSnapshot()
        self._custom_auto_control_lock = asyncio.Lock()
        self._custom_auto_options = custom_auto_options or parse_custom_auto_options(
            {}, profile.custom_auto_defaults
        )

        environment = HomeAssistantBluetoothEnvironment(
            hass, address, bluetooth_settings
        )
        transport = GattTransport(name=self.name, settings=bluetooth_settings)
        protocol = GoveePurifierProtocol(self.profile)
        self.client = ReliablePurifierClient(
            environment=environment,
            transport=transport,
            protocol=protocol,
            profile=self.profile,
            state_callback=self._state_updated,
            availability_callback=self._availability_updated,
            observation_callback=self._observation_updated,
        )
        self.custom_auto_controller = (
            CustomAutoController(
                self._custom_auto_options,
                request_sample=self.async_query_air_quality,
                cancel_sample=self.async_cancel_air_quality_query,
                send_fan_mode=self._async_custom_auto_fan_mode,
                clock=lambda: asyncio.get_running_loop().time(),
            )
            if self._custom_auto_options.enabled
            else None
        )

    async def async_start(self) -> None:
        """Start persistent Bluetooth recovery in the background."""
        if self._started:
            return
        if self._shutdown:
            raise RuntimeError("Purifier coordinator is already shut down")
        controller = self.custom_auto_controller
        if controller is not None:
            await controller.start()
            self._custom_auto_state_remove = controller.add_state_listener(
                self._custom_auto_state_updated
            )
            self._custom_auto_observation_remove = self.add_observation_listener(
                self._route_custom_auto_observation
            )
            controller.set_connection(
                available=self._client_available,
                generation=self.client.connection_generation,
            )
            if self.data.power is not None:
                controller.set_powered(self.data.power)
        self._started = True
        try:
            await self.client.async_start()
        except Exception:
            self._started = False
            if self._custom_auto_observation_remove is not None:
                self._custom_auto_observation_remove()
                self._custom_auto_observation_remove = None
            if controller is not None:
                await controller.shutdown()
            if self._custom_auto_state_remove is not None:
                self._custom_auto_state_remove()
                self._custom_auto_state_remove = None
            raise

    async def async_wait_until_ready(self) -> None:
        """Wait for essential state during explicit setup validation."""
        await self.client.async_wait_until_ready()

    @property
    def client_available(self) -> bool:
        """Return whether essential initialization completed and is connected."""
        return self._client_available

    @property
    def custom_auto_snapshot(self) -> CustomAutoSnapshot | None:
        """Return the cached controller snapshot without performing I/O."""
        controller = self.custom_auto_controller
        return controller.snapshot if controller is not None else None

    @property
    def custom_auto_handoff(self) -> CustomAutoHandoffSnapshot:
        """Return the latest bounded switch-off handoff outcome."""
        return self._custom_auto_handoff

    def custom_auto_diagnostic_snapshot(
        self, *, now: float | None = None
    ) -> dict[str, Any]:
        """Return bounded cached Custom Auto diagnostics without performing I/O."""
        options = self._custom_auto_options
        controller = self.custom_auto_controller
        snapshot = controller.snapshot if controller is not None else None
        if now is None:
            now = asyncio.get_running_loop().time()

        def age(observed_at: float | None) -> float | None:
            if (
                observed_at is None
                or isinstance(observed_at, bool)
                or not isinstance(observed_at, int | float)
                or not math.isfinite(observed_at)
                or isinstance(now, bool)
                or not isinstance(now, int | float)
                or not math.isfinite(now)
            ):
                return None
            return max(0.0, float(now) - float(observed_at))

        def provenance(value: Any) -> dict[str, object] | None:
            if value is None:
                return None
            return {
                "mode": value.mode.value,
                "source": value.source,
                "purpose": value.purpose,
                "revision": value.revision,
                "connection_generation": value.connection_generation,
                "command_origin": value.command_origin,
            }

        task_counts = {
            "actor": snapshot.actor_tasks if snapshot is not None else 0,
            "sample": snapshot.sample_tasks if snapshot is not None else 0,
            "timer": snapshot.timer_tasks if snapshot is not None else 0,
            "command": snapshot.command_tasks if snapshot is not None else 0,
        }
        listener_counts = {
            "controller_state": (
                snapshot.controller_listeners if snapshot is not None else 0
            ),
            "coordinator_state": len(self._custom_auto_listeners),
            "observation_total": len(self._observation_listeners),
            "custom_auto_observation": int(
                self._custom_auto_observation_remove is not None
            ),
        }
        upshift = None
        downshifts: list[dict[str, object]] = []
        if snapshot is not None and snapshot.pending_upshift is not None:
            pending = snapshot.pending_upshift
            delay = options.upshift_confirmation_seconds
            elapsed = age(pending.started_at)
            upshift = {
                "revision": pending.revision,
                "sample_requested": pending.sample_requested,
                "delay_seconds": delay,
                "elapsed_seconds": elapsed,
                "remaining_seconds": (
                    max(0.0, delay - elapsed) if elapsed is not None else None
                ),
            }
        if snapshot is not None:
            for pending in snapshot.pending_downshifts:
                delay = options.downshift_delays_minutes[
                    pending.target_level - 1
                ] * 60
                elapsed = age(pending.started_at)
                downshifts.append(
                    {
                        "target_level": pending.target_level,
                        "target_mode": pending.target_mode.value,
                        "revision": pending.revision,
                        "sample_requested": pending.sample_requested,
                        "delay_seconds": delay,
                        "elapsed_seconds": elapsed,
                        "remaining_seconds": (
                            max(0.0, delay - elapsed)
                            if elapsed is not None
                            else None
                        ),
                    }
                )

        handoff = self._custom_auto_handoff
        return {
            "exposed": options.enabled,
            "enabled": options.enabled,
            "controller_present": snapshot is not None,
            "active": snapshot.active if snapshot is not None else False,
            "suspended": snapshot.suspended if snapshot is not None else False,
            "available": snapshot.available if snapshot is not None else False,
            "powered": snapshot.powered if snapshot is not None else False,
            "sampling_policy": {
                "strategy": "event_plus_bounded_one_shot",
                "fixed_cadence": False,
                "positive_upshift_distinct_revisions": 2,
                "zero_delay_first_sample": True,
                "downshift_requires_fresh_matured_sample": True,
            },
            "underlying_fan_mode": (
                self.data.fan_mode.value if self.data.fan_mode is not None else None
            ),
            "last_accepted_pm25": {
                "value": snapshot.last_pm25 if snapshot is not None else None,
                "revision": (
                    snapshot.last_pm25_revision if snapshot is not None else None
                ),
                "connection_generation": (
                    snapshot.last_pm25_connection_generation
                    if snapshot is not None
                    else None
                ),
                "age_seconds": (
                    age(snapshot.last_pm25_observed_at)
                    if snapshot is not None
                    else None
                ),
            },
            "confirmed_level": (
                snapshot.confirmed_level if snapshot is not None else None
            ),
            "confirmed_mode": (
                snapshot.confirmed_mode.value
                if snapshot is not None and snapshot.confirmed_mode is not None
                else None
            ),
            "pending_target": (
                snapshot.pending_target.value
                if snapshot is not None and snapshot.pending_target is not None
                else None
            ),
            "effective_settings": {
                "pm25_boundaries": list(options.pm25_boundaries),
                "upshift_confirmation_seconds": (
                    options.upshift_confirmation_seconds
                ),
                "downshift_delays_minutes": list(
                    options.downshift_delays_minutes
                ),
            },
            "pending_upshift_confirmation": upshift,
            "pending_downshift_dwells": downshifts,
            "command": {
                "state": snapshot.command_state if snapshot is not None else "idle",
                "last_error_type": (
                    snapshot.last_error_type if snapshot is not None else None
                ),
            },
            "fan_provenance": {
                "last_physical": provenance(
                    snapshot.last_physical_fan if snapshot is not None else None
                ),
                "last_command": provenance(
                    snapshot.last_command_fan if snapshot is not None else None
                ),
            },
            "physical_auto_redirect": {
                "state": (
                    snapshot.auto_redirect_state
                    if snapshot is not None
                    else "idle"
                ),
                "last_error_type": (
                    snapshot.last_error_type
                    if snapshot is not None
                    and snapshot.auto_redirect_state == "failed"
                    else None
                ),
            },
            "handoff": {
                "state": handoff.state,
                "reason": handoff.reason,
                "error_type": handoff.error_type,
            },
            "generations": {
                "configuration": (
                    snapshot.configuration_generation
                    if snapshot is not None
                    else None
                ),
                "activation": (
                    snapshot.activation_generation if snapshot is not None else None
                ),
                "connection": (
                    snapshot.connection_generation if snapshot is not None else None
                ),
            },
            "task_counts": task_counts,
            "listener_counts": listener_counts,
        }

    def add_custom_auto_listener(
        self, listener: Callable[[CustomAutoSnapshot], None]
    ) -> Callable[[], None]:
        """Register a synchronous listener for cached controller state."""
        self._custom_auto_listeners.add(listener)

        def remove() -> None:
            self._custom_auto_listeners.discard(listener)

        return remove

    async def async_shutdown(self) -> None:
        """Cancel recovery and close any active BLE connection."""
        if self._shutdown:
            return
        self._shutdown = True
        if self._custom_auto_observation_remove is not None:
            self._custom_auto_observation_remove()
            self._custom_auto_observation_remove = None
        self._started = False
        controller = self.custom_auto_controller
        if controller is not None:
            await controller.shutdown()
        if self._custom_auto_state_remove is not None:
            self._custom_auto_state_remove()
            self._custom_auto_state_remove = None
        self._custom_auto_listeners.clear()
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

    async def async_set_fan_mode(
        self,
        mode: FanMode,
        *,
        origin: CommandOrigin = CommandOrigin.HOME_ASSISTANT,
    ) -> None:
        """Set a documented fan mode and wait for its exact acknowledgement."""
        if origin is CommandOrigin.HOME_ASSISTANT:
            await self.async_apply_ha_fan_mode(mode, power_on=False)
            return
        await self._async_execute(SetFanMode(FanMode(mode)), origin=origin)

    async def async_apply_ha_fan_mode(
        self, mode: FanMode, *, power_on: bool
    ) -> None:
        """Linearize ownership yield, optional power-on, and explicit HA mode."""
        async with self._custom_auto_control_lock:
            controller = self.custom_auto_controller
            if controller is not None:
                await controller.ha_override()
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="superseded",
                    reason="ownership_yielded_to_ha",
                )
                self._publish_custom_auto_state()
            if power_on:
                await self._async_execute(SetPower(True))
            await self._async_execute(SetFanMode(FanMode(mode)))

    async def async_activate_custom_auto(self) -> None:
        """Activate policy ownership without relying on cached PM2.5."""
        controller = self.custom_auto_controller
        if controller is None:
            raise HomeAssistantError("Custom Auto is not enabled for this purifier")
        async with self._custom_auto_control_lock:
            self._custom_auto_handoff = CustomAutoHandoffSnapshot()
            await controller.activate()

    async def async_deactivate_custom_auto(self) -> None:
        """Clear policy intent, then hand powered-on hardware to Auto."""
        controller = self.custom_auto_controller
        if controller is None:
            raise HomeAssistantError("Custom Auto is not enabled for this purifier")
        async with self._custom_auto_control_lock:
            was_active = controller.snapshot.active
            retrying_not_attempted = (
                not was_active
                and self._custom_auto_handoff.state
                in {
                    "not_attempted_unknown_power",
                    "not_attempted_unavailable",
                }
            )
            await controller.deactivate()
            if not was_active and not retrying_not_attempted:
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="superseded",
                    reason="ownership_already_yielded",
                )
                self._publish_custom_auto_state()
                return
            if self.data.power is False:
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="not_required",
                    reason="powered_off",
                )
                self._publish_custom_auto_state()
                return
            if self.data.power is None:
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="not_attempted_unknown_power",
                    reason="power_unknown",
                )
                self._publish_custom_auto_state()
                return
            if not self._client_available or not self.client.is_ready:
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="not_attempted_unavailable",
                    reason="unavailable",
                )
                self._publish_custom_auto_state()
                return
            self._custom_auto_handoff = CustomAutoHandoffSnapshot(state="pending")
            self._publish_custom_auto_state()
            try:
                await self._async_execute(
                    SetFanMode(FanMode.AUTO),
                    origin=CommandOrigin.HANDOFF,
                )
            except Exception as err:
                cause = err.__cause__ or err
                self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                    state="failed",
                    error_type=type(cause).__name__,
                )
                self._publish_custom_auto_state()
                raise
            self._custom_auto_handoff = CustomAutoHandoffSnapshot(
                state="confirmed"
            )
            self._publish_custom_auto_state()

    async def async_query_air_quality(self) -> None:
        """Request one coalesced, preemptible air-quality observation."""
        if self._publishing_observation:
            raise RuntimeError("Observation listeners cannot issue nested I/O")
        try:
            await self.client.async_query_air_quality()
        except PurifierClientError as err:
            raise HomeAssistantError(
                f"Unable to query air quality from {self.name}: {err}"
            ) from err

    async def async_cancel_air_quality_query(self) -> None:
        """Deactivate one-shot work after transaction ownership is released."""
        if self._publishing_observation:
            raise RuntimeError("Observation listeners cannot issue nested I/O")
        await self.client.async_cancel_air_quality_query()

    def add_observation_listener(
        self, listener: Callable[[PurifierObservation], object]
    ) -> Callable[[], None]:
        """Register a synchronous, non-I/O semantic observation listener."""
        if inspect.iscoroutinefunction(listener):
            raise TypeError("Observation listeners must be synchronous")
        self._observation_listeners.add(listener)

        def remove() -> None:
            self._observation_listeners.discard(listener)

        return remove

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

    async def _async_execute(
        self,
        command: ProtocolCommand,
        *,
        origin: CommandOrigin = CommandOrigin.HOME_ASSISTANT,
    ) -> None:
        if self._publishing_observation:
            raise RuntimeError("Observation listeners cannot issue nested I/O")
        try:
            if origin is CommandOrigin.HOME_ASSISTANT:
                await self.client.async_execute(command)
            else:
                await self.client.async_execute(command, origin=origin)
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

    async def _async_custom_auto_fan_mode(
        self, mode: FanMode, origin: CommandOrigin
    ) -> None:
        """Route policy output through the existing reliable command path."""
        if origin is not CommandOrigin.CUSTOM_AUTO:
            raise ValueError("Custom Auto controller used an invalid command origin")
        await self.async_set_fan_mode(mode, origin=origin)

    def _state_updated(self, state: PurifierState) -> None:
        # During (re)initialization, collect state privately until the full
        # startup sequence has been attempted and essential state is known.
        # Exhausted secondary requests do not prevent later publication.
        if self.client.is_ready:
            self.async_set_updated_data(state)
        controller = self.custom_auto_controller
        if self._started and controller is not None and state.power is not None:
            controller.set_powered(state.power)

    def _availability_updated(self, available: bool, error: Exception | None) -> None:
        self._client_available = available
        controller = self.custom_auto_controller
        if self._started and controller is not None:
            controller.set_connection(
                available=available,
                generation=self.client.connection_generation,
            )
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

    def _route_custom_auto_observation(
        self, observation: PurifierObservation
    ) -> None:
        """Synchronously enqueue typed controller ingress without nested I/O."""
        controller = self.custom_auto_controller
        if controller is None:
            return
        if isinstance(observation, AirQualityObservation):
            controller.observe_air_quality(observation)
        elif isinstance(observation, FanModeObservation):
            controller.observe_fan_mode(observation)

    def _custom_auto_state_updated(self, _: CustomAutoSnapshot) -> None:
        """Fan out controller cached-state changes synchronously."""
        self._publish_custom_auto_state()

    def _publish_custom_auto_state(self) -> None:
        snapshot = self.custom_auto_snapshot
        if snapshot is None:
            return
        for listener in tuple(self._custom_auto_listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - state listeners are isolated
                continue

    def _observation_updated(self, observation: PurifierObservation) -> None:
        """Synchronously fan out one immutable event without permitting awaits."""
        if self._publishing_observation:
            raise RuntimeError("Nested observation publication is not allowed")
        self._publishing_observation = True
        try:
            for listener in tuple(self._observation_listeners):
                try:
                    result = listener(observation)
                    if inspect.isawaitable(result):
                        close = getattr(result, "close", None)
                        if close is not None:
                            close()
                        else:
                            cancel = getattr(result, "cancel", None)
                            if cancel is not None:
                                cancel()
                        raise TypeError("Observation listeners cannot await")
                except Exception as err:  # noqa: BLE001 - isolate extensions
                    _LOGGER.error(
                        "Purifier observation listener failed: name=%s error=%s",
                        self.name,
                        err,
                        exc_info=(type(err), err, err.__traceback__),
                    )
        finally:
            self._publishing_observation = False
