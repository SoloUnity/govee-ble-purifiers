"""Tests for quiet coordinator availability transitions."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
        self.cancelled_commands: list[object] = []
        self.execute_error: Exception | None = None
        self.execute_gate: asyncio.Event | None = None
        self.start_check: Callable[[], None] | None = None
        self.shutdown_check: Callable[[], None] | None = None
        self.start_error: Exception | None = None

    async def async_start(self) -> None:
        if self.start_check is not None:
            self.start_check()
        self.calls.append(("start",))
        if self.start_error is not None:
            raise self.start_error

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
        try:
            if self.execute_gate is not None:
                await self.execute_gate.wait()
        except asyncio.CancelledError:
            self.cancelled_commands.append(command)
            raise
        if self.execute_error is not None:
            raise self.execute_error


class FakeCustomAutoMemory:
    """Coordinator-facing verified boolean memory fake."""

    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.calls: list[bool] = []
        self.error_for: bool | None = None
        self.block_for: bool | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.clear_calls = 0
        self.block_clear = False
        self.block_clear_call: int | None = None
        self.clear_failures = 0
        self.clear_started = asyncio.Event()
        self.clear_release = asyncio.Event()

    async def async_set_active(self, active: bool) -> None:
        self.calls.append(active)
        if self.block_for is active:
            self.started.set()
            await self.release.wait()
        if self.error_for is active:
            raise HomeAssistantError("injected memory failure")
        self.active = active

    async def async_clear(self) -> None:
        self.clear_calls += 1
        if self.block_clear or self.block_clear_call == self.clear_calls:
            self.clear_started.set()
            await self.clear_release.wait()
        if self.clear_failures:
            self.clear_failures -= 1
            raise HomeAssistantError("injected clear failure")
        self.active = False


def _runtime_coordinator(
    hass,
    *,
    enabled: bool,
    memory: FakeCustomAutoMemory | None = None,
    restore: bool = False,
):
    profile = DeviceProfile.for_model(Model.H7124)
    coordinator = GoveeDataUpdateCoordinator(
        hass,
        address="AA:BB:CC:DD:EE:FF",
        profile=profile,
        bluetooth_settings=bluetooth_settings_from_profile(profile),
        custom_auto_options=_enabled_options(profile) if enabled else None,
        custom_auto_memory=memory,  # type: ignore[arg-type]
        restore_custom_auto=restore,
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
async def test_restore_off_stays_inactive_and_restore_on_arms_before_client(
    hass,
) -> None:
    off_memory = FakeCustomAutoMemory(False)
    off, off_client = _runtime_coordinator(
        hass, enabled=True, memory=off_memory, restore=False
    )
    await off.async_start()
    assert not off.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert off_client.calls == [("start",)]
    await off.async_shutdown()

    on_memory = FakeCustomAutoMemory(True)
    on, on_client = _runtime_coordinator(
        hass, enabled=True, memory=on_memory, restore=True
    )
    on.data = PurifierState(power=True, pm25=999)

    def assert_armed_before_start() -> None:
        snapshot = on.custom_auto_snapshot
        assert snapshot is not None and snapshot.active
        assert snapshot.last_pm25 is None
        assert not any(call[0] == "execute" for call in on_client.calls)
        on_client.is_ready = True
        on_client.state = on.data
        on._availability_updated(True, None)
        on._state_updated(on.data)
        on._observation_updated(
            AirQualityObservation(
                revision=1,
                generation=1,
                observed_at=asyncio.get_running_loop().time(),
                source=ObservationSource.DEVICE,
                purpose=ObservationPurpose.UNSOLICITED,
                pm25=2,
                filter_life=90,
            )
        )

    on_client.start_check = assert_armed_before_start
    await on.async_start()
    assert on_memory.calls == []
    assert not any(call[0] == "execute" for call in on_client.calls)
    await _flush()
    execute = next(call for call in on_client.calls if call[0] == "execute")
    assert execute[1] == SetFanMode(FanMode.SLEEP)
    await on.async_shutdown()


@pytest.mark.asyncio
async def test_persistence_failures_preserve_pretransaction_runtime(hass) -> None:
    memory = FakeCustomAutoMemory(False)
    memory.error_for = True
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory
    )
    await coordinator.async_start()

    with pytest.raises(HomeAssistantError, match="memory failure"):
        await coordinator.async_activate_custom_auto()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]

    memory.error_for = None
    await coordinator.async_activate_custom_auto()
    memory.error_for = False
    coordinator.data = PurifierState(power=True)
    client.is_ready = True
    coordinator._client_available = True
    calls_before = list(client.calls)
    with pytest.raises(HomeAssistantError, match="memory failure"):
        await coordinator.async_deactivate_custom_auto()
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert client.calls == calls_before
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_transaction_settles_before_propagating(hass) -> None:
    memory = FakeCustomAutoMemory(False)
    memory.block_for = True
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory
    )
    await coordinator.async_start()

    activate = asyncio.create_task(coordinator.async_activate_custom_auto())
    await memory.started.wait()
    activate.cancel()
    await _flush()
    assert not activate.done()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]

    memory.release.set()
    with pytest.raises(asyncio.CancelledError):
        await activate
    assert memory.active
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_off_transaction_finishes_handoff_before_propagating(
    hass,
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    coordinator.data = PurifierState(power=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator._client_available = True
    client.execute_gate = asyncio.Event()

    deactivate = asyncio.create_task(coordinator.async_deactivate_custom_auto())
    while not any(
        call[0] == "execute" and call[1] == SetFanMode(FanMode.AUTO)
        for call in client.calls
    ):
        await asyncio.sleep(0)
    deactivate.cancel()
    await _flush()
    assert not deactivate.done()
    assert not memory.active
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]

    client.execute_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await deactivate
    assert coordinator.custom_auto_handoff.state == "confirmed"
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_concurrent_toggles_last_verified_transaction_wins(hass) -> None:
    memory = FakeCustomAutoMemory(False)
    memory.block_for = True
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory
    )
    await coordinator.async_start()

    activate = asyncio.create_task(coordinator.async_activate_custom_auto())
    await memory.started.wait()
    deactivate = asyncio.create_task(coordinator.async_deactivate_custom_auto())
    await _flush()
    assert memory.calls == [True]
    memory.release.set()
    await asyncio.gather(activate, deactivate)

    assert memory.calls == [True, False]
    assert not memory.active
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_quiesce_rearm_and_shutdown_do_not_change_memory(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    assert await coordinator.async_quiesce_custom_auto()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert memory.calls == []
    await coordinator.async_rearm_custom_auto_after_quiesce()
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert memory.calls == []
    await coordinator.async_shutdown()
    assert memory.calls == []


@pytest.mark.asyncio
async def test_physical_override_persistence_cannot_overtake_later_on(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.block_for = False
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=1,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.HIGH,
        )
    )
    await memory.started.wait()
    reactivate = asyncio.create_task(coordinator.async_activate_custom_auto())
    await _flush()
    assert memory.calls == [False]

    memory.release.set()
    await reactivate
    assert memory.calls == [False, True]
    assert memory.active
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_registry_block_is_synchronous_then_clears_and_stays_off(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    coordinator.data = PurifierState(power=True)
    coordinator._client_available = True
    client.is_ready = True

    coordinator.set_custom_auto_registry_allowed(False)
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()
    with pytest.raises(HomeAssistantError, match="command blocked"):
        await coordinator._async_custom_auto_fan_mode(  # noqa: SLF001
            FanMode.HIGH, CommandOrigin.CUSTOM_AUTO
        )
    assert not any(call[0] == "execute" for call in client.calls)

    await _flush()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert memory.clear_calls == 1
    assert not memory.active
    assert coordinator.custom_auto_handoff.state == "confirmed"
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.AUTO),
        CommandOrigin.HANDOFF,
    )

    coordinator.set_custom_auto_registry_allowed(True)
    await _flush()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert memory.calls == []
    await coordinator.async_activate_custom_auto()
    assert memory.active
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_registry_cleanup_settles_before_reenable_and_later_on(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.block_clear = True
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    coordinator.set_custom_auto_registry_allowed(False)
    await memory.clear_started.wait()
    coordinator.set_custom_auto_registry_allowed(True)
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()

    memory.clear_release.set()
    cleanup = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert cleanup is not None
    await cleanup
    await coordinator.async_activate_custom_auto()

    assert memory.clear_calls == 2
    assert memory.calls == [True]
    assert memory.active
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_terminal_disable_cannot_reopen_or_wake_waiting_activation(
    hass,
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    await coordinator._custom_auto_control_lock.acquire()  # noqa: SLF001
    activation = asyncio.create_task(coordinator.async_activate_custom_auto())
    await _flush()
    disable = asyncio.create_task(coordinator.async_disable_custom_auto_and_clear())
    await _flush()
    coordinator.set_custom_auto_registry_allowed(True)
    coordinator._custom_auto_control_lock.release()  # noqa: SLF001

    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await activation
    await disable
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert not memory.active
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_terminal_disable_keeps_cleanup_ownership_while_lock_waits(
    hass,
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    await coordinator._custom_auto_control_lock.acquire()  # noqa: SLF001

    disable = asyncio.create_task(coordinator.async_disable_custom_auto_and_clear())
    await _flush()
    disable.cancel()
    await _flush()

    assert not disable.done()
    assert memory.clear_calls == 0
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()

    coordinator._custom_auto_control_lock.release()  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await disable
    assert memory.clear_calls == 1
    assert not memory.active
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_rearm_racing_registry_cleanup_is_rejected_and_stays_off(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.block_clear = True
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    coordinator.set_custom_auto_registry_allowed(False)
    await memory.clear_started.wait()
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_rearm_custom_auto_after_quiesce()

    memory.clear_release.set()
    cleanup = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert cleanup is not None
    await cleanup
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert not memory.active
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_failed_registry_clear_retries_before_unhide_and_later_on(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.clear_failures = 1
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    retry_waiting = asyncio.Event()
    retry_release = asyncio.Event()

    async def controlled_sleep(_: float) -> None:
        retry_waiting.set()
        await retry_release.wait()

    monkeypatch.setattr(
        coordinator, "_async_registry_cleanup_sleep", controlled_sleep
    )
    coordinator.set_custom_auto_registry_allowed(False)
    await retry_waiting.wait()
    coordinator.set_custom_auto_registry_allowed(True)
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()

    retry_release.set()
    cleanup = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert cleanup is not None
    await cleanup
    await coordinator.async_activate_custom_auto()

    assert memory.clear_calls == 2
    assert memory.calls == [True]
    assert memory.active
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_same_blocked_state_reschedules_exhausted_cleanup(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.clear_failures = 3
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    monkeypatch.setattr(
        coordinator,
        "_async_registry_cleanup_sleep",
        AsyncMock(),
    )

    coordinator.set_custom_auto_registry_allowed(False)
    failed = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert failed is not None
    with pytest.raises(HomeAssistantError, match="clear failure"):
        await failed
    await _flush()

    coordinator.set_custom_auto_registry_allowed(False)
    retried = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert retried is not None and retried is not failed
    await retried
    coordinator.set_custom_auto_registry_allowed(True)
    await coordinator.async_activate_custom_auto()
    assert memory.clear_calls == 4
    assert memory.active
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_repeated_block_during_final_attempt_gets_fresh_retry_budget(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.clear_failures = 3
    memory.block_clear_call = 3
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    monkeypatch.setattr(
        coordinator,
        "_async_registry_cleanup_sleep",
        AsyncMock(),
    )

    coordinator.set_custom_auto_registry_allowed(False)
    cleanup = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert cleanup is not None
    await memory.clear_started.wait()
    coordinator.set_custom_auto_registry_allowed(False)
    memory.clear_release.set()
    await cleanup

    assert memory.clear_calls == 4
    assert not memory.active
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()
    coordinator.set_custom_auto_registry_allowed(True)
    await coordinator.async_activate_custom_auto()
    assert memory.active
    await _flush()
    assert memory.clear_calls == 4
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_unhide_after_final_failure_before_done_callback_owns_fresh_retry(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.clear_failures = 3
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    monkeypatch.setattr(
        coordinator,
        "_async_registry_cleanup_sleep",
        AsyncMock(),
    )

    coordinator.set_custom_auto_registry_allowed(False)
    failed = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert failed is not None
    assert (
        failed.remove_done_callback(coordinator._custom_auto_registry_cleanup_done)  # noqa: SLF001
        == 1
    )
    with pytest.raises(HomeAssistantError, match="clear failure"):
        await failed

    coordinator.set_custom_auto_registry_allowed(True)
    with pytest.raises(HomeAssistantError, match="entity is unavailable"):
        await coordinator.async_activate_custom_auto()
    coordinator._custom_auto_registry_cleanup_done(failed)  # noqa: SLF001
    retried = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert retried is not None and retried is not failed
    await retried
    await coordinator.async_activate_custom_auto()

    assert memory.clear_calls == 4
    assert memory.active
    await _flush()
    assert memory.clear_calls == 4
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_unhide_after_success_before_done_callback_opens_settled_gate(
    hass,
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    coordinator.set_custom_auto_registry_allowed(False)
    cleanup = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert cleanup is not None
    assert (
        cleanup.remove_done_callback(coordinator._custom_auto_registry_cleanup_done)  # noqa: SLF001
        == 1
    )
    await cleanup
    assert not coordinator._custom_auto_command_gate  # noqa: SLF001

    coordinator.set_custom_auto_registry_allowed(True)
    assert not coordinator._custom_auto_command_gate  # noqa: SLF001
    coordinator._custom_auto_registry_cleanup_done(cleanup)  # noqa: SLF001
    await coordinator.async_activate_custom_auto()

    assert memory.active
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_shutdown_surfaces_final_required_clear_after_closing_client(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.clear_failures = 4
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    monkeypatch.setattr(
        coordinator,
        "_async_registry_cleanup_sleep",
        AsyncMock(),
    )
    coordinator.set_custom_auto_registry_allowed(False)
    failed = coordinator._custom_auto_registry_cleanup_task  # noqa: SLF001
    assert failed is not None
    with pytest.raises(HomeAssistantError, match="clear failure"):
        await failed
    await _flush()

    with pytest.raises(HomeAssistantError, match="clear failure"):
        await coordinator.async_shutdown()
    assert client.calls[-1] == ("shutdown",)
    assert coordinator._shutdown  # noqa: SLF001


@pytest.mark.asyncio
async def test_registry_gate_closing_during_command_dispatch_prevents_send(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()
    dispatch_entered = asyncio.Event()
    dispatch_release = asyncio.Event()
    original_execute = coordinator._async_execute  # noqa: SLF001

    async def delayed_execute(*args, **kwargs) -> None:
        dispatch_entered.set()
        await dispatch_release.wait()
        await original_execute(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_async_execute", delayed_execute)
    command = asyncio.create_task(
        coordinator._async_custom_auto_fan_mode(  # noqa: SLF001
            FanMode.HIGH, CommandOrigin.CUSTOM_AUTO
        )
    )
    await dispatch_entered.wait()

    coordinator.set_custom_auto_registry_allowed(False)
    dispatch_release.set()

    with pytest.raises(HomeAssistantError, match="command blocked"):
        await command
    assert not any(call[0] == "execute" for call in client.calls)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_registry_rename_allowed_reread_does_not_deactivate(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    coordinator.set_custom_auto_registry_allowed(True)
    await _flush()

    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert memory.clear_calls == 0
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_ha_override_persists_off_before_manual_command(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    await coordinator.async_apply_ha_fan_mode(FanMode.MEDIUM, power_on=False)

    assert memory.calls == [False]
    assert not memory.active
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert client.calls[-1] == (
        "execute",
        SetFanMode(FanMode.MEDIUM),
        CommandOrigin.HOME_ASSISTANT,
    )
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_ha_override_persistence_failure_keeps_ownership_and_sends_nothing(
    hass,
) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.error_for = False
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    with pytest.raises(HomeAssistantError, match="memory failure"):
        await coordinator.async_apply_ha_fan_mode(
            FanMode.HIGH, power_on=True
        )

    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert not any(call[0] == "execute" for call in client.calls)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_ha_override_settles_off_without_manual_command(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.block_for = False
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    await coordinator.async_start()

    override = asyncio.create_task(
        coordinator.async_apply_ha_fan_mode(FanMode.MEDIUM, power_on=True)
    )
    await memory.started.wait()
    override.cancel()
    await _flush()
    assert not override.done()
    assert coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]

    memory.release.set()
    with pytest.raises(asyncio.CancelledError):
        await override
    assert not memory.active
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    assert not any(call[0] == "execute" for call in client.calls)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_activation_reports_incomplete_rollback(hass, monkeypatch) -> None:
    memory = FakeCustomAutoMemory(False)
    coordinator, _client = _runtime_coordinator(
        hass, enabled=True, memory=memory
    )
    await coordinator.async_start()
    controller = coordinator.custom_auto_controller
    assert controller is not None
    monkeypatch.setattr(
        controller,
        "activate",
        AsyncMock(side_effect=RuntimeError("activation failed")),
    )
    monkeypatch.setattr(
        controller,
        "deactivate",
        AsyncMock(side_effect=RuntimeError("deactivation failed")),
    )
    memory.error_for = False

    with pytest.raises(HomeAssistantError, match="rollback was incomplete"):
        await coordinator.async_activate_custom_auto()

    assert memory.calls == [True, False]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_restored_startup_settles_complete_cleanup(
    hass, monkeypatch
) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    controller = coordinator.custom_auto_controller
    assert controller is not None
    original_invalidate = controller._invalidate_operational  # noqa: SLF001
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_invalidate(*args, **kwargs) -> None:
        entered.set()
        await release.wait()
        await original_invalidate(*args, **kwargs)

    monkeypatch.setattr(controller, "_invalidate_operational", blocked_invalidate)
    startup = asyncio.create_task(coordinator.async_start())
    await entered.wait()
    startup.cancel()
    await _flush()
    assert not startup.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await startup
    assert controller.snapshot.actor_tasks == 0
    assert coordinator._custom_auto_observation_remove is None
    assert coordinator._custom_auto_state_remove is None
    assert coordinator._custom_auto_persistence_tasks == set()
    assert client.calls == [("shutdown",)]
    assert memory.calls == []


@pytest.mark.asyncio
async def test_client_start_failure_cleans_actor_listeners_and_client(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    client.start_error = RuntimeError("client start failed")
    controller = coordinator.custom_auto_controller
    assert controller is not None

    with pytest.raises(RuntimeError, match="client start failed"):
        await coordinator.async_start()

    assert controller.snapshot.actor_tasks == 0
    assert coordinator._custom_auto_observation_remove is None
    assert coordinator._custom_auto_state_remove is None
    assert client.calls == [("start",), ("shutdown",)]
    assert memory.calls == []


@pytest.mark.asyncio
async def test_client_start_failure_drains_registry_cleanup_task(hass) -> None:
    memory = FakeCustomAutoMemory(True)
    memory.block_clear = True
    coordinator, client = _runtime_coordinator(
        hass, enabled=True, memory=memory, restore=True
    )
    client.start_check = lambda: coordinator.set_custom_auto_registry_allowed(
        False
    )
    client.start_error = RuntimeError("client start failed")

    startup = asyncio.create_task(coordinator.async_start())
    await memory.clear_started.wait()
    await _flush()
    assert not startup.done()

    memory.clear_release.set()
    with pytest.raises(RuntimeError, match="client start failed"):
        await startup

    assert coordinator._custom_auto_registry_cleanup_task is None  # noqa: SLF001
    assert coordinator.custom_auto_snapshot.actor_tasks == 0  # type: ignore[union-attr]
    assert client.calls == [("start",), ("shutdown",)]


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
async def test_initializing_current_generation_physical_manual_disables(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    coordinator.data = PurifierState(power=True)
    coordinator.custom_auto_controller.set_powered(True)  # type: ignore[union-attr]
    await coordinator.async_activate_custom_auto()
    client.connection_generation = 2

    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=2,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.MEDIUM,
        )
    )
    coordinator._observation_updated(
        FanModeObservation(
            revision=2,
            generation=1,
            observed_at=2,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.AUTO,
        )
    )
    await _flush()

    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and not snapshot.active
    assert snapshot.connection_generation == 2
    assert snapshot.last_physical_fan is not None
    assert snapshot.last_physical_fan.mode is FanMode.MEDIUM
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_initializing_physical_auto_redirect_samples_once_at_ready(hass) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    coordinator.data = PurifierState(power=True)
    coordinator.custom_auto_controller.set_powered(True)  # type: ignore[union-attr]
    await coordinator.async_activate_custom_auto()
    client.connection_generation = 2
    samples_before = client.calls.count(("sample",))

    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=2,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.AUTO,
        )
    )
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active
    assert snapshot.auto_redirect_state == "pending"
    assert client.calls.count(("sample",)) == samples_before

    client.is_ready = True
    client.state = coordinator.data
    coordinator._availability_updated(True, None)
    await _flush()
    assert client.calls.count(("sample",)) == samples_before + 1
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_quiesces_custom_auto_command_before_power_write(hass) -> None:
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
            source=ObservationSource.DEVICE,
            purpose=ObservationPurpose.UNSOLICITED,
            pm25=20,
            filter_life=90,
        )
    )
    await _flush()

    power_off = asyncio.create_task(coordinator.async_set_power(False))
    await _flush()
    assert client.calls[-1] == (
        "execute",
        SetPower(False),
        CommandOrigin.HOME_ASSISTANT,
    )
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and snapshot.suspended
    assert snapshot.command_tasks == 0
    client.execute_gate.set()
    await power_off
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_failed_power_off_restores_active_controller_from_cached_power(
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
    await _flush()
    samples_before = client.calls.count(("sample",))
    client.execute_error = PurifierClientError("injected power failure")

    with pytest.raises(HomeAssistantError, match="injected power failure"):
        await coordinator.async_set_power(False)
    await _flush()

    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.suspended
    assert client.calls.count(("sample",)) == samples_before + 1
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancel_during_begin_power_off_finishes_transition_and_propagates(
    hass, monkeypatch
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
    controller = coordinator.custom_auto_controller
    assert controller is not None
    original_invalidate = controller._invalidate_operational  # noqa: SLF001
    begin_entered = asyncio.Event()
    release_begin = asyncio.Event()

    async def blocked_invalidate(*, reset_policy: bool) -> None:
        begin_entered.set()
        await release_begin.wait()
        await original_invalidate(reset_policy=reset_policy)

    monkeypatch.setattr(controller, "_invalidate_operational", blocked_invalidate)
    power_off = asyncio.create_task(coordinator.async_set_power(False))
    await begin_entered.wait()
    power_off.cancel()
    await _flush()
    assert not power_off.done()
    assert not any(
        call[0] == "execute" and call[1] == SetPower(False)
        for call in client.calls
    )

    release_begin.set()
    with pytest.raises(asyncio.CancelledError):
        await power_off
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.suspended
    assert snapshot.powered is True
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_cancel_during_power_off_command_safely_restores_and_propagates(
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
    samples_before = client.calls.count(("sample",))
    client.execute_gate = asyncio.Event()

    power_off = asyncio.create_task(coordinator.async_set_power(False))
    while not any(
        call[0] == "execute" and call[1] == SetPower(False)
        for call in client.calls
    ):
        await asyncio.sleep(0)
    power_off.cancel()
    with pytest.raises(asyncio.CancelledError):
        await power_off
    await _flush()

    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.suspended
    assert snapshot.powered is True
    assert client.calls.count(("sample",)) == samples_before + 1
    assert client.cancelled_commands == [SetPower(False)]
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
    client.state = coordinator.data
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
async def test_power_off_quiesces_active_automatic_send_before_power_command(
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

    power_off = asyncio.create_task(coordinator.async_set_power(False))
    await _flush()
    assert client.cancelled_commands == [SetFanMode(FanMode.TURBO)]
    assert client.calls[-1] == (
        "execute",
        SetPower(False),
        CommandOrigin.HOME_ASSISTANT,
    )
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and snapshot.suspended

    client.execute_gate.set()
    await power_off
    assert coordinator.custom_auto_snapshot.powered is False  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_failed_power_off_restores_cached_on_state_and_fresh_barrier(
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
    samples_before = client.calls.count(("sample",))
    client.execute_error = PurifierClientError("injected off failure")

    with pytest.raises(HomeAssistantError, match="injected off failure"):
        await coordinator.async_set_power(False)
    await _flush()

    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.suspended
    assert snapshot.powered is True
    assert client.calls.count(("sample",)) == samples_before + 1
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_new_generation_manual_during_initialization_permanently_yields(
    hass,
) -> None:
    coordinator, client = _runtime_coordinator(hass, enabled=True)
    await coordinator.async_start()
    client.is_ready = True
    coordinator.data = PurifierState(power=True)
    coordinator._availability_updated(True, None)
    coordinator._state_updated(coordinator.data)
    await _flush()
    await coordinator.async_activate_custom_auto()

    client.connection_generation = 2
    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=2,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.HIGH,
        )
    )
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None
    assert snapshot.connection_generation == 2
    assert not snapshot.available
    assert not snapshot.active

    coordinator._availability_updated(True, None)
    await _flush()
    assert not coordinator.custom_auto_snapshot.active  # type: ignore[union-attr]
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_new_generation_auto_waits_for_ready_and_old_generation_is_ignored(
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
    samples_before = client.calls.count(("sample",))

    client.connection_generation = 2
    coordinator._observation_updated(
        FanModeObservation(
            revision=1,
            generation=2,
            observed_at=1,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.AUTO,
        )
    )
    coordinator._observation_updated(
        FanModeObservation(
            revision=99,
            generation=1,
            observed_at=2,
            source=ObservationSource.PHYSICAL,
            purpose=ObservationPurpose.UNSOLICITED,
            mode=FanMode.HIGH,
        )
    )
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and not snapshot.available
    assert snapshot.auto_redirect_state == "pending"
    assert client.calls.count(("sample",)) == samples_before

    coordinator._availability_updated(True, None)
    await _flush()
    snapshot = coordinator.custom_auto_snapshot
    assert snapshot is not None and snapshot.active and snapshot.available
    assert snapshot.auto_redirect_state == "pending"
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
