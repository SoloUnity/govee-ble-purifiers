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


def exception_detail(error: BaseException) -> str:
    """Return a useful one-line exception description, including empty errors."""
    message = str(error).strip()
    return f"{type(error).__name__}: {message or repr(error)}"


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

    def _advertisement_received(self, service_info: Any, *_: Any) -> None:
        _LOGGER.debug(
            "Advertisement received for %s: name=%s source=%s rssi=%s "
            "connectable=%s",
            self.address,
            getattr(service_info, "name", None),
            getattr(service_info, "source", None),
            getattr(service_info, "rssi", None),
            getattr(service_info, "connectable", None),
        )
        self._advertisement_event.set()

    def get_connectable_device(self) -> BLEDevice | None:
        """Return the best currently reachable connectable route."""
        device = bluetooth.async_ble_device_from_address(
            self._hass, self.address, connectable=True
        )
        route = self.route_diagnostics()
        _LOGGER.debug(
            "Resolved Bluetooth route for %s: device=%s source=%s rssi=%s",
            self.address,
            device.name if device is not None else None,
            route["source"],
            route["rssi"],
        )
        return device

    def route_diagnostics(self) -> dict[str, Any]:
        """Return secret-free details for the best current HA Bluetooth route."""
        try:
            service_info = bluetooth.async_last_service_info(
                self._hass,
                self.address,
                connectable=True,
            )
        except Exception as err:
            # Diagnostics must never prevent an otherwise valid connection.
            _LOGGER.debug(
                "Unable to inspect the Home Assistant Bluetooth route for %s: %s",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return {
                "present": None,
                "name": None,
                "source": None,
                "rssi": None,
                "tx_power": None,
                "error": exception_detail(err),
            }
        if service_info is None:
            return {
                "present": False,
                "name": None,
                "source": None,
                "rssi": None,
                "tx_power": None,
            }
        return {
            "present": True,
            "name": getattr(service_info, "name", None),
            "source": getattr(service_info, "source", None),
            "rssi": getattr(service_info, "rssi", None),
            "tx_power": getattr(service_info, "tx_power", None),
        }

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
            _LOGGER.debug(
                "No fresh connectable advertisement for %s within %.1f seconds",
                self.address,
                timeout,
            )
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
        self._connection_attempts = 0
        self._successful_connections = 0
        self._wire_tx_count = 0
        self._wire_rx_count = 0
        self._last_error: str | None = None

    @property
    def generation(self) -> int:
        """Return the generation of the current connection attempt."""
        return self._generation

    @property
    def is_connected(self) -> bool:
        """Return whether the current client reports an active connection."""
        return self._client is not None and self._client.is_connected

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return counters and current transport state without frame contents."""
        return {
            "generation": self._generation,
            "is_connected": self.is_connected,
            "connection_attempts": self._connection_attempts,
            "successful_connections": self._successful_connections,
            "wire_tx_count": self._wire_tx_count,
            "wire_rx_count": self._wire_rx_count,
            "last_error": self._last_error,
        }

    def set_disconnect_callback(self, callback: DisconnectCallback | None) -> None:
        """Set the callback invoked for an unexpected current disconnect."""
        self._disconnect_callback = callback

    async def async_connect(self, device: BLEDevice) -> int:
        """Create and connect a fresh Bleak client."""
        await self.async_disconnect()
        self._generation += 1
        generation = self._generation
        self._loop = asyncio.get_running_loop()
        self._connection_attempts += 1
        _LOGGER.debug(
            "Connecting to %s at %s: generation=%d attempt=%d timeout=%.1fs "
            "connector_attempts=%d",
            self.name,
            device.address,
            generation,
            self._connection_attempts,
            CONNECT_TIMEOUT,
            CONNECT_ATTEMPTS,
        )

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
            self._last_error = exception_detail(err)
            _LOGGER.debug(
                "Bluetooth connection failed for %s at %s generation=%d: %s",
                self.name,
                device.address,
                generation,
                self._last_error,
                exc_info=True,
            )
            raise BluetoothUnavailableError(
                f"Unable to connect to {self.name} at {device.address}; "
                f"generation={generation}; cause={self._last_error}"
            ) from err

        if generation != self._generation:
            with suppress(Exception):
                await client.disconnect()
            raise BluetoothUnavailableError("Connection was superseded")

        self._client = client
        try:
            self._resolve_characteristics(client.services)
        except Exception as err:
            self._last_error = exception_detail(err)
            await self.async_disconnect()
            raise

        self._successful_connections += 1
        self._last_error = None
        _LOGGER.debug(
            "Connected to %s at %s: generation=%d successful_connections=%d",
            self.name,
            device.address,
            generation,
            self._successful_connections,
        )
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
        _LOGGER.debug(
            "Resolved purifier GATT characteristics: notify_handle=%s "
            "notify_properties=%s command_handle=%s command_properties=%s",
            getattr(notify_characteristic, "handle", None),
            getattr(notify_characteristic, "properties", None),
            getattr(command_characteristic, "handle", None),
            getattr(command_characteristic, "properties", None),
        )

    async def async_subscribe(self, callback: NotificationCallback) -> None:
        """Subscribe to notifications before any transaction is sent."""
        client = self._require_client()
        generation = self._generation
        self._notification_callback = callback

        def notification_received(_: Any, data: bytearray) -> None:
            if generation != self._generation:
                _LOGGER.debug(
                    "Ignoring wire notification from stale generation=%d "
                    "current_generation=%d length=%d",
                    generation,
                    self._generation,
                    len(data),
                )
                return
            self._wire_rx_count += 1
            _LOGGER.debug(
                "RX wire notification: generation=%d count=%d length=%d",
                generation,
                self._wire_rx_count,
                len(data),
            )
            current_callback = self._notification_callback
            if current_callback is not None:
                current_callback(bytes(data))

        _LOGGER.debug(
            "Subscribing to purifier notifications: generation=%d characteristic=%s",
            generation,
            NOTIFY_CHARACTERISTIC_UUID,
        )
        try:
            await client.start_notify(
                self._notify_characteristic, notification_received
            )
        except Exception as err:
            self._last_error = exception_detail(err)
            raise GattTransportError(
                "Unable to subscribe to notifications; "
                f"generation={generation}; cause={self._last_error}"
            ) from err
        _LOGGER.debug("Notification subscription active: generation=%d", generation)

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
                self._last_error = exception_detail(err)
                raise GattTransportError(
                    "Unable to write command frame; "
                    f"generation={generation}; cause={self._last_error}"
                ) from err
            self._wire_tx_count += 1
            _LOGGER.debug(
                "TX wire frame: generation=%d count=%d length=%d response=False",
                generation,
                self._wire_tx_count,
                len(data),
            )
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

        _LOGGER.debug(
            "Disconnecting %s: generation=%d connected=%s",
            self.name,
            self._generation,
            client.is_connected,
        )
        # Clearing the client before awaiting makes the Bleak disconnect callback
        # a deliberate/stale disconnect rather than an unexpected one.
        with suppress(Exception):
            if client.is_connected:
                await client.disconnect()
        _LOGGER.debug(
            "Disconnect complete for %s: generation=%d",
            self.name,
            self._generation,
        )

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
        _LOGGER.debug(
            "%s disconnected unexpectedly: generation=%d wire_tx=%d wire_rx=%d",
            self.name,
            generation,
            self._wire_tx_count,
            self._wire_rx_count,
        )
        if self._disconnect_callback is not None:
            self._disconnect_callback(generation)
