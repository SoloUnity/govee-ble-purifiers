"""Direct tests for deterministic command-operation lifecycle policy."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.govee_ble_air_purifier.client import (
    _Operation as ClientOperation,
)
from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import (
    FanMode,
    Model,
    PurifierState,
    SetFanMode,
    SetNightLightColor,
    SetPower,
)
from custom_components.govee_ble_air_purifier.observations import CommandOrigin
from custom_components.govee_ble_air_purifier.operations import (
    AirQualityQueryCancelled,
    AirQualityQueryController,
    CommandDeadlineExceeded,
    CommandOperationController,
    CommandSuperseded,
    PurifierClientError,
    _Operation,
)
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile
from custom_components.govee_ble_air_purifier.protocol import (
    GoveePurifierProtocol,
)
from custom_components.govee_ble_air_purifier.state_reducer import (
    PurifierStateReducer,
)


def make_controller(
    model: Model = Model.H7124,
) -> tuple[CommandOperationController, PurifierStateReducer]:
    profile = DeviceProfile.for_model(model)
    reducer = PurifierStateReducer(profile)
    return (
        CommandOperationController(
            profile.timings,
            GoveePurifierProtocol(profile),
            reducer,
        ),
        reducer,
    )


def make_operation(
    command: SetPower | SetFanMode | SetNightLightColor,
    *,
    deadline: float = 130.0,
    created_at: float = 10.0,
) -> _Operation:
    return _Operation(
        command,
        asyncio.get_running_loop().create_future(),
        deadline,
        created_at=created_at,
    )


def test_client_operation_is_reexported_alias() -> None:
    """The compatibility import refers to the sole operations definition."""
    assert ClientOperation is _Operation


@pytest.mark.asyncio
async def test_enqueue_coalesces_only_matching_pending_command_type() -> None:
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    old_power = controller.enqueue(SetPower(True), loop=loop, now=10.0)
    fan = controller.enqueue(
        SetFanMode(FanMode.MEDIUM), loop=loop, now=11.0
    )
    replacement = controller.enqueue(SetPower(False), loop=loop, now=12.0)

    assert isinstance(old_power.future.exception(), CommandSuperseded)
    assert str(old_power.future.exception()) == (
        "A newer control superseded this request"
    )
    assert list(controller.pending) == [fan, replacement]
    assert replacement.deadline == 132.0
    assert controller.event.is_set()

    fan.future.cancel()
    replacement.future.cancel()


@pytest.mark.asyncio
async def test_active_command_is_not_superseded_and_only_one_can_be_taken() -> None:
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    active = controller.enqueue(SetPower(True), loop=loop, now=10.0)

    assert controller.take_next(now=10.0, generation=1) is active
    replacement = controller.enqueue(SetPower(False), loop=loop, now=11.0)
    assert controller.take_next(now=11.0, generation=1) is None
    assert not active.future.done()

    controller.release(active)
    assert controller.take_next(now=11.0, generation=1) is replacement
    active.future.cancel()
    replacement.future.cancel()


@pytest.mark.asyncio
async def test_cancelled_sent_fan_reserves_late_exact_echo_as_stale() -> None:
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    operation = controller.enqueue(
        SetFanMode(FanMode.HIGH), loop=loop, now=10.0
    )
    assert controller.take_next(now=10.0, generation=4) is operation
    controller.record_send(operation, generation=4, now=10.1)

    controller.cancel(operation, generation=4)
    controller.release(operation)
    echo = controller._protocol.command_request(operation.command).frame  # noqa: SLF001

    assert controller.consume_superseded_fan_echo(generation=4, frame=echo)
    assert controller.consume_superseded_fan_echo(generation=4, frame=echo)


@pytest.mark.asyncio
async def test_cancelled_multi_attempt_fan_quarantine_retires_on_replacement_send(
) -> None:
    controller, _ = make_controller(Model.H7129)
    loop = asyncio.get_running_loop()
    command = SetFanMode(FanMode.HIGH)
    cancelled = controller.enqueue(command, loop=loop, now=10.0)
    assert controller.take_next(now=10.0, generation=4) is cancelled
    controller.record_send(cancelled, generation=4, now=10.1)
    controller.record_send(cancelled, generation=4, now=10.2)
    controller.cancel(cancelled, generation=4)
    controller.release(cancelled)
    echo = controller._protocol.command_request(command).frame  # noqa: SLF001

    assert controller.consume_superseded_fan_echo(generation=4, frame=echo)
    assert controller.consume_superseded_fan_echo(generation=4, frame=echo)

    replacement = controller.enqueue(command, loop=loop, now=11.0)
    assert controller.take_next(now=11.0, generation=4) is replacement
    controller.record_send(replacement, generation=4, now=11.1)
    assert not controller.consume_superseded_fan_echo(generation=4, frame=echo)
    replacement.future.cancel()


@pytest.mark.asyncio
async def test_cancelled_fan_quarantines_are_bounded_and_lifecycle_cleared() -> None:
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()

    modes = list(FanMode)
    for index in range(12):
        mode = modes[index % len(modes)]
        operation = controller.enqueue(
            SetFanMode(mode), loop=loop, now=float(index)
        )
        assert controller.take_next(now=float(index), generation=index) is operation
        controller.record_send(operation, generation=index, now=index + 0.1)
        controller.cancel(operation, generation=index)
        controller.release(operation)

    assert len(controller._cancelled_fan_echoes) <= 8  # noqa: SLF001
    controller.clear_superseded_fan_echoes()
    assert not controller._cancelled_fan_echoes  # noqa: SLF001


@pytest.mark.asyncio
async def test_physical_authority_forces_replacement_send_and_consumes_old_echo(
) -> None:
    controller, reducer = make_controller()
    loop = asyncio.get_running_loop()
    active = controller.enqueue(
        SetFanMode(FanMode.HIGH), loop=loop, now=10.0
    )
    assert controller.take_next(now=10.0, generation=1) is active
    controller.record_send(active, generation=1, now=10.1)

    assert controller.supersede_active_fan_by_physical(generation=1)
    assert isinstance(active.future.exception(), CommandSuperseded)
    assert active.cancel_event.is_set()
    reducer.replace_state(PurifierState(fan_mode=FanMode.HIGH))
    replacement = controller.enqueue(
        SetFanMode(FanMode.HIGH), loop=loop, now=11.0
    )
    assert replacement.force_fan_confirmation
    assert controller.prepare_for_execution(
        replacement, now=11.0, generation=1
    )
    assert not replacement.future.done()

    echo = build_frame(bytes.fromhex("3a 05 01 03"))
    assert controller.consume_superseded_fan_echo(
        generation=1, frame=echo
    )
    assert not controller.consume_superseded_fan_echo(
        generation=1, frame=echo
    )
    replacement.future.cancel()


@pytest.mark.asyncio
async def test_forced_replacement_send_retires_matching_old_echo() -> None:
    """The first matching echo after replacement transmission belongs to it."""
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    command = SetFanMode(FanMode.HIGH)
    active = controller.enqueue(command, loop=loop, now=10.0)
    assert controller.take_next(now=10.0, generation=1) is active
    controller.record_send(active, generation=1, now=10.1)
    assert controller.supersede_active_fan_by_physical(generation=1)
    controller.release(active)

    replacement = controller.enqueue(command, loop=loop, now=11.0)
    assert replacement.force_fan_confirmation
    controller.record_send(replacement, generation=1, now=11.1)

    echo = build_frame(bytes.fromhex("3a 05 01 03"))
    assert not controller.consume_superseded_fan_echo(
        generation=1, frame=echo
    )
    replacement.future.cancel()


@pytest.mark.asyncio
async def test_superseded_echoes_are_bounded_and_clear_at_generation_boundary(
) -> None:
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    echo = build_frame(bytes.fromhex("3a 05 01 03"))

    for index in range(12):
        active = controller.enqueue(
            SetFanMode(FanMode.HIGH), loop=loop, now=float(index)
        )
        assert controller.take_next(now=float(index), generation=index) is active
        controller.record_send(
            active, generation=index, now=float(index) + 0.1
        )
        assert controller.supersede_active_fan_by_physical(generation=index)
        assert isinstance(active.future.exception(), CommandSuperseded)
        controller.release(active)

    consumed = sum(
        controller.consume_superseded_fan_echo(generation=index, frame=echo)
        for index in range(12)
    )
    assert consumed == 8

    active = controller.enqueue(
        SetFanMode(FanMode.HIGH), loop=loop, now=20.0
    )
    assert controller.take_next(now=20.0, generation=3) is active
    controller.record_send(active, generation=3, now=20.1)
    assert controller.supersede_active_fan_by_physical(generation=3)
    assert isinstance(active.future.exception(), CommandSuperseded)
    controller.clear_superseded_fan_echoes()
    assert not controller.consume_superseded_fan_echo(
        generation=3, frame=echo
    )
    controller.release(active)


@pytest.mark.asyncio
async def test_expiry_preserves_order_and_exact_diagnostic_shape() -> None:
    controller, _ = make_controller()
    expired = make_operation(SetPower(True), deadline=20.0)
    live = make_operation(SetFanMode(FanMode.HIGH), deadline=30.0)
    controller.pending.extend((expired, live))
    controller.event.set()

    controller.discard_expired(now=20.0, generation=4)

    error = expired.future.exception()
    assert isinstance(error, CommandDeadlineExceeded)
    assert str(error).startswith(
        "Command deadline expired while queued; command=SetPower; "
        "frame=33 01 01"
    )
    assert list(controller.pending) == [live]
    assert controller.snapshot().as_dict() == {
        "last_command_diagnostics": str(error),
        "pending_command_count": 1,
        "active_command": None,
        "active_command_send_attempts": None,
    }
    live.future.cancel()


@pytest.mark.asyncio
async def test_send_evidence_and_reconnect_requeue_preserve_budget_and_order() -> None:
    controller, _ = make_controller()
    ambiguous = make_operation(SetPower(True))
    later = make_operation(SetFanMode(FanMode.LOW))
    controller.pending.append(later)

    controller.record_send(ambiguous, generation=2, now=10.25)
    controller.record_response_failure(
        ambiguous,
        generation=2,
        error=TimeoutError("silent"),
    )
    controller.requeue_for_reconciliation(
        ambiguous,
        now=20.0,
        generation=2,
    )

    assert list(controller.pending) == [ambiguous, later]
    assert ambiguous.send_attempts == 1
    assert ambiguous.send_diagnostics == [
        "attempt=1,generation=2,elapsed=0.250s"
    ]
    assert ambiguous.response_failures == ["generation=2: silent"]
    assert controller.event.is_set()
    ambiguous.future.cancel()
    later.future.cancel()


@pytest.mark.asyncio
async def test_reconciliation_uses_state_but_rgb_stays_ambiguous() -> None:
    controller, reducer = make_controller()
    reducer.replace_state(PurifierState(power=True, light_rgb=(1, 2, 3)))
    previous = make_operation(SetFanMode(FanMode.LOW))
    previous_error = controller.failure_error(
        previous,
        reason="previous command failure",
        generation=1,
    )
    power = make_operation(SetPower(True))
    color = make_operation(SetNightLightColor(1, 2, 3))

    assert controller.reconcile(power)
    assert power.future.done()
    assert power.future.exception() is None
    assert controller.last_command_diagnostics == str(previous_error)
    assert not controller.reconcile(color)
    assert not color.future.done()
    previous.future.cancel()
    color.future.cancel()


@pytest.mark.asyncio
async def test_fan_evidence_prefers_active_command_and_is_bounded_in_failure() -> None:
    controller, _ = make_controller()
    fan = make_operation(SetFanMode(FanMode.MEDIUM))
    controller.active = fan

    mismatch = controller.record_fan_frame(
        generation=7,
        frame=build_frame(bytes.fromhex("3a 05 01 03")),
        phase="fan_mode_command",
        now=10.5,
    )
    physical = controller.record_fan_frame(
        generation=7,
        frame=build_frame(bytes.fromhex("ee 05 01 01")),
        phase="ready",
        now=10.75,
    )
    error = controller.fail(
        fan,
        reason="Command remained unconfirmed after bounded retries",
        generation=8,
    )

    assert mismatch is not None and "matches_request=False" in mismatch
    assert physical is not None and "decoded_mode=low" in physical
    assert "observed_fan_frames=" in str(error)
    assert "current_generation=8" in str(error)


@pytest.mark.asyncio
async def test_shutdown_fails_captured_active_and_all_pending_commands() -> None:
    controller, _ = make_controller()
    active = make_operation(SetPower(True))
    pending = make_operation(SetFanMode(FanMode.TURBO))
    controller.active = active
    controller.pending.append(pending)
    controller.event.set()

    controller.fail_for_shutdown(
        PurifierClientError("Purifier client stopped"),
        active=active,
    )

    assert isinstance(active.future.exception(), PurifierClientError)
    assert str(active.future.exception()) == "Purifier client stopped"
    assert isinstance(pending.future.exception(), PurifierClientError)
    assert not controller.pending
    assert controller.event.is_set()


@pytest.mark.asyncio
async def test_command_identity_origin_and_one_shot_coalescing_are_immutable_data(
) -> None:
    """Commands get unique origins while one-shot callers share one request."""
    controller, _ = make_controller()
    loop = asyncio.get_running_loop()
    first = controller.enqueue(
        SetPower(True),
        loop=loop,
        now=1.0,
        origin=CommandOrigin.HANDOFF,
    )
    second = controller.enqueue(
        SetFanMode(FanMode.HIGH),
        loop=loop,
        now=2.0,
        origin=CommandOrigin.CUSTOM_AUTO,
    )
    assert first.operation_id != second.operation_id
    assert first.origin is CommandOrigin.HANDOFF
    assert second.origin is CommandOrigin.CUSTOM_AUTO

    lane = AirQualityQueryController()
    query = lane.acquire(loop=loop, now=3.0, timeout=4.0, generation=5)
    coalesced = lane.acquire(loop=loop, now=3.5, timeout=4.0, generation=5)
    assert coalesced is query
    assert query.deadline == 7.0
    lane.cancel()
    assert isinstance(query.future.exception(), AirQualityQueryCancelled)
    first.future.cancel()
    second.future.cancel()
