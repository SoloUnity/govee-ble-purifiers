"""Deterministic command-operation lifecycle and diagnostic evidence."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field

from .models import FanModeEvent, ProtocolCommand, SetFanMode
from .profiles import TimingProfile
from .protocol import GoveePurifierProtocol
from .state_reducer import PurifierStateReducer


class PurifierClientError(ConnectionError):
    """Base class for reliable-client errors."""


class CommandDeadlineExceeded(PurifierClientError):
    """Raised when a control could not be confirmed within its bounded window."""


class CommandSuperseded(PurifierClientError):
    """Raised when a newer pending control replaces an older one."""


@dataclass(slots=True)
class _Operation:
    """One absolute command and its bounded cross-reconnect evidence."""

    command: ProtocolCommand
    future: asyncio.Future[None]
    deadline: float
    send_attempts: int = 0
    created_at: float = 0.0
    send_diagnostics: list[str] = field(default_factory=list)
    response_failures: list[str] = field(default_factory=list)
    observed_fan_frames: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    """Immutable command diagnostics exposed by the reliable client."""

    last_command_diagnostics: str | None
    pending_command_count: int
    active_command: str | None
    active_command_send_attempts: int | None

    def as_dict(self) -> dict[str, object]:
        """Return the existing top-level command diagnostic fields."""
        return {
            "last_command_diagnostics": self.last_command_diagnostics,
            "pending_command_count": self.pending_command_count,
            "active_command": self.active_command,
            "active_command_send_attempts": (
                self.active_command_send_attempts
            ),
        }


class CommandOperationController:
    """Own command queue policy and evidence without asynchronous I/O."""

    def __init__(
        self,
        timings: TimingProfile,
        protocol: GoveePurifierProtocol,
        state_reducer: PurifierStateReducer,
    ) -> None:
        self.timings = timings
        self._protocol = protocol
        self._state_reducer = state_reducer
        self.pending: deque[_Operation] = deque()
        self.active: _Operation | None = None
        self.event = asyncio.Event()
        self._last_command_diagnostics: str | None = None

    @property
    def last_command_diagnostics(self) -> str | None:
        """Return the last final bounded command error, if any."""
        return self._last_command_diagnostics

    @property
    def send_attempt_limit(self) -> int:
        """Return the immutable bounded application-send budget."""
        return self.timings.command_send_attempts

    def enqueue(
        self,
        command: ProtocolCommand,
        *,
        loop: asyncio.AbstractEventLoop,
        now: float,
    ) -> _Operation:
        """Create and queue one command after superseding matching pending work."""
        operation = _Operation(
            command,
            loop.create_future(),
            now + self.timings.command_deadline,
            created_at=now,
        )
        self._coalesce_pending(operation)
        self.pending.append(operation)
        self.event.set()
        return operation

    def take_next(self, *, now: float, generation: int) -> _Operation | None:
        """Expire stale work and activate the next queued operation."""
        self.discard_expired(now=now, generation=generation)
        if self.active is not None or not self.pending:
            return None
        operation = self.pending.popleft()
        self.active = operation
        return operation

    def release(self, operation: _Operation) -> None:
        """Release an operation after the client finishes its async execution."""
        if self.active is operation:
            self.active = None

    def has_pending(self, *, now: float) -> bool:
        """Return whether an unexpired user command is waiting for the owner."""
        return any(
            not operation.future.done() and now < operation.deadline
            for operation in self.pending
        )

    def command_is_satisfied(self, command: ProtocolCommand) -> bool:
        """Delegate authoritative satisfaction checks to the state reducer."""
        return self._state_reducer.command_is_satisfied(command)

    def reconcile(self, operation: _Operation) -> bool:
        """Complete an ambiguous operation when refreshed state confirms it."""
        if not self.command_is_satisfied(operation.command):
            return False
        if not operation.future.done():
            operation.future.set_result(None)
        return True

    def prepare_for_execution(
        self,
        operation: _Operation,
        *,
        now: float,
        generation: int,
    ) -> bool:
        """Apply pre-execution completion, expiry, and budget policy."""
        if operation.future.done():
            return False
        if now >= operation.deadline:
            self.fail(
                operation,
                reason="Command deadline expired before execution",
                generation=generation,
            )
            return False
        if self.reconcile(operation):
            return False
        if operation.send_attempts >= self.send_attempt_limit:
            self.fail(
                operation,
                reason="Command remained unconfirmed after bounded retries",
                generation=generation,
            )
            return False
        return True

    def prepare_for_send(
        self,
        operation: _Operation,
        *,
        now: float,
        generation: int,
    ) -> bool:
        """Apply cancellation, deadline, and send-budget policy before a retry."""
        if (
            operation.future.done()
            or operation.send_attempts >= self.send_attempt_limit
        ):
            return False
        if now >= operation.deadline:
            self.fail(
                operation,
                reason="Command deadline expired before retry",
                generation=generation,
            )
            return False
        return True

    def should_retry_after_response_failure(
        self,
        operation: _Operation,
        *,
        now: float,
    ) -> bool:
        """Return whether response silence may consume another same-session send."""
        return (
            operation.send_attempts < self.send_attempt_limit
            and not operation.future.done()
            and now < operation.deadline
        )

    def complete(self, operation: _Operation) -> None:
        """Complete a confirmed command exactly once and clear old failure evidence."""
        if not operation.future.done():
            operation.future.set_result(None)
        self._last_command_diagnostics = None

    def record_send(
        self,
        operation: _Operation,
        *,
        generation: int,
        now: float,
    ) -> None:
        """Spend one send attempt and append its bounded timeline evidence."""
        operation.send_attempts += 1
        elapsed = now - operation.created_at if operation.created_at > 0 else 0.0
        operation.send_diagnostics.append(
            f"attempt={operation.send_attempts},generation={generation},"
            f"elapsed={elapsed:.3f}s"
        )

    def record_response_failure(
        self,
        operation: _Operation,
        *,
        generation: int,
        error: Exception,
    ) -> None:
        """Retain response evidence from one bounded send attempt."""
        operation.response_failures.append(f"generation={generation}: {error}")

    def requeue_for_reconciliation(
        self,
        operation: _Operation,
        *,
        now: float,
        generation: int,
    ) -> None:
        """Preserve an ambiguous command for startup-state checks after reconnect."""
        if operation.future.done():
            return
        if now >= operation.deadline:
            self.fail(
                operation,
                reason="Command deadline expired during recovery",
                generation=generation,
            )
            return
        self.pending.appendleft(operation)
        self.event.set()

    def discard_expired(self, *, now: float, generation: int) -> None:
        """Fail expired queued commands while retaining their relative order."""
        retained: deque[_Operation] = deque()
        while self.pending:
            operation = self.pending.popleft()
            if operation.future.done():
                continue
            if now >= operation.deadline:
                self.fail(
                    operation,
                    reason="Command deadline expired while queued",
                    generation=generation,
                )
            else:
                retained.append(operation)
        self.pending.extend(retained)
        if not self.pending:
            self.event.clear()

    def cancel(self, operation: _Operation) -> None:
        """Cancel a caller-abandoned operation and remove it if still queued."""
        if not operation.future.done():
            operation.future.cancel()
        with suppress(ValueError):
            self.pending.remove(operation)

    def fail(
        self,
        operation: _Operation,
        *,
        reason: str,
        generation: int,
    ) -> CommandDeadlineExceeded:
        """Create, retain, and publish one final bounded command failure."""
        error = self.failure_error(
            operation,
            reason=reason,
            generation=generation,
        )
        if not operation.future.done():
            operation.future.set_exception(error)
        return error

    def failure_error(
        self,
        operation: _Operation,
        *,
        reason: str,
        generation: int,
    ) -> CommandDeadlineExceeded:
        """Create and retain a final command error without changing its future."""
        summary = self._diagnostic_summary(
            operation,
            reason=reason,
            generation=generation,
        )
        self._last_command_diagnostics = summary
        return CommandDeadlineExceeded(summary)

    def fail_for_shutdown(
        self,
        error: Exception,
        *,
        active: _Operation | None = None,
    ) -> None:
        """Fail captured active and currently queued commands during shutdown."""
        current = active if active is not None else self.active
        if current is not None and not current.future.done():
            current.future.set_exception(error)
        while self.pending:
            operation = self.pending.popleft()
            if not operation.future.done():
                operation.future.set_exception(error)

    def record_fan_frame(
        self,
        *,
        generation: int,
        frame: bytes,
        phase: str,
        now: float,
    ) -> str | None:
        """Correlate fan acknowledgements and physical updates with a command."""
        if not frame.startswith((b"\x3a\x05", b"\xee\x05")):
            return None
        operation = self._pending_fan_operation()
        if operation is None:
            return None

        mode: str | None = None
        if frame.startswith(b"\x3a\x05"):
            role = "command_echo"
            matches_request = (
                frame == self._protocol.command_request(operation.command).frame
            )
            if matches_request:
                mode = operation.command.mode.value
        else:
            role = "physical_update"
            matches_request = False
            try:
                event = self._protocol.decode(frame)
            except Exception as err:  # noqa: BLE001 - retain malformed evidence
                mode = f"decode_error:{type(err).__name__}"
            else:
                if isinstance(event, FanModeEvent) and event.mode is not None:
                    mode = event.mode.value

        elapsed = now - operation.created_at if operation.created_at > 0 else 0.0
        detail = (
            f"generation={generation},elapsed={elapsed:.3f}s,phase={phase},"
            f"role={role},matches_request={matches_request},"
            f"decoded_mode={mode},frame={frame.hex(' ')}"
        )
        operation.observed_fan_frames.append(detail)
        return detail

    def snapshot(self) -> OperationSnapshot:
        """Return the existing command diagnostics at the current instant."""
        active = self.active
        return OperationSnapshot(
            last_command_diagnostics=self._last_command_diagnostics,
            pending_command_count=sum(
                not operation.future.done() for operation in self.pending
            ),
            active_command=(
                type(active.command).__name__ if active is not None else None
            ),
            active_command_send_attempts=(
                active.send_attempts if active is not None else None
            ),
        )

    def _coalesce_pending(self, replacement: _Operation) -> None:
        key = type(replacement.command)
        retained: deque[_Operation] = deque()
        while self.pending:
            pending = self.pending.popleft()
            if type(pending.command) is key and not pending.future.done():
                pending.future.set_exception(
                    CommandSuperseded("A newer control superseded this request")
                )
            else:
                retained.append(pending)
        self.pending.extend(retained)

    def _pending_fan_operation(self) -> _Operation | None:
        active = self.active
        if (
            active is not None
            and isinstance(active.command, SetFanMode)
            and not active.future.done()
        ):
            return active
        return next(
            (
                operation
                for operation in self.pending
                if isinstance(operation.command, SetFanMode)
                and not operation.future.done()
            ),
            None,
        )

    def _diagnostic_summary(
        self,
        operation: _Operation,
        *,
        reason: str,
        generation: int,
    ) -> str:
        descriptor = self._protocol.command_request(operation.command)
        command_detail = type(operation.command).__name__
        if isinstance(operation.command, SetFanMode):
            command_detail += f"(mode={operation.command.mode.value})"
        sends = " | ".join(
            operation.send_diagnostics[-self.timings.command_send_attempts :]
        )
        failures = " | ".join(
            operation.response_failures[-self.timings.command_send_attempts :]
        )
        fan_frames = " | ".join(operation.observed_fan_frames[-8:])
        cached_fan_mode = (
            self._state_reducer.state.fan_mode.value
            if self._state_reducer.state.fan_mode is not None
            else None
        )
        return (
            f"{reason}; command={command_detail}; "
            f"frame={descriptor.frame.hex(' ')}; "
            "sends="
            f"{operation.send_attempts}/{self.timings.command_send_attempts}; "
            f"send_timeline={sends or 'none'}; "
            f"response_failures={failures or 'none'}; "
            f"observed_fan_frames={fan_frames or 'none'}; "
            f"current_generation={generation}; "
            f"cached_fan_mode={cached_fan_mode}"
        )
