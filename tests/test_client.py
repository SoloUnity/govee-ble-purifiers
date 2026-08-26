"""Tests for reliable-client scheduling and generation guards."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.govee_ble_air_purifier import client as client_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    CONNECTION_ATTEMPT_TIMEOUT,
    BluetoothUnavailableError,
    GattTransportError,
)
from custom_components.govee_ble_air_purifier.client import (
    ReliablePurifierClient,
    _Operation,
)
from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import (
    DeviceProfile,
    FanMode,
    Model,
    PurifierState,
    SetFanMode,
    SetNightLightColor,
    SetPower,
)
from custom_components.govee_ble_air_purifier.protocol import (
    GoveePurifierProtocol,
    RequestDescriptor,
)


class FakeEnvironment:
    address = "AA:BB:CC:DD:EE:FF"

    def __init__(self) -> None:
        self.recent_advertisement = False
        self.advertisement_event = asyncio.Event()

    async def async_start(self) -> None:
        return

    def route_diagnostics(self) -> dict[str, object]:
        return {"present": True, "source": "test", "rssi": -50}

    def reachability_diagnostics(self) -> str:
        return "test route is reachable"

    def has_recent_advertisement(self, _: float) -> bool:
        return self.recent_advertisement

    async def async_wait_for_advertisement_after(self, _: float) -> None:
        await self.advertisement_event.wait()

    async def async_wait_for_fresh_device(self, _: float) -> SimpleNamespace:
        return SimpleNamespace(
            name="ihoment_H7129_TEST",
            address=self.address,
        )

    async def async_stop(self) -> None:
        return


class FakeTransport:
    generation = 7

    def __init__(self) -> None:
        self.disconnects = 0

    async def async_disconnect(self) -> None:
        self.disconnects += 1

    async def async_cleanup_stale_connection(self, *, reason: str) -> bool:
        return True

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {"generation": self.generation, "is_connected": False}


class PollChannel:
    """Return an aa-01 response for every sent frame."""

    ready = True

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self.callback = callback
        self.writes: list[tuple[float, bytes]] = []

    async def async_send(self, frame: bytes) -> None:
        self.writes.append((asyncio.get_running_loop().time(), frame))
        self.callback(build_frame(b"\xaa\x01\x01"))

    def invalidate(self) -> None:
        self.ready = False


class SilentChannel:
    """Accept writes without producing any notification."""

    ready = True

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def async_send(self, frame: bytes) -> None:
        self.writes.append(frame)

    def invalidate(self) -> None:
        self.ready = False


def make_client() -> ReliablePurifierClient:
    profile = DeviceProfile.for_model(Model.H7124)
    return ReliablePurifierClient(
        environment=FakeEnvironment(),  # type: ignore[arg-type]
        transport=FakeTransport(),  # type: ignore[arg-type]
        protocol=GoveePurifierProtocol(profile),
        profile=profile,
        state_callback=lambda _: None,
        availability_callback=lambda *_: None,
    )


def test_old_generation_callbacks_are_ignored() -> None:
    """Late frames and disconnects from an old connection cannot mutate state."""
    client = make_client()
    client._session_generation = 4

    client._on_plaintext_frame(3, build_frame(b"\xaa\x01\x01"))
    client._on_disconnected(3, 7)

    assert client._frame_queue.empty()
    assert not client._disconnected.is_set()


def test_explicit_validation_budget_is_five_minutes() -> None:
    """Explicit setup validation gets a bounded five-minute recovery window."""
    maximum_first_backoff = client_module.BACKOFF_MIN * 1.2
    two_cycle_budget = (
        2 * (client_module.FRESH_ADVERTISEMENT_TIMEOUT + CONNECTION_ATTEMPT_TIMEOUT)
        + maximum_first_backoff
    )

    assert client_module.STARTUP_TIMEOUT >= two_cycle_budget
    assert client_module.STARTUP_TIMEOUT == 300.0


def test_command_recovery_budget_handles_slow_encrypted_reconnect() -> None:
    """Controls remain pending through a poor-signal reconnect and negotiation."""
    assert client_module.COMMAND_DEADLINE == 120.0
    assert client_module.COMMAND_SEND_ATTEMPTS == 3


@pytest.mark.asyncio
async def test_shutdown_does_not_overwrite_detailed_availability_error() -> None:
    """Cleanup cannot replace a meaningful failure with disconnected."""
    client = make_client()
    updates: list[tuple[bool, Exception | None]] = []
    client._availability_callback = lambda available, error: updates.append(
        (available, error)
    )
    detailed_error = BluetoothUnavailableError(
        "stage=establish_connection; reachability=no free proxy slots"
    )
    client._set_available(False, detailed_error)

    await client.async_shutdown()

    assert updates == [(False, detailed_error)]


@pytest.mark.asyncio
async def test_startup_timeout_surfaces_one_final_diagnostic_without_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded startup failure remains useful after cleanup completes."""
    monkeypatch.setattr(client_module, "STARTUP_TIMEOUT", 0.01)
    client = make_client()
    updates: list[tuple[bool, Exception | None]] = []
    client._availability_callback = lambda available, error: updates.append(
        (available, error)
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    client._run = wait_forever  # type: ignore[method-assign]

    await client.async_start()

    with pytest.raises(BluetoothUnavailableError) as raised:
        await client.async_wait_until_ready()

    message = str(raised.value)
    assert "did not become ready" in message
    assert "cause=TimeoutError" in message
    assert "route={'present': True, 'source': 'test', 'rssi': -50}" in message
    assert "reachability=test route is reachable" in message
    assert updates == []
    assert client._runner is not None

    await client.async_shutdown()


@pytest.mark.asyncio
async def test_start_returns_while_initial_recovery_is_pending() -> None:
    """An offline purifier cannot hold Home Assistant entry setup open."""
    client = make_client()
    recovery_started = asyncio.Event()

    async def wait_forever() -> None:
        recovery_started.set()
        await asyncio.Event().wait()

    client._run = wait_forever  # type: ignore[method-assign]

    await asyncio.wait_for(client.async_start(), timeout=0.1)
    await asyncio.wait_for(recovery_started.wait(), timeout=0.1)

    assert client._runner is not None
    assert not client._first_ready.done()

    await client.async_shutdown()


@pytest.mark.asyncio
async def test_connection_error_surfaces_selected_and_current_routes() -> None:
    """Normal HA errors contain the route evidence needed for diagnosis."""
    client = make_client()
    transport = client._transport
    transport.set_disconnect_callback = lambda _: None  # type: ignore[attr-defined,method-assign]

    async def fail_connect(_: object) -> None:
        raise BluetoothUnavailableError("connector timed out")

    transport.async_connect = fail_connect  # type: ignore[attr-defined,method-assign]

    with pytest.raises(BluetoothUnavailableError) as raised:
        await client._connect_initialize_and_run()

    message = str(raised.value)
    assert "connector timed out" in message
    assert "selected_route={'present': True, 'source': 'test', 'rssi': -50}" in message
    assert "current_route={'present': True, 'source': 'test', 'rssi': -50}" in message
    assert "reachability=test route is reachable" in message


@pytest.mark.asyncio
async def test_current_disconnect_interrupts_transaction_immediately() -> None:
    """An unplug or link loss wakes a transaction without waiting for timeout."""
    client = make_client()
    client._session_generation = 4
    channel = PollChannel(lambda _: None)
    client._channel = channel  # type: ignore[assignment]

    client._on_disconnected(4, 7)

    assert client._disconnected.is_set()
    assert not channel.ready
    with pytest.raises(GattTransportError, match="disconnected"):
        await client._async_next_transaction_frame(
            asyncio.get_running_loop().time() + 1
        )


@pytest.mark.asyncio
async def test_cancelling_transaction_wait_cleans_up_child_tasks() -> None:
    """Shutdown cannot leave Queue.get or Event.wait tasks pending."""
    client = make_client()
    before = set(asyncio.all_tasks())
    wait_task = asyncio.create_task(
        client._async_next_transaction_frame(asyncio.get_running_loop().time() + 60)
    )
    await asyncio.sleep(0)
    child_tasks = set(asyncio.all_tasks()) - before - {wait_task}
    assert len(child_tasks) == 2

    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task

    assert all(task.done() for task in child_tasks)


@pytest.mark.asyncio
async def test_cancelling_idle_wait_cleans_up_child_tasks() -> None:
    """Idle-loop cancellation cleans up all three temporary wait tasks."""
    client = make_client()
    before = set(asyncio.all_tasks())
    wait_task = asyncio.create_task(client._async_wait_for_ready_work(60))
    await asyncio.sleep(0)
    child_tasks = set(asyncio.all_tasks()) - before - {wait_task}
    assert len(child_tasks) == 3

    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task

    assert all(task.done() for task in child_tasks)


@pytest.mark.asyncio
async def test_connection_loop_retries_after_link_failure() -> None:
    """A failed connection cycle becomes unavailable, backs off, and retries."""
    client = make_client()
    client._has_ever_been_ready = True
    availability: list[bool] = []
    client._availability_callback = lambda available, _: availability.append(available)
    cycles = 0

    async def run_cycle() -> None:
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            raise GattTransportError("purifier was unplugged")
        client._stopping.set()

    client._connect_initialize_and_run = run_cycle  # type: ignore[method-assign]
    client._async_backoff = AsyncMock()  # type: ignore[method-assign]

    await client._run()

    assert cycles == 2
    assert availability == [False]
    client._async_backoff.assert_awaited_once_with(client_module.BACKOFF_MIN)
    assert client._transport.disconnects == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_initial_connection_failures_remain_quiet_while_retrying() -> None:
    """Setup retries do not publish intermediate unavailable errors."""
    client = make_client()
    availability: list[bool] = []
    client._availability_callback = lambda available, _: availability.append(available)
    cycles = 0

    async def run_cycle() -> None:
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            raise GattTransportError("weak signal")
        client._stopping.set()

    client._connect_initialize_and_run = run_cycle  # type: ignore[method-assign]
    client._async_backoff = AsyncMock()  # type: ignore[method-assign]

    await client._run()

    assert cycles == 2
    assert availability == []


def test_recent_advertisements_cap_recovery_backoff() -> None:
    """A visible weak purifier is retried without reaching minute-long delays."""
    client = make_client()
    environment = client._environment
    environment.recent_advertisement = True  # type: ignore[attr-defined]

    assert client._recovery_backoff_delay(60.0) == 8.0

    environment.recent_advertisement = False  # type: ignore[attr-defined]
    assert client._recovery_backoff_delay(60.0) == 60.0


@pytest.mark.asyncio
async def test_new_advertisement_wakes_long_recovery_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh radio evidence bypasses a minute-long scheduled recovery delay."""
    monkeypatch.setattr(client_module.random, "uniform", lambda *_: 1.0)
    monkeypatch.setattr(client_module, "ADVERTISEMENT_RECOVERY_COOLDOWN", 0.0)
    client = make_client()
    environment = client._environment

    backoff = asyncio.create_task(client._async_backoff(60.0))
    await asyncio.sleep(0)
    environment.advertisement_event.set()  # type: ignore[attr-defined]

    await asyncio.wait_for(backoff, timeout=0.1)


@pytest.mark.asyncio
async def test_queued_command_wakes_recovery_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user control never waits behind the current exponential delay."""
    monkeypatch.setattr(client_module.random, "uniform", lambda *_: 1.0)
    client = make_client()

    backoff = asyncio.create_task(client._async_backoff(60.0))
    await asyncio.sleep(0)
    client._operation_event.set()

    await asyncio.wait_for(backoff, timeout=0.1)
    assert not client._operation_event.is_set()


@pytest.mark.asyncio
async def test_failure_before_command_send_does_not_spend_send_budget() -> None:
    """Connection/channel recovery never counts as an application send."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), future, loop.time() + 120)

    async def fail_before_send(*_: object, **__: object) -> tuple[bytes, ...]:
        raise GattTransportError("session dropped before command write")

    client._async_execute_descriptor = fail_before_send  # type: ignore[method-assign]

    with pytest.raises(GattTransportError, match="before command write"):
        await client._async_execute_operation(operation)

    assert operation.send_attempts == 0
    assert not future.done()
    assert client._operations.popleft() is operation


def test_duplicate_unavailable_updates_are_suppressed() -> None:
    """One weak-link outage produces one availability transition."""
    client = make_client()
    updates: list[bool] = []
    client._availability_callback = lambda available, _: updates.append(available)

    client._set_available(True, None)
    client._set_available(False, GattTransportError("first failure"))
    client._set_available(False, GattTransportError("retry failure"))
    client._set_available(True, None)

    assert updates == [True, False, True]


def test_reconnect_invalidates_fan_and_never_assumes_cached_rgb() -> None:
    """Unqueryable state cannot suppress recovery commands after reconnect."""
    client = make_client()
    client.state = PurifierState(
        fan_mode=FanMode.HIGH,
        light_rgb=(10, 20, 30),
    )

    client._invalidate_connection_scoped_state()

    assert client.state.fan_mode is None
    assert not client._command_is_satisfied(SetNightLightColor(10, 20, 30))


def test_brightness_echo_does_not_clear_cached_light_power() -> None:
    """The brightness selector value 02 is not a false light-power state."""
    client = make_client()
    client.state = PurifierState(light_power=True, light_brightness=25)

    client._process_plaintext_frame(build_frame(b"\x3a\x1b\x01\x02\x32"))

    assert client.state.light_power is True
    assert client.state.light_brightness == 50


@pytest.mark.asyncio
async def test_initialization_exhausts_secondary_request_then_completes_sweep() -> None:
    """One silent secondary request cannot discard the remaining startup work."""
    client = make_client()
    descriptors = client._protocol.initialization_requests()
    calls: list[tuple[str, int]] = []

    async def execute(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        calls.append((descriptor.name, attempts))
        if descriptor.name == "capability_b2":
            raise client_module.TransactionTimeoutError("secondary stayed silent")
        return ()

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    await client._async_run_initialization(descriptors)

    assert [name for name, _ in calls] == [
        descriptor.name for descriptor in descriptors
    ]
    assert all(attempts == 3 for _, attempts in calls)
    diagnostics = client.diagnostic_snapshot()
    assert diagnostics["incomplete_initialization_requests"] == ("capability_b2",)
    assert diagnostics["initialization_failure_summaries"] == {
        "capability_b2": "secondary stayed silent"
    }


@pytest.mark.asyncio
async def test_initialization_retries_essential_state_on_same_session() -> None:
    """A silent aa-01 batch is retried without abandoning the connected channel."""
    client = make_client()
    descriptors = client._protocol.initialization_requests()
    calls: list[str] = []
    device_state_calls = 0

    async def execute(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        nonlocal device_state_calls
        assert attempts == 3
        calls.append(descriptor.name)
        if descriptor.name == "device_state":
            device_state_calls += 1
            if device_state_calls == 1:
                raise client_module.TransactionTimeoutError("aa 01 stayed silent")
        return ()

    wait_for_work = AsyncMock()
    client._async_execute_descriptor = execute  # type: ignore[method-assign]
    client._async_wait_for_ready_work = wait_for_work  # type: ignore[method-assign]

    await client._async_run_initialization(descriptors)

    assert calls[: len(descriptors)] == [
        descriptor.name for descriptor in descriptors
    ]
    assert calls[-1] == "device_state"
    assert device_state_calls == 2
    wait_for_work.assert_awaited_once_with(client_module.INITIALIZATION_RETRY_DELAY)
    assert client.diagnostic_snapshot()["incomplete_initialization_requests"] == ()
    assert client._transport.disconnects == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_initialization_recycles_after_three_silent_essential_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zombie connected session gets nine aa-01 attempts, not an infinite loop."""
    monkeypatch.setattr(client_module, "TRANSACTION_TIMEOUT", 0.001)
    client = make_client()
    client.status = client_module.ClientStatus.INITIALIZING
    channel = SilentChannel()
    client._channel = channel  # type: ignore[assignment]
    wait_for_work = AsyncMock()
    client._async_wait_for_ready_work = wait_for_work  # type: ignore[method-assign]
    essential = (client._protocol.device_state_poll(),)

    with pytest.raises(
        client_module.TransactionTimeoutError,
        match=r"3 batch\(es\) and 9 attempt\(s\)",
    ):
        await client._async_run_initialization(essential)

    assert wait_for_work.await_count == 2
    wait_for_work.assert_awaited_with(client_module.INITIALIZATION_RETRY_DELAY)
    assert len(channel.writes) == 9  # type: ignore[attr-defined]
    diagnostics = client.diagnostic_snapshot()
    assert diagnostics["essential_initialization_batches"] == 3
    assert diagnostics["essential_initialization_attempts"] == 9
    assert diagnostics["essential_initialization_batch_limit"] == 3
    assert diagnostics["incomplete_initialization_requests"] == ("device_state",)
    assert client._transport.disconnects == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_initialization_transport_failure_remains_fatal() -> None:
    """Only response exhaustion is best-effort; a broken GATT link still recovers."""
    client = make_client()
    descriptors = client._protocol.initialization_requests()
    calls: list[str] = []

    async def execute(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        assert attempts == 3
        calls.append(descriptor.name)
        raise GattTransportError("link dropped")

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    with pytest.raises(GattTransportError, match="link dropped"):
        await client._async_run_initialization(descriptors)

    assert calls == [descriptors[0].name]
    assert client.diagnostic_snapshot()["incomplete_initialization_requests"] == ()


@pytest.mark.asyncio
async def test_periodic_poll_uses_three_attempts_on_existing_connection() -> None:
    """One READY session retries aa-01 before escalating to reconnection."""
    client = make_client()
    client._next_poll_due = asyncio.get_running_loop().time()
    calls: list[tuple[str, int, bool]] = []

    async def execute(
        descriptor: RequestDescriptor,
        *,
        attempts: int,
        is_periodic_poll: bool = False,
    ) -> tuple[bytes, ...]:
        calls.append((descriptor.name, attempts, is_periodic_poll))
        client._stopping.set()
        return ()

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    await client._async_ready_loop()

    assert calls == [("device_state", 3, True)]


@pytest.mark.asyncio
async def test_periodic_poll_exhaustion_leaves_ready_loop_for_reconnect() -> None:
    """Three silent aa-01 attempts remain a fatal health failure."""
    client = make_client()
    client._next_poll_due = asyncio.get_running_loop().time()

    async def execute(
        descriptor: RequestDescriptor,
        *,
        attempts: int,
        is_periodic_poll: bool = False,
    ) -> tuple[bytes, ...]:
        assert descriptor.name == "device_state"
        assert attempts == 3
        assert is_periodic_poll
        raise client_module.TransactionTimeoutError("three aa 01 attempts failed")

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    with pytest.raises(
        client_module.TransactionTimeoutError,
        match="three aa 01 attempts failed",
    ):
        await client._async_ready_loop()


@pytest.mark.asyncio
async def test_refresh_exhausts_secondary_request_and_preserves_connection() -> None:
    """An ee-aa refresh records secondary silence and completes the sweep."""
    client = make_client()
    descriptors = client._protocol.refresh_requests()
    calls: list[tuple[str, int]] = []

    async def execute(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        calls.append((descriptor.name, attempts))
        if descriptor.name == "capability_b5":
            raise client_module.TransactionTimeoutError("refresh stayed silent")
        return ()

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    await client._async_run_refresh(descriptors)

    assert [name for name, _ in calls] == [
        descriptor.name for descriptor in descriptors
    ]
    assert all(attempts == 3 for _, attempts in calls)
    assert not client._refresh_running
    diagnostics = client.diagnostic_snapshot()
    assert diagnostics["incomplete_refresh_requests"] == ("capability_b5",)
    assert diagnostics["refresh_failure_summaries"] == {
        "capability_b5": "refresh stayed silent"
    }
    assert client._transport.disconnects == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_refresh_essential_exhaustion_requires_reconnect() -> None:
    """A refresh cannot remain healthy after three missing aa-01 responses."""
    client = make_client()
    descriptors = client._protocol.refresh_requests()
    calls: list[str] = []

    async def execute(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        assert attempts == 3
        calls.append(descriptor.name)
        if descriptor.name == "device_state":
            raise client_module.TransactionTimeoutError("essential refresh failed")
        return ()

    client._async_execute_descriptor = execute  # type: ignore[method-assign]

    with pytest.raises(
        client_module.TransactionTimeoutError,
        match="essential refresh failed",
    ):
        await client._async_run_refresh(descriptors)

    assert calls == ["capability_b5", "device_state"]
    assert not client._refresh_running
    diagnostics = client.diagnostic_snapshot()
    assert diagnostics["incomplete_refresh_requests"] == ("device_state",)


@pytest.mark.asyncio
async def test_steady_scheduler_polls_only_aa01(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No status, light, or metadata query enters the steady poll loop."""
    monkeypatch.setattr(client_module, "POLL_INTERVAL", 0.01)
    client = make_client()
    client._session_generation = 1
    channel = PollChannel(lambda frame: client._on_plaintext_frame(1, frame))
    client._channel = channel  # type: ignore[assignment]
    client._next_poll_due = asyncio.get_running_loop().time()

    task = asyncio.create_task(client._async_ready_loop())
    while len(channel.writes) < 3:
        await asyncio.sleep(0)
    client._stopping.set()
    await task

    assert all(frame[:2] == b"\xaa\x01" for _, frame in channel.writes)
    intervals = [
        second[0] - first[0]
        for first, second in zip(channel.writes, channel.writes[1:], strict=False)
    ]
    assert all(interval >= 0.009 for interval in intervals)


@pytest.mark.asyncio
async def test_ambiguous_command_is_bounded_and_reconciled() -> None:
    """Three same-session silences recycle once for authoritative reconciliation."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), future, loop.time() + 30)
    calls = 0

    async def fail_transaction(*_: object, **kwargs: object) -> tuple[bytes, ...]:
        nonlocal calls
        calls += 1
        on_send = kwargs["on_send"]
        assert callable(on_send)
        on_send()
        raise client_module.TransactionTimeoutError("command response stayed silent")

    client._async_execute_descriptor = fail_transaction  # type: ignore[method-assign]

    with pytest.raises(
        client_module.TransactionTimeoutError,
        match="command response stayed silent",
    ):
        await client._async_execute_operation(operation)

    assert calls == 3
    assert operation.send_attempts == 3
    assert not future.done()
    assert client._operations.popleft() is operation

    # Reconnect initialization observes that the ambiguous write was applied.
    client.state = PurifierState(power=True)
    await client._async_execute_operation(operation)
    assert future.done()
    assert future.exception() is None


@pytest.mark.asyncio
async def test_fan_failure_preserves_wire_diagnostics_across_reconnect() -> None:
    """The final HA error retains all fan evidence from the prior session."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(
        SetFanMode(FanMode.MEDIUM),
        future,
        loop.time() + 30,
        created_at=loop.time(),
    )
    client._active_operation = operation
    client._session_generation = 4
    calls = 0

    async def fail_transaction(*_: object, **kwargs: object) -> tuple[bytes, ...]:
        nonlocal calls
        calls += 1
        on_send = kwargs["on_send"]
        assert callable(on_send)
        on_send()
        if calls == 1:
            client._on_plaintext_frame(4, build_frame(bytes.fromhex("ee 05 01 03")))
        raise client_module.TransactionTimeoutError(
            "received=1, matched_fragments=0, ignored=1, "
            "ignored_sample=ee 05 01 03"
        )

    client._async_execute_descriptor = fail_transaction  # type: ignore[method-assign]

    with pytest.raises(client_module.TransactionTimeoutError):
        await client._async_execute_operation(operation)

    assert calls == 3
    assert operation.send_attempts == 3
    assert len(operation.response_failures) == 3
    assert len(operation.observed_fan_frames) == 1

    # The command survives the recovery connection and then reports why its
    # already-bounded sends could not be confirmed.
    client._active_operation = None
    client._session_generation = 5
    await client._async_execute_operation(operation)

    error = future.exception()
    assert isinstance(error, client_module.CommandDeadlineExceeded)
    summary = str(error)
    assert "SetFanMode(mode=medium)" in summary
    assert "frame=3a 05 01 02" in summary
    assert "sends=3/3" in summary
    assert "generation=4" in summary
    assert "ignored_sample=ee 05 01 03" in summary
    assert "decoded_mode=high" in summary
    assert "current_generation=5" in summary
    assert client.diagnostic_snapshot()["last_command_diagnostics"] == summary


@pytest.mark.asyncio
async def test_command_can_succeed_on_third_same_session_attempt() -> None:
    """Response silence does not spend the command deadline reconnecting."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), future, loop.time() + 30)
    calls = 0

    async def succeed_on_third(*_: object, **kwargs: object) -> tuple[bytes, ...]:
        nonlocal calls
        calls += 1
        on_send = kwargs["on_send"]
        assert callable(on_send)
        on_send()
        if calls < 3:
            raise client_module.TransactionTimeoutError("temporary silence")
        return ()

    client._async_execute_descriptor = succeed_on_third  # type: ignore[method-assign]

    await client._async_execute_operation(operation)

    assert calls == 3
    assert operation.send_attempts == 3
    assert future.done()
    assert future.exception() is None
    assert not client._operations


@pytest.mark.asyncio
async def test_command_transport_failure_remains_pending_across_reconnect() -> None:
    """A dropped link preserves an absolute command for normal recovery."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), future, loop.time() + 30)

    async def disconnect(*_: object, **kwargs: object) -> tuple[bytes, ...]:
        on_send = kwargs["on_send"]
        assert callable(on_send)
        on_send()
        raise GattTransportError("link dropped during command")

    client._async_execute_descriptor = disconnect  # type: ignore[method-assign]

    with pytest.raises(GattTransportError, match="link dropped during command"):
        await client._async_execute_operation(operation)

    assert operation.send_attempts == 1
    assert not future.done()
    assert client._operations.popleft() is operation


@pytest.mark.asyncio
async def test_refresh_yields_and_resumes_when_command_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long best-effort refresh cannot hold the owner ahead of a control."""
    monkeypatch.setattr(client_module, "TRANSACTION_TIMEOUT", 10.0)
    client = make_client()
    channel = SilentChannel()
    client._channel = channel  # type: ignore[assignment]
    descriptors = client._protocol.refresh_requests()

    refresh_task = asyncio.create_task(client._async_run_refresh(descriptors))
    while not channel.writes:
        await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    command_future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), command_future, loop.time() + 30)
    client._operations.append(operation)
    client._operation_event.set()

    await asyncio.wait_for(refresh_task, 0.1)

    assert len(channel.writes) == 1
    assert client._refresh_pending
    assert not client._refresh_running
    assert client._refresh_resume_requests == descriptors
    diagnostics = client.diagnostic_snapshot()
    assert diagnostics["refresh_preemptions"] == 1
    assert diagnostics["last_refresh_preempted_request"] == descriptors[0].name
    assert diagnostics["pending_command_count"] == 1

    # Once the higher-priority command is gone, resume with the interrupted
    # request instead of skipping or reordering the protocol sweep.
    client._operations.clear()
    command_future.cancel()
    client._operation_event.clear()
    resumed: list[str] = []

    async def complete(
        descriptor: RequestDescriptor, *, attempts: int, **_: object
    ) -> tuple[bytes, ...]:
        assert attempts == 3
        resumed.append(descriptor.name)
        return ()

    client._async_execute_descriptor = complete  # type: ignore[method-assign]
    await client._async_run_refresh(descriptors)

    assert resumed == [descriptor.name for descriptor in descriptors]
    assert not client._refresh_pending
    assert client._refresh_resume_requests == ()


@pytest.mark.asyncio
async def test_timeout_reports_zero_received_frames_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HA-facing timeout distinguishes silence from matcher rejection."""
    monkeypatch.setattr(client_module, "TRANSACTION_TIMEOUT", 0.01)
    client = make_client()
    client._channel = SilentChannel()  # type: ignore[assignment]
    descriptor = client._protocol.initialization_requests()[0]

    with pytest.raises(client_module.TransactionTimeoutError) as raised:
        await client._async_execute_descriptor(descriptor, attempts=2)

    message = str(raised.value)
    assert "capability_b2" in message
    assert "attempt 1/2: received=0" in message
    assert "attempt 2/2: received=0" in message
    assert "ignored_sample=none" in message


@pytest.mark.asyncio
async def test_timeout_reports_unmatched_plaintext_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid but unexpected notification is included in timeout evidence."""
    monkeypatch.setattr(client_module, "TRANSACTION_TIMEOUT", 0.01)
    client = make_client()
    client._session_generation = 1
    channel = PollChannel(lambda frame: client._on_plaintext_frame(1, frame))
    client._channel = channel  # type: ignore[assignment]
    descriptor = client._protocol.initialization_requests()[0]

    with pytest.raises(client_module.TransactionTimeoutError) as raised:
        await client._async_execute_descriptor(descriptor, attempts=1)

    message = str(raised.value)
    assert "received=1" in message
    assert "ignored=1" in message
    assert build_frame(b"\xaa\x01\x01").hex(" ") in message
