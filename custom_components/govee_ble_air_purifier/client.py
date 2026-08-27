"""Reliable, serialized purifier client for unreliable BLE links."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum

from .bluetooth import (
    BluetoothUnavailableError,
    GattTransport,
    GattTransportError,
    HomeAssistantBluetoothEnvironment,
    exception_detail,
)
from .channel import (
    ChannelError,
    H7129SessionChannel,
    PlaintextChannel,
    SecureChannel,
)
from .models import (
    AirQualityEvent,
    DeviceProfile,
    DeviceStateEvent,
    FanMode,
    FanModeEvent,
    NightLightColorEvent,
    NightLightStateEvent,
    ProtocolCommand,
    PurifierState,
    RefreshRequestedEvent,
    SecurityMode,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
    StartupFanModeEvent,
)
from .protocol import GoveePurifierProtocol, MatchResult, RequestDescriptor

_LOGGER = logging.getLogger(__name__)

RECOVERY_EVENT_HISTORY_LIMIT = 32

StateCallback = Callable[[PurifierState], None]
AvailabilityCallback = Callable[[bool, Exception | None], None]


class ClientStatus(str, Enum):
    """Operational state of the long-running purifier client."""

    STOPPED = "stopped"
    WAITING_FOR_ADVERTISEMENT = "waiting_for_advertisement"
    CONNECTING = "connecting"
    SUBSCRIBING = "subscribing"
    NEGOTIATING = "negotiating"
    INITIALIZING = "initializing"
    READY = "ready"
    BACKOFF = "backoff"


class PurifierClientError(ConnectionError):
    """Base class for reliable-client errors."""


class TransactionTimeoutError(PurifierClientError):
    """Raised when the device does not complete a request in time."""


class CommandDeadlineExceeded(PurifierClientError):
    """Raised when a control could not be confirmed within its bounded window."""


class CommandSuperseded(PurifierClientError):
    """Raised when a newer pending control replaces an older one."""


class _RefreshPreempted(PurifierClientError):
    """Interrupt lower-priority refresh work when a command is waiting."""


@dataclass(slots=True)
class _Operation:
    command: ProtocolCommand
    future: asyncio.Future[None]
    deadline: float
    send_attempts: int = 0
    created_at: float = 0.0
    send_diagnostics: list[str] = field(default_factory=list)
    response_failures: list[str] = field(default_factory=list)
    observed_fan_frames: list[str] = field(default_factory=list)


class ReliablePurifierClient:
    """Own connection recovery, one-in-flight transactions, and cached state."""

    def __init__(
        self,
        *,
        environment: HomeAssistantBluetoothEnvironment,
        transport: GattTransport,
        protocol: GoveePurifierProtocol,
        profile: DeviceProfile,
        state_callback: StateCallback,
        availability_callback: AvailabilityCallback,
    ) -> None:
        self._environment = environment
        self._transport = transport
        self._protocol = protocol
        self._profile = profile
        self._timings = profile.timings
        if (
            protocol.profile is not profile
            or environment.profile is not profile
            or transport.profile is not profile
        ):
            raise ValueError(
                "coordinator, Bluetooth, protocol, and client must share one profile"
            )
        self._state_callback = state_callback
        self._availability_callback = availability_callback

        self.state = PurifierState()
        self.status = ClientStatus.STOPPED
        self._channel: SecureChannel | None = None
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._frame_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._operations: deque[_Operation] = deque()
        self._active_operation: _Operation | None = None
        self._operation_event = asyncio.Event()
        self._first_ready: asyncio.Future[None] | None = None
        self._session_generation = 0
        self._refresh_pending = False
        self._refresh_running = False
        self._refresh_resume_requests: tuple[RequestDescriptor, ...] = ()
        self._refresh_preemptions = 0
        self._last_refresh_preempted_request: str | None = None
        self._incomplete_refresh_requests: tuple[str, ...] = ()
        self._refresh_failure_summaries: dict[str, str] = {}
        self._next_poll_due = 0.0
        self._ready_since: float | None = None
        self._recovery_reset_handle: asyncio.TimerHandle | None = None
        self._connection_cycles = 0
        self._recovery_failure_times: deque[float] = deque(
            maxlen=RECOVERY_EVENT_HISTORY_LIMIT
        )
        self._recovery_advertisement_wake_times: deque[float] = deque(
            maxlen=RECOVERY_EVENT_HISTORY_LIMIT
        )
        self._last_recovery_failure_stage: str | None = None
        self._last_recovery_failure_cycle: int | None = None
        self._last_recovery_failure_stable_seconds = 0.0
        self._last_cycle_duration_seconds: float | None = None
        self._last_cycle_cleanup_succeeded: bool | None = None
        self._last_backoff_requested_seconds: float | None = None
        self._last_backoff_effective_seconds: float | None = None
        self._last_backoff_jittered_seconds: float | None = None
        self._last_backoff_floor_seconds = 0.0
        self._last_backoff_planned_seconds: float | None = None
        self._last_backoff_elapsed_seconds: float | None = None
        self._last_backoff_wake_reason: str | None = None
        self._plaintext_rx_count = 0
        self._active_request: str | None = None
        self._last_error: str | None = None
        self._last_timeout_summary: str | None = None
        self._last_command_diagnostics: str | None = None
        self._incomplete_initialization_requests: tuple[str, ...] = ()
        self._initialization_failure_summaries: dict[str, str] = {}
        self._essential_initialization_batches = 0
        self._essential_initialization_attempts = 0
        self._has_ever_been_ready = False
        self._reported_available: bool | None = None
        self._last_startup_mode_code: int | None = None
        self._last_startup_manual_level: int | None = None
        self._last_startup_selector_01_value: int | None = None
        self._last_startup_auto_parameter: int | None = None
        self._startup_mode_generation: int | None = None
        self._awaiting_h7129_manual_level = False
        self._startup_mode_resolution = "not_observed"

    @property
    def is_ready(self) -> bool:
        return self.status is ClientStatus.READY

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return secret-free runtime evidence for Home Assistant diagnostics."""
        current_recovery_floor = self._recovery_cooldown_floor(time.monotonic())
        return {
            "profile": self._profile.diagnostic_snapshot(
                requested_model=self._profile.model.value
            ),
            "status": self.status.value,
            "is_ready": self.is_ready,
            "has_ever_been_ready": self._has_ever_been_ready,
            "session_generation": self._session_generation,
            "connection_cycles": self._connection_cycles,
            "recovery": {
                "failure_count_in_window": len(self._recovery_failure_times),
                "advertisement_wake_count_in_window": len(
                    self._recovery_advertisement_wake_times
                ),
                "window_seconds": self._timings.recovery_storm_window,
                "failure_threshold": (
                    self._timings.recovery_storm_failure_threshold
                ),
                "advertisement_wake_threshold": (
                    self._timings.recovery_storm_advertisement_threshold
                ),
                "stable_reset_seconds": self._timings.backoff_reset_after,
                "circuit_breaker_active": current_recovery_floor > 0,
                "current_circuit_floor_seconds": current_recovery_floor,
                "last_failure_stage": self._last_recovery_failure_stage,
                "last_failure_cycle": self._last_recovery_failure_cycle,
                "last_failure_stable_seconds": (
                    self._last_recovery_failure_stable_seconds
                ),
                "last_cycle_duration_seconds": self._last_cycle_duration_seconds,
                "last_cycle_cleanup_succeeded": (
                    self._last_cycle_cleanup_succeeded
                ),
                "last_backoff_requested_seconds": (
                    self._last_backoff_requested_seconds
                ),
                "last_backoff_effective_seconds": (
                    self._last_backoff_effective_seconds
                ),
                "last_backoff_jittered_seconds": (
                    self._last_backoff_jittered_seconds
                ),
                "last_backoff_floor_seconds": self._last_backoff_floor_seconds,
                "last_backoff_planned_seconds": (
                    self._last_backoff_planned_seconds
                ),
                "last_backoff_elapsed_seconds": (
                    self._last_backoff_elapsed_seconds
                ),
                "last_backoff_wake_reason": self._last_backoff_wake_reason,
            },
            "plaintext_rx_count": self._plaintext_rx_count,
            "active_request": self._active_request,
            "last_error": self._last_error,
            "last_timeout_summary": self._last_timeout_summary,
            "last_command_diagnostics": self._last_command_diagnostics,
            "startup_fan_mode": {
                "last_mode_code": self._last_startup_mode_code,
                "last_manual_level": self._last_startup_manual_level,
                "last_selector_01_value": (
                    self._last_startup_selector_01_value
                ),
                "last_auto_parameter": self._last_startup_auto_parameter,
                "awaiting_h7129_manual_level": (
                    self._awaiting_h7129_manual_level
                ),
                "resolved_mode": (
                    self.state.fan_mode.value
                    if self.state.fan_mode is not None
                    else None
                ),
                "resolution": self._startup_mode_resolution,
                "generation": self._startup_mode_generation,
            },
            "incomplete_initialization_requests": (
                self._incomplete_initialization_requests
            ),
            "initialization_failure_summaries": dict(
                self._initialization_failure_summaries
            ),
            "essential_initialization_batches": (
                self._essential_initialization_batches
            ),
            "essential_initialization_attempts": (
                self._essential_initialization_attempts
            ),
            "essential_initialization_batch_limit": (
                self._timings.essential_initialization_max_batches
            ),
            "incomplete_refresh_requests": self._incomplete_refresh_requests,
            "refresh_failure_summaries": dict(self._refresh_failure_summaries),
            "refresh_pending": self._refresh_pending,
            "refresh_running": self._refresh_running,
            "refresh_resume_requests": tuple(
                descriptor.name for descriptor in self._refresh_resume_requests
            ),
            "refresh_preemptions": self._refresh_preemptions,
            "last_refresh_preempted_request": (
                self._last_refresh_preempted_request
            ),
            "pending_command_count": sum(
                not operation.future.done() for operation in self._operations
            ),
            "active_command": (
                type(self._active_operation.command).__name__
                if self._active_operation is not None
                else None
            ),
            "active_command_send_attempts": (
                self._active_operation.send_attempts
                if self._active_operation is not None
                else None
            ),
            "route": self._environment.route_diagnostics(),
            "transport": self._transport.diagnostic_snapshot(),
        }

    async def async_start(self) -> None:
        """Start persistent recovery without waiting for the purifier."""
        if self._runner is not None:
            return

        self._stopping.clear()
        self._first_ready = asyncio.get_running_loop().create_future()
        await self._environment.async_start()
        self._runner = asyncio.create_task(
            self._run(), name=f"govee-purifier-{self._environment.address}"
        )

    async def async_wait_until_ready(self, timeout: float | None = None) -> None:
        """Wait for essential initialization during explicit validation."""
        first_ready = self._first_ready
        if self._runner is None or first_ready is None:
            raise PurifierClientError("Purifier client is not running")
        if timeout is None:
            timeout = self._timings.startup_timeout

        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(first_ready)
        except Exception as err:
            raise BluetoothUnavailableError(
                f"Purifier {self._environment.address} did not become ready; "
                f"status={self.status.value}; "
                f"last_cycle_error={self._last_error}; "
                f"cause={exception_detail(err)}; "
                f"route={self._environment.route_diagnostics()}; "
                f"reachability={self._environment.reachability_diagnostics()}; "
                f"transport={self._transport.diagnostic_snapshot()}"
            ) from err

    async def async_shutdown(self) -> None:
        """Stop recovery, fail outstanding controls, and release BLE resources."""
        self._stopping.set()
        self._operation_event.set()
        active_operation = self._active_operation
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

        channel = self._channel
        self._channel = None
        if channel is not None:
            channel.invalidate()
        await self._transport.async_disconnect()
        await self._transport.async_cleanup_stale_connection(reason="shutdown")
        await self._environment.async_stop()
        if active_operation is not None and not active_operation.future.done():
            active_operation.future.set_exception(
                PurifierClientError("Purifier client stopped")
            )
        self._fail_all_operations(PurifierClientError("Purifier client stopped"))
        self._cancel_recovery_reset()
        self._reset_recovery_storm()
        self.status = ClientStatus.STOPPED

    async def async_execute(self, command: ProtocolCommand) -> None:
        """Execute one absolute control within a bounded recovery window."""
        if self._runner is None:
            raise PurifierClientError("Purifier client is not running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        now = loop.time()
        operation = _Operation(
            command,
            future,
            now + self._timings.command_deadline,
            created_at=now,
        )
        self._coalesce_pending(operation)
        self._operations.append(operation)
        self._operation_event.set()

        try:
            async with asyncio.timeout(self._timings.command_deadline):
                await asyncio.shield(future)
        except TimeoutError as err:
            if not future.done():
                future.cancel()
            with suppress(ValueError):
                self._operations.remove(operation)
            raise self._command_failure_error(
                operation,
                "Command was not confirmed within "
                f"{self._timings.command_deadline:.0f} seconds",
            ) from err
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            with suppress(ValueError):
                self._operations.remove(operation)
            raise

    async def _run(self) -> None:
        backoff = self._timings.backoff_initial
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            cycle_started = loop.time()
            try:
                await self._connect_initialize_and_run()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._last_error = f"{type(err).__name__}: {err}"
                failed_stage = self.status
                stable_for = (
                    loop.time() - self._ready_since
                    if self._ready_since is not None
                    else 0.0
                )
                self._record_recovery_failure(
                    failed_stage,
                    stable_for=stable_for,
                    now=loop.time(),
                )
                recovery_floor = self._recovery_cooldown_floor(loop.time())
                _LOGGER.debug(
                    "Purifier connection cycle %d failed at status=%s "
                    "session_generation=%d stable_for=%.3fs "
                    "failure_count=%d advertisement_wakes=%d "
                    "recovery_floor=%.1fs: %s",
                    self._connection_cycles,
                    failed_stage.value,
                    self._session_generation,
                    stable_for,
                    len(self._recovery_failure_times),
                    len(self._recovery_advertisement_wake_times),
                    recovery_floor,
                    self._last_error,
                    exc_info=True,
                )
                if stable_for >= self._timings.backoff_reset_after:
                    backoff = self._timings.backoff_initial
                if self._has_ever_been_ready:
                    self._set_available(False, err)
                else:
                    _LOGGER.debug(
                        "Initial purifier setup is still recovering after "
                        "cycle=%d; suppressing transient availability error",
                        self._connection_cycles,
                    )
            finally:
                self._cancel_recovery_reset()
                self._ready_since = None
                channel = self._channel
                self._channel = None
                if channel is not None:
                    channel.invalidate()
                await self._transport.async_disconnect()
                self._last_cycle_cleanup_succeeded = (
                    await self._transport.async_cleanup_stale_connection(
                        reason="connection_cycle_end"
                    )
                )
                self._last_cycle_duration_seconds = round(
                    max(0.0, loop.time() - cycle_started), 3
                )

            if self._stopping.is_set():
                break
            self.status = ClientStatus.BACKOFF
            self._last_backoff_requested_seconds = backoff
            effective_backoff = self._recovery_backoff_delay(backoff)
            self._last_backoff_effective_seconds = effective_backoff
            await self._async_backoff(effective_backoff)
            backoff = min(self._timings.backoff_max, backoff * 2)

    async def _connect_initialize_and_run(self) -> None:
        self._connection_cycles += 1
        self._session_generation += 1
        session_generation = self._session_generation
        self._disconnected.clear()
        self._drain_frame_queue()
        self._refresh_pending = False
        self._refresh_running = False
        self._refresh_resume_requests = ()
        self._incomplete_refresh_requests = ()
        self._refresh_failure_summaries = {}
        self._incomplete_initialization_requests = ()
        self._initialization_failure_summaries = {}
        self._essential_initialization_batches = 0
        self._essential_initialization_attempts = 0
        self._invalidate_connection_scoped_state()
        cached_route = self._environment.route_diagnostics()
        cached_reachability = self._environment.reachability_diagnostics()
        _LOGGER.debug(
            "Starting purifier connection cycle=%d session_generation=%d "
            "model=%s security=%s route=%s reachability=%s",
            self._connection_cycles,
            session_generation,
            self._profile.model.value,
            self._profile.security.value,
            cached_route,
            cached_reachability,
        )

        self.status = ClientStatus.WAITING_FOR_ADVERTISEMENT
        device = await self._environment.async_wait_for_fresh_device(
            self._timings.fresh_advertisement_timeout
        )
        if device is None:
            raise BluetoothUnavailableError(
                "No recent or newly observed connectable advertisement was received; "
                f"cached_route={cached_route}; "
                f"cached_reachability={cached_reachability}; "
                f"current_route={self._environment.route_diagnostics()}; "
                f"current_reachability="
                f"{self._environment.reachability_diagnostics()}"
            )

        self.status = ClientStatus.CONNECTING
        selected_route = self._environment.route_diagnostics()
        _LOGGER.debug(
            "Connectable device selected for cycle=%d: name=%s address=%s route=%s",
            self._connection_cycles,
            device.name,
            device.address,
            selected_route,
        )
        self._transport.set_disconnect_callback(
            lambda disconnected_generation: self._on_disconnected(
                session_generation, disconnected_generation
            )
        )
        try:
            transport_generation = await self._transport.async_connect(device)
        except (BluetoothUnavailableError, GattTransportError) as err:
            route_summary = (
                f"selected_route={selected_route}; "
                f"current_route={self._environment.route_diagnostics()}; "
                f"reachability={self._environment.reachability_diagnostics()}"
            )
            if isinstance(err, BluetoothUnavailableError):
                raise BluetoothUnavailableError(f"{err}; {route_summary}") from err
            raise GattTransportError(f"{err}; {route_summary}") from err
        if transport_generation != self._transport.generation:
            raise GattTransportError("Connection generation changed unexpectedly")

        def callback(frame: bytes) -> None:
            self._on_plaintext_frame(session_generation, frame)

        if self._profile.security is SecurityMode.H7129_SESSION:
            self.status = ClientStatus.NEGOTIATING
            channel: SecureChannel = H7129SessionChannel(
                self._transport,
                callback,
                self._profile,
            )
        else:
            self.status = ClientStatus.SUBSCRIBING
            channel = PlaintextChannel(
                self._transport,
                callback,
                self._profile,
            )
        self._channel = channel
        await channel.async_establish()
        _LOGGER.debug(
            "Application channel established: cycle=%d generation=%d channel=%s",
            self._connection_cycles,
            session_generation,
            type(channel).__name__,
        )

        self.status = ClientStatus.INITIALIZING
        initialization_requests = self._protocol.initialization_requests()
        _LOGGER.debug(
            "Starting initialization sweep: cycle=%d requests=%d "
            "attempts_per_request=%d",
            self._connection_cycles,
            len(initialization_requests),
            self._timings.initialization_attempts,
        )
        await self._async_run_initialization(initialization_requests)

        self.status = ClientStatus.READY
        self._has_ever_been_ready = True
        loop = asyncio.get_running_loop()
        self._ready_since = loop.time()
        self._schedule_recovery_reset(session_generation)
        initial_poll_delay = self._timings.initial_poll_delay
        self._next_poll_due = loop.time() + initial_poll_delay
        self._last_error = None
        self._last_timeout_summary = None
        _LOGGER.debug(
            "Purifier ready: cycle=%d session_generation=%d initial_poll_delay=%.3fs "
            "incomplete_startup_requests=%s state=%s",
            self._connection_cycles,
            session_generation,
            initial_poll_delay,
            self._incomplete_initialization_requests,
            self.state,
        )
        self._set_available(True, None)
        if self._first_ready is not None and not self._first_ready.done():
            self._first_ready.set_result(None)
        await self._async_ready_loop()

    async def _async_run_initialization(
        self, descriptors: tuple[RequestDescriptor, ...]
    ) -> None:
        """Attempt the full startup sweep while preserving a healthy channel."""
        failures: dict[str, str] = {}
        essential: RequestDescriptor | None = None

        for index, descriptor in enumerate(descriptors):
            if descriptor.name == self._profile.protocol.essential_request:
                essential = descriptor
                self._essential_initialization_batches += 1
            try:
                await self._async_execute_descriptor(
                    descriptor,
                    attempts=self._timings.initialization_attempts,
                )
            except TransactionTimeoutError as err:
                failures[descriptor.name] = str(err)
                self._update_initialization_failures(failures)
                _LOGGER.debug(
                    "Initialization request exhausted its retries: request=%s "
                    "attempts=%d essential=%s; continuing startup sweep",
                    descriptor.name,
                    self._timings.initialization_attempts,
                    descriptor.name == self._profile.protocol.essential_request,
                    exc_info=True,
                )
            if index + 1 < len(descriptors):
                await asyncio.sleep(self._timings.between_request_delay)

        if essential is None:
            raise PurifierClientError(
                "Initialization sequence has no essential device-state request"
            )

        while essential.name in failures:
            if (
                self._essential_initialization_batches
                >= self._timings.essential_initialization_max_batches
            ):
                summary = failures[essential.name]
                raise TransactionTimeoutError(
                    "Essential initialization remained silent after "
                    f"{self._essential_initialization_batches} batch(es) and "
                    f"{self._essential_initialization_attempts} attempt(s); "
                    "recycling the connected session; "
                    f"last_failure={summary}"
                )
            _LOGGER.debug(
                "Essential initialization request remains incomplete; preserving "
                "the connected session and retrying request=%s batch=%d/%d in %.1fs",
                essential.name,
                self._essential_initialization_batches + 1,
                self._timings.essential_initialization_max_batches,
                self._timings.initialization_retry_delay,
            )
            await self._async_wait_for_ready_work(
                self._timings.initialization_retry_delay
            )
            self._essential_initialization_batches += 1
            try:
                await self._async_execute_descriptor(
                    essential,
                    attempts=self._timings.initialization_attempts,
                )
            except TransactionTimeoutError as err:
                failures[essential.name] = str(err)
                self._update_initialization_failures(failures)
                continue
            failures.pop(essential.name)
            self._update_initialization_failures(failures)

        if failures:
            _LOGGER.debug(
                "Purifier initialization is usable with exhausted secondary "
                "requests: requests=%s attempts_per_request=%d",
                tuple(failures),
                self._timings.initialization_attempts,
            )

    def _update_initialization_failures(self, failures: dict[str, str]) -> None:
        """Publish secret-free evidence for startup requests that stayed silent."""
        self._incomplete_initialization_requests = tuple(failures)
        self._initialization_failure_summaries = dict(failures)

    async def _async_ready_loop(self) -> None:
        while not self._stopping.is_set():
            self._discard_expired_operations()

            if self._operations:
                operation = self._operations.popleft()
                self._active_operation = operation
                try:
                    await self._async_execute_operation(operation)
                finally:
                    self._active_operation = None
                continue

            if self._refresh_pending:
                # An idle ee-aa capture began its refresh at about +1 ms.
                await asyncio.sleep(self._timings.between_request_delay)
                await self._async_run_refresh(self._protocol.refresh_requests())
                continue

            loop = asyncio.get_running_loop()
            if loop.time() >= self._next_poll_due:
                await self._async_execute_descriptor(
                    self._protocol.device_state_poll(),
                    attempts=self._timings.periodic_poll_attempts,
                    is_periodic_poll=True,
                )
                continue

            await self._async_wait_for_ready_work(
                max(0.0, self._next_poll_due - loop.time())
            )

    async def _async_execute_operation(self, operation: _Operation) -> None:
        if operation.future.done():
            return
        loop = asyncio.get_running_loop()
        if loop.time() >= operation.deadline:
            operation.future.set_exception(
                self._command_failure_error(
                    operation,
                    "Command deadline expired before execution",
                )
            )
            return

        # Initialization after every reconnect re-queries the relevant state.
        # If an ambiguous previous write was actually applied, do not replay it.
        if self._command_is_satisfied(operation.command):
            operation.future.set_result(None)
            return

        if operation.send_attempts >= self._timings.command_send_attempts:
            operation.future.set_exception(
                self._command_failure_error(
                    operation,
                    "Command remained unconfirmed after bounded retries",
                )
            )
            return

        descriptor = self._protocol.command_request(operation.command)
        while operation.send_attempts < self._timings.command_send_attempts:
            if operation.future.done():
                return
            if loop.time() >= operation.deadline:
                operation.future.set_exception(
                    self._command_failure_error(
                        operation,
                        "Command deadline expired before retry",
                    )
                )
                return

            _LOGGER.debug(
                "Command transaction: command=%s attempt=%d/%d remaining=%.3fs "
                "session_generation=%d frame=%s",
                type(operation.command).__name__,
                operation.send_attempts + 1,
                self._timings.command_send_attempts,
                max(0.0, operation.deadline - loop.time()),
                self._session_generation,
                descriptor.frame.hex(" "),
            )

            send_recorded = False

            def record_send() -> None:
                nonlocal send_recorded
                if not send_recorded:
                    operation.send_attempts += 1
                    send_recorded = True
                    elapsed = (
                        loop.time() - operation.created_at
                        if operation.created_at > 0
                        else 0.0
                    )
                    operation.send_diagnostics.append(
                        f"attempt={operation.send_attempts},"
                        f"generation={self._session_generation},"
                        f"elapsed={elapsed:.3f}s"
                    )

            try:
                async with asyncio.timeout_at(operation.deadline):
                    await self._async_execute_descriptor(
                        descriptor,
                        attempts=1,
                        on_send=record_send,
                    )
            except TransactionTimeoutError as err:
                operation.response_failures.append(
                    f"generation={self._session_generation}: {err}"
                )
                if (
                    operation.send_attempts < self._timings.command_send_attempts
                    and not operation.future.done()
                    and loop.time() < operation.deadline
                ):
                    _LOGGER.debug(
                        "Command response stayed silent; retrying on the same "
                        "session command=%s next_attempt=%d/%d",
                        type(operation.command).__name__,
                        operation.send_attempts + 1,
                        self._timings.command_send_attempts,
                    )
                    continue
                self._queue_operation_for_reconciliation(operation)
                raise
            except TimeoutError as err:
                failure = self._command_failure_error(
                    operation,
                    "Command deadline expired during Bluetooth transaction",
                )
                if not operation.future.done():
                    operation.future.set_exception(failure)
                raise failure from err
            except Exception:
                self._queue_operation_for_reconciliation(operation)
                raise
            else:
                self._apply_confirmed_command(operation.command)
                if not operation.future.done():
                    operation.future.set_result(None)
                self._last_command_diagnostics = None
                return

    def _apply_confirmed_command(self, command: ProtocolCommand) -> None:
        """Publish state established by a documented command acknowledgement."""
        if isinstance(command, SetFanMode):
            self._clear_startup_mode_partial("superseded_by_command_acknowledgement")
            self._startup_mode_resolution = "superseded_by_command_acknowledgement"
            self._startup_mode_generation = self._session_generation
            if self.state.fan_mode is not command.mode:
                self.state = replace(self.state, fan_mode=command.mode)
                self._state_callback(self.state)

    async def _async_run_refresh(
        self, descriptors: tuple[RequestDescriptor, ...]
    ) -> None:
        """Run resumable best-effort telemetry below queued command priority."""
        resuming = bool(self._refresh_resume_requests)
        active_descriptors = self._refresh_resume_requests or descriptors
        failures = dict(self._refresh_failure_summaries) if resuming else {}
        self._refresh_running = True
        self._refresh_pending = False
        self._refresh_resume_requests = ()
        self._update_refresh_failures(failures)
        try:
            for index, descriptor in enumerate(active_descriptors):
                if self._has_pending_command():
                    self._defer_refresh(active_descriptors[index:], descriptor.name)
                    return
                _LOGGER.debug(
                    "Refresh request %d/%d: name=%s attempts=%d",
                    index + 1,
                    len(active_descriptors),
                    descriptor.name,
                    self._timings.refresh_attempts,
                )
                try:
                    await self._async_execute_descriptor(
                        descriptor,
                        attempts=self._timings.refresh_attempts,
                        interrupt_for_command=True,
                    )
                except _RefreshPreempted:
                    self._defer_refresh(active_descriptors[index:], descriptor.name)
                    return
                except TransactionTimeoutError as err:
                    failures[descriptor.name] = str(err)
                    self._update_refresh_failures(failures)
                    if descriptor.name == self._profile.protocol.essential_request:
                        _LOGGER.debug(
                            "Essential refresh request exhausted its retries; "
                            "reconnecting request=%s attempts=%d",
                            descriptor.name,
                            self._timings.refresh_attempts,
                            exc_info=True,
                        )
                        raise
                    _LOGGER.debug(
                        "Secondary refresh request exhausted its retries; "
                        "preserving connection request=%s attempts=%d",
                        descriptor.name,
                        self._timings.refresh_attempts,
                        exc_info=True,
                    )
                if index + 1 < len(active_descriptors):
                    await asyncio.sleep(self._timings.between_request_delay)
        finally:
            self._refresh_running = False

    def _has_pending_command(self) -> bool:
        """Return whether an unexpired user command is waiting for the owner."""
        now = asyncio.get_running_loop().time()
        return any(
            not operation.future.done() and now < operation.deadline
            for operation in self._operations
        )

    def _defer_refresh(
        self,
        remaining: tuple[RequestDescriptor, ...],
        interrupted_request: str,
    ) -> None:
        """Preserve refresh order and resume it after higher-priority controls."""
        self._refresh_resume_requests = remaining
        self._refresh_pending = True
        self._refresh_preemptions += 1
        self._last_refresh_preempted_request = interrupted_request
        _LOGGER.debug(
            "Refresh yielded to a pending command: request=%s remaining=%s "
            "preemptions=%d",
            interrupted_request,
            tuple(descriptor.name for descriptor in remaining),
            self._refresh_preemptions,
        )

    def _update_refresh_failures(self, failures: dict[str, str]) -> None:
        """Publish response exhaustion from the most recent refresh sweep."""
        self._incomplete_refresh_requests = tuple(failures)
        self._refresh_failure_summaries = dict(failures)

    async def _async_execute_descriptor(
        self,
        descriptor: RequestDescriptor,
        *,
        attempts: int,
        is_periodic_poll: bool = False,
        interrupt_for_command: bool = False,
        on_send: Callable[[], None] | None = None,
    ) -> tuple[bytes, ...]:
        channel = self._channel
        if channel is None or not channel.ready:
            raise ChannelError("Application channel is not ready")

        loop = asyncio.get_running_loop()
        attempt_summaries: list[str] = []
        self._active_request = descriptor.name
        try:
            for attempt in range(attempts):
                if (
                    self.status is ClientStatus.INITIALIZING
                    and descriptor.name == self._profile.protocol.essential_request
                ):
                    self._essential_initialization_attempts += 1
                matcher = self._protocol.new_response_matcher(descriptor)
                observed_frames: list[bytes] = []
                ignored_frames: list[bytes] = []
                started = loop.time()
                _LOGGER.debug(
                    "TX transaction: request=%s attempt=%d/%d frame=%s",
                    descriptor.name,
                    attempt + 1,
                    attempts,
                    descriptor.frame.hex(" "),
                )
                if on_send is not None:
                    on_send()
                await channel.async_send(descriptor.frame)
                if is_periodic_poll:
                    # This is deliberately write-to-write, not response-to-write.
                    self._next_poll_due = loop.time() + self._timings.poll_interval
                deadline = loop.time() + self._timings.transaction_timeout

                try:
                    while not matcher.complete:
                        frame = await self._async_next_transaction_frame(
                            deadline,
                            interrupt_for_command=interrupt_for_command,
                        )
                        observed_frames.append(frame)
                        result = matcher.feed(frame)
                        self._process_plaintext_frame(
                            frame,
                            matched_request=(
                                descriptor.name
                                if result is MatchResult.COMPLETE
                                else None
                            ),
                        )
                        _LOGGER.debug(
                            "Transaction candidate: request=%s attempt=%d/%d "
                            "match=%s frame=%s",
                            descriptor.name,
                            attempt + 1,
                            attempts,
                            result.value,
                            frame.hex(" "),
                        )
                        if result is MatchResult.IGNORED:
                            ignored_frames.append(frame)
                        if result is MatchResult.COMPLETE:
                            self._last_timeout_summary = None
                            _LOGGER.debug(
                                "Transaction complete: request=%s attempt=%d/%d "
                                "latency=%.3fs received=%d matched=%d",
                                descriptor.name,
                                attempt + 1,
                                attempts,
                                loop.time() - started,
                                len(observed_frames),
                                len(matcher.frames),
                            )
                            return matcher.frames
                except TimeoutError:
                    ignored_sample = " | ".join(
                        frame.hex(" ") for frame in ignored_frames[:4]
                    )
                    summary = (
                        f"attempt {attempt + 1}/{attempts}: "
                        f"received={len(observed_frames)}, "
                        f"matched_fragments={len(matcher.frames)}, "
                        f"ignored={len(ignored_frames)}, "
                        f"ignored_sample={ignored_sample or 'none'}"
                    )
                    attempt_summaries.append(summary)
                    self._last_timeout_summary = f"request={descriptor.name}; {summary}"
                    _LOGGER.debug(
                        "Transaction timeout: request=%s elapsed=%.3fs %s",
                        descriptor.name,
                        loop.time() - started,
                        summary,
                    )

            details = "; ".join(attempt_summaries)
            raise TransactionTimeoutError(
                f"Timed out waiting for {descriptor.name} response after "
                f"{attempts} attempt(s) with "
                f"{self._timings.transaction_timeout:.1f}s deadlines; "
                f"{details}"
            )
        finally:
            self._active_request = None

    async def _async_next_transaction_frame(
        self,
        deadline: float,
        *,
        interrupt_for_command: bool = False,
    ) -> bytes:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError

        frame_task = asyncio.create_task(self._frame_queue.get())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        operation_task = (
            asyncio.create_task(self._operation_event.wait())
            if interrupt_for_command
            else None
        )
        tasks = (
            (frame_task, disconnect_task, operation_task)
            if operation_task is not None
            else (frame_task, disconnect_task)
        )
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not done:
            raise TimeoutError
        if disconnect_task in done and disconnect_task.result():
            if not frame_task.done():
                frame_task.cancel()
            raise GattTransportError("Purifier disconnected during transaction")
        if frame_task in done:
            return frame_task.result()
        if operation_task is not None and operation_task in done:
            raise _RefreshPreempted("Refresh yielded to a pending command")
        raise TimeoutError

    async def _async_wait_for_ready_work(self, timeout: float) -> None:
        frame_task = asyncio.create_task(self._frame_queue.get())
        operation_task = asyncio.create_task(self._operation_event.wait())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        tasks = (frame_task, operation_task, disconnect_task)
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if disconnect_task in done and disconnect_task.result():
            raise GattTransportError("Purifier disconnected")
        if frame_task in done:
            self._process_plaintext_frame(frame_task.result())
        if operation_task in done:
            self._operation_event.clear()

    def _process_plaintext_frame(
        self,
        frame: bytes,
        *,
        matched_request: str | None = None,
    ) -> None:
        event = self._protocol.decode(frame)
        new_state = self.state
        if isinstance(event, DeviceStateEvent) and event.power is not None:
            new_state = replace(new_state, power=event.power)
        elif isinstance(event, StartupFanModeEvent):
            new_state = self._apply_startup_fan_mode_event(
                event,
                matched_request=matched_request,
            )
        elif isinstance(event, FanModeEvent):
            self._clear_startup_mode_partial("superseded_by_physical_update")
            self._startup_mode_resolution = "superseded_by_physical_update"
            self._startup_mode_generation = self._session_generation
            new_state = replace(new_state, fan_mode=event.mode)
        elif isinstance(event, NightLightStateEvent):
            changes: dict[str, object] = {}
            if event.power is not None:
                changes["light_power"] = event.power
            if 1 <= event.brightness <= 100:
                changes["light_brightness"] = event.brightness
            if changes:
                new_state = replace(new_state, **changes)
        elif (
            isinstance(event, NightLightColorEvent)
            and event.color_available
            and not event.acknowledgement_only
        ):
            assert event.red is not None
            assert event.green is not None
            assert event.blue is not None
            new_state = replace(
                new_state, light_rgb=(event.red, event.green, event.blue)
            )
        elif isinstance(event, AirQualityEvent):
            new_state = replace(
                new_state,
                pm25=event.pm25_ug_m3,
                filter_life=event.filter_life,
            )
        elif isinstance(event, RefreshRequestedEvent):
            # The short sweep is documented only for an active H7129 session.
            if self._profile.capabilities.refresh and not self._refresh_running:
                self._refresh_pending = True

        if new_state != self.state:
            self.state = new_state
            self._state_callback(new_state)

    def _apply_startup_fan_mode_event(
        self,
        event: StartupFanModeEvent,
        *,
        matched_request: str | None,
    ) -> PurifierState:
        """Apply an ``aa 05`` response only to its completed named query."""
        expected_request = f"mode_data_{event.selector:02x}"
        if matched_request != expected_request or expected_request not in {
            "mode_data_00",
            "mode_data_01",
            "mode_data_03",
        }:
            _LOGGER.debug(
                "Ignoring unmatched startup fan-mode response: selector=%02x "
                "matched_request=%s expected_request=%s frame=%s",
                event.selector,
                matched_request,
                expected_request,
                event.frame.hex(" "),
            )
            return self.state

        if event.selector == 0x03:
            self._startup_mode_generation = self._session_generation
            self._last_startup_auto_parameter = event.auto_parameter
            _LOGGER.debug(
                "Recorded startup fan Auto parameter: model=%s generation=%d "
                "value=%s",
                self._profile.model.value,
                self._session_generation,
                event.auto_parameter,
            )
            return self.state

        if event.selector == 0x01:
            self._last_startup_selector_01_value = event.level_or_configuration
            if (
                self._profile.protocol.startup_mode_strategy
                != "h7129_selector_pair"
            ):
                self._startup_mode_generation = self._session_generation
                return self.state
            completes_current_pair = (
                self._awaiting_h7129_manual_level
                and self._startup_mode_generation == self._session_generation
            )
            self._startup_mode_generation = self._session_generation
            if not completes_current_pair:
                _LOGGER.debug(
                    "Ignoring H7129 startup manual-level response without a "
                    "current manual category: generation=%d value=%s frame=%s",
                    self._session_generation,
                    event.level_or_configuration,
                    event.frame.hex(" "),
                )
                return self.state

            self._last_startup_manual_level = event.level_or_configuration
            mode = {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(event.level_or_configuration)
            self._awaiting_h7129_manual_level = False
            if mode is None:
                self._startup_mode_resolution = (
                    f"unknown_manual_level:{event.level_or_configuration}"
                )
                _LOGGER.debug(
                    "H7129 startup fan mode remains unknown: generation=%d "
                    "mode_code=01 manual_level=%s frame=%s",
                    self._session_generation,
                    event.level_or_configuration,
                    event.frame.hex(" "),
                )
                return replace(self.state, fan_mode=None)
            self._startup_mode_resolution = f"resolved:{mode.value}"
            return replace(self.state, fan_mode=mode)

        assert event.selector == 0x00
        self._last_startup_mode_code = event.mode_code
        self._last_startup_manual_level = event.manual_level
        self._clear_startup_mode_partial("new_mode_category")
        self._startup_mode_generation = self._session_generation

        if (
            self._profile.protocol.startup_mode_strategy
            == "h7124_selector_00"
        ):
            mode = self._decode_h7124_startup_mode(
                event.mode_code,
                event.manual_level,
            )
            if mode is None:
                self._startup_mode_resolution = (
                    f"unknown_h7124_combination:{event.mode_code}:"
                    f"{event.manual_level}"
                )
                _LOGGER.debug(
                    "H7124 startup fan mode remains unknown: generation=%d "
                    "mode_code=%s manual_level=%s frame=%s",
                    self._session_generation,
                    event.mode_code,
                    event.manual_level,
                    event.frame.hex(" "),
                )
            else:
                self._startup_mode_resolution = f"resolved:{mode.value}"
            return replace(self.state, fan_mode=mode)

        if event.mode_code == 0x01:
            self._awaiting_h7129_manual_level = True
            self._startup_mode_resolution = "awaiting_manual_level"
            return replace(self.state, fan_mode=None)

        mode = {
            0x03: FanMode.AUTO,
            0x05: FanMode.SLEEP,
            0x07: FanMode.TURBO,
        }.get(event.mode_code)
        if mode is None:
            self._startup_mode_resolution = (
                f"unknown_h7129_mode_code:{event.mode_code}"
            )
            _LOGGER.debug(
                "H7129 startup fan mode remains unknown: generation=%d "
                "mode_code=%s frame=%s",
                self._session_generation,
                event.mode_code,
                event.frame.hex(" "),
            )
        else:
            self._startup_mode_resolution = f"resolved:{mode.value}"
        return replace(self.state, fan_mode=mode)

    @staticmethod
    def _decode_h7124_startup_mode(
        mode_code: int | None,
        manual_level: int | None,
    ) -> FanMode | None:
        if mode_code == 0x01:
            return {
                0x01: FanMode.LOW,
                0x02: FanMode.MEDIUM,
                0x03: FanMode.HIGH,
            }.get(manual_level)
        if manual_level != 0x00:
            return None
        return {
            0x03: FanMode.AUTO,
            0x05: FanMode.SLEEP,
            0x07: FanMode.TURBO,
        }.get(mode_code)

    def _clear_startup_mode_partial(self, reason: str) -> None:
        """Discard connection-scoped H7129 mode assembly state."""
        if self._awaiting_h7129_manual_level:
            _LOGGER.debug(
                "Clearing pending H7129 startup fan mode: generation=%s reason=%s",
                self._startup_mode_generation,
                reason,
            )
        self._awaiting_h7129_manual_level = False

    def _command_is_satisfied(self, command: ProtocolCommand) -> bool:
        if isinstance(command, SetPower):
            return self.state.power is command.on
        if isinstance(command, SetFanMode):
            return self.state.fan_mode is command.mode
        if isinstance(command, SetNightLightPower):
            return self.state.light_power is command.on
        if isinstance(command, SetNightLightBrightness):
            return self.state.light_brightness == command.percent
        if isinstance(command, SetNightLightColor):
            # H7129 may answer the startup color query with the value-less fc
            # form, while 3a color frames are acknowledgement echoes only.
            # Cached RGB therefore cannot safely suppress an ambiguous retry.
            return False
        return False

    def _command_diagnostic_summary(
        self,
        operation: _Operation,
        reason: str,
    ) -> str:
        """Return bounded evidence that survives command reconciliation."""
        descriptor = self._protocol.command_request(operation.command)
        command_detail = type(operation.command).__name__
        if isinstance(operation.command, SetFanMode):
            command_detail += f"(mode={operation.command.mode.value})"
        sends = " | ".join(
            operation.send_diagnostics[-self._timings.command_send_attempts :]
        )
        failures = " | ".join(
            operation.response_failures[-self._timings.command_send_attempts :]
        )
        fan_frames = " | ".join(operation.observed_fan_frames[-8:])
        cached_fan_mode = (
            self.state.fan_mode.value if self.state.fan_mode is not None else None
        )
        return (
            f"{reason}; command={command_detail}; "
            f"frame={descriptor.frame.hex(' ')}; "
            "sends="
            f"{operation.send_attempts}/{self._timings.command_send_attempts}; "
            f"send_timeline={sends or 'none'}; "
            f"response_failures={failures or 'none'}; "
            f"observed_fan_frames={fan_frames or 'none'}; "
            f"current_generation={self._session_generation}; "
            f"cached_fan_mode={cached_fan_mode}"
        )

    def _command_failure_error(
        self,
        operation: _Operation,
        reason: str,
    ) -> CommandDeadlineExceeded:
        """Create and retain the final bounded diagnostic command error."""
        summary = self._command_diagnostic_summary(operation, reason)
        self._last_command_diagnostics = summary
        return CommandDeadlineExceeded(summary)

    def _pending_fan_operation(self) -> _Operation | None:
        """Return the fan command to which a fan response may be relevant."""
        active = self._active_operation
        if (
            active is not None
            and isinstance(active.command, SetFanMode)
            and not active.future.done()
        ):
            return active
        return next(
            (
                operation
                for operation in self._operations
                if isinstance(operation.command, SetFanMode)
                and not operation.future.done()
            ),
            None,
        )

    def _record_fan_frame_diagnostic(self, generation: int, frame: bytes) -> None:
        """Correlate fan acknowledgements and notifications with a command."""
        if not frame.startswith((b"\x3a\x05", b"\xee\x05")):
            return
        operation = self._pending_fan_operation()
        if operation is None:
            return

        mode: str | None = None
        if frame.startswith(b"\x3a\x05"):
            role = "command_echo"
            matches_request = (
                frame
                == self._protocol.command_request(operation.command).frame
            )
            if matches_request:
                mode = operation.command.mode.value
        else:
            role = "physical_update"
            matches_request = False
            try:
                event = self._protocol.decode(frame)
            except Exception as err:  # noqa: BLE001 - diagnostics must not drop a frame
                mode = f"decode_error:{type(err).__name__}"
            else:
                if isinstance(event, FanModeEvent) and event.mode is not None:
                    mode = event.mode.value

        loop = asyncio.get_running_loop()
        elapsed = (
            loop.time() - operation.created_at if operation.created_at > 0 else 0.0
        )
        phase = self._active_request or self.status.value
        detail = (
            f"generation={generation},elapsed={elapsed:.3f}s,phase={phase},"
            f"role={role},matches_request={matches_request},"
            f"decoded_mode={mode},frame={frame.hex(' ')}"
        )
        operation.observed_fan_frames.append(detail)
        _LOGGER.debug(
            "Fan command diagnostic notification: requested_mode=%s %s",
            operation.command.mode.value,
            detail,
        )

    def _invalidate_connection_scoped_state(self) -> None:
        """Forget values that must be re-established after reconnect."""
        self._clear_startup_mode_partial("connection_invalidated")
        self._startup_mode_resolution = "awaiting_startup_query"
        if self.state.fan_mode is not None:
            self.state = replace(self.state, fan_mode=None)

    def _on_plaintext_frame(self, generation: int, frame: bytes) -> None:
        if generation != self._session_generation or self._stopping.is_set():
            _LOGGER.debug(
                "Ignoring plaintext frame from stale/stopped session: "
                "frame_generation=%d current_generation=%d stopping=%s frame=%s",
                generation,
                self._session_generation,
                self._stopping.is_set(),
                frame.hex(" "),
            )
            return
        self._record_fan_frame_diagnostic(generation, frame)
        self._plaintext_rx_count += 1
        _LOGGER.debug(
            "RX plaintext queued: session_generation=%d count=%d active_request=%s "
            "frame=%s",
            generation,
            self._plaintext_rx_count,
            self._active_request,
            frame.hex(" "),
        )
        self._frame_queue.put_nowait(frame)

    def _on_disconnected(
        self,
        session_generation: int,
        disconnected_transport_generation: int,
    ) -> None:
        if (
            session_generation != self._session_generation
            or disconnected_transport_generation != self._transport.generation
        ):
            _LOGGER.debug(
                "Ignoring stale disconnect callback: session_generation=%d/%d "
                "transport_generation=%d/%d",
                session_generation,
                self._session_generation,
                disconnected_transport_generation,
                self._transport.generation,
            )
            return
        _LOGGER.debug(
            "Current purifier connection dropped: status=%s active_request=%s "
            "session_generation=%d transport_generation=%d",
            self.status.value,
            self._active_request,
            session_generation,
            disconnected_transport_generation,
        )
        channel = self._channel
        if channel is not None:
            channel.invalidate()
        self._invalidate_connection_scoped_state()
        self._disconnected.set()

    def _set_available(self, available: bool, error: Exception | None) -> None:
        if self._reported_available is available:
            _LOGGER.debug(
                "Purifier availability unchanged: available=%s status=%s",
                available,
                self.status.value,
            )
            return
        self._reported_available = available
        _LOGGER.debug(
            "Purifier availability update: available=%s status=%s error=%s",
            available,
            self.status.value,
            f"{type(error).__name__}: {error}" if error is not None else None,
        )
        self._availability_callback(available, error)

    def _trim_recovery_window(self, now: float) -> None:
        """Discard failure-storm evidence outside the bounded time window."""
        cutoff = now - self._timings.recovery_storm_window
        while (
            self._recovery_failure_times
            and self._recovery_failure_times[0] < cutoff
        ):
            self._recovery_failure_times.popleft()
        while (
            self._recovery_advertisement_wake_times
            and self._recovery_advertisement_wake_times[0] < cutoff
        ):
            self._recovery_advertisement_wake_times.popleft()

    def _reset_recovery_storm(self) -> None:
        """Forget consecutive unstable-cycle evidence after stable operation."""
        self._recovery_failure_times.clear()
        self._recovery_advertisement_wake_times.clear()

    def _cancel_recovery_reset(self) -> None:
        """Cancel the stable-session reset callback for the previous cycle."""
        handle = self._recovery_reset_handle
        self._recovery_reset_handle = None
        if handle is not None:
            handle.cancel()

    def _schedule_recovery_reset(self, session_generation: int) -> None:
        """Clear circuit evidence once this READY session is durably healthy."""
        self._cancel_recovery_reset()
        loop = asyncio.get_running_loop()

        def reset_if_current() -> None:
            self._recovery_reset_handle = None
            if (
                self.status is not ClientStatus.READY
                or session_generation != self._session_generation
            ):
                return
            self._reset_recovery_storm()
            _LOGGER.debug(
                "Bluetooth recovery circuit reset after stable READY session: "
                "session_generation=%d stable_for=%.1fs",
                session_generation,
                self._timings.backoff_reset_after,
            )

        self._recovery_reset_handle = loop.call_later(
            self._timings.backoff_reset_after,
            reset_if_current,
        )

    def _record_recovery_failure(
        self,
        stage: ClientStatus,
        *,
        stable_for: float,
        now: float,
    ) -> None:
        """Record one cycle that failed before becoming durably healthy."""
        self._last_recovery_failure_stage = stage.value
        self._last_recovery_failure_cycle = self._connection_cycles
        self._last_recovery_failure_stable_seconds = round(
            max(0.0, stable_for), 3
        )
        if stable_for >= self._timings.backoff_reset_after:
            self._reset_recovery_storm()
            return
        self._trim_recovery_window(now)
        self._recovery_failure_times.append(now)

    def _record_advertisement_wake(self, now: float) -> None:
        """Record a fresh advertisement that ended a scheduled recovery wait."""
        self._trim_recovery_window(now)
        self._recovery_advertisement_wake_times.append(now)

    def _recovery_cooldown_floor(self, now: float) -> float:
        """Return the circuit-breaker floor for repeated unstable recovery."""
        self._trim_recovery_window(now)
        failures = len(self._recovery_failure_times)
        advertisement_wakes = len(self._recovery_advertisement_wake_times)
        if (
            failures < self._timings.recovery_storm_failure_threshold
            or advertisement_wakes
            < self._timings.recovery_storm_advertisement_threshold
        ):
            return 0.0
        if failures == self._timings.recovery_storm_failure_threshold:
            return self._timings.recovery_storm_initial_floor
        return self._timings.recovery_storm_max_floor

    def _recovery_backoff_delay(self, requested: float) -> float:
        """Cap recovery delay while Home Assistant still sees advertisements."""
        recent = self._environment.has_recent_advertisement(
            self._timings.fresh_advertisement_timeout
        )
        delay = (
            min(requested, self._timings.recent_advertisement_backoff_max)
            if recent
            else requested
        )
        _LOGGER.debug(
            "Bluetooth recovery backoff policy: requested=%.1fs effective=%.1fs "
            "recent_advertisement=%s recent_limit=%.1fs",
            requested,
            delay,
            recent,
            self._timings.fresh_advertisement_timeout,
        )
        return delay

    def _coalesce_pending(self, replacement: _Operation) -> None:
        key = type(replacement.command)
        retained: deque[_Operation] = deque()
        while self._operations:
            pending = self._operations.popleft()
            if type(pending.command) is key and not pending.future.done():
                pending.future.set_exception(
                    CommandSuperseded("A newer control superseded this request")
                )
            else:
                retained.append(pending)
        self._operations = retained

    def _queue_operation_for_reconciliation(self, operation: _Operation) -> None:
        """Keep an ambiguous command pending for post-reconnect state checks."""
        if operation.future.done():
            return
        if asyncio.get_running_loop().time() >= operation.deadline:
            operation.future.set_exception(
                self._command_failure_error(
                    operation,
                    "Command deadline expired during recovery",
                )
            )
            return
        self._operations.appendleft(operation)
        self._operation_event.set()

    def _discard_expired_operations(self) -> None:
        now = asyncio.get_running_loop().time()
        retained: deque[_Operation] = deque()
        while self._operations:
            operation = self._operations.popleft()
            if operation.future.done():
                continue
            if now >= operation.deadline:
                operation.future.set_exception(
                    self._command_failure_error(
                        operation,
                        "Command deadline expired while queued",
                    )
                )
            else:
                retained.append(operation)
        self._operations = retained
        if not self._operations:
            self._operation_event.clear()

    def _fail_all_operations(self, error: Exception) -> None:
        while self._operations:
            operation = self._operations.popleft()
            if not operation.future.done():
                operation.future.set_exception(error)

    def _drain_frame_queue(self) -> None:
        while True:
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _async_backoff(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        advertisement_cutoff = time.monotonic()
        jittered_delay = delay * random.uniform(0.8, 1.2)
        recovery_floor = self._recovery_cooldown_floor(started)
        planned_delay = max(jittered_delay, recovery_floor)
        self._last_backoff_jittered_seconds = round(jittered_delay, 3)
        self._last_backoff_floor_seconds = recovery_floor
        self._last_backoff_planned_seconds = round(planned_delay, 3)
        _LOGGER.debug(
            "Bluetooth recovery backoff: base=%.1fs jittered=%.3fs "
            "circuit_floor=%.1fs planned=%.3fs next_max=%.1fs",
            delay,
            jittered_delay,
            recovery_floor,
            planned_delay,
            self._timings.backoff_max,
        )
        stop_task = asyncio.create_task(self._stopping.wait())
        operation_task = asyncio.create_task(self._operation_event.wait())
        advertisement_task = asyncio.create_task(
            self._environment.async_wait_for_advertisement_after(
                advertisement_cutoff
            )
        )
        tasks = (stop_task, operation_task, advertisement_task)
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=planned_delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
            advertisement_triggered = advertisement_task in done
            if not done:
                reason = "scheduled_delay"
            elif stop_task in done and stop_task.result():
                reason = "shutdown"
            elif operation_task in done and operation_task.result():
                reason = "queued_command"
                # Consume this wake edge. The operation itself remains in the
                # queue, so another failed connection cannot busy-loop merely
                # because the same command is still pending.
                self._operation_event.clear()
            else:
                reason = "fresh_advertisement"

            minimum_wait = recovery_floor
            if reason == "fresh_advertisement":
                minimum_wait = max(
                    minimum_wait,
                    self._timings.advertisement_settle_delay,
                )
            remaining_floor = max(
                0.0,
                minimum_wait - (loop.time() - started),
            )
            if reason != "shutdown" and remaining_floor:
                # Once the circuit opens, advertisements and commands remain
                # useful wake evidence but cannot force BlueZ into another
                # immediate connection attempt. Shutdown always wins.
                floor_tasks = (
                    (stop_task,)
                    if recovery_floor > 0
                    else (stop_task, operation_task)
                )
                done, _ = await asyncio.wait(
                    floor_tasks,
                    timeout=remaining_floor,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done and stop_task.result():
                    reason = "shutdown"
                elif operation_task in done and operation_task.result():
                    reason = "queued_command"
                    self._operation_event.clear()

            if reason != "shutdown" and self._operation_event.is_set():
                reason = "queued_command"
                self._operation_event.clear()
            if advertisement_triggered and reason != "shutdown":
                self._record_advertisement_wake(loop.time())

            elapsed = max(0.0, loop.time() - started)
            self._last_backoff_elapsed_seconds = round(elapsed, 3)
            self._last_backoff_wake_reason = reason
            _LOGGER.debug(
                "Bluetooth recovery backoff ended: reason=%s elapsed=%.3fs "
                "circuit_floor=%.1fs failure_count=%d advertisement_wakes=%d",
                reason,
                elapsed,
                recovery_floor,
                len(self._recovery_failure_times),
                len(self._recovery_advertisement_wake_times),
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
