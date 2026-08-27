"""Tests for Home Assistant Bluetooth transport diagnostics."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.govee_ble_air_purifier import bluetooth as bluetooth_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    BluetoothUnavailableError,
    GattTransport,
    GattTransportError,
    HomeAssistantBluetoothEnvironment,
    exception_chain_detail,
)


def _set_transport_timings(
    transport: GattTransport,
    **changes: float | int,
) -> None:
    transport._timings = replace(transport._timings, **changes)


@pytest.fixture(autouse=True)
def _no_real_bluez_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transport tests independent from the host Bluetooth stack."""

    async def close_address(_: str) -> None:
        return

    async def no_connected_devices(_: object) -> list[object]:
        return []

    monkeypatch.setattr(
        bluetooth_module, "close_stale_connections_by_address", close_address
    )
    monkeypatch.setattr(bluetooth_module, "get_connected_devices", no_connected_devices)


def test_exception_chain_detail_preserves_nested_causes() -> None:
    """The visible diagnostic retains the actual nested adapter failure."""
    adapter_error = RuntimeError("adapter route unavailable")
    connector_error = ConnectionError("connector failed")
    connector_error.__cause__ = adapter_error

    assert exception_chain_detail(connector_error) == (
        "ConnectionError: connector failed <- "
        "RuntimeError: adapter route unavailable"
    )


@pytest.mark.asyncio
async def test_notification_subscription_timeout_is_bounded_and_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled BlueZ start-notify call cannot block connection recovery."""
    cancelled = asyncio.Event()

    class HangingClient:
        is_connected = True

        async def start_notify(self, *_: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(transport, notification_subscribe_timeout=0.01)
    transport._client = HangingClient()  # type: ignore[assignment]
    transport._notify_characteristic = object()

    with pytest.raises(GattTransportError, match="operation=start_notify"):
        await transport.async_subscribe(lambda _: None)

    await asyncio.wait_for(cancelled.wait(), 0.1)
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["active_gatt_operation"] is None
    assert diagnostics["last_gatt_operation"] == "start_notify"
    assert diagnostics["last_gatt_operation_deadline_seconds"] == 0.01
    assert diagnostics["last_gatt_operation_timed_out"] is True
    assert diagnostics["gatt_operation_timeouts"] == 1
    assert diagnostics["pending_gatt_operation_tasks"] == 0


@pytest.mark.asyncio
async def test_write_timeout_is_bounded_and_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled write is converted into the normal transport recovery error."""
    cancelled = asyncio.Event()

    class HangingClient:
        is_connected = True

        async def write_gatt_char(self, *_: object, **__: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(transport, gatt_write_timeout=0.01)
    transport._client = HangingClient()  # type: ignore[assignment]
    transport._command_characteristic = object()

    with pytest.raises(GattTransportError, match="operation=write_gatt_char"):
        await transport.async_write(bytes(20))

    await asyncio.wait_for(cancelled.wait(), 0.1)
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["connection_stage"] == "write_command"
    assert diagnostics["last_gatt_operation"] == "write_gatt_char"
    assert diagnostics["last_gatt_operation_timed_out"] is True
    assert diagnostics["pending_gatt_operation_tasks"] == 0
    assert diagnostics["wire_tx_count"] == 0


@pytest.mark.asyncio
async def test_disconnect_timeout_cannot_block_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled backend disconnect is bounded, observed, and suppressed."""
    cancelled = asyncio.Event()

    class HangingClient:
        is_connected = True

        async def disconnect(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(transport, gatt_disconnect_timeout=0.01)
    transport._client = HangingClient()  # type: ignore[assignment]

    await asyncio.wait_for(transport.async_disconnect(), 0.1)

    await asyncio.wait_for(cancelled.wait(), 0.1)
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["is_connected"] is False
    assert diagnostics["connection_stage"] == "disconnected"
    assert diagnostics["last_gatt_operation"] == "disconnect"
    assert diagnostics["last_gatt_operation_timed_out"] is True
    assert diagnostics["pending_gatt_operation_tasks"] == 0


@pytest.mark.asyncio
async def test_connect_error_preserves_underlying_bleak_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal Home Assistant errors explain the underlying connector failure."""
    connector_kwargs: dict[str, object] = {}

    async def fail_connection(*_: object, **kwargs: object) -> None:
        connector_kwargs.update(kwargs)
        raise RuntimeError("adapter route unavailable")

    monkeypatch.setattr(
        bluetooth_module,
        "establish_connection",
        fail_connection,
    )
    transport = GattTransport(name="Bedroom purifier")
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="GVH7124TEST",
    )

    with pytest.raises(BluetoothUnavailableError) as raised:
        await transport.async_connect(device)  # type: ignore[arg-type]

    message = str(raised.value)
    assert "generation=1" in message
    assert "stage=establish_connection" in message
    assert "elapsed=" in message
    assert "pre_return_disconnects=0" in message
    assert "cause=RuntimeError: adapter route unavailable" in message
    assert "stage=establish_connection" in str(
        transport.diagnostic_snapshot()["last_error"]
    )
    assert connector_kwargs["max_attempts"] == 3


