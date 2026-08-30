"""Reliable, serialized purifier client for unreliable BLE links."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
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
    FanMode,
    FanModeEvent,
    ProtocolCommand,
    PurifierState,
    QueryAirQuality,
    SecurityMode,
    SetFanMode,
    StartupFanModeEvent,
)
from .observations import (
    AirQualityObservation,
    CommandOrigin,
    FanModeObservation,
    ObservationPurpose,
    ObservationSource,
    PurifierObservation,
    ReceivedFrame,
)
from .operations import (
    AirQualityQueryCancelled,
    AirQualityQueryController,
    AirQualityQueryPreempted,
    AirQualityQueryTimeout,
    CommandDeadlineExceeded,
    CommandOperationController,
    CommandSuperseded,
    PurifierClientError,
    _AirQualityRequest,
    _Operation,
)
from .profiles import DeviceProfile
from .protocol import (
    GoveePurifierProtocol,
    RequestDescriptor,
)
from .protocol import MatchResult as MatchResult
from .recovery import BackoffDecision, RecoveryController
from .state_reducer import PurifierStateReducer
from .transactions import (
    RefreshPreemptedError,
    TransactionExecutor,
    TransactionTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "AirQualityQueryCancelled",
    "AirQualityQueryPreempted",
    "AirQualityQueryTimeout",
    "CommandDeadlineExceeded",
    "CommandSuperseded",
    "PurifierClientError",
    "ReliablePurifierClient",
    "TransactionTimeoutError",
    "_Operation",
]

StateCallback = Callable[[PurifierState], None]
AvailabilityCallback = Callable[[bool, Exception | None], None]
ObservationCallback = Callable[[PurifierObservation], None]


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


_RefreshPreempted = RefreshPreemptedError


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
        observation_callback: ObservationCallback | None = None,
    ) -> None:
        self._environment = environment
        self._transport = transport
        self._protocol = protocol
        self._profile = profile
        self._timings = profile.timings
        if protocol.profile is not profile:
            raise ValueError("protocol and client must share one device profile")
        if environment.settings is not transport.settings:
            raise ValueError(
                "Bluetooth environment and transport must share one settings instance"
            )
        self._state_callback = state_callback
        self._availability_callback = availability_callback
        self._observation_callback = observation_callback

        self._state_reducer = PurifierStateReducer(profile)
        self.status = ClientStatus.STOPPED
        self._channel: SecureChannel | None = None
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._frame_queue: asyncio.Queue[ReceivedFrame] = asyncio.Queue()
        self._command_operations = CommandOperationController(
            self._timings,
            protocol,
            self._state_reducer,
        )
        self._air_quality_queries = AirQualityQueryController()
        self._transactions = TransactionExecutor(
            matcher_factory=protocol.new_response_matcher,
            frame_queue=self._frame_queue,
            disconnected=self._disconnected,
            command_wake=self._command_operations.event,
            secondary_wake=self._air_quality_queries.event,
            disconnect_error=lambda: GattTransportError(
                "Purifier disconnected during transaction"
            ),
        )
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
        self._recovery = RecoveryController(self._timings)
        self._plaintext_rx_count = 0
        self._last_error: str | None = None
        self._incomplete_initialization_requests: tuple[str, ...] = ()
        self._initialization_failure_summaries: dict[str, str] = {}
        self._essential_initialization_batches = 0
        self._essential_initialization_attempts = 0
        self._has_ever_been_ready = False
        self._reported_available: bool | None = None
        self._observation_revision = 0

    @property
    def state(self) -> PurifierState:
        """Return state owned by the synchronous reducer."""
        return self._state_reducer.state

    @state.setter
    def state(self, state: PurifierState) -> None:
        """Replace reducer state for focused tests and coordinator setup."""
        self._state_reducer.replace_state(state)

    @property
    def is_ready(self) -> bool:
        return self.status is ClientStatus.READY

    @property
    def connection_generation(self) -> int:
        """Return the current connection generation for lifecycle consumers."""
        return self._session_generation

    @property
    def _operations(self) -> deque[_Operation]:
        """Compatibility view of pending operations for focused tests."""
        return self._command_operations.pending

    @property
    def _active_operation(self) -> _Operation | None:
        """Compatibility view of the active operation for focused tests."""
        return self._command_operations.active

    @_active_operation.setter
    def _active_operation(self, operation: _Operation | None) -> None:
        self._command_operations.active = operation

    @property
    def _operation_event(self) -> asyncio.Event:
        """Compatibility view of the command wake edge for race tests."""
        return self._command_operations.event

    @property
    def _active_request(self) -> str | None:
        """Compatibility view of the active descriptor for diagnostics/tests."""
        return self._transactions.active_request

    @property
    def _last_timeout_summary(self) -> str | None:
        """Compatibility view of bounded transaction timeout evidence."""
        return self._transactions.last_timeout_summary

    @_last_timeout_summary.setter
    def _last_timeout_summary(self, value: str | None) -> None:
        if value is not None:
            raise ValueError("transaction timeout diagnostics can only be cleared")
        self._transactions.clear_timeout_summary()

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return secret-free runtime evidence for Home Assistant diagnostics."""
        diagnostics = {
            "profile": self._profile.diagnostic_snapshot(
                requested_model=self._profile.model.value
            ),
            "status": self.status.value,
            "is_ready": self.is_ready,
            "has_ever_been_ready": self._has_ever_been_ready,
            "session_generation": self._session_generation,
            "connection_cycles": self._connection_cycles,
            "recovery": self._recovery.snapshot(now=time.monotonic()).as_dict(),
            "plaintext_rx_count": self._plaintext_rx_count,
            "active_request": self._active_request,
            "last_error": self._last_error,
            "last_timeout_summary": self._last_timeout_summary,
            "startup_fan_mode": self._state_reducer.startup_fan_diagnostics(),
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
            "last_refresh_preempted_request": (self._last_refresh_preempted_request),
            "route": self._environment.route_diagnostics(),
            "transport": self._transport.diagnostic_snapshot(),
        }
        diagnostics.update(self._command_operations.snapshot().as_dict())
        return diagnostics

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
        self._command_operations.clear_superseded_fan_echoes()
        self._operation_event.set()
        air_quality_request = self._air_quality_queries.request
        self._air_quality_queries.disconnect()
        active_operation = self._active_operation
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        if air_quality_request is not None:
            await air_quality_request.quiesced.wait()

        channel = self._channel
        self._channel = None
        if channel is not None:
            channel.invalidate()
        await self._transport.async_disconnect()
        await self._transport.async_cleanup_stale_connection(reason="shutdown")
        await self._environment.async_stop()
        self._command_operations.fail_for_shutdown(
            PurifierClientError("Purifier client stopped"),
            active=active_operation,
        )
        self._cancel_recovery_reset()
        self._recovery.reset_after_stable_session()
        self.status = ClientStatus.STOPPED

    async def async_execute(
        self,
        command: ProtocolCommand,
        *,
        origin: CommandOrigin = CommandOrigin.HOME_ASSISTANT,
    ) -> None:
        """Execute one absolute control within a bounded recovery window."""
        if self._runner is None:
            raise PurifierClientError("Purifier client is not running")

        loop = asyncio.get_running_loop()
        now = loop.time()
        operation = self._command_operations.enqueue(
            command,
            loop=loop,
            now=now,
            origin=origin,
        )
        future = operation.future

        try:
            async with asyncio.timeout(self._timings.command_deadline):
                await asyncio.shield(future)
        except TimeoutError as err:
            self._command_operations.cancel(
                operation, generation=self._session_generation
            )
            await asyncio.shield(operation.quiesced.wait())
            raise self._command_operations.failure_error(
                operation,
                reason=(
                    "Command was not confirmed within "
                    f"{self._timings.command_deadline:.0f} seconds"
                ),
                generation=self._session_generation,
            ) from err
        except asyncio.CancelledError:
            self._command_operations.cancel(
                operation, generation=self._session_generation
            )
            await asyncio.shield(operation.quiesced.wait())
            raise
        except CommandSuperseded:
            await asyncio.shield(operation.quiesced.wait())
            raise

    async def async_query_air_quality(self) -> None:
        """Run or join one connection-scoped, one-attempt aa-19 request."""
        if self._runner is None or not self.is_ready:
            raise PurifierClientError("Purifier client is not ready")
        loop = asyncio.get_running_loop()
        current = self._air_quality_queries.request
        if current is not None and current.generation != self._session_generation:
            self._air_quality_queries.disconnect()
        if current is not None and current.future.done():
            await current.quiesced.wait()
        request = self._air_quality_queries.acquire(
            loop=loop,
            now=loop.time(),
            timeout=self._timings.transaction_timeout,
            generation=self._session_generation,
        )
        try:
            async with asyncio.timeout_at(request.deadline):
                await asyncio.shield(request.future)
        except TimeoutError as err:
            if not request.future.done():
                self._air_quality_queries.fail(
                    request,
                    AirQualityQueryTimeout(
                        "Air-quality query exceeded its end-to-end deadline"
                    ),
                )
            raise AirQualityQueryTimeout(
                "Air-quality query exceeded its end-to-end deadline"
            ) from err

    async def async_cancel_air_quality_query(self) -> None:
        """Deactivate one-shot work and wait until channel ownership is released."""
        request = self._air_quality_queries.cancel()
        if request is not None:
            await request.quiesced.wait()

    async def _run(self) -> None:
        self._recovery.begin_sequence()
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
                self._recovery.record_failure(
                    stage=failed_stage.value,
                    cycle=self._connection_cycles,
                    stable_for=stable_for,
                    now=loop.time(),
                )
                recovery_snapshot = self._recovery.snapshot(now=loop.time())
                _LOGGER.debug(
                    "Purifier connection cycle %d failed at status=%s "
                    "session_generation=%d stable_for=%.3fs "
                    "failure_count=%d advertisement_wakes=%d "
                    "recovery_floor=%.1fs: %s",
                    self._connection_cycles,
                    failed_stage.value,
                    self._session_generation,
                    stable_for,
                    recovery_snapshot.failure_count_in_window,
                    recovery_snapshot.advertisement_wake_count_in_window,
                    recovery_snapshot.current_circuit_floor_seconds,
                    self._last_error,
                    exc_info=True,
                )
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
                cleanup_succeeded = (
                    await self._transport.async_cleanup_stale_connection(
                        reason="connection_cycle_end"
                    )
                )
                self._recovery.record_cycle(
                    started_at=cycle_started,
                    finished_at=loop.time(),
                    cleanup_succeeded=cleanup_succeeded,
                )

            if self._stopping.is_set():
                break
            self.status = ClientStatus.BACKOFF
            recent_advertisement = self._environment.has_recent_advertisement(
                self._timings.fresh_advertisement_timeout
            )
            plan = self._recovery.plan_backoff(
                now=loop.time(),
                recent_advertisement=recent_advertisement,
                jitter_factor=random.uniform(0.8, 1.2),
            )
            _LOGGER.debug(
                "Bluetooth recovery backoff policy: requested=%.1fs "
                "effective=%.1fs recent_advertisement=%s recent_limit=%.1fs",
                plan.requested_seconds,
                plan.effective_seconds,
                recent_advertisement,
                self._timings.fresh_advertisement_timeout,
            )
            await self._async_backoff(plan)

    async def _connect_initialize_and_run(self) -> None:
        self._connection_cycles += 1
        self._session_generation += 1
        self._command_operations.clear_superseded_fan_echoes()
        session_generation = self._session_generation
        self._disconnected.clear()
        self._air_quality_queries.disconnect()
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
                    purpose=ObservationPurpose.STARTUP,
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
                    purpose=ObservationPurpose.STARTUP,
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
            operation = self._command_operations.take_next(
                now=asyncio.get_running_loop().time(),
                generation=self._session_generation,
            )
            if operation is not None:
                try:
                    await self._async_execute_operation(operation)
                finally:
                    self._command_operations.release(operation)
                continue

            air_quality_request = self._air_quality_queries.take(
                now=asyncio.get_running_loop().time(),
                generation=self._session_generation,
            )
            if air_quality_request is not None:
                await self._async_execute_air_quality_query(air_quality_request)
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
                    purpose=ObservationPurpose.PERIODIC,
                )
                continue

            await self._async_wait_for_ready_work(
                max(0.0, self._next_poll_due - loop.time())
            )

    async def _async_execute_operation(self, operation: _Operation) -> None:
        loop = asyncio.get_running_loop()
        if not self._command_operations.prepare_for_execution(
            operation,
            now=loop.time(),
            generation=self._session_generation,
        ):
            return

        descriptor = self._protocol.command_request(operation.command)
        confirmation: ReceivedFrame | None = None

        def record_confirmation(received: ReceivedFrame) -> None:
            nonlocal confirmation
            confirmation = received

        while self._command_operations.prepare_for_send(
            operation,
            now=loop.time(),
            generation=self._session_generation,
        ):

            _LOGGER.debug(
                "Command transaction: command=%s attempt=%d/%d remaining=%.3fs "
                "session_generation=%d frame=%s",
                type(operation.command).__name__,
                operation.send_attempts + 1,
                self._command_operations.send_attempt_limit,
                max(0.0, operation.deadline - loop.time()),
                self._session_generation,
                descriptor.frame.hex(" "),
            )

            send_recorded = False

            def record_send() -> None:
                nonlocal send_recorded
                if not send_recorded:
                    send_recorded = True
                    self._command_operations.record_send(
                        operation,
                        generation=self._session_generation,
                        now=loop.time(),
                    )

            try:
                async with asyncio.timeout_at(operation.deadline):
                    transaction = asyncio.create_task(
                        self._async_execute_descriptor(
                            descriptor,
                            attempts=1,
                            on_send=record_send,
                            purpose=ObservationPurpose.COMMAND,
                            operation_id=operation.operation_id,
                            command_origin=operation.origin,
                            on_match=record_confirmation,
                        )
                    )
                    cancelled = asyncio.create_task(operation.cancel_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (transaction, cancelled),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancelled in done and cancelled.result():
                            transaction.cancel()
                            await asyncio.gather(
                                transaction, return_exceptions=True
                            )
                            return
                        await transaction
                    finally:
                        if not transaction.done():
                            transaction.cancel()
                        if not cancelled.done():
                            cancelled.cancel()
                        await asyncio.gather(
                            transaction,
                            cancelled,
                            return_exceptions=True,
                        )
            except TransactionTimeoutError as err:
                self._command_operations.record_response_failure(
                    operation,
                    generation=self._session_generation,
                    error=err,
                )
                if self._command_operations.should_retry_after_response_failure(
                    operation,
                    now=loop.time(),
                ):
                    _LOGGER.debug(
                        "Command response stayed silent; retrying on the same "
                        "session command=%s next_attempt=%d/%d",
                        type(operation.command).__name__,
                        operation.send_attempts + 1,
                        self._command_operations.send_attempt_limit,
                    )
                    continue
                self._command_operations.requeue_for_reconciliation(
                    operation,
                    now=loop.time(),
                    generation=self._session_generation,
                )
                raise
            except TimeoutError as err:
                failure = self._command_operations.fail(
                    operation,
                    reason="Command deadline expired during Bluetooth transaction",
                    generation=self._session_generation,
                )
                raise failure from err
            except Exception:
                self._command_operations.requeue_for_reconciliation(
                    operation,
                    now=loop.time(),
                    generation=self._session_generation,
                )
                raise
            else:
                if operation.future.done():
                    return
                self._apply_confirmed_command(
                    operation.command,
                    confirmation=confirmation,
                    operation=operation,
                )
                self._command_operations.complete(operation)
                return

    async def _async_execute_air_quality_query(
        self, request: _AirQualityRequest
    ) -> None:
        """Execute one one-shot send, interruptible by commands or deactivation."""
        descriptor = self._protocol.command_request(QueryAirQuality())
        transaction = asyncio.create_task(
            self._async_execute_descriptor(
                descriptor,
                attempts=1,
                interrupt_for_command=True,
                purpose=ObservationPurpose.ONE_SHOT,
                request_id=request.request_id,
            )
        )
        cancelled = asyncio.create_task(request.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                (transaction, cancelled),
                timeout=max(
                    0.0,
                    request.deadline - asyncio.get_running_loop().time(),
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done and cancelled.result():
                transaction.cancel()
                await asyncio.gather(transaction, return_exceptions=True)
                return
            if transaction not in done:
                transaction.cancel()
                await asyncio.gather(transaction, return_exceptions=True)
                self._air_quality_queries.fail(
                    request,
                    AirQualityQueryTimeout(
                        "Air-quality query exceeded its end-to-end deadline"
                    ),
                )
                return
            try:
                await transaction
            except RefreshPreemptedError:
                self._air_quality_queries.fail(
                    request,
                    AirQualityQueryPreempted(
                        "Air-quality query yielded to a pending command"
                    ),
                )
            except TransactionTimeoutError as err:
                self._air_quality_queries.fail(
                    request,
                    AirQualityQueryTimeout(str(err)),
                )
            except Exception as err:
                failure = (
                    err
                    if isinstance(err, PurifierClientError)
                    else PurifierClientError(
                        f"Air-quality query failed: {type(err).__name__}: {err}"
                    )
                )
                self._air_quality_queries.fail(request, failure)
            else:
                if request.generation == self._session_generation:
                    self._air_quality_queries.complete(request)
                else:
                    self._air_quality_queries.disconnect()
        finally:
            if not transaction.done():
                transaction.cancel()
                await asyncio.gather(transaction, return_exceptions=True)
            if not cancelled.done():
                cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            if not request.future.done():
                self._air_quality_queries.fail(
                    request,
                    PurifierClientError("Air-quality query execution ended"),
                )
            self._air_quality_queries.release(request)

    def _apply_confirmed_command(
        self,
        command: ProtocolCommand,
        *,
        confirmation: ReceivedFrame | None,
        operation: _Operation,
    ) -> None:
        """Publish state established by a documented command acknowledgement."""
        reduction = self._state_reducer.apply_confirmed_command(
            command,
            generation=self._session_generation,
        )
        if reduction.state_changed:
            self._state_callback(reduction.state)
        if (
            isinstance(command, SetFanMode)
            and confirmation is not None
            and confirmation.generation == self._session_generation
            and operation.operation_id is not None
        ):
            if confirmation.frame.startswith(b"\x3a\x05"):
                self._publish_command_fan_observation(
                    mode=command.mode,
                    received=confirmation,
                    operation_id=operation.operation_id,
                    origin=operation.origin,
                )

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
                if self._has_pending_command() or self._has_pending_air_quality_query():
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
                        interrupt_for_query=True,
                        purpose=ObservationPurpose.REFRESH,
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
        return self._command_operations.has_pending(
            now=asyncio.get_running_loop().time()
        )

    def _has_pending_air_quality_query(self) -> bool:
        """Return whether current connection-scoped one-shot work is waiting."""
        request = self._air_quality_queries.request
        return (
            request is not None
            and not request.future.done()
            and request.generation == self._session_generation
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
        interrupt_for_query: bool = False,
        on_send: Callable[[], None] | None = None,
        purpose: ObservationPurpose = ObservationPurpose.UNSOLICITED,
        request_id: int | None = None,
        operation_id: int | None = None,
        command_origin: CommandOrigin | None = None,
        on_match: Callable[[ReceivedFrame], None] | None = None,
    ) -> tuple[bytes, ...]:
        channel = self._channel
        if channel is None or not channel.ready:
            raise ChannelError("Application channel is not ready")

        loop = asyncio.get_running_loop()

        def record_attempt() -> None:
            if (
                self.status is ClientStatus.INITIALIZING
                and descriptor.name == self._profile.protocol.essential_request
            ):
                self._essential_initialization_attempts += 1

        def record_sent() -> None:
            if is_periodic_poll:
                # This is deliberately write-to-write, not response-to-write.
                self._next_poll_due = loop.time() + self._timings.poll_interval

        generation = self._session_generation

        async def next_current_frame(
            deadline: float,
            *,
            interrupt_for_command: bool = False,
            interrupt_for_secondary: bool = False,
        ) -> ReceivedFrame:
            while True:
                received = await self._transactions.async_next_frame(
                    deadline,
                    interrupt_for_command=interrupt_for_command,
                    interrupt_for_secondary=interrupt_for_secondary,
                )
                if received.generation == generation:
                    if self._command_operations.consume_superseded_fan_echo(
                        generation=generation,
                        frame=received.frame,
                    ):
                        _LOGGER.debug(
                            "Ignoring exact fan acknowledgement from physically "
                            "superseded work: generation=%d frame=%s",
                            generation,
                            received.frame.hex(" "),
                        )
                        continue
                    return received
                _LOGGER.debug(
                    "Ignoring queued frame from stale session: "
                    "frame_generation=%d request_generation=%d frame=%s",
                    received.generation,
                    generation,
                    received.frame.hex(" "),
                )

        def reduce_frame(
            received: ReceivedFrame, *, matched_request: str | None = None
        ) -> None:
            self._process_plaintext_frame(
                received,
                matched_request=matched_request,
                purpose=purpose,
                request_id=request_id,
                operation_id=operation_id,
                command_origin=command_origin,
            )

        return await self._transactions.async_execute(
            descriptor,
            attempts=attempts,
            timeout=self._timings.transaction_timeout,
            send=channel.async_send,
            reduce_frame=reduce_frame,
            next_frame=next_current_frame,
            interrupt_for_command=interrupt_for_command,
            interrupt_for_secondary=interrupt_for_query,
            on_attempt=record_attempt,
            on_send=on_send,
            on_sent=record_sent,
            on_match=on_match,
        )

    async def _async_next_transaction_frame(
        self,
        deadline: float,
        *,
        interrupt_for_command: bool = False,
        interrupt_for_secondary: bool = False,
    ) -> ReceivedFrame:
        return await self._transactions.async_next_frame(
            deadline,
            interrupt_for_command=interrupt_for_command,
            interrupt_for_secondary=interrupt_for_secondary,
        )

    async def _async_wait_for_ready_work(self, timeout: float) -> None:
        frame_task = asyncio.create_task(self._frame_queue.get())
        operation_task = asyncio.create_task(self._operation_event.wait())
        query_task = asyncio.create_task(self._air_quality_queries.event.wait())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        tasks = (frame_task, operation_task, query_task, disconnect_task)
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
        received: ReceivedFrame | bytes,
        *,
        matched_request: str | None = None,
        generation: int | None = None,
        purpose: ObservationPurpose = ObservationPurpose.UNSOLICITED,
        request_id: int | None = None,
        operation_id: int | None = None,
        command_origin: CommandOrigin | None = None,
    ) -> None:
        if isinstance(received, ReceivedFrame):
            frame = received.frame
            generation = received.generation
            observed_at = received.received_at
        else:
            frame = received
            if generation is None:
                generation = self._session_generation
            observed_at = time.monotonic()
        if generation != self._session_generation or self._stopping.is_set():
            return
        if self._command_operations.consume_superseded_fan_echo(
            generation=generation,
            frame=frame,
        ):
            _LOGGER.debug(
                "Ignoring exact fan acknowledgement from physically superseded "
                "work: generation=%d frame=%s",
                generation,
                frame.hex(" "),
            )
            return
        event = self._protocol.decode(frame)
        if (
            isinstance(event, FanModeEvent)
            and event.mode is not None
            and frame.startswith(b"\xee\x05")
            and matched_request == "SetFanMode"
        ):
            self._command_operations.discard_active_fan_echo_reservation(
                generation=generation
            )
        if (
            isinstance(event, FanModeEvent)
            and event.mode is not None
            and frame.startswith(b"\xee\x05")
            and matched_request is None
        ):
            self._command_operations.supersede_active_fan_by_physical(
                generation=generation
            )
        previous_state = self.state
        reduction = self._state_reducer.reduce_event(
            event,
            generation=generation,
            matched_request=matched_request,
        )
        if reduction.refresh_requested and not self._refresh_running:
            self._refresh_pending = True
        if reduction.state_changed:
            self._state_callback(reduction.state)
        self._publish_semantic_observation(
            event,
            previous_state=previous_state,
            matched_request=matched_request,
            generation=generation,
            purpose=purpose,
            request_id=request_id,
            operation_id=operation_id,
            command_origin=command_origin,
            observed_at=observed_at,
        )

    def _publish_semantic_observation(
        self,
        event: object,
        *,
        previous_state: PurifierState,
        matched_request: str | None,
        generation: int,
        purpose: ObservationPurpose,
        request_id: int | None,
        operation_id: int | None,
        command_origin: CommandOrigin | None,
        observed_at: float,
    ) -> None:
        """Publish semantic authority after matcher correlation and reduction."""
        callback = self._observation_callback
        if callback is None or generation != self._session_generation:
            return
        source: ObservationSource
        observation: PurifierObservation
        self._observation_revision += 1
        revision = self._observation_revision

        if isinstance(event, AirQualityEvent):
            source = (
                ObservationSource.DEVICE
                if event.unsolicited
                else ObservationSource.QUERY
            )
            correlated = (
                matched_request in {"QueryAirQuality", "air_quality"}
                and not event.unsolicited
            )
            observation = AirQualityObservation(
                revision=revision,
                generation=generation,
                observed_at=observed_at,
                source=source,
                purpose=(
                    ObservationPurpose.UNSOLICITED
                    if event.unsolicited
                    else purpose
                ),
                request_id=request_id if correlated else None,
                operation_id=operation_id if correlated else None,
                command_origin=command_origin if correlated else None,
                pm25=event.pm25_ug_m3,
                filter_life=event.filter_life,
            )
        else:
            mode = self.state.fan_mode
            if isinstance(event, FanModeEvent) and mode is not None:
                matching_command = (
                    matched_request == "SetFanMode"
                    and operation_id is not None
                    and command_origin is not None
                    and isinstance(self._active_operation, _Operation)
                    and isinstance(self._active_operation.command, SetFanMode)
                    and self._active_operation.command.mode is mode
                )
                source = (
                    ObservationSource.COMMAND
                    if matching_command
                    else ObservationSource.PHYSICAL
                )
                if source is ObservationSource.PHYSICAL:
                    operation_id = None
                    command_origin = None
                observation = FanModeObservation(
                    revision=revision,
                    generation=generation,
                    observed_at=observed_at,
                    source=source,
                    purpose=(
                        ObservationPurpose.COMMAND
                        if source is ObservationSource.COMMAND
                        else ObservationPurpose.UNSOLICITED
                    ),
                    request_id=None,
                    operation_id=operation_id,
                    command_origin=command_origin,
                    mode=mode,
                )
            elif (
                isinstance(event, StartupFanModeEvent)
                and matched_request == f"mode_data_{event.selector:02x}"
                and mode is not None
                and (
                    (
                        self._profile.protocol.startup_mode_strategy
                        == "h7124_selector_00"
                        and event.selector == 0x00
                    )
                    or (
                        self._profile.protocol.startup_mode_strategy
                        == "h7129_selector_pair"
                        and (
                            (event.selector == 0x00 and event.mode_code != 0x01)
                            or (
                                event.selector == 0x01
                                and previous_state.fan_mode is None
                            )
                        )
                    )
                )
            ):
                observation = FanModeObservation(
                    revision=revision,
                    generation=generation,
                    observed_at=observed_at,
                    source=ObservationSource.STARTUP,
                    purpose=purpose,
                    request_id=request_id,
                    operation_id=None,
                    command_origin=None,
                    mode=mode,
                )
            else:
                self._observation_revision -= 1
                return
        callback(observation)

    def _publish_command_fan_observation(
        self,
        *,
        mode: FanMode,
        received: ReceivedFrame,
        operation_id: int,
        origin: CommandOrigin,
    ) -> None:
        """Publish exact 3a-05 command confirmation after state application."""
        callback = self._observation_callback
        if (
            callback is None
            or received.generation != self._session_generation
            or self._stopping.is_set()
        ):
            return
        self._observation_revision += 1
        callback(
            FanModeObservation(
                revision=self._observation_revision,
                generation=received.generation,
                observed_at=received.received_at,
                source=ObservationSource.COMMAND,
                purpose=ObservationPurpose.COMMAND,
                operation_id=operation_id,
                command_origin=origin,
                mode=mode,
            )
        )

    def _command_is_satisfied(self, command: ProtocolCommand) -> bool:
        return self._command_operations.command_is_satisfied(command)

    def _invalidate_connection_scoped_state(self) -> None:
        """Forget values that must be re-established after reconnect."""
        self._state_reducer.invalidate_connection()

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
        if frame.startswith(b"\xee\x05"):
            try:
                event = self._protocol.decode(frame)
            except Exception:  # noqa: BLE001 - normal processing retains evidence
                pass
            else:
                if isinstance(event, FanModeEvent) and event.mode is not None:
                    # Establish old-echo ownership at receipt time so a physical
                    # frame and its following stale echo cannot both queue first.
                    self._command_operations.reserve_active_fan_echo_by_physical(
                        generation=generation
                    )
        if self._command_operations.consume_superseded_fan_echo(
            generation=generation,
            frame=frame,
        ):
            _LOGGER.debug(
                "Ignoring exact fan acknowledgement received before its "
                "forced replacement send: generation=%d frame=%s",
                generation,
                frame.hex(" "),
            )
            return
        received_at = asyncio.get_running_loop().time()
        fan_detail = self._command_operations.record_fan_frame(
            generation=generation,
            frame=frame,
            phase=self._active_request or self.status.value,
            now=received_at,
        )
        if fan_detail is not None:
            _LOGGER.debug("Fan command diagnostic notification: %s", fan_detail)
        self._plaintext_rx_count += 1
        _LOGGER.debug(
            "RX plaintext queued: session_generation=%d count=%d active_request=%s "
            "frame=%s",
            generation,
            self._plaintext_rx_count,
            self._active_request,
            frame.hex(" "),
        )
        self._frame_queue.put_nowait(
            ReceivedFrame(bytes(frame), generation, received_at)
        )

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
        self._command_operations.clear_superseded_fan_echoes()
        self._air_quality_queries.disconnect()
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
            self._recovery.reset_after_stable_session()
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

    def _drain_frame_queue(self) -> None:
        while True:
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _async_backoff(self, plan: BackoffDecision) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        advertisement_cutoff = time.monotonic()
        _LOGGER.debug(
            "Bluetooth recovery backoff: base=%.1fs jittered=%.3fs "
            "circuit_floor=%.1fs planned=%.3fs next_max=%.1fs",
            plan.effective_seconds,
            plan.jittered_seconds,
            plan.floor_seconds,
            plan.planned_seconds,
            self._timings.backoff_max,
        )
        stop_task = asyncio.create_task(self._stopping.wait())
        operation_task = asyncio.create_task(self._operation_event.wait())
        advertisement_task = asyncio.create_task(
            self._environment.async_wait_for_advertisement_after(advertisement_cutoff)
        )
        tasks = (stop_task, operation_task, advertisement_task)
        try:
            done, _ = await asyncio.wait(
                tasks,
                timeout=plan.planned_seconds,
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

            minimum_wait = plan.floor_seconds
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
                    if plan.floor_seconds > 0
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
            finished = loop.time()
            self._recovery.complete_backoff(
                started_at=started,
                now=finished,
                wake_reason=reason,
                advertisement_triggered=(
                    advertisement_triggered and reason != "shutdown"
                ),
            )
            recovery_snapshot = self._recovery.snapshot(now=finished)
            _LOGGER.debug(
                "Bluetooth recovery backoff ended: reason=%s elapsed=%.3fs "
                "circuit_floor=%.1fs failure_count=%d advertisement_wakes=%d",
                reason,
                max(0.0, finished - started),
                plan.floor_seconds,
                recovery_snapshot.failure_count_in_window,
                recovery_snapshot.advertisement_wake_count_in_window,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
