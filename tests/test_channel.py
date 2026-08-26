"""Tests for plaintext and H7129 application channels."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from custom_components.govee_ble_air_purifier import channel as channel_module
from custom_components.govee_ble_air_purifier.channel import (
    ChannelNotReadyError,
    H7129SessionChannel,
    NegotiationError,
)
from custom_components.govee_ble_air_purifier.crypto import (
    COMMUNICATION_KEY,
    decrypt_frame,
    encrypt_frame,
)
from custom_components.govee_ble_air_purifier.frame import build_frame


class FakeTransport:
    """Respond synchronously to negotiation writes like a fast BLE device."""

    def __init__(self, session_key: bytes) -> None:
        self.session_key = session_key
        self.callback: Callable[[bytes], None] | None = None
        self.writes: list[bytes] = []

    async def async_subscribe(self, callback: Callable[[bytes], None]) -> None:
        self.callback = callback

    async def async_write(self, data: bytes) -> None:
        self.writes.append(data)
        plaintext = decrypt_frame(data, COMMUNICATION_KEY)
        assert self.callback is not None
        if plaintext[:2] == b"\xe7\x01":
            response = build_frame(b"\xe7\x01" + self.session_key)
            wire_response = encrypt_frame(response, COMMUNICATION_KEY)
            self.callback(wire_response)
            self.callback(wire_response)  # observed exact duplicate
        elif plaintext[:2] == b"\xe7\x02":
            response = encrypt_frame(build_frame(b"\xe7\x02"), COMMUNICATION_KEY)
            self.callback(response)
            self.callback(response)  # observed immediate duplicate


@pytest.mark.asyncio
async def test_h7129_ignores_delayed_negotiation_frames() -> None:
    """Delayed e7 duplicates never leak into application transactions."""
    session_key = b"0123456789abcdef"
    transport = FakeTransport(session_key)
    received: list[bytes] = []
    channel = H7129SessionChannel(transport, received.append)  # type: ignore[arg-type]

    await channel.async_establish()
    assert channel.ready
    assert len(transport.writes) == 2
    assert received == []

    assert transport.callback is not None
    delayed_e7 = encrypt_frame(build_frame(b"\xe7\x02"), COMMUNICATION_KEY)
    transport.callback(delayed_e7)
    assert received == []

    state = build_frame(b"\xaa\x01\x01")
    transport.callback(encrypt_frame(state, session_key))
    assert received == [state]

    # ee-aa is allowed under the communication key during an active session.
    refresh = build_frame(b"\xee\xaa")
    transport.callback(encrypt_frame(refresh, COMMUNICATION_KEY))
    assert received == [state, refresh]


@pytest.mark.asyncio
async def test_h7129_invalidation_forgets_session_key() -> None:
    """A disconnected session can never send with its former key."""
    transport = FakeTransport(b"0123456789abcdef")
    channel = H7129SessionChannel(transport, lambda _: None)  # type: ignore[arg-type]
    await channel.async_establish()

    channel.invalidate()

    with pytest.raises(ChannelNotReadyError):
        await channel.async_send(build_frame(b"\xaa\x01"))


@pytest.mark.asyncio
async def test_h7129_retries_same_negotiation_request_on_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost negotiation packet is retried without replacing the GATT link."""
    monkeypatch.setattr(channel_module, "NEGOTIATION_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(channel_module, "NEGOTIATION_PHASE_TIMEOUT", 0.03)
    session_key = b"0123456789abcdef"

    class DelayedTransport(FakeTransport):
        async def async_write(self, data: bytes) -> None:
            self.writes.append(data)
            plaintext = decrypt_frame(data, COMMUNICATION_KEY)
            assert self.callback is not None
            if plaintext[:2] == b"\xe7\x01" and self.writes.count(data) == 1:
                response = encrypt_frame(
                    build_frame(b"\xe7\x01" + self.session_key),
                    COMMUNICATION_KEY,
                )
                asyncio.get_running_loop().call_later(0.015, self.callback, response)
            elif plaintext[:2] == b"\xe7\x02":
                self.callback(
                    encrypt_frame(build_frame(b"\xe7\x02"), COMMUNICATION_KEY)
                )

    transport = DelayedTransport(session_key)
    channel = H7129SessionChannel(transport, lambda _: None)  # type: ignore[arg-type]

    await channel.async_establish()

    assert channel.ready
    assert len(transport.writes) == 3
    assert transport.writes[0] == transport.writes[1]


@pytest.mark.asyncio
async def test_h7129_disconnect_aborts_negotiation_without_more_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidating a dropped connection interrupts the open phase immediately."""
    monkeypatch.setattr(channel_module, "NEGOTIATION_RETRY_INTERVAL", 60.0)
    monkeypatch.setattr(channel_module, "NEGOTIATION_PHASE_TIMEOUT", 180.0)

    class SilentTransport:
        callback: Callable[[bytes], None] | None = None
        writes: list[bytes] = []

        async def async_subscribe(self, callback: Callable[[bytes], None]) -> None:
            self.callback = callback

        async def async_write(self, data: bytes) -> None:
            self.writes.append(data)

    transport = SilentTransport()
    channel = H7129SessionChannel(transport, lambda _: None)  # type: ignore[arg-type]
    establish = asyncio.create_task(channel.async_establish())
    await asyncio.sleep(0)

    channel.invalidate()

    with pytest.raises(NegotiationError, match="invalidated"):
        await establish
    assert len(transport.writes) == 1


@pytest.mark.asyncio
async def test_h7129_reconnects_only_after_phase_retry_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent phase transmits three identical requests before it fails."""
    monkeypatch.setattr(channel_module, "NEGOTIATION_RETRY_INTERVAL", 0.005)
    monkeypatch.setattr(channel_module, "NEGOTIATION_PHASE_TIMEOUT", 0.015)

    class SilentTransport:
        writes: list[bytes]

        def __init__(self) -> None:
            self.writes = []

        async def async_subscribe(self, _: Callable[[bytes], None]) -> None:
            return

        async def async_write(self, data: bytes) -> None:
            self.writes.append(data)

    transport = SilentTransport()
    channel = H7129SessionChannel(transport, lambda _: None)  # type: ignore[arg-type]

    with pytest.raises(NegotiationError, match=r"3 attempt\(s\)"):
        await channel.async_establish()

    assert len(transport.writes) == 3
    assert len(set(transport.writes)) == 1
