"""Home Assistant Bluetooth access and a small, protocol-agnostic GATT transport."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
    get_connected_devices,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
NOTIFY_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
COMMAND_CHARACTERISTIC_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"

CONNECT_ATTEMPTS = 1
CONNECTION_ATTEMPT_TIMEOUT = 15.0
CONNECTION_ABORT_TIMEOUT = 5.0
STALE_CONNECTION_CLEANUP_TIMEOUT = 5.0
STALE_CONNECTION_CHECK_INTERVAL = 0.25
RECENT_CONNECTION_FAILURE_LIMIT = 4
ADVERTISEMENT_CHECK_INTERVAL = 0.25

NotificationCallback = Callable[[bytes], None]
DisconnectCallback = Callable[[int], None]


def exception_detail(error: BaseException) -> str:
    """Return a useful one-line exception description, including empty errors."""
    message = str(error).strip()
    return f"{type(error).__name__}: {message or repr(error)}"


def exception_chain_detail(error: BaseException) -> str:
    """Return the complete explicit/implicit exception chain on one line."""
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        details.append(exception_detail(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " <- ".join(details)


class BluetoothUnavailableError(ConnectionError):
    """Raised when there is no usable Bluetooth route to the purifier."""


class GattTransportError(ConnectionError):
    """Raised when a GATT operation cannot be completed."""


async def async_close_stale_connections(address: str, *, reason: str) -> None:
    """Close local BlueZ connections for an address through HA's BLE library."""
    _LOGGER.debug(
        "Closing stale Bluetooth connections by address: address=%s reason=%s",
        address,
        reason,
    )
    await close_stale_connections_by_address(address)


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
        self._last_callback_time: float | None = None
        self._last_callback_advertisement_time: float | None = None
        self._live_service_info: Any = None
        self._fresh_after: float | None = None
        self._fresh_advertisements = 0

    async def async_start(self) -> None:
        """Listen passively for a fresh route to the configured address."""
        if self._cancel_advertisement is not None:
            return

        callback_kwargs: dict[str, Any] = {}
        if replay_type := getattr(bluetooth, "BluetoothCallbackReplay", None):
            callback_kwargs["replay"] = replay_type.DISABLED
        self._cancel_advertisement = bluetooth.async_register_callback(
            self._hass,
            self._advertisement_received,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.PASSIVE,
            **callback_kwargs,
        )

    async def async_stop(self) -> None:
        """Stop listening for advertisements."""
        if self._cancel_advertisement is not None:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        self._advertisement_event.set()

    def _advertisement_received(self, service_info: Any, *_: Any) -> None:
        received_at = time.monotonic()
        advertisement_time = getattr(service_info, "time", None)
        self._last_callback_time = received_at
        self._last_callback_advertisement_time = (
            advertisement_time if isinstance(advertisement_time, int | float) else None
        )
        fresh_after = self._fresh_after
        # Registration replay is synchronous and happens before a connection
        # wait begins. Any callback received after this cutoff is therefore a
        # live scanner delivery, even if the scanner timestamp is coarse.
        is_live = fresh_after is not None and received_at >= fresh_after
        _LOGGER.debug(
            "Advertisement received for %s: name=%s source=%s rssi=%s "
            "connectable=%s live_for_current_wait=%s",
            self.address,
            getattr(service_info, "name", None),
            getattr(service_info, "source", None),
            getattr(service_info, "rssi", None),
            getattr(service_info, "connectable", None),
            is_live,
        )
        if is_live:
            self._live_service_info = service_info
            self._fresh_advertisements += 1
            self._advertisement_event.set()

    def clear_advertisement_history(self) -> None:
        """Make Home Assistant dispatch the next identical advertisement."""
        clear_history = getattr(bluetooth, "async_clear_advertisement_history", None)
        if clear_history is None:
            _LOGGER.debug(
                "Home Assistant does not expose advertisement-history clearing "
                "for %s; using timestamp polling fallback",
                self.address,
            )
            return
        try:
            clear_history(self._hass, self.address)
        except Exception as err:
            _LOGGER.debug(
                "Unable to clear Bluetooth advertisement history for %s: %s; "
                "using timestamp polling fallback",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return
        _LOGGER.debug("Cleared Bluetooth advertisement history for %s", self.address)

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
                "advertisement_age_seconds": None,
                "callback_age_seconds": self._callback_age(),
                "callback_advertisement_age_seconds": (
                    self._callback_advertisement_age()
                ),
                "fresh_advertisements": self._fresh_advertisements,
                "error": exception_detail(err),
            }
        if service_info is None:
            return {
                "present": False,
                "name": None,
                "source": None,
                "rssi": None,
                "tx_power": None,
                "advertisement_age_seconds": None,
                "callback_age_seconds": self._callback_age(),
                "callback_advertisement_age_seconds": (
                    self._callback_advertisement_age()
                ),
                "fresh_advertisements": self._fresh_advertisements,
            }
        advertisement_time = getattr(service_info, "time", None)
        advertisement_age = (
            max(0.0, time.monotonic() - advertisement_time)
            if isinstance(advertisement_time, int | float)
            else None
        )
        return {
            "present": True,
            "name": getattr(service_info, "name", None),
            "source": getattr(service_info, "source", None),
            "rssi": getattr(service_info, "rssi", None),
            "tx_power": getattr(service_info, "tx_power", None),
            "advertisement_age_seconds": (
                round(advertisement_age, 3) if advertisement_age is not None else None
            ),
            "callback_age_seconds": self._callback_age(),
            "callback_advertisement_age_seconds": (self._callback_advertisement_age()),
            "fresh_advertisements": self._fresh_advertisements,
        }

    def _callback_age(self) -> float | None:
        """Return seconds since this integration directly saw an advertisement."""
        if self._last_callback_time is None:
            return None
        return round(max(0.0, time.monotonic() - self._last_callback_time), 3)

    def _callback_advertisement_age(self) -> float | None:
        """Return the age encoded by the last callback's service information."""
        if self._last_callback_advertisement_time is None:
            return None
        return round(
            max(0.0, time.monotonic() - self._last_callback_advertisement_time),
            3,
        )

    def _fresh_service_info_since(self, cutoff: float) -> Any:
        """Return current route information only when it is newer than cutoff."""
        service_info = bluetooth.async_last_service_info(
            self._hass,
            self.address,
            connectable=True,
        )
        advertisement_time = (
            getattr(service_info, "time", None) if service_info is not None else None
        )
        if (
            service_info is not None
            and isinstance(advertisement_time, int | float)
            and advertisement_time >= cutoff
        ):
            return service_info
        if self._last_callback_time is not None and self._last_callback_time >= cutoff:
            return self._live_service_info
        return None

    def reachability_diagnostics(self) -> str | None:
        """Return Home Assistant's human-readable connection route diagnosis."""
        diagnose = getattr(bluetooth, "async_address_reachability_diagnostics", None)
        intent_type = getattr(bluetooth, "BluetoothReachabilityIntent", None)
        if diagnose is None or intent_type is None:
            return None
        try:
            return str(diagnose(self._hass, self.address, intent_type.CONNECTION))
        except Exception as err:
            _LOGGER.debug(
                "Unable to obtain Bluetooth reachability diagnostics for %s: %s",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return f"unavailable: {exception_detail(err)}"

    def address_is_present(self) -> bool:
        """Return whether a connectable scanner can currently see the address."""
        return bluetooth.async_address_present(
            self._hass, self.address, connectable=True
        )

    async def async_wait_for_fresh_device(self, timeout: float) -> BLEDevice | None:
        """Wait for a live advertisement, then resolve HA's current best route."""
        started_at = time.monotonic()
        deadline = started_at + timeout
        self._fresh_after = started_at
        self._live_service_info = None
        self._advertisement_event.clear()
        self.clear_advertisement_history()
        _LOGGER.debug(
            "Waiting for a live connectable advertisement for %s: timeout=%.1fs",
            self.address,
            timeout,
        )

        try:
            while (remaining := deadline - time.monotonic()) > 0:
                service_info = self._fresh_service_info_since(started_at)
                if service_info is not None:
                    advertisement_time = getattr(service_info, "time", None)
                    device = self.get_connectable_device()
                    if device is None:
                        device = getattr(service_info, "device", None)
                    if device is not None:
                        _LOGGER.debug(
                            "Live Bluetooth route selected for %s after %.3fs: "
                            "source=%s rssi=%s advertisement_age=%s",
                            self.address,
                            time.monotonic() - started_at,
                            getattr(service_info, "source", None),
                            getattr(service_info, "rssi", None),
                            (
                                round(
                                    max(0.0, time.monotonic() - advertisement_time),
                                    3,
                                )
                                if isinstance(advertisement_time, int | float)
                                else None
                            ),
                        )
                        return device

                self._advertisement_event.clear()
                # Close the check/clear race before blocking again.
                if self._fresh_service_info_since(started_at) is not None:
                    continue

                try:
                    async with asyncio.timeout(
                        min(ADVERTISEMENT_CHECK_INTERVAL, remaining)
                    ):
                        await self._advertisement_event.wait()
                except TimeoutError:
                    pass
        finally:
            self._fresh_after = None

        if time.monotonic() >= deadline:
            _LOGGER.debug(
                "No live connectable advertisement for %s within %.1f seconds; "
                "route=%s reachability=%s",
                self.address,
                timeout,
                self.route_diagnostics(),
                self.reachability_diagnostics(),
            )
        return None


class GattTransport:
    """Own one active GATT connection and transport opaque 20-byte frames."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self._client: BleakClientWithServiceCache | None = None
        self._connecting_client: BleakClientWithServiceCache | None = None
        self._connecting_address: str | None = None
        self._last_device: BLEDevice | None = None
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
        self._connection_stage = "idle"
        self._connection_started_at: float | None = None
        self._stage_started_at: float | None = None
        self._pre_return_disconnects = 0
        self._last_disconnect_stage: str | None = None
        self._last_disconnect_elapsed: float | None = None
        self._recent_connection_failures: deque[str] = deque(
            maxlen=RECENT_CONNECTION_FAILURE_LIMIT
        )
        self._address_cleanup_attempts = 0
        self._address_cleanup_successes = 0
        self._address_cleanup_failures = 0
        self._last_address_cleanup_reason: str | None = None
        self._last_address_cleanup_error: str | None = None
        self._last_address_cleanup_elapsed: float | None = None
        self._last_address_cleanup_remaining: int | None = None

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
            "connection_stage": self._connection_stage,
            "pre_return_disconnects": self._pre_return_disconnects,
            "last_disconnect_stage": self._last_disconnect_stage,
            "last_disconnect_elapsed_seconds": self._last_disconnect_elapsed,
            "connecting_client_present": self._connecting_client is not None,
            "recent_connection_failures": list(self._recent_connection_failures),
            "address_cleanup_attempts": self._address_cleanup_attempts,
            "address_cleanup_successes": self._address_cleanup_successes,
            "address_cleanup_failures": self._address_cleanup_failures,
            "last_address_cleanup_reason": self._last_address_cleanup_reason,
            "last_address_cleanup_error": self._last_address_cleanup_error,
            "last_address_cleanup_elapsed_seconds": (
                self._last_address_cleanup_elapsed
            ),
            "last_address_cleanup_remaining_connections": (
                self._last_address_cleanup_remaining
            ),
        }

    def _set_stage(self, stage: str) -> None:
        """Record the current GATT stage and its monotonic start time."""
        self._connection_stage = stage
        self._stage_started_at = time.monotonic()

    def _elapsed(self, started_at: float | None = None) -> float:
        """Return monotonic seconds since a connection or stage began."""
        started = started_at
        if started is None:
            started = self._connection_started_at
        if started is None:
            return 0.0
        return max(0.0, time.monotonic() - started)

    def _failure_summary(self, error: BaseException) -> str:
        """Describe a transport failure without hiding its stage or cause chain."""
        return (
            f"attempt={self._connection_attempts}; "
            f"stage={self._connection_stage}; elapsed={self._elapsed():.3f}s; "
            f"pre_return_disconnects={self._pre_return_disconnects}; "
            f"cause={exception_chain_detail(error)}"
        )

    def _record_connection_failure(self, detail: str) -> None:
        """Keep bounded evidence from attempts preceding the final failure."""
        self._last_error = detail
        self._recent_connection_failures.append(detail)

    async def _async_abort_connecting_client(self) -> None:
        """Release a partially established client before another route is tried."""
        client = self._connecting_client
        address = self._connecting_address
        if client is None:
            return

        self._set_stage("aborting_connection")
        _LOGGER.debug(
            "Aborting incomplete Bluetooth connection to %s: generation=%d "
            "address=%s connected=%s",
            self.name,
            self._generation,
            address,
            client.is_connected,
        )
        try:
            async with asyncio.timeout(CONNECTION_ABORT_TIMEOUT):
                await client.disconnect()
        except Exception as err:
            _LOGGER.debug(
                "Incomplete Bluetooth connection cleanup failed for %s at %s: %s",
                self.name,
                address,
                exception_detail(err),
                exc_info=True,
            )
        finally:
            self._set_stage("connection_aborted")

    async def async_cleanup_stale_connection(self, *, reason: str) -> bool:
        """Close and verify local BlueZ state without touching a healthy client."""
        if self.is_connected:
            self._last_address_cleanup_reason = reason
            self._last_address_cleanup_error = "owned client is still connected"
            self._last_address_cleanup_remaining = None
            _LOGGER.debug(
                "Skipping stale Bluetooth cleanup for %s: reason=%s "
                "owned_client_connected=True",
                self.name,
                reason,
            )
            return False

        device = self._last_device
        address = device.address if device is not None else self._connecting_address
        if address is None:
            return True

        self._address_cleanup_attempts += 1
        self._last_address_cleanup_reason = reason
        self._last_address_cleanup_error = None
        self._last_address_cleanup_remaining = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(STALE_CONNECTION_CLEANUP_TIMEOUT):
                await async_close_stale_connections(address, reason=reason)
                if device is not None:
                    deadline = time.monotonic() + STALE_CONNECTION_CLEANUP_TIMEOUT
                    while True:
                        connected_devices = await get_connected_devices(device)
                        remaining = len(connected_devices)
                        self._last_address_cleanup_remaining = remaining
                        if remaining == 0:
                            break
                        if time.monotonic() >= deadline:
                            raise BluetoothUnavailableError(
                                f"BlueZ still reports {remaining} connection(s) "
                                f"for {address}"
                            )
                        await asyncio.sleep(STALE_CONNECTION_CHECK_INTERVAL)
        except Exception as err:
            self._address_cleanup_failures += 1
            self._last_address_cleanup_error = exception_detail(err)
            self._last_address_cleanup_elapsed = round(
                max(0.0, time.monotonic() - started), 3
            )
            _LOGGER.debug(
                "Stale Bluetooth cleanup failed for %s at %s: reason=%s "
                "elapsed=%.3fs remaining=%s cause=%s",
                self.name,
                address,
                reason,
                self._last_address_cleanup_elapsed,
                self._last_address_cleanup_remaining,
                self._last_address_cleanup_error,
                exc_info=True,
            )
            return False

        self._address_cleanup_successes += 1
        self._last_address_cleanup_elapsed = round(
            max(0.0, time.monotonic() - started), 3
        )
        self._connecting_client = None
        self._connecting_address = None
        _LOGGER.debug(
            "Stale Bluetooth cleanup complete for %s at %s: reason=%s " "elapsed=%.3fs",
            self.name,
            address,
            reason,
            self._last_address_cleanup_elapsed,
        )
        return True

    def set_disconnect_callback(self, callback: DisconnectCallback | None) -> None:
        """Set the callback invoked for an unexpected current disconnect."""
        self._disconnect_callback = callback

    async def async_connect(self, device: BLEDevice) -> int:
        """Create and connect a fresh Bleak client."""
        await self.async_disconnect()
        self._last_device = device
        await self._async_abort_connecting_client()
        if not await self.async_cleanup_stale_connection(reason="before_connection"):
            raise BluetoothUnavailableError(
                f"Refusing to connect to {self.name} at {device.address} while "
                "a stale local Bluetooth connection may remain; "
                f"cleanup_error={self._last_address_cleanup_error}; "
                f"remaining={self._last_address_cleanup_remaining}"
            )
        self._generation += 1
        generation = self._generation
        self._loop = asyncio.get_running_loop()
        self._connection_started_at = time.monotonic()
        self._pre_return_disconnects = 0
        self._last_disconnect_stage = None
        self._last_disconnect_elapsed = None
        self._set_stage("establish_connection")
        self._connection_attempts += 1
        _LOGGER.debug(
            "Connecting to %s at %s: generation=%d attempt=%d "
            "connector_attempts=%d attempt_timeout=%.1fs",
            self.name,
            device.address,
            generation,
            self._connection_attempts,
            CONNECT_ATTEMPTS,
            CONNECTION_ATTEMPT_TIMEOUT,
        )

        def create_tracked_client(
            *args: Any, **kwargs: Any
        ) -> BleakClientWithServiceCache:
            client = BleakClientWithServiceCache(*args, **kwargs)
            self._connecting_client = client
            self._connecting_address = device.address
            return client

        try:
            async with asyncio.timeout(CONNECTION_ATTEMPT_TIMEOUT):
                client = await establish_connection(
                    create_tracked_client,  # type: ignore[arg-type]
                    device,
                    self.name,
                    disconnected_callback=lambda connected_client: (
                        self._disconnected_from_bleak(connected_client, generation)
                    ),
                    max_attempts=CONNECT_ATTEMPTS,
                )
        except TimeoutError as err:
            detail = (
                f"attempt={self._connection_attempts}; "
                f"stage={self._connection_stage}; elapsed={self._elapsed():.3f}s; "
                f"deadline={CONNECTION_ATTEMPT_TIMEOUT:.1f}s; "
                f"pre_return_disconnects={self._pre_return_disconnects}; "
                "cause=TimeoutError: connection attempt deadline exceeded"
            )
            await self._async_abort_connecting_client()
            cleanup_ok = await self.async_cleanup_stale_connection(
                reason="connection_timeout"
            )
            if not cleanup_ok:
                detail = (
                    f"{detail}; address_cleanup_error="
                    f"{self._last_address_cleanup_error}; "
                    f"remaining={self._last_address_cleanup_remaining}"
                )
            self._record_connection_failure(detail)
            _LOGGER.debug(
                "Bluetooth connection attempt timed out for %s at %s "
                "generation=%d: %s",
                self.name,
                device.address,
                generation,
                detail,
                exc_info=True,
            )
            raise BluetoothUnavailableError(
                f"Unable to connect to {self.name} at {device.address}; "
                f"generation={generation}; cause={detail}"
            ) from err
        except asyncio.CancelledError:
            await self._async_abort_connecting_client()
            await self.async_cleanup_stale_connection(reason="connection_cancelled")
            raise
        except Exception as err:
            detail = self._failure_summary(err)
            await self._async_abort_connecting_client()
            cleanup_ok = await self.async_cleanup_stale_connection(
                reason="connection_failed"
            )
            if not cleanup_ok:
                detail = (
                    f"{detail}; address_cleanup_error="
                    f"{self._last_address_cleanup_error}; "
                    f"remaining={self._last_address_cleanup_remaining}"
                )
            self._record_connection_failure(detail)
            _LOGGER.debug(
                "Bluetooth connection failed for %s at %s generation=%d: %s",
                self.name,
                device.address,
                generation,
                detail,
                exc_info=True,
            )
            raise BluetoothUnavailableError(
                f"Unable to connect to {self.name} at {device.address}; "
                f"generation={generation}; cause={detail}"
            ) from err

        self._connecting_client = None
        self._connecting_address = None

        if generation != self._generation:
            with suppress(Exception):
                await client.disconnect()
            raise BluetoothUnavailableError("Connection was superseded")

        connector_elapsed = self._elapsed()
        self._set_stage("resolve_characteristics")
        self._client = client
        services = client.services
        service_count = len(getattr(services, "services", {}))
        _LOGGER.debug(
            "Bluetooth connector returned for %s at %s: generation=%d "
            "elapsed=%.3fs connected=%s services=%d "
            "pre_return_disconnects=%d",
            self.name,
            device.address,
            generation,
            connector_elapsed,
            client.is_connected,
            service_count,
            self._pre_return_disconnects,
        )
        try:
            self._resolve_characteristics(services)
        except Exception as err:
            self._last_error = self._failure_summary(err)
            await self.async_disconnect()
            raise GattTransportError(self._last_error) from err

        self._successful_connections += 1
        self._last_error = None
        self._set_stage("connected")
        _LOGGER.debug(
            "Connected to %s at %s: generation=%d successful_connections=%d "
            "elapsed=%.3fs",
            self.name,
            device.address,
            generation,
            self._successful_connections,
            self._elapsed(),
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
        self._set_stage("subscribe_notifications")

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
            self._last_error = self._failure_summary(err)
            raise GattTransportError(
                "Unable to subscribe to notifications; "
                f"generation={generation}; cause={self._last_error}"
            ) from err
        subscribe_elapsed = self._elapsed(self._stage_started_at)
        self._set_stage("notifications_active")
        _LOGGER.debug(
            "Notification subscription active: generation=%d elapsed=%.3fs "
            "connection_elapsed=%.3fs",
            generation,
            subscribe_elapsed,
            self._elapsed(),
        )

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

        self._set_stage("disconnecting")
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
        self._set_stage("disconnected")

    def _require_client(self) -> BleakClientWithServiceCache:
        client = self._client
        if client is None or not client.is_connected:
            raise GattTransportError("Purifier is not connected")
        return client

    def _disconnected_from_bleak(
        self, disconnected_client: BleakClientWithServiceCache, generation: int
    ) -> None:
        if (
            generation == self._generation
            and self._connection_stage == "establish_connection"
            and self._client is None
        ):
            # Record this synchronously so a connector exception cannot overtake
            # the event-loop callback and hide the failed internal connection.
            self._pre_return_disconnects += 1
            self._last_disconnect_stage = self._connection_stage
            self._last_disconnect_elapsed = round(self._elapsed(), 3)
            _LOGGER.debug(
                "%s disconnected before establish_connection returned: "
                "generation=%d elapsed=%.3fs count=%d",
                self.name,
                generation,
                self._last_disconnect_elapsed,
                self._pre_return_disconnects,
            )
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(
            self._handle_disconnected, disconnected_client, generation
        )

    def _handle_disconnected(
        self, disconnected_client: BleakClientWithServiceCache, generation: int
    ) -> None:
        if generation != self._generation:
            _LOGGER.debug(
                "Ignoring disconnect callback from stale generation=%d "
                "current_generation=%d stage=%s",
                generation,
                self._generation,
                self._connection_stage,
            )
            return
        if disconnected_client is not self._client:
            _LOGGER.debug(
                "Ignoring disconnect callback from unowned client: "
                "generation=%d stage=%s elapsed=%.3fs",
                generation,
                self._connection_stage,
                self._elapsed(),
            )
            return
        self._client = None
        self._notification_callback = None
        self._notify_characteristic = None
        self._command_characteristic = None
        disconnected_stage = self._connection_stage
        disconnected_elapsed = round(self._elapsed(), 3)
        self._last_disconnect_stage = disconnected_stage
        self._last_disconnect_elapsed = disconnected_elapsed
        self._last_error = (
            f"unexpected_disconnect; stage={disconnected_stage}; "
            f"elapsed={disconnected_elapsed:.3f}s"
        )
        self._set_stage("disconnected")
        _LOGGER.debug(
            "%s disconnected unexpectedly: generation=%d stage=%s "
            "elapsed=%.3fs wire_tx=%d wire_rx=%d",
            self.name,
            generation,
            disconnected_stage,
            disconnected_elapsed,
            self._wire_tx_count,
            self._wire_rx_count,
        )
        if self._disconnect_callback is not None:
            self._disconnect_callback(generation)
