"""Deterministic actor tests for the inert Custom Auto controller."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest

from custom_components.govee_ble_air_purifier.custom_auto_controller import (
    CustomAutoController,
    CustomAutoSnapshot,
)
from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    CustomAutoOptions,
    parse_custom_auto_options,
)
from custom_components.govee_ble_air_purifier.models import FanMode, Model
from custom_components.govee_ble_air_purifier.observations import (
    AirQualityObservation,
    CommandOrigin,
    FanModeObservation,
    ObservationPurpose,
    ObservationSource,
)
from custom_components.govee_ble_air_purifier.profiles import get_profile_registry


def _options(*, upshift: int = 3) -> CustomAutoOptions:
    defaults = (
        get_profile_registry().for_model(Model.H7124).custom_auto_defaults
    )
    return parse_custom_auto_options(
        {
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: upshift,
        },
        defaults,
    )


@dataclass
class FakeClock:
    now: float = 0
    sleepers: list[tuple[float, asyncio.Event]] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep_until(self, deadline: float) -> None:
        event = asyncio.Event()
        self.sleepers.append((deadline, event))
        await event.wait()

    def advance(self, now: float) -> None:
        self.now = now
        for deadline, event in tuple(self.sleepers):
            if deadline <= now:
                event.set()


class Callbacks:
    def __init__(self) -> None:
        self.samples = 0
        self.sample_cancels = 0
        self.commands: list[tuple[FanMode, CommandOrigin]] = []
        self.block_sample = False
        self.block_command = False
        self.sample_gate = asyncio.Event()
        self.command_gate = asyncio.Event()
        self.command_failures = 0
        self.sample_failures = 0
        self.command_cancelled = 0

    async def request_sample(self) -> None:
        self.samples += 1
        if self.sample_failures:
            self.sample_failures -= 1
            raise RuntimeError("injected sample failure")
        if self.block_sample:
            await self.sample_gate.wait()

    async def cancel_sample(self) -> None:
        self.sample_cancels += 1

    async def send_mode(self, mode: FanMode, origin: CommandOrigin) -> None:
        self.commands.append((mode, origin))
        try:
            if self.block_command:
                await self.command_gate.wait()
        except asyncio.CancelledError:
            self.command_cancelled += 1
            raise
        if self.command_failures:
            self.command_failures -= 1
            raise RuntimeError("injected command failure")


async def _flush(turns: int = 8) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def _air(
    revision: int,
    pm25: int | None,
    *,
    generation: int = 1,
    observed_at: float = 0,
) -> AirQualityObservation:
    return AirQualityObservation(
        revision=revision,
        generation=generation,
        observed_at=observed_at,
        source=ObservationSource.QUERY,
        purpose=ObservationPurpose.ONE_SHOT,
        pm25=pm25,
        filter_life=90,
    )


def _fan(
    revision: int,
    mode: FanMode,
    *,
    source: ObservationSource,
    generation: int = 1,
    origin: CommandOrigin | None = None,
) -> FanModeObservation:
    return FanModeObservation(
        revision=revision,
        generation=generation,
        observed_at=0,
        source=source,
        purpose=ObservationPurpose.UNSOLICITED,
        command_origin=origin,
        mode=mode,
    )


async def _controller(
    *,
    options: CustomAutoOptions | None = None,
    callbacks: Callbacks | None = None,
    clock: FakeClock | None = None,
) -> tuple[CustomAutoController, Callbacks, FakeClock]:
    callbacks = callbacks or Callbacks()
    clock = clock or FakeClock()
    controller = CustomAutoController(
        options or _options(),
        request_sample=callbacks.request_sample,
        cancel_sample=callbacks.cancel_sample,
        send_fan_mode=callbacks.send_mode,
        clock=clock,
        sleep_until=clock.sleep_until,
    )
    assert controller.snapshot.actor_tasks == 0
    await controller.start()
    controller.set_connection(available=True, generation=1)
    controller.set_powered(True)
    await _flush()
    return controller, callbacks, clock


async def _activate_and_confirm(
    controller: CustomAutoController,
    mode: FanMode,
    *,
    pm25: int,
    revision: int = 1,
) -> None:
    await controller.activate()
    controller.observe_air_quality(_air(revision, pm25))
    await _flush()
    controller.observe_fan_mode(
        _fan(
            revision,
            mode,
            source=ObservationSource.COMMAND,
            origin=CommandOrigin.CUSTOM_AUTO,
        )
    )
    await _flush()


@pytest.mark.asyncio
async def test_activation_barrier_requests_fresh_and_rejects_cached_revision() -> None:
    controller, callbacks, _ = await _controller(options=_options(upshift=0))
    controller.observe_air_quality(_air(5, 4))
    await _flush()

    await controller.activate()
    await _flush()
    controller.observe_air_quality(_air(5, 20))
    await _flush()
    assert callbacks.commands == []
    assert callbacks.samples == 1

    controller.observe_air_quality(_air(6, 20))
    await _flush()
    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_pre_activation_receipt_processed_later_fails_time_barrier() -> None:
    clock = FakeClock(now=10)
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), clock=clock
    )
    stale_frame = _air(1, 20, observed_at=9)
    await controller.activate()

    # This frame was received before activation but reaches the actor after it.
    controller.observe_air_quality(stale_frame)
    await _flush()
    assert callbacks.commands == []

    # Equality is deliberately fresh: only receipt times before the barrier
    # are rejected.
    controller.observe_air_quality(_air(2, 20, observed_at=10))
    await _flush()
    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_equal_pm_distinct_revisions_confirm_positive_upshift() -> None:
    controller, callbacks, clock = await _controller()
    await _activate_and_confirm(controller, FanMode.SLEEP, pm25=0)
    callbacks.commands.clear()

    controller.observe_air_quality(_air(2, 4, observed_at=0))
    await _flush()
    clock.advance(3)
    await _flush()
    controller.observe_air_quality(_air(3, 4, observed_at=3))
    await _flush()

    assert callbacks.commands == [(FanMode.LOW, CommandOrigin.CUSTOM_AUTO)]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_downshift_boundary_samples_then_observation_commands() -> None:
    controller, callbacks, clock = await _controller()
    await _activate_and_confirm(controller, FanMode.TURBO, pm25=20)
    callbacks.commands.clear()
    samples_before = callbacks.samples

    controller.observe_air_quality(_air(2, 0, observed_at=0))
    await _flush()
    clock.advance(300)
    await _flush()

    assert callbacks.samples == samples_before + 1
    assert callbacks.commands == []
    controller.observe_air_quality(_air(3, 0, observed_at=300))
    await _flush()
    assert callbacks.commands == [(FanMode.LOW, CommandOrigin.CUSTOM_AUTO)]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_pending_target_deduplicates_and_failure_waits_for_new_sample() -> None:
    callbacks = Callbacks()
    callbacks.command_failures = 1
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 10))
    await _flush()

    assert len(callbacks.commands) == 1
    assert controller.snapshot.command_state == "failed"
    await _flush()
    assert len(callbacks.commands) == 1
    controller.observe_air_quality(_air(1, 10))
    await _flush()
    assert len(callbacks.commands) == 1

    controller.observe_air_quality(_air(2, 10))
    await _flush()
    assert len(callbacks.commands) == 2
    controller.observe_air_quality(_air(3, 10))
    await _flush()
    assert len(callbacks.commands) == 2
    await controller.shutdown()


@pytest.mark.asyncio
async def test_pending_high_reconciles_newer_turbo_sequentially() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 10))
    await _flush()
    controller.observe_air_quality(_air(2, 20))
    await _flush()

    assert callbacks.commands == [(FanMode.HIGH, CommandOrigin.CUSTOM_AUTO)]
    assert controller.snapshot.pending_target is FanMode.HIGH
    callbacks.command_gate.set()
    await _flush()
    assert callbacks.commands == [
        (FanMode.HIGH, CommandOrigin.CUSTOM_AUTO),
        (FanMode.TURBO, CommandOrigin.CUSTOM_AUTO),
    ]
    assert controller.snapshot.confirmed_mode is FanMode.TURBO
    assert controller.snapshot.pending_target is None
    await controller.shutdown()


@pytest.mark.asyncio
async def test_failed_downshift_retries_matured_target_at_actor_level() -> None:
    callbacks = Callbacks()
    options = replace(_options(), downshift_delays_minutes=(7, 5, 5, 5))
    controller, callbacks, clock = await _controller(
        options=options, callbacks=callbacks
    )
    await _activate_and_confirm(controller, FanMode.TURBO, pm25=20)
    callbacks.commands.clear()

    controller.observe_air_quality(_air(2, 0, observed_at=0))
    await _flush()
    clock.advance(300)
    await _flush()
    callbacks.command_failures = 1
    controller.observe_air_quality(_air(3, 0, observed_at=300))
    await _flush()
    assert callbacks.commands == [(FanMode.LOW, CommandOrigin.CUSTOM_AUTO)]
    assert controller.snapshot.command_state == "failed"

    clock.advance(301)
    controller.observe_air_quality(_air(4, 0, observed_at=301))
    await _flush()
    assert callbacks.commands[-1] == (FanMode.LOW, CommandOrigin.CUSTOM_AUTO)
    assert controller.snapshot.confirmed_mode is FanMode.LOW

    clock.advance(420)
    await _flush()
    controller.observe_air_quality(_air(5, 0, observed_at=420))
    await _flush()
    assert callbacks.commands == [
        (FanMode.LOW, CommandOrigin.CUSTOM_AUTO),
        (FanMode.LOW, CommandOrigin.CUSTOM_AUTO),
        (FanMode.SLEEP, CommandOrigin.CUSTOM_AUTO),
    ]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_changed_pending_target_is_scheduled_after_old_worker_exits() -> None:
    """Completion of superseded work cannot strand its replacement target."""
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 10))
    await _flush()
    assert callbacks.commands == [(FanMode.HIGH, CommandOrigin.CUSTOM_AUTO)]

    controller._policy = replace(  # noqa: SLF001 - exercises worker race state
        controller._policy,  # noqa: SLF001
        pending_target=FanMode.TURBO,
    )
    callbacks.command_gate.set()
    await _flush()

    assert callbacks.commands == [
        (FanMode.HIGH, CommandOrigin.CUSTOM_AUTO),
        (FanMode.TURBO, CommandOrigin.CUSTOM_AUTO),
    ]
    assert controller.snapshot.confirmed_mode is FanMode.TURBO
    assert controller.snapshot.pending_target is None
    await controller.shutdown()


@pytest.mark.asyncio
async def test_physical_auto_redirects_same_target_but_manual_turns_off() -> None:
    controller, callbacks, _ = await _controller(options=_options(upshift=0))
    await _activate_and_confirm(controller, FanMode.LOW, pm25=4)
    callbacks.commands.clear()

    controller.observe_fan_mode(
        _fan(2, FanMode.AUTO, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    assert controller.snapshot.active
    assert controller.snapshot.auto_redirect_state == "pending"
    assert callbacks.samples == 2
    controller.observe_air_quality(_air(2, 4))
    await _flush()
    assert callbacks.commands == [(FanMode.LOW, CommandOrigin.CUSTOM_AUTO)]
    assert controller.snapshot.auto_redirect_state == "confirmed"

    controller.observe_fan_mode(
        _fan(3, FanMode.MEDIUM, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    assert not controller.snapshot.active
    assert controller.snapshot.auto_redirect_state == "idle"
    await controller.shutdown()


@pytest.mark.asyncio
async def test_physical_auto_redirect_failure_is_bounded_diagnostic_state() -> None:
    callbacks = Callbacks()
    callbacks.command_failures = 1
    controller, _, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_fan_mode(
        _fan(1, FanMode.AUTO, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    controller.observe_air_quality(_air(1, 20))
    await _flush()

    snapshot = controller.snapshot
    assert snapshot.auto_redirect_state == "failed"
    assert snapshot.command_state == "failed"
    assert snapshot.last_error_type == "RuntimeError"
    assert snapshot.last_physical_fan is not None
    assert snapshot.last_physical_fan.mode is FanMode.AUTO
    assert snapshot.last_physical_fan.source == "physical"
    await controller.shutdown()


@pytest.mark.asyncio
async def test_failed_redirect_recovers_from_unsolicited_sample_and_command() -> None:
    callbacks = Callbacks()
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    await _flush()
    callbacks.sample_failures = 1
    controller.observe_fan_mode(
        _fan(1, FanMode.AUTO, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    assert controller.snapshot.auto_redirect_state == "failed"

    controller.observe_air_quality(_air(1, 20))
    await _flush()

    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]
    assert controller.snapshot.auto_redirect_state == "confirmed"
    await controller.shutdown()


@pytest.mark.asyncio
async def test_redirect_command_failure_remains_failed() -> None:
    callbacks = Callbacks()
    callbacks.command_failures = 1
    controller, _, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    await _flush()
    callbacks.sample_failures = 1
    controller.observe_fan_mode(
        _fan(1, FanMode.AUTO, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    controller.observe_air_quality(_air(1, 20))
    await _flush()

    assert controller.snapshot.auto_redirect_state == "failed"
    assert controller.snapshot.command_state == "failed"
    await controller.shutdown()


@pytest.mark.asyncio
async def test_begin_power_off_quiesces_work_and_gates_fresh_observations() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, clock = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 20))
    await _flush()
    assert controller.snapshot.command_tasks == 1

    await controller.begin_power_off()
    assert controller.snapshot.active
    assert controller.snapshot.suspended
    assert controller.snapshot.command_tasks == 0
    assert callbacks.command_cancelled == 1

    controller.observe_air_quality(_air(2, 4, observed_at=clock.now))
    clock.advance(600)
    await _flush()
    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]

    await controller.finish_power_off(False)
    assert controller.snapshot.active
    assert controller.snapshot.suspended
    await controller.shutdown()


@pytest.mark.asyncio
async def test_failed_power_off_restores_power_and_requires_fresh_sample() -> None:
    controller, callbacks, _ = await _controller(options=_options(upshift=0))
    await controller.activate()
    samples_before = callbacks.samples

    await controller.begin_power_off()
    controller.observe_air_quality(_air(1, 20))
    await _flush()
    await controller.finish_power_off(True)
    await _flush()

    assert controller.snapshot.active
    assert not controller.snapshot.suspended
    assert callbacks.samples == samples_before + 1
    assert callbacks.commands == []
    await controller.shutdown()


@pytest.mark.asyncio
async def test_begin_power_off_cancels_pending_timer_without_later_send() -> None:
    controller, callbacks, clock = await _controller(options=_options(upshift=30))
    await _activate_and_confirm(controller, FanMode.SLEEP, pm25=0)
    callbacks.commands.clear()
    controller.observe_air_quality(_air(2, 20))
    await _flush()
    assert controller.snapshot.timer_tasks == 1

    await controller.begin_power_off()
    assert controller.snapshot.timer_tasks == 0
    controller.observe_air_quality(_air(2, 20, observed_at=1))
    clock.advance(60)
    await _flush()

    assert callbacks.commands == []
    assert controller.snapshot.active
    assert controller.snapshot.suspended
    await controller.finish_power_off(False)
    await controller.shutdown()


@pytest.mark.asyncio
async def test_reconnect_quiesces_requeued_intent_until_fresh_current_sample() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 20))
    await _flush()
    assert controller.snapshot.command_tasks == 1

    controller.set_connection(available=False, generation=2)
    await _flush()
    assert callbacks.command_cancelled == 1
    assert controller.snapshot.command_tasks == 0
    controller.set_connection(available=True, generation=2)
    await _flush()
    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]

    controller.observe_air_quality(_air(1, 20, generation=2, observed_at=1))
    callbacks.block_command = False
    callbacks.command_gate.set()
    await _flush()
    assert callbacks.commands == [
        (FanMode.TURBO, CommandOrigin.CUSTOM_AUTO),
        (FanMode.TURBO, CommandOrigin.CUSTOM_AUTO),
    ]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_late_command_ack_after_physical_auto_cannot_confirm_old_work() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 10))
    await _flush()

    controller.observe_fan_mode(
        _fan(2, FanMode.AUTO, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    callbacks.command_gate.set()
    controller.observe_fan_mode(
        _fan(
            3,
            FanMode.HIGH,
            source=ObservationSource.COMMAND,
            origin=CommandOrigin.CUSTOM_AUTO,
        )
    )
    controller.observe_air_quality(_air(2, 20))
    await _flush()

    assert callbacks.command_cancelled == 1
    assert callbacks.commands == [
        (FanMode.HIGH, CommandOrigin.CUSTOM_AUTO),
        (FanMode.TURBO, CommandOrigin.CUSTOM_AUTO),
    ]
    assert controller.snapshot.confirmed_mode is FanMode.TURBO
    await controller.shutdown()


@pytest.mark.asyncio
async def test_late_ack_after_physical_manual_cannot_restore_ownership() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 10))
    await _flush()

    controller.observe_fan_mode(
        _fan(2, FanMode.MEDIUM, source=ObservationSource.PHYSICAL)
    )
    await _flush()
    controller.observe_fan_mode(
        _fan(
            3,
            FanMode.HIGH,
            source=ObservationSource.COMMAND,
            origin=CommandOrigin.CUSTOM_AUTO,
        )
    )
    controller.observe_air_quality(_air(2, 20))
    await _flush()

    assert callbacks.command_cancelled == 1
    assert callbacks.commands == [(FanMode.HIGH, CommandOrigin.CUSTOM_AUTO)]
    assert not controller.snapshot.active
    assert controller.snapshot.confirmed_mode is None
    await controller.shutdown()


@pytest.mark.asyncio
async def test_startup_and_non_custom_command_observations_are_not_physical() -> None:
    controller, _, _ = await _controller(options=_options(upshift=0))
    await controller.activate()
    controller.observe_fan_mode(
        _fan(1, FanMode.AUTO, source=ObservationSource.STARTUP)
    )
    controller.observe_fan_mode(
        _fan(
            2,
            FanMode.HIGH,
            source=ObservationSource.COMMAND,
            origin=CommandOrigin.HOME_ASSISTANT,
        )
    )
    await _flush()

    assert controller.snapshot.active
    assert controller.snapshot.pending_target is None
    await controller.shutdown()


@pytest.mark.asyncio
async def test_power_suspension_and_resume_preserve_on_intent() -> None:
    callbacks = Callbacks()
    callbacks.block_sample = True
    controller, callbacks, _ = await _controller(callbacks=callbacks)
    await controller.activate()
    await _flush()
    controller.set_powered(False)
    await _flush()

    assert controller.snapshot.active
    assert controller.snapshot.suspended
    assert controller.snapshot.sample_tasks == 0
    assert callbacks.sample_cancels == 1
    controller.set_powered(True)
    await _flush()
    assert controller.snapshot.active
    assert not controller.snapshot.suspended
    assert callbacks.samples == 2
    await controller.shutdown()


@pytest.mark.asyncio
async def test_disconnect_invalidates_generation_and_recovery_requires_fresh() -> None:
    controller, callbacks, _ = await _controller(options=_options(upshift=0))
    await controller.activate()
    controller.set_connection(available=False, generation=1)
    controller.observe_air_quality(_air(1, 20, generation=1))
    controller.set_connection(available=True, generation=2)
    await _flush()
    assert controller.snapshot.active
    assert callbacks.commands == []

    controller.observe_air_quality(_air(100, 20, generation=1))
    controller.set_connection(available=False, generation=1)
    await _flush()
    assert callbacks.commands == []
    assert controller.snapshot.available
    assert controller.snapshot.connection_generation == 2
    controller.observe_air_quality(_air(1, 20, generation=2))
    await _flush()
    assert callbacks.commands == [(FanMode.TURBO, CommandOrigin.CUSTOM_AUTO)]
    await controller.shutdown()


@pytest.mark.asyncio
async def test_reconfigure_and_reactivation_invalidate_old_sample_barriers() -> None:
    controller, callbacks, _ = await _controller(options=_options(upshift=0))
    await controller.activate()
    controller.observe_air_quality(_air(3, 4))
    await _flush()
    await controller.deactivate()
    await controller.activate()
    controller.observe_air_quality(_air(3, 20))
    await _flush()
    assert len(callbacks.commands) == 1

    await controller.reconfigure(_options(upshift=0))
    controller.observe_air_quality(_air(3, 20))
    await _flush()
    assert len(callbacks.commands) == 1
    controller.observe_air_quality(_air(4, 20))
    await _flush()
    assert len(callbacks.commands) == 2
    assert controller.snapshot.configuration_generation == 1
    await controller.shutdown()


@pytest.mark.asyncio
async def test_deactivate_cancels_command_race_before_yielding_ownership() -> None:
    callbacks = Callbacks()
    callbacks.block_command = True
    controller, callbacks, _ = await _controller(
        options=_options(upshift=0), callbacks=callbacks
    )
    await controller.activate()
    controller.observe_air_quality(_air(1, 20))
    await _flush()
    assert controller.snapshot.command_tasks == 1

    await controller.ha_override()

    assert not controller.snapshot.active
    assert controller.snapshot.pending_target is None
    assert controller.snapshot.command_tasks == 0
    assert callbacks.command_cancelled == 1
    await controller.shutdown()


@pytest.mark.asyncio
async def test_listeners_snapshot_task_counts_and_shutdown_cleanup() -> None:
    callbacks = Callbacks()
    callbacks.block_sample = True
    controller, _, clock = await _controller(callbacks=callbacks)
    snapshots: list[CustomAutoSnapshot] = []
    remove = controller.add_state_listener(snapshots.append)
    await controller.activate()
    await _flush()

    snapshot = controller.snapshot
    assert snapshot.actor_tasks == 1
    assert snapshot.sample_tasks == 1
    assert snapshot.timer_tasks == snapshot.command_tasks == 0
    assert snapshots and snapshots[-1].active
    remove()
    count = len(snapshots)
    controller.set_powered(False)
    await _flush()
    assert len(snapshots) == count

    await controller.shutdown()
    assert controller.snapshot.actor_tasks == 0
    assert controller.snapshot.sample_tasks == 0
    assert controller.snapshot.timer_tasks == 0
    assert controller.snapshot.command_tasks == 0
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("govee-custom-auto")
    ]
    assert clock.sleepers == []


@pytest.mark.asyncio
async def test_shutdown_before_start_is_task_free() -> None:
    async def no_op_sample() -> None:
        return None

    async def no_op_command(mode: FanMode, origin: CommandOrigin) -> None:
        return None

    async def no_op_cancel() -> None:
        return None

    async def no_op_sleep(deadline: float) -> None:
        return None

    controller = CustomAutoController(
        _options(),
        request_sample=no_op_sample,
        cancel_sample=no_op_cancel,
        send_fan_mode=no_op_command,
        clock=lambda: 0,
        sleep_until=no_op_sleep,
    )
    await controller.shutdown()
    assert controller.snapshot.actor_tasks == 0
