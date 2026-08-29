"""Exhaustive deterministic tests for the pure Custom Auto policy."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    parse_custom_auto_options,
)
from custom_components.govee_ble_air_purifier.custom_auto_policy import (
    confirm_pending_target,
    evaluate_boundary,
    evaluate_observation,
    initial_policy_state,
    invalidate_policy_ownership,
    level_for_pm25,
    mark_command_failed,
    rebase_confirmed_mode,
)
from custom_components.govee_ble_air_purifier.models import FanMode, Model
from custom_components.govee_ble_air_purifier.profiles import get_profile_registry


def _options(
    model: Model = Model.H7124, overrides: dict[str, object] | None = None
):
    defaults = get_profile_registry().for_model(model).custom_auto_defaults
    return parse_custom_auto_options(
        {CONF_CUSTOM_AUTO_ENABLED: True, **(overrides or {})}, defaults
    )


@pytest.mark.parametrize(
    ("model", "boundaries"),
    [(Model.H7124, (3, 5, 9, 15)), (Model.H7129, (7, 9, 13, 19))],
)
def test_both_baselines_and_boundary_equality_select_lower_band(
    model: Model, boundaries: tuple[int, ...]
) -> None:
    options = _options(model)
    for level, boundary in enumerate(boundaries, start=1):
        assert level_for_pm25(boundary, options) == level
        assert level_for_pm25(boundary + 1, options) == level + 1


def test_positive_confirmation_uses_distinct_revisions_and_confirming_target() -> None:
    options = _options()
    state = initial_policy_state(FanMode.SLEEP, options)
    first = evaluate_observation(state, options, pm25=6, revision=1, observed_at=10)
    early = evaluate_observation(
        first.state, options, pm25=20, revision=2, observed_at=12
    )
    confirmed = evaluate_observation(
        early.state, options, pm25=10, revision=3, observed_at=13
    )

    assert first.target is early.target is None
    assert confirmed.target is FanMode.HIGH


def test_equal_values_with_distinct_revisions_confirm_stale_do_not() -> None:
    options = _options()
    state = initial_policy_state(FanMode.SLEEP, options)
    first = evaluate_observation(state, options, pm25=4, revision=10, observed_at=0)
    stale = evaluate_observation(
        first.state, options, pm25=4, revision=10, observed_at=3
    )
    confirmed = evaluate_observation(
        stale.state, options, pm25=4, revision=11, observed_at=3
    )

    assert stale.state == first.state
    assert confirmed.target is FanMode.LOW


def test_zero_confirmation_allows_first_reading_and_direct_jump() -> None:
    options = _options(
        overrides={CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0}
    )
    state = initial_policy_state(FanMode.SLEEP, options)

    result = evaluate_observation(state, options, pm25=999, revision=1, observed_at=0)

    assert result.target is FanMode.TURBO
    assert result.state.confirmed_level == 1
    assert result.state.pending_target is FanMode.TURBO


@pytest.mark.parametrize(
    ("current", "pm25", "delay", "target"),
    [
        (FanMode.LOW, 0, 7, FanMode.SLEEP),
        (FanMode.MEDIUM, 4, 5, FanMode.LOW),
        (FanMode.HIGH, 6, 5, FanMode.MEDIUM),
        (FanMode.TURBO, 10, 5, FanMode.HIGH),
    ],
)
def test_each_legacy_downshift_delay_requires_a_fresh_mature_sample(
    current: FanMode, pm25: int, delay: int, target: FanMode
) -> None:
    options = _options()
    state = initial_policy_state(current, options)
    started = evaluate_observation(
        state, options, pm25=pm25, revision=1, observed_at=0
    )
    before = evaluate_observation(
        started.state,
        options,
        pm25=pm25,
        revision=2,
        observed_at=delay * 60 - 0.1,
    )
    mature = evaluate_observation(
        before.state, options, pm25=pm25, revision=3, observed_at=delay * 60
    )

    assert started.target is before.target is None
    assert mature.target is target


def test_independent_downward_boundaries_emit_the_lowest_mature_target() -> None:
    options = _options(
        overrides={CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [7, 5, 5, 5]}
    )
    state = initial_policy_state(FanMode.TURBO, options)
    started = evaluate_observation(state, options, pm25=0, revision=1, observed_at=0)
    five_minutes = evaluate_observation(
        started.state, options, pm25=0, revision=2, observed_at=300
    )
    confirmed_low = confirm_pending_target(
        five_minutes.state, FanMode.LOW, options
    )
    seven_minutes = evaluate_observation(
        confirmed_low, options, pm25=0, revision=3, observed_at=420
    )

    assert five_minutes.target is FanMode.LOW
    assert seven_minutes.target is FanMode.SLEEP


def test_pending_command_sample_cancels_incompatible_retained_dwell() -> None:
    options = _options()
    state = initial_policy_state(FanMode.TURBO, options)
    started = evaluate_observation(state, options, pm25=0, revision=1, observed_at=0)
    emitted_low = evaluate_observation(
        started.state, options, pm25=0, revision=2, observed_at=300
    )
    assert emitted_low.target is FanMode.LOW
    assert [dwell.target_level for dwell in emitted_low.state.downshifts] == [1]

    medium_air = evaluate_observation(
        emitted_low.state, options, pm25=6, revision=3, observed_at=360
    )
    assert medium_air.target is None
    assert medium_air.state.downshifts == ()

    confirmed_low = confirm_pending_target(
        medium_air.state, FanMode.LOW, options
    )
    seven_minutes = evaluate_observation(
        confirmed_low, options, pm25=0, revision=4, observed_at=420
    )

    assert seven_minutes.target is None
    assert len(seven_minutes.state.downshifts) == 1
    assert seven_minutes.state.downshifts[0].started_at == 420


def test_dirtier_air_cancels_only_incompatible_downshifts() -> None:
    options = _options()
    state = initial_policy_state(FanMode.TURBO, options)
    clean = evaluate_observation(state, options, pm25=0, revision=1, observed_at=0)
    dirtier = evaluate_observation(
        clean.state, options, pm25=6, revision=2, observed_at=100
    )

    assert [item.target_level for item in dirtier.state.downshifts] == [3, 4]


def test_boundary_events_request_once_and_never_emit_commands() -> None:
    options = _options()
    state = initial_policy_state(FanMode.SLEEP, options)
    pending = evaluate_observation(state, options, pm25=4, revision=1, observed_at=0)
    at_boundary = evaluate_boundary(pending.state, options, now=3)
    repeated = evaluate_boundary(at_boundary.state, options, now=4)

    assert at_boundary.request_sample is True
    assert at_boundary.target is None
    assert repeated.request_sample is False
    assert repeated.target is None


@pytest.mark.parametrize("pm25", [None, -1, 1000, True])
def test_invalid_or_missing_values_do_nothing(pm25: int | None) -> None:
    options = _options()
    state = initial_policy_state(FanMode.LOW, options)

    result = evaluate_observation(state, options, pm25=pm25, revision=1, observed_at=0)

    assert result.state == state
    assert result.target is None


def test_overrides_drive_policy_and_pending_targets_are_deduplicated() -> None:
    options = _options(
        overrides={
            CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [10, 20, 30, 40],
            CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0,
        }
    )
    state = initial_policy_state(FanMode.SLEEP, options)
    first = evaluate_observation(state, options, pm25=35, revision=1, observed_at=0)
    duplicate = evaluate_observation(
        first.state, options, pm25=35, revision=2, observed_at=1
    )

    assert first.target is FanMode.HIGH
    assert duplicate.target is None
    assert duplicate.state.pending_target is FanMode.HIGH
    assert duplicate.state.confirmed_level == 1


@pytest.mark.parametrize("mode", [None, FanMode.AUTO])
def test_unknown_or_hardware_auto_activation_immediately_maps_first_sample(
    mode: FanMode | None,
) -> None:
    options = _options()
    state = initial_policy_state(mode, options)

    result = evaluate_observation(state, options, pm25=6, revision=1, observed_at=0)

    assert result.target is FanMode.MEDIUM
    assert result.state.confirmed_level is None
    assert result.state.pending_target is FanMode.MEDIUM


def test_ownership_invalidation_allows_same_target_physical_auto_redirect() -> None:
    options = _options(
        overrides={CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0}
    )
    state = initial_policy_state(FanMode.LOW, options)
    first = evaluate_observation(state, options, pm25=4, revision=1, observed_at=0)
    assert first.target is None

    invalidated = invalidate_policy_ownership(first.state)
    redirected = evaluate_observation(
        invalidated, options, pm25=4, revision=2, observed_at=1
    )

    assert redirected.target is FanMode.LOW
    assert redirected.state.confirmed_level is None


def test_exact_confirmation_advances_only_confirmed_level() -> None:
    options = _options(
        overrides={CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0}
    )
    state = initial_policy_state(FanMode.SLEEP, options)
    emitted = evaluate_observation(state, options, pm25=10, revision=1, observed_at=0)

    assert emitted.state.confirmed_level == 1
    confirmed = confirm_pending_target(emitted.state, FanMode.HIGH, options)

    assert confirmed.confirmed_level == 4
    assert confirmed.pending_target is None


def test_command_failure_waits_for_new_revision_before_retry() -> None:
    options = _options(
        overrides={CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 0}
    )
    state = initial_policy_state(FanMode.SLEEP, options)
    emitted = evaluate_observation(state, options, pm25=10, revision=4, observed_at=0)
    failed = mark_command_failed(emitted.state)
    stale = evaluate_observation(failed, options, pm25=10, revision=4, observed_at=1)
    retried = evaluate_observation(
        stale.state, options, pm25=10, revision=5, observed_at=2
    )

    assert stale.target is None
    assert stale.state == failed
    assert retried.target is FanMode.HIGH
    assert retried.state.confirmed_level == 1


def test_rebase_clears_pending_ownership_and_hysteresis() -> None:
    options = _options()
    state = initial_policy_state(FanMode.SLEEP, options)
    pending = evaluate_observation(state, options, pm25=6, revision=1, observed_at=0)

    rebased = rebase_confirmed_mode(pending.state, FanMode.TURBO, options)

    assert rebased.confirmed_level == 5
    assert rebased.pending_target is None
    assert rebased.upshift is None