@pytest.mark.asyncio
async def test_connect_error_records_disconnect_before_connector_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed internal connector client is no longer a diagnostic blind spot."""

    async def disconnect_then_timeout(
        *_: object, disconnected_callback: object, **__: object
    ) -> None:
        disconnected_callback(SimpleNamespace())  # type: ignore[operator]
        await asyncio.sleep(0)
        raise TimeoutError

    monkeypatch.setattr(
        bluetooth_module,
        "establish_connection",
        disconnect_then_timeout,
    )
    transport = GattTransport(name="Bedroom purifier")
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="ihoment_H7129_TEST",
    )

    with pytest.raises(BluetoothUnavailableError) as raised:
        await transport.async_connect(device)  # type: ignore[arg-type]

    assert "pre_return_disconnects=1" in str(raised.value)
    assert "connection attempt deadline exceeded" not in str(raised.value)
    assert "cause=TimeoutError: TimeoutError()" in str(raised.value)
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["pre_return_disconnects"] == 1
    assert diagnostics["last_disconnect_stage"] == "establish_connection"
    assert isinstance(diagnostics["last_disconnect_elapsed_seconds"], float)


@pytest.mark.asyncio
async def test_connection_deadline_cleans_partial_client_and_records_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck BlueZ attempt is released before a fresh-route retry."""
    address_cleanups: list[str] = []

    class FakeClient:
        def __init__(self, *_: object, **__: object) -> None:
            self.is_connected = False
            self.services = SimpleNamespace(services={})
            self.disconnect_calls = 0
            clients.append(self)

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    clients: list[FakeClient] = []

    async def hang_connection(
        client_class: object, device: object, *_: object, **__: object
    ) -> None:
        client_class(device)  # type: ignore[operator]
        await asyncio.Event().wait()

    async def close_address(address: str) -> None:
        address_cleanups.append(address)

    async def no_connected_devices(_: object) -> list[object]:
        return []

    monkeypatch.setattr(bluetooth_module, "BleakClientWithServiceCache", FakeClient)
    monkeypatch.setattr(bluetooth_module, "establish_connection", hang_connection)
    monkeypatch.setattr(
        bluetooth_module, "close_stale_connections_by_address", close_address
    )
    monkeypatch.setattr(bluetooth_module, "get_connected_devices", no_connected_devices)
    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(transport, connection_attempt_timeout=0.01)
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="ihoment_H7129_TEST",
    )

    for attempt in range(1, 3):
        with pytest.raises(BluetoothUnavailableError) as raised:
            await transport.async_connect(device)  # type: ignore[arg-type]
        assert f"attempt={attempt}" in str(raised.value)
        assert "connection attempt deadline exceeded" in str(raised.value)

    assert len(clients) == 2
    assert [client.disconnect_calls for client in clients] == [1, 1]
    assert address_cleanups == [device.address] * 4
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["connecting_client_present"] is False
    timeout_diagnostics = diagnostics["last_connection_timeout_diagnostics"]
    assert timeout_diagnostics == {
        "partial_client_present": True,
        "partial_client_connected": False,
        "partial_client_connected_error": None,
        "service_count": 0,
        "service_error": None,
        "bluez_connection_count": 0,
        "bluez_connection_error": None,
        "device_path": None,
        "adapter": None,
        "cleanup": {
            "success": True,
            "remaining_connections": 0,
            "elapsed_seconds": pytest.approx(0, abs=0.01),
            "error": None,
        },
    }
    failures = diagnostics["recent_connection_failures"]
    assert isinstance(failures, list)
    assert len(failures) == 2
    assert "attempt=1" in failures[0]
    assert "attempt=2" in failures[1]


