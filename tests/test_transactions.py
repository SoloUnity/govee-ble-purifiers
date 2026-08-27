"""Direct tests for one-in-flight descriptor transaction execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from custom_components.govee_ble_air_purifier import client as client_module
from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile
from custom_components.govee_ble_air_purifier.protocol import GoveePurifierProtocol
from custom_components.govee_ble_air_purifier.transactions import (
    RefreshPreemptedError,
    TransactionExecutor,
    TransactionTimeoutError,
)


def _executor(
    *,
    disconnect_error: Callable[[], Exception] | None = None,
) -> tuple[
    TransactionExecutor,
    GoveePurifierProtocol,
    asyncio.Queue[bytes],
    asyncio.Event,
    asyncio.Event,
]:
    protocol = GoveePurifierProtocol(DeviceProfile.for_model(Model.H7124))
    frame_queue: asyncio.Queue[bytes] = asyncio.Queue()
    disconnected = asyncio.Event()
    command_wake = asyncio.Event()
    executor = TransactionExecutor(
        matcher_factory=protocol.new_response_matcher,
        frame_queue=frame_queue,
        disconnected=disconnected,
        command_wake=command_wake,
        disconnect_error=disconnect_error or (lambda: RuntimeError("disconnected")),
    )
    return executor, protocol, frame_queue, disconnected, command_wake


def test_client_reexports_transaction_control_errors() -> None:
    """Existing imports keep pointing at each error's single definition."""
    assert client_module.TransactionTimeoutError is TransactionTimeoutError
    assert client_module._RefreshPreempted is RefreshPreemptedError


@pytest.mark.asyncio
async def test_executor_reduces_every_candidate_and_authorizes_only_completion(
) -> None:
    """Ignored frames stay useful, but only completion carries request authority."""
    executor, protocol, frame_queue, _, _ = _executor()
    descriptor = next(
        request
        for request in protocol.initialization_requests()
        if request.name == "mode_data_03"
    )
    ignored = build_frame(b"\xaa\x05\x01\x03")
    matched = build_frame(b"\xaa\x05\x03\x00\x00\x14")
    frame_queue.put_nowait(ignored)
    frame_queue.put_nowait(matched)
    sent: list[bytes] = []
    effects: list[str] = []
    reduced: list[tuple[bytes, str | None]] = []

    async def send(frame: bytes) -> None:
        assert executor.active_request == descriptor.name
        sent.append(frame)

    def reduce(frame: bytes, *, matched_request: str | None = None) -> None:
        protocol.decode(frame)
        reduced.append((frame, matched_request))

    frames = await executor.async_execute(
        descriptor,
        attempts=1,
        timeout=1.0,
        send=send,
        reduce_frame=reduce,
        on_attempt=lambda: effects.append("attempt"),
        on_send=lambda: effects.append("send"),
        on_sent=lambda: effects.append("sent"),
    )

    assert sent == [descriptor.frame]
    assert effects == ["attempt", "send", "sent"]
    assert reduced == [(ignored, None), (matched, descriptor.name)]
    assert frames == (matched,)
    assert executor.active_request is None
    assert executor.last_timeout_summary is None


@pytest.mark.asyncio
async def test_executor_bounds_attempts_and_retains_ignored_evidence() -> None:
    """Retries have independent deadlines and retain only a bounded frame sample."""
    executor, protocol, _, _, _ = _executor()
    descriptor = protocol.initialization_requests()[0]
    ignored = [build_frame(bytes((0xEE, 0x05, 0x01, value))) for value in range(5)]
    candidates = iter((*ignored, None, None))
    sends: list[bytes] = []
    reductions: list[bytes] = []
    attempt_effects = 0

    async def send(frame: bytes) -> None:
        sends.append(frame)

    async def next_frame(
        _: float,
        *,
        interrupt_for_command: bool = False,
    ) -> bytes:
        assert not interrupt_for_command
        candidate = next(candidates)
        if candidate is None:
            raise TimeoutError
        return candidate

    def reduce(frame: bytes, *, matched_request: str | None = None) -> None:
        assert matched_request is None
        protocol.decode(frame)
        reductions.append(frame)

    def record_attempt() -> None:
        nonlocal attempt_effects
        attempt_effects += 1

    with pytest.raises(TransactionTimeoutError) as raised:
        await executor.async_execute(
            descriptor,
            attempts=2,
            timeout=0.5,
            send=send,
            reduce_frame=reduce,
            next_frame=next_frame,
            on_attempt=record_attempt,
        )

    message = str(raised.value)
    assert len(sends) == 2
    assert attempt_effects == 2
    assert reductions == ignored
    assert "attempt 1/2: received=5, matched_fragments=0, ignored=5" in message
    assert "attempt 2/2: received=0" in message
    assert ignored[3].hex(" ") in message
    assert ignored[4].hex(" ") not in message
    assert executor.last_timeout_summary == (
        f"request={descriptor.name}; attempt 2/2: received=0, "
        "matched_fragments=0, ignored=0, ignored_sample=none"
    )
    assert executor.active_request is None


@pytest.mark.asyncio
async def test_next_frame_disconnect_wins_and_command_wake_is_optional() -> None:
    """Disconnect and refresh-preemption edges interrupt without timeout delay."""
    executor, _, frame_queue, disconnected, command_wake = _executor(
        disconnect_error=lambda: RuntimeError("link dropped")
    )
    loop = asyncio.get_running_loop()

    disconnected.set()
    frame_queue.put_nowait(build_frame(b"\xaa\x01\x01"))
    with pytest.raises(RuntimeError, match="link dropped"):
        await executor.async_next_frame(loop.time() + 1)

    disconnected.clear()
    command_wake.set()
    with pytest.raises(RefreshPreemptedError, match="pending command"):
        await executor.async_next_frame(
            loop.time() + 1,
            interrupt_for_command=True,
        )


@pytest.mark.asyncio
async def test_cancelling_next_frame_observes_all_child_tasks() -> None:
    """Cancelling the owner leaves no queue/event waiter behind."""
    executor, _, _, _, _ = _executor()
    before = set(asyncio.all_tasks())
    wait_task = asyncio.create_task(
        executor.async_next_frame(asyncio.get_running_loop().time() + 60)
    )
    await asyncio.sleep(0)
    child_tasks = set(asyncio.all_tasks()) - before - {wait_task}
    assert len(child_tasks) == 2

    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task

    assert all(task.done() for task in child_tasks)
