"""Home Assistant Bluetooth access and a small, protocol-agnostic GATT transport."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
NOTIFY_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
COMMAND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"

CONNECT_TIMEOUT = 15.0
CONNECT_ATTEMPTS = 3

NotificationCallback = Callable[[bytes], None]
DisconnectCallback = Callable[[int], None]


class BluetoothUnavailableError(ConnectionError):
    """Raised when there is no usable Bluetooth route to the purifier."""


class GattTransportError(ConnectionError):
    """Raised when a GATT operation cannot be completed."""


class HomeAssistantBluetoothEnvironment:
    """Expose Home Assistant's shared scanner without owning a scanner.

    A BLEDevice is deliberately looked up again for every connection attempt.
    Home Assistant can then choose the currently best local adapter or proxy.
    """

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self.address = address
        self._advertisement_event = asyncio.Event()
        self._cancel_advertisement: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Listen passively for a fresh route to the configured address."""
        if self._cancel_advertisement is not None:
            return

        self._cancel_advertisement = bluetooth.async_register_callback(
            self._hass,
            self._advertisement_received,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    async def async_stop(self) -> None:
        """Stop listening for advertisements."""
        if self._cancel_advertisement is not None:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        self._advertisement_event.set()

    def _advertisement_received(self, *_: Any) -> None:
        self._advertisement_event.set()

    def get_connectable_device(self) -> BLEDevice | None:
        """Return the best currently reachable connectable route."""
        return bluetooth.async_ble_device_from_address(
            self._hass, self.address, connectable=True
        )

    def address_is_present(self) -> bool:
        """Return whether a connectable scanner can currently see the address."""
        return bluetooth.async_address_present(
            self._hass, self.address, connectable=True
        )

    async def async_wait_for_device(self, timeout: float) -> BLEDevice | None:
        """Wait for an advertisement, then resolve the best current route."""
        device = self.get_connectable_device()
        if device is not None:
            return device

        self._advertisement_event.clear()

        # Close the check/clear race if an advertisement arrived in between.
        device = self.get_connectable_device()
        if device is not None:
            return device

        try:
            async with asyncio.timeout(timeout):
                await self._advertisement_event.wait()
        except TimeoutError:
            return None

        return self.get_connectable_device()


class GattTransport:
    """Own one active GATT connection and transport opaque 20-byte frames."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self._client: BleakClientWithServiceCache | None = None
        self._notify_characteristic: Any = None
        self._command_characteristic: Any = None
        self._notification_callback: NotificationCallback | None = None
        self._disconnect_callback: DisconnectCallback | None = None
        self._write_lock = asyncio.Lock()
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def generation(self) -> int:
        """Return the generation of the current connection attempt."""
        return self._generation

    @property
    def is_connected(self) -> bool:
        """Return whether the current client reports an active connection."""
        return self._client is not None and self._client.is_connected

    def set_disconnect_callback(self, callback: DisconnectCallback | None) -> None:
        """Set the callback invoked for an unexpected current disconnect."""
        self._disconnect_callback = callback

    async def async_connect(self, device: BLEDevice) -> int:
        """Create and connect a fresh Bleak client."""
        await self.async_disconnect()
        self._generation += 1
        generation = self._generation
        self._loop = asyncio.get_running_loop()

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.name,
                    disconnected_callback=lambda connected_client: (
                        self._disconnected_from_bleak(connected_client, generation)
                    ),
                    max_attempts=CONNECT_ATTEMPTS,
                )
        except Exception as err:
            raise BluetoothUnavailableError(
                f"Unable to connect to {self.name} at {device.address}"
            ) from err

        if generation != self._generation:
            with suppress(Exception):
                await client.disconnect()
            raise BluetoothUnavailableError("Connection was superseded")

        self._client = client
        try:
            self._resolve_characteristics(client.services)
        except Exception:
            await self.async_disconnect()
            raise

        return generation

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> None:
        service = services.get_service(SERVICE_UUID)
        notify_characteristic = services.get_characteristic(NOTIFY_CHARACTERISTIC_UUID)
        command_characteristic = services.get_characteristic(
            COMMAND_CHARACTERISTIC_UUID
        )
        if (
            service is None
            or notify_characteristic is None
            or command_characteristic is None
        ):
            raise GattTransportError(
                "The purifier GATT service or characteristics were not found"
            )
        self._notify_characteristic = notify_characteristic
        self._command_characteristic = command_characteristic

    async def async_subscribe(self, callback: NotificationCallback) -> None:
        """Subscribe to notifications before any transaction is sent."""
        client = self._require_client()
        generation = self._generation
        self._notification_callback = callback

        def notification_received(_: Any, data: bytearray) -> None:
            if generation != self._generation:
                return
            current_callback = self._notification_callback
            if current_callback is not None:
                current_callback(bytes(data))

        try:
            await client.start_notify(
                self._notify_characteristic, notification_received
            )
        except Exception as err:
            raise GattTransportError("Unable to subscribe to notifications") from err

    async def async_write(self, data: bytes) -> None:
        """Write one frame using ATT Write Command (without response)."""
        if len(data) != 20:
            raise ValueError(f"Expected a 20-byte frame, got {len(data)} bytes")

        async with self._write_lock:
            client = self._require_client()
            generation = self._generation
            try:
                await client.write_gatt_char(
                    self._command_characteristic, data, response=False
                )
            except Exception as err:
                raise GattTransportError("Unable to write command frame") from err
            if generation != self._generation or not client.is_connected:
                raise GattTransportError("Connection dropped during command write")

    async def async_disconnect(self) -> None:
        """Close the active client and invalidate all of its callbacks."""
        client = self._client
        self._client = None
        self._notification_callback = None
        self._notify_characteristic = None
        self._command_characteristic = None
        if client is None:
            return

        # Clearing the client before awaiting makes the Bleak disconnect callback
        # a deliberate/stale disconnect rather than an unexpected one.
        with suppress(Exception):
            if client.is_connected:
                await client.disconnect()

    def _require_client(self) -> BleakClientWithServiceCache:
        client = self._client
        if client is None or not client.is_connected:
            raise GattTransportError("Purifier is not connected")
        return client

    def _disconnected_from_bleak(
        self, disconnected_client: BleakClientWithServiceCache, generation: int
    ) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(
            self._handle_disconnected, disconnected_client, generation
        )

    def _handle_disconnected(
        self, disconnected_client: BleakClientWithServiceCache, generation: int
    ) -> None:
        if generation != self._generation or disconnected_client is not self._client:
            return
        self._client = None
        self._notification_callback = None
        self._notify_characteristic = None
        self._command_characteristic = None
        _LOGGER.debug("%s disconnected (generation %s)", self.name, generation)
        if self._disconnect_callback is not None:
            self._disconnect_callback(generation)