@pytest.mark.asyncio
async def test_connection_deadline_inspects_live_partial_client_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout evidence distinguishes a connected client from stalled discovery."""
    class ConnectedClient:
        def __init__(self, *_: object, **__: object) -> None:
            self.is_connected = True
            self.services = SimpleNamespace(services={"service": object()})
            clients.append(self)

        async def disconnect(self) -> None:
            self.is_connected = False

    clients: list[ConnectedClient] = []

    async def hang_connection(
        client_class: object, device: object, *_: object, **__: object
    ) -> None:
        client_class(device)  # type: ignore[operator]
        await asyncio.Event().wait()

    async def connected_while_client_is_live(device: object) -> list[object]:
        return [device] if clients and clients[0].is_connected else []

    monkeypatch.setattr(
        bluetooth_module, "BleakClientWithServiceCache", ConnectedClient
    )
    monkeypatch.setattr(bluetooth_module, "establish_connection", hang_connection)
    monkeypatch.setattr(
        bluetooth_module, "get_connected_devices", connected_while_client_is_live
    )
    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(transport, connection_attempt_timeout=0.01)
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="ihoment_H7129_TEST",
        details={"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"},
    )

    with pytest.raises(BluetoothUnavailableError) as raised:
        await transport.async_connect(device)  # type: ignore[arg-type]

    diagnostics = transport.diagnostic_snapshot()[
        "last_connection_timeout_diagnostics"
    ]
    assert isinstance(diagnostics, dict)
    assert diagnostics["partial_client_present"] is True
    assert diagnostics["partial_client_connected"] is True
    assert diagnostics["service_count"] == 1
    assert diagnostics["bluez_connection_count"] == 1
    assert diagnostics["device_path"] == (
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    )
    assert diagnostics["adapter"] == "hci0"
    assert diagnostics["cleanup"]["success"] is True
    assert diagnostics["cleanup"]["remaining_connections"] == 0
    assert "timeout_diagnostics=" in str(raised.value)


@pytest.mark.asyncio
async def test_surviving_bluez_connection_blocks_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry cannot consume another slot while BlueZ retains the address."""
    connector_called = False

    async def close_address(_: str) -> None:
        return

    async def still_connected(device: object) -> list[object]:
        return [device]

    async def unexpected_connector(*_: object, **__: object) -> None:
        nonlocal connector_called
        connector_called = True

    monkeypatch.setattr(
        bluetooth_module, "close_stale_connections_by_address", close_address
    )
    monkeypatch.setattr(bluetooth_module, "get_connected_devices", still_connected)
    monkeypatch.setattr(bluetooth_module, "establish_connection", unexpected_connector)
    transport = GattTransport(name="Bedroom purifier")
    _set_transport_timings(
        transport,
        stale_connection_cleanup_timeout=0.01,
    )
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="ihoment_H7129_TEST",
    )

    with pytest.raises(BluetoothUnavailableError) as raised:
        await transport.async_connect(device)  # type: ignore[arg-type]

    assert "Refusing to connect" in str(raised.value)
    assert connector_called is False
    diagnostics = transport.diagnostic_snapshot()
    assert diagnostics["connection_attempts"] == 0
    assert diagnostics["address_cleanup_failures"] == 1
    assert diagnostics["last_address_cleanup_remaining_connections"] == 1


@pytest.mark.asyncio
async def test_stale_cleanup_never_disconnects_healthy_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive cleanup cannot tear down the transport's ready connection."""
    close_address = AsyncMock()
    monkeypatch.setattr(
        bluetooth_module, "close_stale_connections_by_address", close_address
    )
    transport = GattTransport(name="Bedroom purifier")
    transport._client = SimpleNamespace(is_connected=True)  # type: ignore[assignment]

    cleaned = await transport.async_cleanup_stale_connection(reason="test")

    assert cleaned is False
    close_address.assert_not_awaited()


def test_route_diagnostics_reports_advertisement_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route diagnostics distinguish a fresh advertisement from stale cache."""
    service_info = SimpleNamespace(
        name="ihoment_H7129_TEST",
        source="local-adapter",
        rssi=-71,
        tx_power=None,
        time=time.monotonic() - 2.0,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_last_service_info",
        lambda *_args, **_kwargs: service_info,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
    )

    route = environment.route_diagnostics()

    assert route["present"] is True
    assert route["source"] == "local-adapter"
    assert route["rssi"] == -71
    assert 1.9 <= route["advertisement_age_seconds"] <= 2.1
    assert route["callback_age_seconds"] is None


def test_recent_advertisement_uses_timestamp_not_presence_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backoff remains short only while connectable evidence is actually recent."""
    service_info = SimpleNamespace(time=time.monotonic() - 2.0)
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_last_service_info",
        lambda *_args, **_kwargs: service_info,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
    )

    assert environment.has_recent_advertisement(5.0)

    service_info.time = time.monotonic() - 6.0
    assert not environment.has_recent_advertisement(5.0)


@pytest.mark.asyncio
async def test_recovery_wait_is_woken_by_connectable_advertisement() -> None:
    """Backoff can react to a callback outside a route-selection wait."""
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
    )
    cutoff = time.monotonic()
    wait_task = asyncio.create_task(
        environment.async_wait_for_advertisement_after(cutoff)
    )
    await asyncio.sleep(0)

    environment._advertisement_received(
        SimpleNamespace(
            name="ihoment_H7129_TEST",
            source="test-adapter",
            rssi=-75,
            connectable=True,
            time=time.monotonic(),
        )
    )

    await asyncio.wait_for(wait_task, timeout=0.1)


