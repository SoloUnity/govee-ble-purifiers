"""Plaintext and H7129 application-session channels."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from .crypto import (
    COMMUNICATION_KEY,
    create_negotiation_frame,
    decrypt_frame,
    encrypt_frame,
    extract_session_key,
)
from .frame import FrameError, build_frame, validate_frame

_LOGGER = logging.getLogger(__name__)

NEGOTIATION_PHASE_TIMEOUT = 3.0
NEGOTIATION_STEP_DELAY = 0.001
FIRST_APPLICATION_DELAY = 0.003

PlaintextCallback = Callable[[bytes], None]


class FrameTransport(Protocol):
    """Protocol-agnostic byte transport required by a secure channel."""

    async def async_subscribe(self, callback: PlaintextCallback) -> None: ...

    async def async_write(self, data: bytes) -> None: ...


class ChannelError(ConnectionError):
    """Base error for application-channel failures."""


class ChannelNotReadyError(ChannelError):
    """Raised when application traffic is attempted before session readiness."""


class NegotiationError(ChannelError):
    """Raised when H7129 session negotiation fails or times out."""


class SecureChannel:
    """Transform plaintext application frames over an opaque BLE transport."""

    def __init__(
        self, transport: FrameTransport, plaintext_callback: PlaintextCallback
    ) -> None:
        self._transport = transport
        self._plaintext_callback = plaintext_callback
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def async_establish(self) -> None:
        raise NotImplementedError

    async def async_send(self, plaintext: bytes) -> None:
        raise NotImplementedError

    def invalidate(self) -> None:
        """Discard all connection-scoped state."""
        self._ready = False


class PlaintextChannel(SecureChannel):
    """Pass validated H7124 application frames through unchanged."""

    async def async_establish(self) -> None:
        _LOGGER.debug("Establishing H7124 plaintext channel")
        self.invalidate()
        await self._transport.async_subscribe(self._wire_received)
        self._ready = True
        # The captured H7124 connection sent its first initialization request
        # about 3 ms after notifications were enabled.
        await asyncio.sleep(FIRST_APPLICATION_DELAY)
        _LOGGER.debug(
            "H7124 plaintext channel ready after %.3f-second application delay",
            FIRST_APPLICATION_DELAY,
        )

    async def async_send(self, plaintext: bytes) -> None:
        if not self._ready:
            raise ChannelNotReadyError("Plaintext channel is not ready")
        plaintext = validate_frame(plaintext)
        _LOGGER.debug("H7124 TX plaintext: %s", plaintext.hex(" "))
        await self._transport.async_write(plaintext)

    def _wire_received(self, wire_frame: bytes) -> None:
        try:
            plaintext = validate_frame(wire_frame)
        except FrameError as err:
            _LOGGER.debug(
                "Discarding invalid H7124 notification: length=%d wire=%s error=%s",
                len(wire_frame),
                wire_frame.hex(" "),
                err,
            )
            return
        _LOGGER.debug("H7124 RX plaintext: %s", plaintext.hex(" "))
        self._plaintext_callback(plaintext)


class H7129SessionChannel(SecureChannel):
    """Negotiate and own one H7129 session key per BLE connection."""

    def __init__(
        self, transport: FrameTransport, plaintext_callback: PlaintextCallback
    ) -> None:
        super().__init__(transport, plaintext_callback)
        self._session_key: bytes | None = None
        self._phase = 0
        self._phase_future: asyncio.Future[bytes] | None = None
        self._refresh_during_negotiation = False

    async def async_establish(self) -> None:
        """Subscribe, then complete the documented e7-01/e7-02 exchange."""
        _LOGGER.debug("Establishing H7129 encrypted session channel")
        self.invalidate()
        await self._transport.async_subscribe(self._wire_received)

        try:
            response_01 = await self._exchange_step(0x01)
            self._session_key = extract_session_key(response_01)

            # Captures place e7-02 1-2 ms after e7-01. Yielding for one
            # millisecond preserves that ordering without busy waiting.
            await asyncio.sleep(NEGOTIATION_STEP_DELAY)
            await self._exchange_step(0x02)
        except (TimeoutError, ValueError, ConnectionError) as err:
            failed_phase = self._phase
            self.invalidate()
            detail = str(err).strip() or repr(err)
            raise NegotiationError(
                "H7129 session negotiation failed during "
                f"e7-{failed_phase:02x}; cause={type(err).__name__}: {detail}"
            ) from err
        except asyncio.CancelledError:
            self.invalidate()
            raise

        self._phase = 3
        self._ready = True
        await asyncio.sleep(FIRST_APPLICATION_DELAY)
        _LOGGER.debug(
            "H7129 session channel ready after %.3f-second application delay; "
            "session key established but intentionally not logged",
            FIRST_APPLICATION_DELAY,
        )

        # ee-aa is legal under the communication key during negotiation. It
        # has no state payload, so deliver one coalesced refresh after ready.
        if self._refresh_during_negotiation:
            self._refresh_during_negotiation = False
            self._plaintext_callback(build_frame(b"\xee\xaa"))

    async def _exchange_step(self, step: int) -> bytes:
        self._phase = step
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        self._phase_future = future
        request = create_negotiation_frame(step)
        started = loop.time()
        _LOGGER.debug(
            "H7129 negotiation TX e7-%02x; random padding and keys are not logged",
            step,
        )
        await self._transport.async_write(encrypt_frame(request, COMMUNICATION_KEY))
        try:
            async with asyncio.timeout(NEGOTIATION_PHASE_TIMEOUT):
                response = await future
                _LOGGER.debug(
                    "H7129 negotiation RX e7-%02x completed in %.3f seconds",
                    step,
                    loop.time() - started,
                )
                return response
        finally:
            if self._phase_future is future:
                self._phase_future = None

    async def async_send(self, plaintext: bytes) -> None:
        key = self._session_key
        if not self._ready or key is None:
            raise ChannelNotReadyError("H7129 session is not ready")
        plaintext = validate_frame(plaintext)
        _LOGGER.debug("H7129 TX application plaintext: %s", plaintext.hex(" "))
        await self._transport.async_write(encrypt_frame(plaintext, key))

    def invalidate(self) -> None:
        """Forget the negotiated key immediately on any disconnect/failure."""
        super().invalidate()
        self._session_key = None
        self._phase = 0
        self._refresh_during_negotiation = False
        future = self._phase_future
        self._phase_future = None
        if future is not None and not future.done():
            future.set_exception(NegotiationError("Connection was invalidated"))

    def _wire_received(self, wire_frame: bytes) -> None:
        """Decrypt one frame while safely recognizing delayed e7 duplicates."""
        communication_plaintext = self._try_decrypt(wire_frame, COMMUNICATION_KEY)
        if communication_plaintext is not None:
            prefix = communication_plaintext[:2]
            if prefix in (b"\xe7\x01", b"\xe7\x02"):
                self._handle_negotiation_frame(communication_plaintext)
                return
            if prefix == b"\xee\xaa":
                if self._ready:
                    self._plaintext_callback(communication_plaintext)
                else:
                    self._refresh_during_negotiation = True
                return

        key = self._session_key
        if key is None:
            _LOGGER.debug(
                "Discarding non-negotiation H7129 frame before session key: "
                "wire_length=%d",
                len(wire_frame),
            )
            return

        session_plaintext = self._try_decrypt(wire_frame, key)
        if session_plaintext is None:
            _LOGGER.debug(
                "Discarding H7129 notification that failed checksum validation "
                "under both communication and session keys: wire_length=%d",
                len(wire_frame),
            )
            return
        if not self._ready:
            _LOGGER.debug("Discarding application frame before session confirmation")
            return
        _LOGGER.debug("H7129 RX application plaintext: %s", session_plaintext.hex(" "))
        self._plaintext_callback(session_plaintext)

    def _handle_negotiation_frame(self, plaintext: bytes) -> None:
        step = plaintext[1]
        future = self._phase_future
        if step != self._phase or future is None or future.done():
            # Exact duplicate and delayed e7 frames are observed in captures.
            # They never complete an application transaction.
            _LOGGER.debug("Ignoring duplicate/delayed e7-%02x frame", step)
            return
        future.set_result(plaintext)

    @staticmethod
    def _try_decrypt(wire_frame: bytes, key: bytes) -> bytes | None:
        with suppress(ValueError):
            return decrypt_frame(wire_frame, key)
        return None
