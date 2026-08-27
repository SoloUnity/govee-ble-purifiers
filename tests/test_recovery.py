"""Direct tests for deterministic purifier recovery policy."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import DeviceProfile
from custom_components.govee_ble_air_purifier.recovery import (
    BackoffDecision,
    RecoveryController,
)


def make_controller(model: Model = Model.H7124) -> RecoveryController:
    return RecoveryController(DeviceProfile.for_model(model).timings)


def arm_circuit(
    controller: RecoveryController,
    *,
    now: float,
    final_stage: str = "initializing",
) -> None:
    controller.record_failure(
        stage="connecting", cycle=1, stable_for=0.0, now=now
    )
    controller.record_advertisement_wake(now=now + 0.1)
    controller.record_failure(
        stage="connecting", cycle=2, stable_for=0.0, now=now + 0.2
    )
    controller.record_advertisement_wake(now=now + 0.3)
    controller.record_failure(
        stage=final_stage, cycle=3, stable_for=0.0, now=now + 0.4
    )


def test_rolling_window_trims_only_expired_evidence() -> None:
    controller = make_controller()
    controller.record_failure(
        stage="connecting", cycle=1, stable_for=0.0, now=10.0
    )
    controller.record_advertisement_wake(now=10.1)

    at_boundary = controller.snapshot(now=130.0)
    assert at_boundary.failure_count_in_window == 1
    assert at_boundary.advertisement_wake_count_in_window == 1

    expired = controller.snapshot(now=130.2)
    assert expired.failure_count_in_window == 0
    assert expired.advertisement_wake_count_in_window == 0


@pytest.mark.parametrize(
    ("model", "stage"),
    [
        (Model.H7124, "initializing"),
        (Model.H7129, "negotiating"),
    ],
)
def test_circuit_threshold_and_escalation_are_shared_by_both_models(
    model: Model,
    stage: str,
) -> None:
    controller = make_controller(model)
    arm_circuit(controller, now=20.0, final_stage=stage)

    assert controller.circuit_floor(now=20.5) == 5.0
    controller.record_failure(
        stage=stage, cycle=4, stable_for=0.0, now=20.6
    )
    assert controller.circuit_floor(now=20.6) == 8.0

    snapshot = controller.snapshot(now=20.6)
    assert snapshot.failure_count_in_window == 4
    assert snapshot.advertisement_wake_count_in_window == 2
    assert snapshot.last_failure_stage == stage
    assert snapshot.last_failure_cycle == 4


def test_circuit_requires_both_failure_and_advertisement_thresholds() -> None:
    controller = make_controller()
    for cycle in range(1, 5):
        controller.record_failure(
            stage="connecting",
            cycle=cycle,
            stable_for=0.0,
            now=float(cycle),
        )

    assert controller.circuit_floor(now=5.0) == 0.0


def test_stable_failure_resets_circuit_and_exponential_sequence() -> None:
    controller = make_controller()
    arm_circuit(controller, now=100.0)
    first = controller.plan_backoff(
        now=100.5, recent_advertisement=False, jitter_factor=1.0
    )
    controller.complete_backoff(
        started_at=100.5,
        now=101.5,
        wake_reason="scheduled_delay",
        advertisement_triggered=False,
    )
    assert first.requested_seconds == 1.0
    assert controller.plan_backoff(
        now=101.5, recent_advertisement=False, jitter_factor=1.0
    ).requested_seconds == 2.0

    controller.record_failure(
        stage="ready", cycle=4, stable_for=30.0, now=102.0
    )

    snapshot = controller.snapshot(now=102.0)
    assert snapshot.failure_count_in_window == 0
    assert snapshot.advertisement_wake_count_in_window == 0
    assert snapshot.current_circuit_floor_seconds == 0.0
    assert controller.plan_backoff(
        now=102.0, recent_advertisement=False, jitter_factor=1.0
    ).requested_seconds == 1.0


def test_exponential_sequence_and_recent_advertisement_cap() -> None:
    controller = make_controller()
    requested: list[float] = []

    for offset in range(7):
        plan = controller.plan_backoff(
            now=float(offset),
            recent_advertisement=False,
            jitter_factor=1.0,
        )
        requested.append(plan.requested_seconds)
        controller.complete_backoff(
            started_at=float(offset),
            now=float(offset),
            wake_reason="scheduled_delay",
            advertisement_triggered=False,
        )

    assert requested == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    capped = controller.plan_backoff(
        now=8.0,
        recent_advertisement=True,
        jitter_factor=1.2,
    )
    assert capped.requested_seconds == 60.0
    assert capped.effective_seconds == 8.0
    assert capped.jittered_seconds == pytest.approx(9.6)
    assert capped.planned_seconds == pytest.approx(9.6)


def test_jitter_bounds_and_active_floor_planning() -> None:
    controller = make_controller()
    arm_circuit(controller, now=200.0)

    planned = controller.plan_backoff(
        now=200.5,
        recent_advertisement=False,
        jitter_factor=0.8,
    )
    assert planned.requested_seconds == 1.0
    assert planned.jittered_seconds == 0.8
    assert planned.floor_seconds == 5.0
    assert planned.planned_seconds == 5.0

    with pytest.raises(ValueError, match="jitter_factor"):
        controller.plan_backoff(
            now=200.5,
            recent_advertisement=False,
            jitter_factor=1.21,
        )
    with pytest.raises(ValueError, match="jitter_factor"):
        controller.plan_backoff(
            now=200.5,
            recent_advertisement=False,
            jitter_factor=0.79,
        )


def test_planning_and_completion_retain_distinct_evidence_times() -> None:
    """A wake enters the rolling window when its scheduled wait completes."""
    controller = make_controller()
    arm_circuit(controller, now=0.0)

    decision = controller.plan_backoff(
        now=120.0,
        recent_advertisement=False,
        jitter_factor=1.0,
    )
    assert isinstance(decision, BackoffDecision)
    assert decision.floor_seconds == 5.0

    controller.complete_backoff(
        started_at=120.0,
        now=125.0,
        wake_reason="fresh_advertisement",
        advertisement_triggered=True,
    )
    at_wake_boundary = controller.snapshot(now=245.0)
    assert at_wake_boundary.failure_count_in_window == 0
    assert at_wake_boundary.advertisement_wake_count_in_window == 1
    assert at_wake_boundary.last_backoff_floor_seconds == 5.0
    assert at_wake_boundary.last_backoff_elapsed_seconds == 5.0

    expired = controller.snapshot(now=245.001)
    assert expired.advertisement_wake_count_in_window == 0


def test_recovery_diagnostics_preserve_keys_and_rounding() -> None:
    controller = make_controller(Model.H7129)
    controller.record_failure(
        stage="negotiating", cycle=7, stable_for=0.12356, now=10.0
    )
    controller.record_cycle(
        started_at=1.0,
        finished_at=2.23456,
        cleanup_succeeded=True,
    )
    controller.plan_backoff(
        now=20.0,
        recent_advertisement=False,
        jitter_factor=1.2,
    )
    controller.complete_backoff(
        started_at=20.0,
        now=20.4567,
        wake_reason="queued_command",
        advertisement_triggered=False,
    )

    assert controller.snapshot(now=20.5).as_dict() == {
        "failure_count_in_window": 1,
        "advertisement_wake_count_in_window": 0,
        "window_seconds": 120.0,
        "failure_threshold": 3,
        "advertisement_wake_threshold": 2,
        "stable_reset_seconds": 30.0,
        "circuit_breaker_active": False,
        "current_circuit_floor_seconds": 0.0,
        "last_failure_stage": "negotiating",
        "last_failure_cycle": 7,
        "last_failure_stable_seconds": 0.124,
        "last_cycle_duration_seconds": 1.235,
        "last_cycle_cleanup_succeeded": True,
        "last_backoff_requested_seconds": 1.0,
        "last_backoff_effective_seconds": 1.0,
        "last_backoff_jittered_seconds": 1.2,
        "last_backoff_floor_seconds": 0.0,
        "last_backoff_planned_seconds": 1.2,
        "last_backoff_elapsed_seconds": 0.457,
        "last_backoff_wake_reason": "queued_command",
    }
