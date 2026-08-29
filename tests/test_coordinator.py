"""Tests for quiet coordinator availability transitions."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.govee_ble_air_purifier import coordinator as coordinator_module
from custom_components.govee_ble_air_purifier.bluetooth_profile import (
    bluetooth_settings_from_profile,
)
from custom_components.govee_ble_air_purifier.coordinator import (
    GoveeDataUpdateCoordinator,
)
from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    parse_custom_auto_options,
)
from custom_components.govee_ble_air_purifier.custom_auto_policy import (
    DownshiftDwell,
    UpshiftConfirmation,
)
from custom_components.govee_ble_air_purifier.models import (
    FanMode,
    Model,
    PurifierState,
    SetFanMode,
    SetPower,
)
from custom_components.govee_ble_air_purifier.observations import (
    AirQualityObservation,
    CommandOrigin,
    FanModeObservation,
    ObservationPurpose,
    ObservationSource,
    PurifierObservation,
)
from custom_components.govee_ble_air_purifier.operations import PurifierClientError
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile


def _coordinator() -> GoveeDataUpdateCoordinator:
    coordinator = object.__new__(GoveeDataUpdateCoordinator)
    coordinator.name = "Bedroom purifier"
    coordinator.data = PurifierState()
    coordinator._client_available = True
    coordinator._observation_listeners = set()
    coordinator._publishing_observation = False
    coordinator._started = False
    coordinator.custom_auto_controller = None
    coordinator.client = SimpleNamespace(state=PurifierState(power=True))
    coordinator.async_update_listeners = Mock()
    coordinator.async_set_updated_data = Mock()
    return coordinator


def test_unavailable_transition_notifies_without_update_error() -> None:
    """Expected weak-signal recovery only changes entity availability."""
    coordinator = _coordinator()

    with patch.object(coordinator_module._LOGGER, "error") as error_log:
        coordinator._availability_updated(False, ConnectionError("weak signal"))

    assert not coordinator.client_available
    error_log.assert_not_called()
    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.async_set_updated_data.assert_not_called()


def test_ready_transition_publishes_initialized_state() -> None:
    """Successful recovery republishes state and restores availability."""
    coordinator = _coordinator()
    coordinator._client_available = False

    coordinator._availability_updated(True, None)

    assert coordinator.client_available
    coordinator.async_set_updated_data.assert_called_once_with(
        coordinator.client.state
    )
    coordinator.async_update_listeners.assert_not_called()


def test_unexpected_recovery_failure_remains_visible() -> None:
    """Quiet link recovery cannot hide an unexpected implementation failure."""
    coordinator = _coordinator()

    with patch.object(coordinator_module._LOGGER, "error") as error_log:
        coordinator._availability_updated(False, RuntimeError("unexpected"))

    error_log.assert_called_once()
    coordinator.async_update_listeners.assert_called_once_with()


def test_observation_fanout_is_synchronous_removable_and_non_awaiting() -> None:
    """Coordinator listeners run inline and cannot be coroutine functions."""
    coordinator = _coordinator()
    observation = AirQualityObservation(
        revision=1,
        generation=2,
        observed_at=3.0,
        source=ObservationSource.QUERY,
        purpose=ObservationPurpose.ONE_SHOT,
        pm25=4,
        filter_life=99,
    )
    received: list[PurifierObservation] = []
    remove = coordinator.add_observation_listener(received.append)

    coordinator._observation_updated(observation)
    remove()
    coordinator._observation_updated(observation)

    assert received == [observation]

    async def asynchronous(_: object) -> None:
        await asyncio.sleep(0)

    try:
        coordinator.add_observation_listener(asynchronous)
    except TypeError as err:
        assert "synchronous" in str(err)
    else:
        raise AssertionError("async listener was accepted")


def test_observation_listener_failures_are_isolated_and_fanout_continues() -> None:
    """Bad listeners cannot propagate into a client transaction or block peers."""
    coordinator = _coordinator()
    observation = AirQualityObservation(
        revision=2,
        generation=3,
        observed_at=4.0,
        source=ObservationSource.DEVICE,
        purpose=ObservationPurpose.UNSOLICITED,
        pm25=5,
        filter_life=98,
    )
    received: list[PurifierObservation] = []

    def raises(_: PurifierObservation) -> None:
        raise RuntimeError("listener broke")

    async def returned_awaitable() -> None:
        await asyncio.sleep(0)

    coordinator.add_observation_listener(raises)
    coordinator.add_observation_listener(lambda _: returned_awaitable())
    coordinator.add_observation_listener(received.append)

    with patch.object(coordinator_module._LOGGER, "error") as error_log:
        coordinator._observation_updated(observation)

    assert received == [observation]
    assert error_log.call_count == 2
    assert not coordinator._publishing_observation


def _enabled_options(profile: DeviceProfile):
    return parse_custom_auto_options(
        {
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0,
        },
        profile.custom_auto_defaults,
    )


class FakeRuntimeClient:
    """Small coordinator-facing client fake retaining call order and origins."""

    def __init__(self) -> None:
        self.state = PurifierState()
        self.is_ready = False
        self.connection_generation = 1
        self.calls: list[tuple[object, ...]] = []
        self.execute_error: Exception | None = None
        self.execute_gate: asyncio.Event | None = None
        self.start_check: Callable[[], None] | None = None
        self.shutdown_check: Callable[[], None] | None = None

    async def async_start(self) -> None:
        if self.start_check is not None:
            self.start_check()
        self.calls.append(("start",))

    async def async_shutdown(self) -> None:
        if self.shutdown_check is not None:
            self.shutdown_check()
        self.calls.append(("shutdown",))

    async def async_wait_until_ready(self) -> None:
        return

    async def async_query_air_quality(self) -> None:
        self.calls.append(("sample",))

    async def async_cancel_air_quality_query(self) -> None:
        self.calls.append(("cancel_sample",))

    async def async_execute(
        self,
        command: object,
        *,
        origin: CommandOrigin = CommandOrigin.HOME_ASSISTANT,
    ) -> None:
        self.calls.append(("execute", command, origin))
        if self.execute_gate is not None:
            await self.execute_gate.wait()
        if self.execute_error is not None:
            raise self.execute_error


def _runtime_coordinator(hass, *, enabled: bool):
    profile = DeviceProfile.for_model(Model.H7124)
    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address="AA:BB:CC:DD:EE:FF",
        profile=profile,
        bluetooth_settings=bluetooth_settings_from_profile(profile),
        custom_auto_options=_enabled_options(profile) if enabled else None,
    )
    client = FakeRuntimeClient()
    coordinator.client = client  # type: ignore[assignment]
    return coordinator, client


async def _flush(turns: int = 10) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_optional_runtime_is_inert_without_enabled_options(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=False)

    assert coordinator.custom_auto_controller is None
    assert coordinator.custom_auto_snapshot is None
    await coordinator.async_start()

    assert not coordinator._observation_listeners
    assert client.calls == [("start",)]
    await coordinator.async_shutdown()
    assert client.calls[-1] == ("shutdown",)

    profile = DeviceProfile.for_model(Model.H7124)
    disabled = parse_custom_auto_options({}, profile.custom_auto_defaults)
    disabled_coordinator = GoveeDataUpdateCoordinator(
        hass,
        address="AA:BB:CC:DD:EE:00",
        profile=profile,
        bluetooth_settings=bluetooth_settings_from_profile(profile),
        custom_auto_options=disabled,
    )
    assert disabled_coordinator.custom_auto_controller is None
    await disabled_coordinator.async_shutdown()


def test_disabled_custom_auto_diagnostics_are_complete_inert_and_profile_backed(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=False)

    diagnostics = coordinator.custom_auto_diagnostic_snapshot(now=100)

    assert diagnostics["exposed"] is False
    assert diagnostics["enabled"] is False
    assert diagnostics["controller_present"] is False
    assert diagnostics["effective_settings"] == {
        "pm25_boundaries": [3, 5, 9, 15],
        "upshift_confirmation_seconds": 3,
        "downshift_delays_minutes": [7, 5, 5, 5],
    }
    assert diagnostics["task_counts"] == {
        "actor": 0,
        "sample": 0,
        "timer": 0,
        "command": 0,
    }
    assert diagnostics["listener_counts"] == {
        "controller_state": 0,
        "coordinator_state": 0,
        "observation_total": 0,
        "custom_auto_observation": 0,
    }
    assert client.calls == []


@pytest.mark.asyncio
async def test_enabled_custom_auto_diagnostics_are_bounded_cached_and_complete(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    controller = coordinator.custom_auto_controller
    assert controller is not None
    coordinator.data = PurifierState(power=True, fan_mode=FanMode.HIGH, pm25=8)
    await coordinator.async_start()
    controller.set_connection(available=True, generation=7)
    controller.set_powered(True)
    await controller.activate()
    await _flush()

    controller._last_pm25 = 8  # noqa: SLF001 - diagnostic evidence fixture
    controller._last_pm25_revision = 42  # noqa: SLF001
    controller._last_pm25_connection_generation = 7  # noqa: SLF001
    controller._last_pm25_observed_at = 105.0  # noqa: SLF001
    controller._policy = replace(  # noqa: SLF001
        controller._policy,  # noqa: SLF001
        confirmed_level=4,
        last_revision=42,
        pending_target=FanMode.TURBO,
        upshift=UpshiftConfirmation(42, 90.0, sample_requested=True),
        downshifts=(
            DownshiftDwell(2, 40, 80.0),
            DownshiftDwell(3, 41, 85.0, sample_requested=True),
        ),
    )
    controller.observe_fan_mode(
        FanModeObservation(
            revision=50,
            generation=7,
            observed_at=99,
            source=ObservationSource.COMMAND,
            purpose=ObservationPurpose.COMMAND,
            command_origin=CommandOrigin.CUSTOM_AUTO,
            mode=FanMode.HIGH,
        )
    )
    remove_state = coordinator.add_custom_auto_listener(lambda _: None)
    remove_observation = coordinator.add_observation_listener(lambda _: None)
    await _flush()
    calls_before = list(client.calls)

    diagnostics = coordinator.custom_auto_diagnostic_snapshot(now=100)

    assert diagnostics["exposed"] is diagnostics["enabled"] is True
    assert diagnostics["controller_present"] is True
    assert diagnostics["active"] is True
    assert diagnostics["available"] is True
    assert diagnostics["powered"] is True
    assert diagnostics["suspended"] is False
    assert diagnostics["sampling_policy"] == {
        "strategy": "event_plus_bounded_one_shot",
        "fixed_cadence": False,
        "positive_upshift_distinct_revisions": 2,
        "zero_delay_first_sample": True,
        "downshift_requires_fresh_matured_sample": True,
    }
    assert diagnostics["underlying_fan_mode"] == "high"
    assert diagnostics["last_accepted_pm25"] == {
        "value": 8,
        "revision": 42,
        "connection_generation": 7,
        "age_seconds": 0.0,
    }
    assert diagnostics["confirmed_level"] == 4
    assert diagnostics["confirmed_mode"] == "high"
    assert diagnostics["pending_target"] == "turbo"
    assert diagnostics["pending_upshift_confirmation"] == {
        "revision": 42,
        "sample_requested": True,
        "delay_seconds": 0,
        "elapsed_seconds": 10.0,
        "remaining_seconds": 0.0,
    }
    assert [
        item["target_level"]
        for item in diagnostics["pending_downshift_dwells"]
    ] == [2, 3]
    assert diagnostics["fan_provenance"]["last_command"] == {
        "mode": "high",
        "source": "command",
        "purpose": "command",
        "revision": 50,
        "connection_generation": 7,
        "command_origin": "custom_auto",
    }
    assert diagnostics["generations"]["connection"] == 7
    assert diagnostics["task_counts"]["actor"] == 1
    assert diagnostics["listener_counts"] == {
        "controller_state": 1,
        "coordinator_state": 1,
        "observation_total": 2,
        "custom_auto_observation": 1,
    }
    assert diagnostics["physical_auto_redirect"]["state"] == "idle"
    assert diagnostics["handoff"] == {
        "state": "idle",
        "reason": None,
        "error_type": None,
    }
    assert client.calls == calls_before

    def assert_bounded_primitives(value: object) -> None:
        if isinstance(value, dict):
            assert len(value) <= 32
            for key, item in value.items():
                assert isinstance(key, str)
                assert_bounded_primitives(item)
        elif isinstance(value, list):
            assert len(value) <= 4
            for item in value:
                assert_bounded_primitives(item)
        else:
            assert value is None or isinstance(value, bool | int | float | str)

    assert_bounded_primitives(diagnostics)
    remove_state()
    remove_observation()
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_enabled_runtime_starts_ingress_before_client_and_shuts_down_first(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    controller = coordinator.custom_auto_controller
    assert controller is not None

    def started() -> None:
        assert controller.snapshot.actor_tasks == 1
        assert coordinator._custom_auto_observation_remove is not None

    def stopping() -> None:
        assert controller.snapshot.actor_tasks == 0
        assert coordinator._custom_auto_observation_remove is None

    client.start_check = started
    client.shutdown_check = stopping
    await coordinator.async_start()
    await coordinator.async_start()
    assert client.calls.count(("start",)) == 1

    await coordinator.async_shutdown()
    await coordinator.async_shutdown()
    assert client.calls.count(("shutdown",)) == 1
    assert controller.snapshot.actor_tasks == 0


@pytest.mark.asyncio
async def test_activation_observation_routing_and_automatic_command_origin(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()

    snapshots = []
    remove_snapshot_listener = coordinator.add_custom_auto_listener(
        snapshots.append
    )
    await coordinator.async_activate_custom_auto()
    await _flush()
    assert snapshots[-1].active
    remove_snapshot_listener()
    samples = client.calls.count(("sample",))
    assert samples == 1
    observation = AirQualityObservation(
        revision=1,
        generation=1,
        observed_at=asyncio.get_running_loop().time(),
        source=ObservationSource.QUERY,
        purpose=ObservationPurpose.ONE_SHOT,
        pm25=20,
        filter_life=90,
    )
    commands_before = sum(call[0] == "execute" for call in client.calls)
    coordinator._observation_updated(observation)
    assert sum(call[0] == "execute" for call in client.calls) == commands_before
    await _flush()

    execute = next(call for call in client.calls if call[0] == "execute")
    assert execute[1] == SetFanMode(FanMode.TURBO)
    assert execute[2] is CommandOrigin.CUSTOM_AUTO
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_physical_auto_redirects_and_manual_mode_yields_ownership(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()
    await coordinator.async_activate_custom_auto()
    await _flush()
    before = client.calls.count(("sample",))

    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=1,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.AUTO,
        )
    )
    await _flush()
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert client.calls.count(("sample",)) == before + 1

    coordinator._observation_updated(
        FanModeObservation(
            revision=2,
            generation=1,
            observed_at=2,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.HIGH,
        )
    )
    await _flush()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_deactivate_handoff_power_availability_and_failure_truth(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    await _flush()
    await coordinator.async_activate_custom_auto()
    client.execute_gate = asyncio.Event()
    deactivate = asyncio.create_task(coordinator.async_deactivate_custom_auto())
    await _flush()
    assert coordinator.custom_auto_handoff.state == "pending"
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    client.execute_gate.set()
    await deactivate
    client.execute_gate = None
    assert coordinator.custom_auto_handoff.state == "confirmed"
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.AUTO),
        CommandOrigin.HANDOFF,
    )
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]

    for power, ready, expected, reason in (
        (False, True, "not_required", "powered_off"),
        (
            None,
            True,
            "not_attempted_unknown_power",
            "power_unknown",
        ),
        (True, False, "not_attempted_unavailable", "unavailable"),
    ):
        client.calls.clear()
        coordinator.data = replace(coordinator.data, power=power)
        client.is_ready = ready
        coordinator._client_available = ready
        await coordinator.async_activate_custom_auto()
        await coordinator.async_deactivate_custom_auto()
        assert coordinator.custom_auto_handoff.state == expected
        assert coordinator.custom_auto_handoff.reason == reason
        assert not any(call[0] == "execute" for call in client.calls)

    coordinator.data = replace(coordinator.data, power=True)
    client.is_ready = True
    coordinator._client_available = True
    client.execute_error = PurifierClientError("injected handoff failure")
    await coordinator.async_activate_custom_auto()
    with pytest.raises(HomeAssistantError, match="injected handoff failure"):
        await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_handoff.state == "failed"
    assert coordinator.custom_auto_handoff.error_type == "PurifierClientError"
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_not_attempted_handoff_retries_after_power_becomes_known(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator._availability_updated(True, None)
    coordinator.data = PurifierState(power=None)
    await _flush()
    await coordinator.async_activate_custom_auto()

    await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_handoff.state == (
        "not_attempted_unknown_power"
    )
    assert not any(call[0] == "execute" for call in client.calls)

    coordinator.data = PurifierState(power=True)
    await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_handoff.state == "confirmed"
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.AUTO),
        CommandOrigin.HANDOFF,
    )
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_unavailable_handoff_repeats_terminal_result_then_retries(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    coordinator.data = PurifierState(power=True)
    client.is_ready = False
    await coordinator.async_activate_custom_auto()

    await coordinator.async_deactivate_custom_auto()
    await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_handoff.state == "not_attempted_unavailable"
    assert not any(call[0] == "execute" for call in client.calls)

    client.is_ready = True
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    await _flush()
    await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_handoff.state == "confirmed"
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.AUTO),
        CommandOrigin.HANDOFF,
    )
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_ha_superseded_inactive_state_never_retries_handoff(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    coordinator._availability_updated(True, None)
    await _flush()
    await coordinator.async_activate_custom_auto()

    await coordinator.async_apply_ha_fan_mode(FanMode.MEDIUM, power_on=False)
    client.calls.clear()
    await coordinator.async_deactivate_custom_auto()

    assert coordinator.custom_auto_handoff.state == "superseded"
    assert not any(call[0] == "execute" for call in client.calls)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_handoff_and_explicit_ha_mode_are_serialized(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    await _flush()
    await coordinator.async_activate_custom_auto()

    client.calls.clear()
    client.execute_gate = asyncio.Event()
    handoff = asyncio.create_task(coordinator.async_deactivate_custom_auto())
    await _flush()
    manual = asyncio.create_task(
        coordinator.async_apply_ha_fan_mode(FanMode.HIGH, power_on=True)
    )
    await _flush()

    assert client.calls == [
        ("execute", SetFanMode(FanMode.AUTO), CommandOrigin.HANDOFF)
    ]
    client.execute_gate.set()
    await asyncio.gather(handoff, manual)
    assert client.calls == [
        ("execute", SetFanMode(FanMode.AUTO), CommandOrigin.HANDOFF),
        ("execute", SetPower(True), CommandOrigin.HOME_ASSISTANT),
        ("execute", SetFanMode(FanMode.HIGH), CommandOrigin.HOME_ASSISTANT),
    ]
    assert coordinator.custom_auto_handoff.state == "superseded"
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_ha_mode_yields_before_command_and_generation_recovery(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()
    await coordinator.async_activate_custom_auto()
    await coordinator.async_set_fan_mode(FanMode.HIGH)
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.HIGH),
        CommandOrigin.HOME_ASSISTANT,
    )

    client.connection_generation = 2
    coordinator._availability_updated(False, ConnectionError("drop"))
    await _flush()
    assert coordinator.custom_auto_snapshot.connection_generation == 2  # type: ignore[union-attr]
    assert not coordinator.custom_auto_snapshot.available  # type: ignore[union-attr]
    coordinator._availability_updated(True, None)
    await _flush()
    assert coordinator.custom_auto_snapshot.available  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_suspends_without_yield_and_plain_on_preserves_intent(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()
    await coordinator.async_activate_custom_auto()

    await coordinator.async_set_power(False)
    coordinator.data = replace(coordinator.data, power=False)
    coordinator._state_updated(coordinator.data)
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and snapshot.suspended

    samples_before = client.calls.count(("sample",))
    await coordinator.async_set_power(True)
    coordinator.data = replace(coordinator.data, power=True)
    coordinator._state_updated(coordinator.data)
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.suspended
    assert client.calls.count(("sample",)) == samples_before + 1
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_ha_override_cancels_inflight_automatic_command_before_manual(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()
    await coordinator.async_activate_custom_auto()
    client.execute_gate = asyncio.Event()
    coordinator._observation_updated(
        AirQualityObservation(
            revision=1,
            generation=1,
            observed_at=asyncio.get_running_loop().time(),
            source=ObservationSource.QUERY,
            purpose=ObservationPurpose.ONE_SHOT,
            pm25=20,
            filter_life=90,
        )
    )
    await _flush()
    assert any(
        call[0] == "execute" and call[2] is CommandOrigin.CUSTOM_AUTO
        for call in client.calls
    )

    manual = asyncio.create_task(coordinator.async_set_fan_mode(FanMode.LOW))
    await _flush()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert any(
        call[0] == "execute" and call[2] is CommandOrigin.HOME_ASSISTANT
        for call in client.calls
    )
    client.execute_gate.set()
    await manual
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.LOW),
        CommandOrigin.HOME_ASSISTANT,
    )
    await coordinator.async_shutdown()
