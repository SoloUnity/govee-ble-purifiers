"""Tests for plaintext and H7129 application channels."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.govee_ble_air_purifier.channel import (
    ChannelNotReadyError,
    H7129SessionChannel,
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
