"""One-in-flight request execution over an established application channel."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from .operations import PurifierClientError
from .protocol import MatchResult, RequestDescriptor, ResponseMatcher

_LOGGER = logging.getLogger(__name__)

__all__ = (
    "RefreshPreemptedError",
    "TransactionExecutor",
    "TransactionTimeoutError",
)

MatcherFactory = Callable[[RequestDescriptor], ResponseMatcher]
SendFrame = Callable[[bytes], Awaitable[None]]
AttemptHook = Callable[[], None]
DisconnectErrorFactory = Callable[[], Exception]


class FrameReducer(Protocol):
    """Decode and reduce one received application frame."""

    def __call__(
        self,
        frame: bytes,
        *,
        matched_request: str | None = None,
    ) -> None: ...


class NextFrame(Protocol):
    """Wait for the next transaction candidate or control edge."""

    async def __call__(
        self,
        deadline: float,
        *,
        interrupt_for_command: bool = False,
    ) -> bytes: ...


class TransactionTimeoutError(PurifierClientError):
    """Raised when the device does not complete a request in time."""


class RefreshPreemptedError(PurifierClientError):
    """Interrupt lower-priority refresh work when a command is waiting."""


class TransactionExecutor:
    """Execute one descriptor at a time and retain bounded response evidence."""

    def __init__(
        self,
        *,
        matcher_factory: MatcherFactory,
        frame_queue: asyncio.Queue[bytes],
        disconnected: asyncio.Event,
        command_wake: asyncio.Event,
        disconnect_error: DisconnectErrorFactory,
    ) -> None:
        self._matcher_factory = matcher_factory
        self._frame_queue = frame_queue
        self._disconnected = disconnected
        self._command_wake = command_wake
        self._disconnect_error = disconnect_error
        self._active_request: str | None = None
        self._last_timeout_summary: str | None = None

    @property
    def active_request(self) -> str | None:
        """Return the descriptor currently owning the application channel."""
        return self._active_request

    @property
    def last_timeout_summary(self) -> str | None:
        """Return bounded evidence from the most recent timed-out attempt."""
        return self._last_timeout_summary

    def clear_timeout_summary(self) -> None:
        """Clear stale timeout evidence after a session becomes ready."""
        self._last_timeout_summary = None

    async def async_execute(
        self,
        descriptor: RequestDescriptor,
        *,
        attempts: int,
        timeout: float,
        send: SendFrame,
        reduce_frame: FrameReducer,
        next_frame: NextFrame | None = None,
        interrupt_for_command: bool = False,
        on_attempt: AttemptHook | None = None,
        on_send: AttemptHook | None = None,
        on_sent: AttemptHook | None = None,
    ) -> tuple[bytes, ...]:
        """Send and match a descriptor within bounded per-attempt deadlines."""
        loop = asyncio.get_running_loop()
        attempt_summaries: list[str] = []
        self._active_request = descriptor.name
        try:
            for attempt in range(attempts):
                if on_attempt is not None:
                    on_attempt()
                matcher = self._matcher_factory(descriptor)
                received_count = 0
                ignored_count = 0
                ignored_sample: list[bytes] = []
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
                await send(descriptor.frame)
                if on_sent is not None:
                    on_sent()
                deadline = loop.time() + timeout

                try:
                    while not matcher.complete:
                        wait_for_frame = next_frame or self.async_next_frame
                        frame = await wait_for_frame(
                            deadline,
                            interrupt_for_command=interrupt_for_command,
                        )
                        received_count += 1
                        result = matcher.feed(frame)
                        reduce_frame(
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
                            ignored_count += 1
                            if len(ignored_sample) < 4:
                                ignored_sample.append(frame)
                        if result is MatchResult.COMPLETE:
                            self._last_timeout_summary = None
                            _LOGGER.debug(
                                "Transaction complete: request=%s attempt=%d/%d "
                                "latency=%.3fs received=%d matched=%d",
                                descriptor.name,
                                attempt + 1,
                                attempts,
                                loop.time() - started,
                                received_count,
                                len(matcher.frames),
                            )
                            return matcher.frames
                except TimeoutError:
                    sample = " | ".join(
                        frame.hex(" ") for frame in ignored_sample
                    )
                    summary = (
                        f"attempt {attempt + 1}/{attempts}: "
                        f"received={received_count}, "
                        f"matched_fragments={len(matcher.frames)}, "
                        f"ignored={ignored_count}, "
                        f"ignored_sample={sample or 'none'}"
                    )
                    attempt_summaries.append(summary)
                    self._last_timeout_summary = (
                        f"request={descriptor.name}; {summary}"
                    )
                    _LOGGER.debug(
                        "Transaction timeout: request=%s elapsed=%.3fs %s",
                        descriptor.name,
                        loop.time() - started,
                        summary,
                    )

            details = "; ".join(attempt_summaries)
            raise TransactionTimeoutError(
                f"Timed out waiting for {descriptor.name} response after "
                f"{attempts} attempt(s) with {timeout:.1f}s deadlines; "
                f"{details}"
            )
        finally:
            self._active_request = None

    async def async_next_frame(
        self,
        deadline: float,
        *,
        interrupt_for_command: bool = False,
    ) -> bytes:
        """Arbitrate the next frame, disconnect, timeout, and command wake."""
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError

        frame_task = asyncio.create_task(self._frame_queue.get())
        disconnect_task = asyncio.create_task(self._disconnected.wait())
        command_task = (
            asyncio.create_task(self._command_wake.wait())
            if interrupt_for_command
            else None
        )
        tasks = (
            (frame_task, disconnect_task, command_task)
            if command_task is not None
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
            raise self._disconnect_error()
        if frame_task in done:
            return frame_task.result()
        if command_task is not None and command_task in done:
            raise RefreshPreemptedError("Refresh yielded to a pending command")
        raise TimeoutError
