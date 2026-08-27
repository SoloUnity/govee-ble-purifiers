"""Small, protocol-agnostic GATT transport."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    get_connected_devices,
)

from .cleanup import async_close_stale_connections
from .errors import (
    BluetoothUnavailableError,
    GattTransportError,
    exception_chain_detail,
    exception_detail,
)
from .ownership import ADDRESS_OWNERSHIP, AddressOwnershipToken
from .settings import BluetoothRuntimeSettings

_LOGGER = logging.getLogger(__package__)

RECENT_CONNECTION_FAILURE_LIMIT = 4

NotificationCallback = Callable[[bytes], None]
DisconnectCallback = Callable[[int], None]


class _ConnectionAttemptDeadlineExceeded(TimeoutError):
    """Raised when the integration's outer connection deadline expires."""


class GattTransport:
    """Own one active GATT connection and transport opaque bytes."""

    def __init__(
        self,
        *,
        name: str,
        settings: BluetoothRuntimeSettings,
    ) -> None:
        self.name = name
        self.settings = settings
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
        self._last_connection_timeout_diagnostics: dict[str, Any] | None = None
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
        self._gatt_operation_tasks: set[asyncio.Task[Any]] = set()
        self._active_gatt_operation: str | None = None
        self._last_gatt_operation: str | None = None
        self._last_gatt_operation_deadline: float | None = None
        self._last_gatt_operation_elapsed: float | None = None
        self._last_gatt_operation_timed_out = False
        self._last_gatt_operation_error: str | None = None
        self._gatt_operation_timeouts = 0
        self._connector_task: asyncio.Task[BleakClientWithServiceCache] | None = None
        self._connector_cleanup_task: asyncio.Task[None] | None = None
        self._connector_started_at: float | None = None
        self._connector_detached = False
        self._ownership_token: AddressOwnershipToken | None = None
        self._last_connector_late_state: str | None = None
        self._last_connector_late_error: str | None = None
        self._last_connector_late_cleanup_elapsed: float | None = None

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
        ownership = ADDRESS_OWNERSHIP.snapshot(
            token=self._ownership_token,
            address=(self._last_device.address if self._last_device else None),
        )
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
            "last_connection_timeout_diagnostics": (
                self._last_connection_timeout_diagnostics
            ),
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
            "active_gatt_operation": self._active_gatt_operation,
            "last_gatt_operation": self._last_gatt_operation,
            "last_gatt_operation_deadline_seconds": (
                self._last_gatt_operation_deadline
            ),
            "last_gatt_operation_elapsed_seconds": self._last_gatt_operation_elapsed,
            "last_gatt_operation_timed_out": self._last_gatt_operation_timed_out,
            "last_gatt_operation_error": self._last_gatt_operation_error,
            "gatt_operation_timeouts": self._gatt_operation_timeouts,
            "pending_gatt_operation_tasks": len(self._gatt_operation_tasks),
            "connector_pending": self._connector_work_pending(),
            "connector_cancellation_requested": (
                bool(ownership and ownership["cancellation_requested"])
            ),
            "connector_pending_elapsed_seconds": (
                round(self._elapsed(self._connector_started_at), 3)
                if self._connector_work_pending()
                else None
            ),
            "last_connector_late_state": self._last_connector_late_state,
            "last_connector_late_error": self._last_connector_late_error,
            "last_connector_late_cleanup_elapsed_seconds": (
                self._last_connector_late_cleanup_elapsed
            ),
            "address_ownership": ownership,
        }

    def _connector_work_pending(self) -> bool:
        """Return whether old connector ownership is still quarantined."""
        connector = self._connector_task
        cleanup = self._connector_cleanup_task
        return bool(
            (
                connector is not None
                and (not connector.done() or self._connector_detached)
            )
            or (cleanup is not None and not cleanup.done())
        )

    def _observe_connector_cleanup_task(self, task: asyncio.Task[None]) -> None:
        """Observe late cleanup and release quarantine only after it finishes."""
        if self._connector_cleanup_task is task:
            self._connector_cleanup_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as err:  # noqa: BLE001
            self._last_connector_late_error = exception_detail(err)
            _LOGGER.debug(
                "Late connector cleanup failed for %s: %s",
                self.name,
                self._last_connector_late_error,
                exc_info=True,
            )

    def _observe_connector_task(
        self, task: asyncio.Task[BleakClientWithServiceCache]
    ) -> None:
        """Consume every connector outcome and clean a detached late success."""
        if not self._connector_detached or self._connector_task is not task:
            if not task.cancelled():
                with suppress(Exception):
                    task.exception()
            return

        cleanup_task = asyncio.create_task(
            self._async_cleanup_late_connector(task),
            name=f"govee-connect-late-cleanup-{self.name}",
        )
        self._connector_cleanup_task = cleanup_task
        token = self._ownership_token
        if token is not None:
            ADDRESS_OWNERSHIP.mark_late_cleanup(token)
            ADDRESS_OWNERSHIP.track_task(token, cleanup_task)
        cleanup_task.add_done_callback(self._observe_connector_cleanup_task)

    async def _async_cleanup_late_connector(
        self, task: asyncio.Task[BleakClientWithServiceCache]
    ) -> None:
        """Dispose of a connector outcome that arrived after its owner returned."""
        started = time.monotonic()
        client = self._connecting_client
        token = self._ownership_token
        try:
            if not ADDRESS_OWNERSHIP.is_current(token):
                return
            if task.cancelled():
                self._last_connector_late_state = "cancelled"
            else:
                try:
                    client = task.result()
                except Exception as err:  # noqa: BLE001
                    self._last_connector_late_state = "failed"
                    self._last_connector_late_error = exception_detail(err)
                    client = self._connecting_client
                else:
                    self._last_connector_late_state = "returned_client"

            if self._connector_task is task:
                self._connector_task = None
            self._connector_detached = False

            if client is not None:
                self._connecting_client = client
                try:
                    await self._async_gatt_operation(
                        "late_connector_disconnect",
                        self.settings.connection.abort_timeout,
                        client.disconnect,
                    )
                except Exception as err:  # noqa: BLE001
                    self._last_connector_late_error = exception_detail(err)

            await self.async_cleanup_stale_connection(
                reason="late_connector", _release_ownership=False
            )
        finally:
            self._last_connector_late_cleanup_elapsed = round(
                max(0.0, time.monotonic() - started), 3
            )
            _LOGGER.debug(
                "Late connector outcome cleaned for %s: state=%s elapsed=%.3fs "
                "error=%s",
                self.name,
                self._last_connector_late_state,
                self._last_connector_late_cleanup_elapsed,
                self._last_connector_late_error,
            )
            if token is not None:
                ADDRESS_OWNERSHIP.request_release(token)
                ADDRESS_OWNERSHIP.finish_cleanup(token)
            if self._ownership_token is token:
                self._ownership_token = None

    def _release_connector_task(
        self, task: asyncio.Task[BleakClientWithServiceCache]
    ) -> None:
        """Release connector ownership after a synchronously observed outcome."""
        if self._connector_task is task:
            self._connector_task = None
        self._connector_detached = False

    async def _async_cancel_connector(
        self, task: asyncio.Task[BleakClientWithServiceCache]
    ) -> bool:
        """Request connector cancellation and observe it for a bounded tail."""
        if task.done():
            return True
        self._connector_detached = True
        token = self._ownership_token
        if token is not None:
            ADDRESS_OWNERSHIP.mark_cancellation_requested(token)
        task.cancel()
        done, _ = await asyncio.wait(
            (task,),
            timeout=self.settings.gatt_operations.operation_cancel_timeout,
        )
        if done:
            # The done callback owns cleanup after the cancellation transition.
            await asyncio.sleep(0)
            cleanup_task = self._connector_cleanup_task
            if cleanup_task is not None and not cleanup_task.done():
                await asyncio.wait(
                    (cleanup_task,),
                    timeout=(
                        self.settings.connection.abort_timeout
                        + self.settings.cleanup.stale_connection_timeout
                        + 2
                        * self.settings.gatt_operations.operation_cancel_timeout
                    ),
                )
            return False
        _LOGGER.debug(
            "Bluetooth connector remains pending for %s after cancellation: "
            "generation=%d elapsed=%.3fs",
            self.name,
            self._generation,
            self._elapsed(self._connector_started_at),
        )
        return False

    def _observe_gatt_operation_task(self, task: asyncio.Task[Any]) -> None:
        """Retain and observe a backend task until cancellation really finishes."""
        self._gatt_operation_tasks.discard(task)
        if task.cancelled():
            return
        with suppress(Exception):
            task.exception()

    def _track_owned_task(self, task: asyncio.Task[Any]) -> None:
        """Retain backend work in both transport and address ownership scopes."""
        self._gatt_operation_tasks.add(task)
        task.add_done_callback(self._observe_gatt_operation_task)
        token = self._ownership_token
        if token is not None:
            ADDRESS_OWNERSHIP.track_task(token, task)

    async def _async_gatt_operation(
        self,
        operation: str,
        timeout: float,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run one backend GATT call with bounded, observed cancellation."""
        started = time.monotonic()
        self._active_gatt_operation = operation
        self._last_gatt_operation = operation
        self._last_gatt_operation_deadline = timeout
        self._last_gatt_operation_timed_out = False
        self._last_gatt_operation_error = None
        task = asyncio.create_task(action(), name=f"govee-gatt-{operation}")
        self._track_owned_task(task)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                self._gatt_operation_timeouts += 1
                self._last_gatt_operation_timed_out = True
                task.cancel()
                await asyncio.wait(
                    (task,),
                    timeout=self.settings.gatt_operations.operation_cancel_timeout,
                )
                elapsed = max(0.0, time.monotonic() - started)
                detail = (
                    f"operation={operation}; stage={self._connection_stage}; "
                    f"elapsed={elapsed:.3f}s; deadline={timeout:.1f}s; "
                    f"generation={self._generation}"
                )
                self._last_gatt_operation_error = detail
                self._last_error = f"gatt_operation_timeout; {detail}"
                raise GattTransportError(f"GATT operation timed out; {detail}")
            return task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await asyncio.wait(
                    (task,),
                    timeout=self.settings.gatt_operations.operation_cancel_timeout,
                )
            raise
        except Exception as err:
            if self._last_gatt_operation_error is None:
                self._last_gatt_operation_error = exception_detail(err)
            raise
        finally:
            self._last_gatt_operation_elapsed = round(
                max(0.0, time.monotonic() - started), 3
            )
            if self._active_gatt_operation == operation:
                self._active_gatt_operation = None

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

    @staticmethod
    def _device_backend_route(device: BLEDevice) -> dict[str, str | None]:
        """Return only the non-secret BlueZ route fields needed for diagnosis."""
        details = getattr(device, "details", None)
        path: str | None = None
        adapter: str | None = None
        if isinstance(details, dict):
            raw_path = details.get("path")
            path = raw_path if isinstance(raw_path, str) else None
            raw_adapter = details.get("adapter")
            props = details.get("props")
            if raw_adapter is None and isinstance(props, dict):
                raw_adapter = props.get("Adapter")
            if isinstance(raw_adapter, str):
                adapter = raw_adapter.rsplit("/", 1)[-1]
        if adapter is None and path is not None:
            adapter = next(
                (part for part in path.split("/") if part.startswith("hci")),
                None,
            )
        return {"device_path": path, "adapter": adapter}

    async def _async_connection_timeout_diagnostics(
        self, device: BLEDevice
    ) -> dict[str, Any]:
        """Inspect a still-running connector before cancellation changes its state."""
        client = self._connecting_client
        diagnostics: dict[str, Any] = {
            "partial_client_present": client is not None,
            "partial_client_connected": None,
            "partial_client_connected_error": None,
            "service_count": None,
            "service_error": None,
            "bluez_connection_count": None,
            "bluez_connection_error": None,
            **self._device_backend_route(device),
        }
        if client is not None:
            try:
                diagnostics["partial_client_connected"] = bool(client.is_connected)
            except Exception as err:  # noqa: BLE001
                diagnostics["partial_client_connected_error"] = exception_detail(err)
            try:
                services = client.services
                diagnostics["service_count"] = len(getattr(services, "services", {}))
            except Exception as err:  # noqa: BLE001
                diagnostics["service_error"] = exception_detail(err)

        diagnostic_task = asyncio.create_task(
            get_connected_devices(device),
            name=f"govee-connect-diagnostics-{device.address}",
        )
        self._track_owned_task(diagnostic_task)
        try:
            done, _ = await asyncio.wait(
                (diagnostic_task,),
                timeout=self.settings.connection.diagnostic_timeout,
            )
            if done:
                diagnostics["bluez_connection_count"] = len(
                    diagnostic_task.result()
                )
            else:
                diagnostic_task.cancel()
                await asyncio.wait(
                    (diagnostic_task,),
                    timeout=(
                        self.settings.gatt_operations.operation_cancel_timeout
                    ),
                )
                diagnostics["bluez_connection_error"] = (
                    "TimeoutError: connection diagnostics deadline exceeded"
                )
        except Exception as err:  # noqa: BLE001
            diagnostics["bluez_connection_error"] = exception_detail(err)

        return diagnostics

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
            await self._async_gatt_operation(
                "abort_connecting_client",
                self.settings.connection.abort_timeout,
                client.disconnect,
            )
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

    async def _async_cleanup_backend_operation(
        self,
        operation: str,
        timeout: float,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run address cleanup work with a hard deadline and retained tail."""
        task = asyncio.create_task(action(), name=f"govee-cleanup-{operation}")
        self._track_owned_task(task)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if done:
                return task.result()
            task.cancel()
            await asyncio.wait(
                (task,),
                timeout=self.settings.gatt_operations.operation_cancel_timeout,
            )
            raise TimeoutError(f"{operation} cleanup deadline exceeded")
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await asyncio.wait(
                    (task,),
                    timeout=self.settings.gatt_operations.operation_cancel_timeout,
                )
            raise

    def _release_address_ownership(self, token: AddressOwnershipToken) -> None:
        """Request release; resistant tracked tasks keep quarantine alive."""
        ADDRESS_OWNERSHIP.request_release(token)
        ADDRESS_OWNERSHIP.finish_cleanup(token)
        if self._ownership_token is token:
            self._ownership_token = None

    async def async_cleanup_stale_connection(
        self, *, reason: str, _release_ownership: bool = True
    ) -> bool:
        """Close and verify local BlueZ state without touching a healthy client."""
        cleanup_task = self._connector_cleanup_task
        if (
            self._connector_work_pending()
            and asyncio.current_task() is not cleanup_task
        ):
            self._last_address_cleanup_reason = reason
            self._last_address_cleanup_error = "connector quarantine is still active"
            self._last_address_cleanup_remaining = None
            _LOGGER.debug(
                "Deferring stale Bluetooth cleanup for %s: reason=%s "
                "connector_pending=True cancellation_requested=%s elapsed=%.3fs",
                self.name,
                reason,
                bool(
                    (ownership := ADDRESS_OWNERSHIP.snapshot(
                        token=self._ownership_token
                    ))
                    and ownership["cancellation_requested"]
                ),
                self._elapsed(self._connector_started_at),
            )
            return False
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

        token = self._ownership_token
        if not ADDRESS_OWNERSHIP.is_current(token):
            if ADDRESS_OWNERSHIP.is_owned(address):
                self._last_address_cleanup_reason = reason
                self._last_address_cleanup_error = (
                    "another transport owns address quarantine"
                )
                return False
            token = ADDRESS_OWNERSHIP.claim(address)
            if token is None:
                return False
            self._ownership_token = token

        self._address_cleanup_attempts += 1
        self._last_address_cleanup_reason = reason
        self._last_address_cleanup_error = None
        self._last_address_cleanup_remaining = None
        started = time.monotonic()
        try:
            deadline = time.monotonic() + self.settings.cleanup.stale_connection_timeout
            await self._async_cleanup_backend_operation(
                "close_stale_connections",
                max(0.0, deadline - time.monotonic()),
                lambda: async_close_stale_connections(address, reason=reason),
            )
            if device is not None:
                while True:
                    remaining_time = max(0.0, deadline - time.monotonic())
                    connected_devices = await self._async_cleanup_backend_operation(
                        "get_connected_devices",
                        remaining_time,
                        lambda: get_connected_devices(device),
                    )
                    remaining = len(connected_devices)
                    self._last_address_cleanup_remaining = remaining
                    if remaining == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise BluetoothUnavailableError(
                            f"BlueZ still reports {remaining} connection(s) "
                            f"for {address}"
                        )
                    await asyncio.sleep(
                        min(
                            self.settings.cleanup.stale_connection_check_interval,
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
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
        finally:
            if _release_ownership and token is not None:
                self._release_address_ownership(token)

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
        if self._connector_work_pending():
            ownership = ADDRESS_OWNERSHIP.snapshot(token=self._ownership_token)
            raise BluetoothUnavailableError(
                f"Refusing to connect to {self.name} while an earlier connector "
                "is quarantined; cancellation_requested="
                f"{bool(ownership and ownership['cancellation_requested'])}; "
                f"elapsed={self._elapsed(self._connector_started_at):.3f}s"
            )
        await self.async_disconnect()
        previous_token = self._ownership_token
        if ADDRESS_OWNERSHIP.is_current(previous_token):
            await self.async_cleanup_stale_connection(reason="before_reconnect")
        if ADDRESS_OWNERSHIP.is_owned(device.address):
            ownership = ADDRESS_OWNERSHIP.snapshot(address=device.address)
            raise BluetoothUnavailableError(
                f"Refusing to connect to {self.name} while address-scoped "
                f"Bluetooth ownership remains active; ownership={ownership}"
            )
        token = ADDRESS_OWNERSHIP.claim(device.address)
        if token is None:
            raise BluetoothUnavailableError(
                f"Refusing to connect to {self.name} while address-scoped "
                "Bluetooth ownership remains active"
            )
        self._ownership_token = token
        self._last_device = device
        try:
            await self._async_abort_connecting_client()
            cleanup_succeeded = await self.async_cleanup_stale_connection(
                reason="before_connection", _release_ownership=False
            )
        except BaseException:
            self._release_address_ownership(token)
            raise
        if not cleanup_succeeded:
            self._release_address_ownership(token)
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
        self._last_connection_timeout_diagnostics = None
        self._set_stage("establish_connection")
        self._connection_attempts += 1
        _LOGGER.debug(
            "Connecting to %s at %s: generation=%d attempt=%d "
            "connector_attempts=%d attempt_timeout=%.1fs",
            self.name,
            device.address,
            generation,
            self._connection_attempts,
            self.settings.connection.attempts,
            self.settings.connection.attempt_timeout,
        )

        def create_tracked_client(
            *args: Any, **kwargs: Any
        ) -> BleakClientWithServiceCache:
            client = BleakClientWithServiceCache(*args, **kwargs)
            self._connecting_client = client
            self._connecting_address = device.address
            return client

        async def establish_with_diagnostics() -> BleakClientWithServiceCache:
            connection_task = asyncio.create_task(
                establish_connection(
                    create_tracked_client,  # type: ignore[arg-type]
                    device,
                    self.name,
                    disconnected_callback=lambda connected_client: (
                        self._disconnected_from_bleak(connected_client, generation)
                    ),
                    max_attempts=self.settings.connection.attempts,
                ),
                name=f"govee-connect-{device.address}",
            )
            self._connector_task = connection_task
            self._connector_started_at = time.monotonic()
            self._connector_detached = False
            self._last_connector_late_state = None
            self._last_connector_late_error = None
            ADDRESS_OWNERSHIP.track_task(token, connection_task)
            connection_task.add_done_callback(self._observe_connector_task)
            try:
                done, _ = await asyncio.wait(
                    (connection_task,),
                    timeout=self.settings.connection.attempt_timeout,
                )
                if not done:
                    self._last_connection_timeout_diagnostics = (
                        await self._async_connection_timeout_diagnostics(device)
                    )
                    raise _ConnectionAttemptDeadlineExceeded(
                        "connection attempt deadline exceeded"
                    )
                return connection_task.result()
            finally:
                acknowledged = await self._async_cancel_connector(connection_task)
                if acknowledged:
                    self._release_connector_task(connection_task)

        try:
            client = await establish_with_diagnostics()
        except _ConnectionAttemptDeadlineExceeded as err:
            detail = (
                f"attempt={self._connection_attempts}; "
                f"stage={self._connection_stage}; elapsed={self._elapsed():.3f}s; "
                f"deadline={self.settings.connection.attempt_timeout:.1f}s; "
                f"pre_return_disconnects={self._pre_return_disconnects}; "
                "cause=TimeoutError: connection attempt deadline exceeded"
            )
            quarantined = self._connector_work_pending()
            late_cleanup_completed = not ADDRESS_OWNERSHIP.is_current(token)
            if quarantined:
                cleanup_ok = False
                self._last_address_cleanup_error = (
                    "connector quarantine is still active"
                )
                self._last_address_cleanup_remaining = None
                self._last_address_cleanup_elapsed = None
            elif late_cleanup_completed:
                cleanup_ok = self._last_address_cleanup_error is None
            else:
                await self._async_abort_connecting_client()
                cleanup_ok = await self.async_cleanup_stale_connection(
                    reason="connection_timeout"
                )
            timeout_diagnostics = self._last_connection_timeout_diagnostics or {}
            timeout_diagnostics["cleanup"] = {
                "success": cleanup_ok,
                "deferred_for_connector": quarantined,
                "remaining_connections": self._last_address_cleanup_remaining,
                "elapsed_seconds": self._last_address_cleanup_elapsed,
                "error": self._last_address_cleanup_error,
            }
            self._last_connection_timeout_diagnostics = timeout_diagnostics
            detail = f"{detail}; timeout_diagnostics={timeout_diagnostics}"
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
            if not self._connector_work_pending():
                await self._async_abort_connecting_client()
                await self.async_cleanup_stale_connection(
                    reason="connection_cancelled"
                )
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
        service = services.get_service(self.settings.endpoints.service_uuid)
        notify_characteristic = services.get_characteristic(
            self.settings.endpoints.notify_uuid
        )
        command_characteristic = services.get_characteristic(
            self.settings.endpoints.write_uuid
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
            self.settings.endpoints.notify_uuid,
        )
        try:
            await self._async_gatt_operation(
                "start_notify",
                self.settings.gatt_operations.notification_subscribe_timeout,
                lambda: client.start_notify(
                    self._notify_characteristic, notification_received
                ),
            )
        except GattTransportError:
            raise
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
        """Write opaque bytes using ATT Write Command (without response)."""
        data = bytes(data)
        async with self._write_lock:
            client = self._require_client()
            generation = self._generation
            self._set_stage("write_command")
            try:
                await self._async_gatt_operation(
                    "write_gatt_char",
                    self.settings.gatt_operations.write_timeout,
                    lambda: client.write_gatt_char(
                        self._command_characteristic, data, response=False
                    ),
                )
            except GattTransportError:
                raise
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
            self._set_stage("notifications_active")

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
        try:
            if client.is_connected:
                await self._async_gatt_operation(
                    "disconnect",
                    self.settings.gatt_operations.disconnect_timeout,
                    client.disconnect,
                )
        except Exception as err:
            self._last_error = exception_detail(err)
            _LOGGER.debug(
                "Disconnect cleanup did not complete normally for %s: "
                "generation=%d error=%s",
                self.name,
                self._generation,
                self._last_error,
                exc_info=True,
            )
        finally:
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
