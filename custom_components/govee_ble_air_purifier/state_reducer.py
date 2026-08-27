"""Deterministic purifier-state reduction from decoded protocol events."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    AirQualityEvent,
    DecodedEvent,
    DeviceStateEvent,
    FanMode,
    FanModeEvent,
    NightLightColorEvent,
    NightLightStateEvent,
    ProtocolCommand,
    PurifierState,
    RefreshRequestedEvent,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
    StartupFanModeEvent,
)
from .profiles import DeviceProfile


@dataclass(frozen=True, slots=True)
class StateReduction:
    """State and orchestration effects produced by one reduction."""

    state: PurifierState
    state_changed: bool = False
    refresh_requested: bool = False


class PurifierStateReducer:
    """Own cached state and apply deterministic state-authority rules."""

    def __init__(self, profile: DeviceProfile) -> None:
        self._startup_mode_strategy = profile.protocol.startup_mode_strategy
        self._refresh_supported = profile.capabilities.refresh
        self._state = PurifierState()
        self._last_startup_mode_code: int | None = None
        self._last_startup_manual_level: int | None = None
        self._last_startup_selector_01_value: int | None = None
        self._last_startup_auto_parameter: int | None = None
        self._startup_mode_generation: int | None = None
        self._awaiting_h7129_manual_level = False
        self._startup_mode_resolution = "not_observed"

    @property
    def state(self) -> PurifierState:
        """Return the current authoritative cached state."""
        return self._state

    def replace_state(self, state: PurifierState) -> None:
        """Replace cached state without inferring additional authority."""
        self._state = state

    def reduce_event(
        self,
        event: DecodedEvent,
        *,
        generation: int,
        matched_request: str | None = None,
    ) -> StateReduction:
        """Reduce one decoded frame and return state/scheduling effects."""
        previous = self._state
        new_state = previous
        refresh_requested = False

        if isinstance(event, DeviceStateEvent) and event.power is not None:
            new_state = replace(previous, power=event.power)
        elif isinstance(event, StartupFanModeEvent):
            new_state = self._apply_startup_fan_mode_event(
                event,
                generation=generation,
                matched_request=matched_request,
            )
        elif isinstance(event, FanModeEvent):
            self._clear_startup_mode_partial()
            self._startup_mode_resolution = "superseded_by_physical_update"
            self._startup_mode_generation = generation
            new_state = replace(previous, fan_mode=event.mode)
        elif isinstance(event, NightLightStateEvent):
            changes: dict[str, object] = {}
            if event.power is not None:
                changes["light_power"] = event.power
            if 1 <= event.brightness <= 100:
                changes["light_brightness"] = event.brightness
            if changes:
                new_state = replace(previous, **changes)
        elif (
            isinstance(event, NightLightColorEvent)
            and event.color_available
            and not event.acknowledgement_only
        ):
            assert event.red is not None
            assert event.green is not None
            assert event.blue is not None
            new_state = replace(
                previous,
                light_rgb=(event.red, event.green, event.blue),
            )
        elif isinstance(event, AirQualityEvent):
            new_state = replace(
                previous,
                pm25=event.pm25_ug_m3,
                filter_life=event.filter_life,
            )
        elif isinstance(event, RefreshRequestedEvent):
            refresh_requested = self._refresh_supported

        self._state = new_state
        return StateReduction(
            state=new_state,
            state_changed=new_state != previous,
            refresh_requested=refresh_requested,
        )

    def apply_confirmed_command(
        self,
        command: ProtocolCommand,
        *,
        generation: int,
    ) -> StateReduction:
        """Apply state established by a documented command acknowledgement."""
        previous = self._state
        if isinstance(command, SetFanMode):
            self._clear_startup_mode_partial()
            self._startup_mode_resolution = "superseded_by_command_acknowledgement"
            self._startup_mode_generation = generation
            self._state = replace(previous, fan_mode=command.mode)
        return StateReduction(
            state=self._state,
            state_changed=self._state != previous,
        )

    def command_is_satisfied(self, command: ProtocolCommand) -> bool:
        """Return whether authoritative cached state confirms a command."""
        if isinstance(command, SetPower):
            return self._state.power is command.on
        if isinstance(command, SetFanMode):
            return self._state.fan_mode is command.mode
        if isinstance(command, SetNightLightPower):
            return self._state.light_power is command.on
        if isinstance(command, SetNightLightBrightness):
            return self._state.light_brightness == command.percent
        if isinstance(command, SetNightLightColor):
            # H7129 may answer the startup color query with the value-less fc
            # form, while 3a color frames are acknowledgement echoes only.
            # Cached RGB therefore cannot safely suppress an ambiguous retry.
            return False
        return False

    def invalidate_connection(self) -> StateReduction:
        """Forget authority that must be re-established after reconnect."""
        previous = self._state
        self._clear_startup_mode_partial()
        self._startup_mode_resolution = "awaiting_startup_query"
        self._state = replace(previous, fan_mode=None)
        return StateReduction(
            state=self._state,
            state_changed=self._state != previous,
        )

    def startup_fan_diagnostics(self) -> dict[str, object]:
        """Return secret-free startup fan-mode assembly evidence."""
        return {
            "last_mode_code": self._last_startup_mode_code,
            "last_manual_level": self._last_startup_manual_level,
            "last_selector_01_value": self._last_startup_selector_01_value,
            "last_auto_parameter": self._last_startup_auto_parameter,
            "awaiting_h7129_manual_level": self._awaiting_h7129_manual_level,
            "resolved_mode": (
                self._state.fan_mode.value
                if self._state.fan_mode is not None
                else None
            ),
            "resolution": self._startup_mode_resolution,
            "generation": self._startup_mode_generation,
        }

    def _apply_startup_fan_mode_event(
        self,
        event: StartupFanModeEvent,
        *,
        generation: int,
        matched_request: str | None,
    ) -> PurifierState:
        expected_request = f"mode_data_{event.selector:02x}"
        if matched_request != expected_request or expected_request not in {
            "mode_data_00",
            "mode_data_01",
            "mode_data_03",
        }:
            return self._state

        if event.selector == 0x03:
            self._startup_mode_generation = generation
            self._last_startup_auto_parameter = event.auto_parameter
            return self._state

        if event.selector == 0x01:
            self._last_startup_selector_01_value = event.level_or_configuration
            if self._startup_mode_strategy != "h7129_selector_pair":
                self._startup_mode_generation = generation
                return self._state
            completes_current_pair = (
                self._awaiting_h7129_manual_level
                and self._startup_mode_generation == generation
            )
            self._startup_mode_generation = generation
            if not completes_current_pair:
                return self._state

            self._last_startup_manual_level = event.level_or_configuration
            mode = {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(event.level_or_configuration)
            self._awaiting_h7129_manual_level = False
            if mode is None:
                self._startup_mode_resolution = (
                    f"unknown_manual_level:{event.level_or_configuration}"
                )
                return replace(self._state, fan_mode=None)
            self._startup_mode_resolution = f"resolved:{mode.value}"
            return replace(self._state, fan_mode=mode)

        assert event.selector == 0x00
        self._last_startup_mode_code = event.mode_code
        self._last_startup_manual_level = event.manual_level
        self._clear_startup_mode_partial()
        self._startup_mode_generation = generation

        if self._startup_mode_strategy == "h7124_selector_00":
            mode = self._decode_h7124_startup_mode(
                event.mode_code,
                event.manual_level,
            )
            if mode is None:
                self._startup_mode_resolution = (
                    f"unknown_h7124_combination:{event.mode_code}:"
                    f"{event.manual_level}"
                )
            else:
                self._startup_mode_resolution = f"resolved:{mode.value}"
            return replace(self._state, fan_mode=mode)

        if event.mode_code == 0x01:
            self._awaiting_h7129_manual_level = True
            self._startup_mode_resolution = "awaiting_manual_level"
            return replace(self._state, fan_mode=None)

        mode = {
            0x03: FanMode.AUTO,
            0x05: FanMode.SLEEP,
            0x07: FanMode.TURBO,
        }.get(event.mode_code)
        if mode is None:
            self._startup_mode_resolution = (
                f"unknown_h7129_mode_code:{event.mode_code}"
            )
        else:
            self._startup_mode_resolution = f"resolved:{mode.value}"
        return replace(self._state, fan_mode=mode)

    @staticmethod
    def _decode_h7124_startup_mode(
        mode_code: int | None,
        manual_level: int | None,
    ) -> FanMode | None:
        if mode_code == 0x01:
            return {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(manual_level)
        if manual_level != 0x00:
            return None
        return {
            0x03: FanMode.AUTO,
            0x05: FanMode.SLEEP,
            0x07: FanMode.TURBO,
        }.get(mode_code)

    def _clear_startup_mode_partial(self) -> None:
        self._awaiting_h7129_manual_level = False
