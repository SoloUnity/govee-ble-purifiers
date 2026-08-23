"""Tests for Home Assistant Bluetooth transport diagnostics."""

from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier import bluetooth as bluetooth_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    BluetoothUnavailableError,
    GattTransport,
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
    assert "cause=RuntimeError: adapter route unavailable" in message
    assert transport.diagnostic_snapshot()["last_error"] == (
        "RuntimeError: adapter route unavailable"
    )
