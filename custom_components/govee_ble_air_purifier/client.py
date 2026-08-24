"""Reliable, serialized purifier client for unreliable BLE links."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
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
    FanModeEvent,
    Model,
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
)
from .protocol import GoveePurifierProtocol, MatchResult, RequestDescriptor

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 3.0
H7124_INITIAL_POLL_DELAY = 1.936
TRANSACTION_TIMEOUT = 3.0
INITIALIZATION_ATTEMPTS = 2
COMMAND_DEADLINE = 30.0
COMMAND_SEND_ATTEMPTS = 3
STARTUP_TIMEOUT = 60.0
FRESH_ADVERTISEMENT_TIMEOUT = 10.0
BETWEEN_REQUEST_DELAY = 0.001
BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
BACKOFF_RESET_AFTER = 30.0

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


@dataclass(slots=True)
class _Operation:
    command: ProtocolCommand
    future: asyncio.Future[None]
    deadline: float
    send_attempts: int = 0


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
        self._next_poll_due = 0.0
        self._ready_since: float | None = None
        self._connection_cycles = 0
        self._plaintext_rx_count = 0
        self._active_request: str | None = None
        self._last_error: str | None = None
        self._last_timeout_summary: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status is ClientStatus.READY

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return secret-free runtime evidence for Home Assistant diagnostics."""
        return {
            "status": self.status.value,
            "is_ready": self.is_ready,
            "session_generation": self._session_generation,
            "connection_cycles": self._connection_cycles,
            "plaintext_rx_count": self._plaintext_rx_count,
            "active_request": self._active_request,
            "last_error": self._last_error,
            "last_timeout_summary": self._last_timeout_summary,
            "route": self._environment.route_diagnostics(),
            "transport": self._transport.diagnostic_snapshot(),
        }

    async def async_start(self) -> None:
        """Start recovery and wait for the first complete initialization."""
        if self._runner is not None:
            return

        self._stopping.clear()
        self._first_ready = asyncio.get_running_loop().create_future()
        await self._environment.async_start()
        self._runner = asyncio.create_task(
            self._run(), name=f"govee-purifier-{self._environment.address}"
        )
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await asyncio.shield(self._first_ready)
        except asyncio.CancelledError:
            await self.async_shutdown()
            raise
        except Exception as err:
            # Cancel only after a bounded setup window. Home Assistant may then
            # retry the config entry instead of leaving a hidden task behind.
            startup_error = BluetoothUnavailableError(
                f"Purifier {self._environment.address} did not become ready; "
                f"status={self.status.value}; "
                f"last_cycle_error={self._last_error}; "
                f"cause={exception_detail(err)}; "
                f"route={self._environment.route_diagnostics()}; "
                f"reachability={self._environment.reachability_diagnostics()}; "
                f"transport={self._transport.diagnostic_snapshot()}"
            )
            await self.async_shutdown()
            self._set_available(False, startup_error)
            raise startup_error from err

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
        self.status = ClientStatus.STOPPED

    async def async_execute(self, command: ProtocolCommand) -> None:
        """Execute one absolute control within a bounded recovery window."""
        if self._runner is None:
            raise PurifierClientError("Purifier client is not running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        operation = _Operation(command, future, loop.time() + COMMAND_DEADLINE)
        self._coalesce_pending(operation)
        self._operations.append(operation)
        self._operation_event.set()

        try:
            async with asyncio.timeout(COMMAND_DEADLINE):
                await asyncio.shield(future)
        except TimeoutError as err:
            if not future.done():
                future.cancel()
            with suppress(ValueError):
                self._operations.remove(operation)
            raise CommandDeadlineExceeded(
                f"Command was not confirmed within {COMMAND_DEADLINE:.0f} seconds"
            ) from err
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            with suppress(ValueError):
                self._operations.remove(operation)
            raise

    async def _run(self) -> None:
        backoff = BACKOFF_MIN
        while not self._stopping.is_set():
            try:
                await self._connect_initialize_and_run()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._last_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug(
                    "Purifier connection cycle %d failed at status=%s "
                    "session_generation=%d: %s",
                    self._connection_cycles,
                    self.status.value,
                    self._session_generation,
                    self._last_error,
                    exc_info=True,
                )
                stable_for = (
                    asyncio.get_running_loop().time() - self._ready_since
                    if self._ready_since is not None
                    else 0.0
                )
                if stable_for >= BACKOFF_RESET_AFTER:
                    backoff = BACKOFF_MIN
                self._set_available(False, err)
            finally:
                self._ready_since = None
                channel = self._channel
                self._channel = None
                if channel is not None:
                    channel.invalidate()
                await self._transport.async_disconnect()
                await self._transport.async_cleanup_stale_connection(
                    reason="connection_cycle_end"
                )

            if self._stopping.is_set():
                break
            self.status = ClientStatus.BACKOFF
            await self._async_backoff(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)

    async def _connect_initialize_and_run(self) -> None:
        self._connection_cycles += 1
        self._session_generation += 1
        session_generation = self._session_generation
        self._disconnected.clear()
        self._drain_frame_queue()
        self._refresh_pending = False
        self._refresh_running = False
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
            FRESH_ADVERTISEMENT_TIMEOUT
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
            channel: SecureChannel = H7129SessionChannel(self._transport, callback)
        else:
            self.status = ClientStatus.SUBSCRIBING
            channel = PlaintextChannel(self._transport, callback)
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
            INITIALIZATION_ATTEMPTS,
        )
        await self._async_run_sweep(
            initialization_requests,
            attempts=INITIALIZATION_ATTEMPTS,
        )

        self.status = ClientStatus.READY
        loop = asyncio.get_running_loop()
        self._ready_since = loop.time()
        initial_poll_delay = (
            H7124_INITIAL_POLL_DELAY
            if self._profile.model is Model.H7124
            else POLL_INTERVAL
        )
        self._next_poll_due = loop.time() + initial_poll_delay
        self._last_error = None
        self._last_timeout_summary = None
        _LOGGER.debug(
            "Purifier ready: cycle=%d session_generation=%d initial_poll_delay=%.3fs "
            "state=%s",
            self._connection_cycles,
            session_generation,
            initial_poll_delay,
            self.state,
        )
        self._set_available(True, None)
        if self._first_ready is not None and not self._first_ready.done():
            self._first_ready.set_result(None)
        await self._async_ready_loop()

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
                await asyncio.sleep(BETWEEN_REQUEST_DELAY)
                await self._async_run_sweep(
                    self._protocol.refresh_requests(),
                    attempts=1,
                    is_refresh=True,
                )
                continue

            loop = asyncio.get_running_loop()
            if loop.time() >= self._next_poll_due:
                await self._async_execute_descriptor(
                    self._protocol.device_state_poll(),
                    attempts=1,
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
            operation.future.set_exception(CommandDeadlineExceeded())
            return

        # Initialization after every reconnect re-queries the relevant state.
        # If an ambiguous previous write was actually applied, do not replay it.
        if self._command_is_satisfied(operation.command):
            operation.future.set_result(None)
            return

        operation.send_attempts += 1
        descriptor = self._protocol.command_request(operation.command)
        try:
            await self._async_execute_descriptor(descriptor, attempts=1)
        except Exception:
            if (
                not operation.future.done()
                and loop.time() < operation.deadline
                and operation.send_attempts < COMMAND_SEND_ATTEMPTS
            ):
                self._operations.appendleft(operation)
                self._operation_event.set()
            elif not operation.future.done():
                operation.future.set_exception(
                    CommandDeadlineExceeded(
                        "Command remained unconfirmed after bounded retries"
                    )
                )
            raise
        else:
            if not operation.future.done():
                operation.future.set_result(None)

    async def _async_run_sweep(
        self,
        descriptors: tuple[RequestDescriptor, ...],
        *,
        attempts: int,
        is_refresh: bool = False,
    ) -> None:
        if is_refresh:
            self._refresh_running = True
            self._refresh_pending = False
        try:
            for index, descriptor in enumerate(descriptors):
                _LOGGER.debug(
                    "Sweep request %d/%d: name=%s",
                    index + 1,
                    len(descriptors),
                    descriptor.name,
                )
                await self._async_execute_descriptor(descriptor, attempts=attempts)
                if index + 1 < len(descriptors):
                    await asyncio.sleep(BETWEEN_REQUEST_DELAY)
        finally:
            if is_refresh:
                self._refresh_running = False

    async def _async_execute_descriptor(
        self,
        descriptor: RequestDescriptor,
        *,
        attempts: int,
        is_periodic_poll: bool = False,
    ) -> tuple[bytes, ...]:
        channel = self._channel
        if channel is None or not channel.ready:
            raise ChannelError("Application channel is not ready")

        loop = asyncio.get_running_loop()
        attempt_summaries: list[str] = []
        self._active_request = descriptor.name
        try:
            for attempt in range(attempts):
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
                await channel.async_send(descriptor.frame)
                if is_periodic_poll:
                    # This is deliberately write-to-write, not response-to-write.
                    self._next_poll_due = loop.time() + POLL_INTERVAL
                deadline = loop.time() + TRANSACTION_TIMEOUT

                try:
                    while not matcher.complete:
                        frame = await self._async_next_transaction_frame(deadline)
                        observed_frames.append(frame)
                        self._process_plaintext_frame(frame)
                        result = matcher.feed(frame)
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
                f"{attempts} attempt(s) with {TRANSACTION_TIMEOUT:.1f}s deadlines; "
                f"{details}"
            )
        finally:
            self._active_request = None

    async def _async_next_transaction_frame(self, deadline: float) -> bytes:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError

        frame_task = asyncio.create_task(self._frame_queue.get())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        tasks = (frame_task, disconnect_task)
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
        return frame_task.result()

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

    def _process_plaintext_frame(self, frame: bytes) -> None:
        event = self._protocol.decode(frame)
        new_state = self.state
        if isinstance(event, DeviceStateEvent) and event.power is not None:
            new_state = replace(new_state, power=event.power)
        elif isinstance(event, FanModeEvent) and event.mode is not None:
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
            if (
                self._profile.security is SecurityMode.H7129_SESSION
                and not self._refresh_running
            ):
                self._refresh_pending = True

        if new_state != self.state:
            self.state = new_state
            self._state_callback(new_state)

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

    def _invalidate_connection_scoped_state(self) -> None:
        """Forget values startup cannot authoritatively query after reconnect."""
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
        self._disconnected.set()

    def _set_available(self, available: bool, error: Exception | None) -> None:
        _LOGGER.debug(
            "Purifier availability update: available=%s status=%s error=%s",
            available,
            self.status.value,
            f"{type(error).__name__}: {error}" if error is not None else None,
        )
        self._availability_callback(available, error)

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

    def _discard_expired_operations(self) -> None:
        now = asyncio.get_running_loop().time()
        retained: deque[_Operation] = deque()
        while self._operations:
            operation = self._operations.popleft()
            if operation.future.done():
                continue
            if now >= operation.deadline:
                operation.future.set_exception(CommandDeadlineExceeded())
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
        jittered_delay = delay * random.uniform(0.8, 1.2)
        _LOGGER.debug(
            "Bluetooth recovery backoff: base=%.1fs jittered=%.3fs next_max=%.1fs",
            delay,
            jittered_delay,
            BACKOFF_MAX,
        )
        try:
            async with asyncio.timeout(jittered_delay):
                await self._stopping.wait()
        except TimeoutError:
            pass
