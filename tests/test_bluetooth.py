"""Tests for Home Assistant Bluetooth transport diagnostics."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.govee_ble_air_purifier import bluetooth as bluetooth_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    BluetoothUnavailableError,
    GattTransport,
    HomeAssistantBluetoothEnvironment,
    exception_chain_detail,
)


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
    assert connector_kwargs["max_attempts"] == 1


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
    monkeypatch.setattr(bluetooth_module, "CONNECTION_ATTEMPT_TIMEOUT", 0.01)
    transport = GattTransport(name="Bedroom purifier")
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
    failures = diagnostics["recent_connection_failures"]
    assert isinstance(failures, list)
    assert len(failures) == 2
    assert "attempt=1" in failures[0]
    assert "attempt=2" in failures[1]


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
    monkeypatch.setattr(bluetooth_module, "STALE_CONNECTION_CLEANUP_TIMEOUT", 0.01)
    transport = GattTransport(name="Bedroom purifier")
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
async def test_wait_for_fresh_device_rejects_cache_and_uses_live_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection starts only after a post-cutoff advertisement is available."""
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
    clears: list[str] = []
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
    monkeypatch.setattr(
        bluetooth_module.bluetooth,
        "async_clear_advertisement_history",
        lambda _hass, address: clears.append(address),
        raising=False,
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
    assert clears == [device.address]
    route = environment.route_diagnostics()
    assert route["rssi"] == -73
    assert route["fresh_advertisements"] == 1


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
