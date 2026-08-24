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
    CommandDeadlineExceeded,
    ReliablePurifierClient,
    _Operation,
)
from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import (
    DeviceProfile,
    FanMode,
    Model,
    PurifierState,
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

    async def async_start(self) -> None:
        return

    def route_diagnostics(self) -> dict[str, object]:
        return {"present": True, "source": "test", "rssi": -50}

    def reachability_diagnostics(self) -> str:
        return "test route is reachable"

    def has_recent_advertisement(self, _: float) -> bool:
        return self.recent_advertisement

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

    async def async_send(self, _: bytes) -> None:
        return

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


def test_startup_budget_is_five_minutes() -> None:
    """Weak-signal setup gets a bounded five-minute recovery window."""
    maximum_first_backoff = client_module.BACKOFF_MIN * 1.2
    two_cycle_budget = (
        2 * (client_module.FRESH_ADVERTISEMENT_TIMEOUT + CONNECTION_ATTEMPT_TIMEOUT)
        + maximum_first_backoff
    )

    assert client_module.STARTUP_TIMEOUT >= two_cycle_budget
    assert client_module.STARTUP_TIMEOUT == 300.0


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

    with pytest.raises(BluetoothUnavailableError) as raised:
        await client.async_start()

    message = str(raised.value)
    assert "did not become ready" in message
    assert "cause=TimeoutError" in message
    assert "route={'present': True, 'source': 'test', 'rssi': -50}" in message
    assert "reachability=test route is reachable" in message
    assert updates == []


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
    """Retry count is bounded, while queried applied state prevents a replay."""
    client = make_client()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    operation = _Operation(SetPower(True), future, loop.time() + 30)

    async def fail_transaction(*_: object, **__: object) -> tuple[bytes, ...]:
        raise TimeoutError

    client._async_execute_descriptor = fail_transaction  # type: ignore[method-assign]
    for expected_attempt in range(1, 4):
        with pytest.raises(TimeoutError):
            await client._async_execute_operation(operation)
        assert operation.send_attempts == expected_attempt
        if expected_attempt < 3:
            assert client._operations.popleft() is operation

    with pytest.raises(CommandDeadlineExceeded):
        await future
    assert operation not in client._operations

    reconciled_future: asyncio.Future[None] = loop.create_future()
    reconciled = _Operation(
        SetPower(True), reconciled_future, loop.time() + 30, send_attempts=1
    )
    client.state = PurifierState(power=True)
    await client._async_execute_operation(reconciled)
    assert reconciled_future.done()
    assert reconciled_future.exception() is None


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
