"""Immutable synchronous hysteresis policy for integration-managed Custom Auto."""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, replace

from .custom_auto_options import CustomAutoOptions
from .models import FanMode


@dataclass(frozen=True, slots=True)
class UpshiftConfirmation:
    """The first authoritative revision in a positive-delay upshift."""

    revision: int
    started_at: float
    sample_requested: bool = False


@dataclass(frozen=True, slots=True)
class DownshiftDwell:
    """A pending crossing of one independently timed downward boundary."""

    target_level: int
    revision: int
    started_at: float
    sample_requested: bool = False


@dataclass(frozen=True, slots=True)
class CustomAutoPolicyState:
    """All state needed to evaluate the next event without side effects."""

    confirmed_level: int | None
    last_revision: int | None = None
    upshift: UpshiftConfirmation | None = None
    downshifts: tuple[DownshiftDwell, ...] = ()
    pending_target: FanMode | None = None
    command_failed_revision: int | None = None


@dataclass(frozen=True, slots=True)
class CustomAutoPolicyResult:
    """A transition result; callers own any sampling or command I/O."""

    state: CustomAutoPolicyState
    target: FanMode | None = None
    request_sample: bool = False
    next_boundary_at: float | None = None


def initial_policy_state(
    mode: FanMode | None, options: CustomAutoOptions
) -> CustomAutoPolicyState:
    """Create state from a confirmed mode, or unknown/Auto ownership."""
    level = _level_for_mode(mode, options)
    return CustomAutoPolicyState(confirmed_level=level)


def invalidate_policy_ownership(
    state: CustomAutoPolicyState,
) -> CustomAutoPolicyState:
    """Forget hardware ownership and all command/timer assumptions."""
    return replace(
        state,
        confirmed_level=None,
        upshift=None,
        downshifts=(),
        pending_target=None,
        command_failed_revision=None,
    )


def rebase_confirmed_mode(
    state: CustomAutoPolicyState,
    mode: FanMode | None,
    options: CustomAutoOptions,
) -> CustomAutoPolicyState:
    """Rebase policy on an independently confirmed hardware mode."""
    return replace(
        state,
        confirmed_level=_level_for_mode(mode, options),
        upshift=None,
        downshifts=(),
        pending_target=None,
        command_failed_revision=None,
    )


def confirm_pending_target(
    state: CustomAutoPolicyState,
    mode: FanMode,
    options: CustomAutoOptions,
) -> CustomAutoPolicyState:
    """Promote an exact pending command acknowledgement to confirmed state."""
    if state.pending_target is None or mode is not state.pending_target:
        raise ValueError("confirmed mode does not match the pending target")
    level = _level_for_mode(mode, options)
    if level is None:  # Defensive: pending targets can never be hardware Auto.
        raise ValueError("Custom Auto cannot confirm an uncontrolled mode")
    return replace(
        state,
        confirmed_level=level,
        pending_target=None,
        command_failed_revision=None,
        downshifts=tuple(
            dwell for dwell in state.downshifts if dwell.target_level < level
        ),
    )


def mark_command_failed(state: CustomAutoPolicyState) -> CustomAutoPolicyState:
    """Release a failed pending target but require a newer sample to retry."""
    if state.pending_target is None:
        return state
    return replace(
        state,
        pending_target=None,
        command_failed_revision=state.last_revision,
    )


def _level_for_mode(
    mode: FanMode | None, options: CustomAutoOptions
) -> int | None:
    if mode is None or mode is FanMode.AUTO:
        return None
    try:
        return options.modes.index(mode) + 1
    except ValueError as err:
        raise ValueError("Custom Auto requires a Sleep through Turbo mode") from err


def level_for_pm25(pm25: int, options: CustomAutoOptions) -> int:
    """Map PM2.5 to a one-based level; equality remains in the lower band."""
    if isinstance(pm25, bool) or not isinstance(pm25, int) or not 0 <= pm25 <= 999:
        raise ValueError("PM2.5 must be an integer from 0 through 999")
    return bisect_left(options.pm25_boundaries, pm25) + 1


