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
from custom_components.govee_ble_air_purifier.operations import (
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
