"""Tests for Home Assistant Bluetooth transport diagnostics."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier import bluetooth as bluetooth_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    BluetoothUnavailableError,
    GattTransport,
    HomeAssistantBluetoothEnvironment,
    exception_chain_detail,
)


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

    async def fail_connection(*_: object, **__: object) -> None:
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