def _valid_time(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _next_boundary(
    state: CustomAutoPolicyState, options: CustomAutoOptions
) -> float | None:
    boundaries: list[float] = []
    if state.upshift is not None and not state.upshift.sample_requested:
        boundaries.append(
            state.upshift.started_at + options.upshift_confirmation_seconds
        )
    boundaries.extend(
        dwell.started_at
        + options.downshift_delays_minutes[dwell.target_level - 1] * 60
        for dwell in state.downshifts
        if not dwell.sample_requested
    )
    return min(boundaries, default=None)


def _result(
    state: CustomAutoPolicyState,
    options: CustomAutoOptions,
    *,
    target: FanMode | None = None,
    request_sample: bool = False,
) -> CustomAutoPolicyResult:
    return CustomAutoPolicyResult(
        state=state,
        target=target,
        request_sample=request_sample,
        next_boundary_at=_next_boundary(state, options),
    )


def evaluate_boundary(
    state: CustomAutoPolicyState,
    options: CustomAutoOptions,
    *,
    now: float,
) -> CustomAutoPolicyResult:
    """Mark mature timers and request at most one sample, never a command."""
    if not options.enabled or not _valid_time(now):
        return _result(state, options)
    request_sample = False
    upshift = state.upshift
    if (
        upshift is not None
        and not upshift.sample_requested
        and now >= upshift.started_at + options.upshift_confirmation_seconds
    ):
        upshift = replace(upshift, sample_requested=True)
        request_sample = True

    downshifts: list[DownshiftDwell] = []
    for dwell in state.downshifts:
        maturity = (
            dwell.started_at
            + options.downshift_delays_minutes[dwell.target_level - 1] * 60
        )
        if not dwell.sample_requested and now >= maturity:
            dwell = replace(dwell, sample_requested=True)
            request_sample = True
        downshifts.append(dwell)
    updated = replace(state, upshift=upshift, downshifts=tuple(downshifts))
    return _result(updated, options, request_sample=request_sample)


def evaluate_observation(
    state: CustomAutoPolicyState,
    options: CustomAutoOptions,
    *,
    pm25: int | None,
    revision: int,
    observed_at: float,
) -> CustomAutoPolicyResult:
    """Evaluate one fresh authoritative PM2.5 revision."""
    if (
        not options.enabled
        or isinstance(pm25, bool)
        or not isinstance(pm25, int)
        or not 0 <= pm25 <= 999
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or (state.last_revision is not None and revision <= state.last_revision)
        or not _valid_time(observed_at)
    ):
        return _result(state, options)

    desired = level_for_pm25(pm25, options)
    state = replace(state, last_revision=revision)
    if state.pending_target is not None:
        # A command in flight suppresses additional commands, not fresh-sample
        # hysteresis maintenance. Retain only downward crossings that the
        # latest air quality still justifies; never let a cleaner old sample
        # survive a dirtier reading and fire after command confirmation.
        justified_downshifts = tuple(
            dwell
            for dwell in state.downshifts
            if desired <= dwell.target_level
        )
        return _result(
            replace(state, downshifts=justified_downshifts), options
        )

    confirmed_level = state.confirmed_level
    if confirmed_level is None:
        return _emit(state, options, desired)

    if state.command_failed_revision is not None:
        state = replace(state, command_failed_revision=None)
        if desired != confirmed_level:
            return _emit(state, options, desired)
        return _result(replace(state, upshift=None, downshifts=()), options)

    if desired > confirmed_level:
        state = replace(state, downshifts=())
        delay = options.upshift_confirmation_seconds
        if delay == 0:
            return _emit(state, options, desired)
        pending = state.upshift
        if pending is None:
            pending = UpshiftConfirmation(revision, float(observed_at))
            return _result(replace(state, upshift=pending), options)
        if observed_at >= pending.started_at + delay:
            return _emit(state, options, desired)
        return _result(state, options)

    state = replace(state, upshift=None)
    if desired == confirmed_level:
        return _result(replace(state, downshifts=()), options)

    # Keep only crossings still justified by this reading, and start any new
    # crossings independently from the same authoritative revision.
    existing = {dwell.target_level: dwell for dwell in state.downshifts}
    downshifts = tuple(
        existing.get(level, DownshiftDwell(level, revision, float(observed_at)))
        for level in range(desired, confirmed_level)
    )
    matured = [
        dwell.target_level
        for dwell in downshifts
        if revision > dwell.revision
        and observed_at
        >= dwell.started_at
        + options.downshift_delays_minutes[dwell.target_level - 1] * 60
    ]
    state = replace(state, downshifts=downshifts)
    if matured:
        return _emit(state, options, min(matured))
    return _result(state, options)


def _emit(
    state: CustomAutoPolicyState, options: CustomAutoOptions, level: int
) -> CustomAutoPolicyResult:
    target = options.modes[level - 1]
    remaining_downshifts = (
        tuple(
            dwell for dwell in state.downshifts if dwell.target_level < level
        )
        if state.confirmed_level is not None and level < state.confirmed_level
        else ()
    )
    updated = replace(
        state,
        upshift=None,
        downshifts=remaining_downshifts,
        pending_target=target,
        command_failed_revision=None,
    )
    return _result(updated, options, target=target)