@pytest.mark.asyncio
async def test_callback_registration_disables_cached_replay_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replayed cache entry cannot masquerade as a live advertisement."""
    registration: dict[str, object] = {}

    def register(*args: object, **kwargs: object) -> object:
        registration["args"] = args
        registration["kwargs"] = kwargs
        return lambda: None

    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "BluetoothCallbackReplay",
        SimpleNamespace(DISABLED="disabled"),
        raising=False,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_register_callback",
        register,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
    )

    await environment.async_start()

    assert registration["kwargs"] == {"replay": "disabled"}


@pytest.mark.asyncio
async def test_wait_for_fresh_device_accepts_recent_cached_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subsecond Home Assistant route is usable without another callback."""
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="H7129")
    recent_info = SimpleNamespace(
        device=device,
        name="ihoment_H7129_TEST",
        source="test-adapter",
        rssi=-75,
        tx_power=None,
        connectable=True,
        time=time.monotonic() - 0.376,
    )
    clear_history = Mock()
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_last_service_info",
        lambda *_args, **_kwargs: recent_info,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_ble_device_from_address",
        lambda *_args, **_kwargs: device,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_clear_advertisement_history",
        clear_history,
        raising=False,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        device.address,
    )

    assert await environment.async_wait_for_fresh_device(1.0) is device

    clear_history.assert_not_called()
    route = environment.route_diagnostics()
    assert route["last_route_selection"] == "recent_cache"
    assert route["selected_advertisement_age_seconds"] == pytest.approx(
        0.376, abs=0.05
    )
    assert route["fresh_advertisements"] == 0


@pytest.mark.asyncio
async def test_wait_for_fresh_device_rejects_stale_cache_and_uses_live_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old cached route remains unusable until a live packet arrives."""
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="H7129")
    stale_info = SimpleNamespace(
        device=device,
        name="ihoment_H7129_TEST",
        source="test-adapter",
        rssi=-80,
        tx_power=None,
        connectable=True,
        time=time.monotonic() - 30,
    )
    current_info = stale_info
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_last_service_info",
        lambda *_args, **_kwargs: current_info,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_ble_device_from_address",
        lambda *_args, **_kwargs: device,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        device.address,
    )

    wait_task = asyncio.create_task(environment.async_wait_for_fresh_device(1.0))
    await asyncio.sleep(0)
    assert not wait_task.done()

    current_info = SimpleNamespace(
        **{
            **stale_info.__dict__,
            "rssi": -73,
            "time": time.monotonic(),
        }
    )
    environment._advertisement_received(current_info)

    assert await wait_task is device
    route = environment.route_diagnostics()
    assert route["rssi"] == -73
    assert route["fresh_advertisements"] == 1
    assert route["last_route_selection"] == "live_callback"


@pytest.mark.asyncio
async def test_retry_requires_advertisement_newer_than_selected_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry cannot immediately reuse the exact cached packet that just failed."""
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="H7129")
    first_time = time.monotonic() - 0.2
    current_info = SimpleNamespace(
        device=device,
        name="ihoment_H7129_TEST",
        source="test-adapter",
        rssi=-75,
        tx_power=None,
        connectable=True,
        time=first_time,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_last_service_info",
        lambda *_args, **_kwargs: current_info,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_ble_device_from_address",
        lambda *_args, **_kwargs: device,
    )
    environment = HomeAssistantBluetoothEnvironment(
        SimpleNamespace(),  # type: ignore[arg-type]
        device.address,
    )

    assert await environment.async_wait_for_fresh_device(1.0) is device

    retry_task = asyncio.create_task(environment.async_wait_for_fresh_device(1.0))
    await asyncio.sleep(0)
    assert not retry_task.done()

    current_info = SimpleNamespace(
        **{
            **current_info.__dict__,
            "rssi": -73,
            "time": time.monotonic(),
        }
    )
    assert await retry_task is device
    route = environment.route_diagnostics()
    assert route["last_route_selection"] == "newer_cache"
    assert route["fresh_advertisements"] == 0


def test_reachability_diagnostics_uses_connection_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure logs request Home Assistant's connection-specific diagnosis."""
    calls: list[tuple[object, str, object]] = []
    connection_intent = object()
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "BluetoothReachabilityIntent",
        SimpleNamespace(CONNECTION=connection_intent),
        raising=False,
    )
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_address_reachability_diagnostics",
        lambda hass, address, intent: (
            calls.append((hass, address, intent)) or "one connectable route"
        ),
        raising=False,
    )
    hass = SimpleNamespace()
    environment = HomeAssistantBluetoothEnvironment(
        hass,  # type: ignore[arg-type]
        "AA:BB:CC:DD:EE:FF",
    )

    assert environment.reachability_diagnostics() == "one connectable route"
    assert calls == [(hass, environment.address, connection_intent)]
