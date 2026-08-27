"""Deterministic recovery policy and diagnostic evidence."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .profiles import TimingProfile

RECOVERY_EVENT_HISTORY_LIMIT = 32
MIN_JITTER_FACTOR = 0.8
MAX_JITTER_FACTOR = 1.2


@dataclass(frozen=True, slots=True)
class BackoffDecision:
    """One immutable delay decision for the client's async scheduler."""

    requested_seconds: float
    effective_seconds: float
    jittered_seconds: float
    floor_seconds: float
    planned_seconds: float


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Immutable secret-free evidence for one recovery controller."""

    failure_count_in_window: int
    advertisement_wake_count_in_window: int
    window_seconds: float
    failure_threshold: int
    advertisement_wake_threshold: int
    stable_reset_seconds: float
    circuit_breaker_active: bool
    current_circuit_floor_seconds: float
    last_failure_stage: str | None
    last_failure_cycle: int | None
    last_failure_stable_seconds: float
    last_cycle_duration_seconds: float | None
    last_cycle_cleanup_succeeded: bool | None
    last_backoff_requested_seconds: float | None
    last_backoff_effective_seconds: float | None
    last_backoff_jittered_seconds: float | None
    last_backoff_floor_seconds: float
    last_backoff_planned_seconds: float | None
    last_backoff_elapsed_seconds: float | None
    last_backoff_wake_reason: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the existing recovery diagnostics subtree."""
        return {
            "failure_count_in_window": self.failure_count_in_window,
            "advertisement_wake_count_in_window": (
                self.advertisement_wake_count_in_window
            ),
            "window_seconds": self.window_seconds,
            "failure_threshold": self.failure_threshold,
            "advertisement_wake_threshold": self.advertisement_wake_threshold,
            "stable_reset_seconds": self.stable_reset_seconds,
            "circuit_breaker_active": self.circuit_breaker_active,
            "current_circuit_floor_seconds": self.current_circuit_floor_seconds,
            "last_failure_stage": self.last_failure_stage,
            "last_failure_cycle": self.last_failure_cycle,
            "last_failure_stable_seconds": self.last_failure_stable_seconds,
            "last_cycle_duration_seconds": self.last_cycle_duration_seconds,
            "last_cycle_cleanup_succeeded": self.last_cycle_cleanup_succeeded,
            "last_backoff_requested_seconds": self.last_backoff_requested_seconds,
            "last_backoff_effective_seconds": self.last_backoff_effective_seconds,
            "last_backoff_jittered_seconds": self.last_backoff_jittered_seconds,
            "last_backoff_floor_seconds": self.last_backoff_floor_seconds,
            "last_backoff_planned_seconds": self.last_backoff_planned_seconds,
            "last_backoff_elapsed_seconds": self.last_backoff_elapsed_seconds,
            "last_backoff_wake_reason": self.last_backoff_wake_reason,
        }


class RecoveryController:
    """Own integration-specific recovery decisions without async scheduling."""

    def __init__(self, timings: TimingProfile) -> None:
        self.timings = timings
        self._failure_times: deque[float] = deque(
            maxlen=RECOVERY_EVENT_HISTORY_LIMIT
        )
        self._advertisement_wake_times: deque[float] = deque(
            maxlen=RECOVERY_EVENT_HISTORY_LIMIT
        )
        self._requested_backoff = timings.backoff_initial
        self._last_failure_stage: str | None = None
        self._last_failure_cycle: int | None = None
        self._last_failure_stable_seconds = 0.0
        self._last_cycle_duration_seconds: float | None = None
        self._last_cycle_cleanup_succeeded: bool | None = None
        self._last_backoff_requested_seconds: float | None = None
        self._last_backoff_effective_seconds: float | None = None
        self._last_backoff_jittered_seconds: float | None = None
        self._last_backoff_floor_seconds = 0.0
        self._last_backoff_planned_seconds: float | None = None
        self._last_backoff_elapsed_seconds: float | None = None
        self._last_backoff_wake_reason: str | None = None

    def begin_sequence(self) -> None:
        """Start a new client runner at the configured one-second base."""
        self._requested_backoff = self.timings.backoff_initial

    def reset_after_stable_session(self) -> None:
        """Forget rolling circuit evidence after durable READY operation."""
        self._failure_times.clear()
        self._advertisement_wake_times.clear()

    def record_failure(
        self,
        *,
        stage: str,
        cycle: int,
        stable_for: float,
        now: float,
    ) -> None:
        """Record one failed connection cycle and apply stable-session reset."""
        stable_seconds = max(0.0, stable_for)
        self._last_failure_stage = stage
        self._last_failure_cycle = cycle
        self._last_failure_stable_seconds = round(stable_seconds, 3)
        if stable_seconds >= self.timings.backoff_reset_after:
            self.reset_after_stable_session()
            self._requested_backoff = self.timings.backoff_initial
            return
        self._trim_window(now)
        self._failure_times.append(now)

    def record_advertisement_wake(self, *, now: float) -> None:
        """Record fresh radio evidence that ended a scheduled wait."""
        self._trim_window(now)
        self._advertisement_wake_times.append(now)

    def record_cycle(
        self,
        *,
        started_at: float,
        finished_at: float,
        cleanup_succeeded: bool,
    ) -> None:
        """Retain the most recent connection-cycle cleanup outcome."""
        self._last_cycle_duration_seconds = round(
            max(0.0, finished_at - started_at), 3
        )
        self._last_cycle_cleanup_succeeded = cleanup_succeeded

    def circuit_floor(self, *, now: float) -> float:
        """Return the active floor for repeated unstable recovery cycles."""
        self._trim_window(now)
        failures = len(self._failure_times)
        advertisement_wakes = len(self._advertisement_wake_times)
        if (
            failures < self.timings.recovery_storm_failure_threshold
            or advertisement_wakes
            < self.timings.recovery_storm_advertisement_threshold
        ):
            return 0.0
        if failures == self.timings.recovery_storm_failure_threshold:
            return self.timings.recovery_storm_initial_floor
        return self.timings.recovery_storm_max_floor

    def plan_backoff(
        self,
        *,
        now: float,
        recent_advertisement: bool,
        jitter_factor: float,
    ) -> BackoffDecision:
        """Select, jitter, and floor the next recovery delay deterministically."""
        if not MIN_JITTER_FACTOR <= jitter_factor <= MAX_JITTER_FACTOR:
            raise ValueError(
                "jitter_factor must be between "
                f"{MIN_JITTER_FACTOR} and {MAX_JITTER_FACTOR}"
            )
        requested = self._requested_backoff
        effective = (
            min(requested, self.timings.recent_advertisement_backoff_max)
            if recent_advertisement
            else requested
        )
        jittered = effective * jitter_factor
        floor = self.circuit_floor(now=now)
        planned = max(jittered, floor)
        decision = BackoffDecision(
            requested_seconds=requested,
            effective_seconds=effective,
            jittered_seconds=jittered,
            floor_seconds=floor,
            planned_seconds=planned,
        )
        self._last_backoff_requested_seconds = requested
        self._last_backoff_effective_seconds = effective
        self._last_backoff_jittered_seconds = round(jittered, 3)
        self._last_backoff_floor_seconds = floor
        self._last_backoff_planned_seconds = round(planned, 3)
        return decision

    def complete_backoff(
        self,
        *,
        started_at: float,
        now: float,
        wake_reason: str,
        advertisement_triggered: bool,
    ) -> None:
        """Record wait evidence and advance the capped exponential sequence."""
        if advertisement_triggered:
            self.record_advertisement_wake(now=now)
        self._last_backoff_elapsed_seconds = round(max(0.0, now - started_at), 3)
        self._last_backoff_wake_reason = wake_reason
        self._requested_backoff = min(
            self.timings.backoff_max,
            self._requested_backoff * 2,
        )

    def snapshot(self, *, now: float) -> RecoverySnapshot:
        """Return the diagnostic subtree state at an explicit monotonic time."""
        floor = self.circuit_floor(now=now)
        return RecoverySnapshot(
            failure_count_in_window=len(self._failure_times),
            advertisement_wake_count_in_window=len(
                self._advertisement_wake_times
            ),
            window_seconds=self.timings.recovery_storm_window,
            failure_threshold=self.timings.recovery_storm_failure_threshold,
            advertisement_wake_threshold=(
                self.timings.recovery_storm_advertisement_threshold
            ),
            stable_reset_seconds=self.timings.backoff_reset_after,
            circuit_breaker_active=floor > 0,
            current_circuit_floor_seconds=floor,
            last_failure_stage=self._last_failure_stage,
            last_failure_cycle=self._last_failure_cycle,
            last_failure_stable_seconds=self._last_failure_stable_seconds,
            last_cycle_duration_seconds=self._last_cycle_duration_seconds,
            last_cycle_cleanup_succeeded=self._last_cycle_cleanup_succeeded,
            last_backoff_requested_seconds=self._last_backoff_requested_seconds,
            last_backoff_effective_seconds=self._last_backoff_effective_seconds,
            last_backoff_jittered_seconds=self._last_backoff_jittered_seconds,
            last_backoff_floor_seconds=self._last_backoff_floor_seconds,
            last_backoff_planned_seconds=self._last_backoff_planned_seconds,
            last_backoff_elapsed_seconds=self._last_backoff_elapsed_seconds,
            last_backoff_wake_reason=self._last_backoff_wake_reason,
        )

    def _trim_window(self, now: float) -> None:
        cutoff = now - self.timings.recovery_storm_window
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()
        while (
            self._advertisement_wake_times
            and self._advertisement_wake_times[0] < cutoff
        ):
            self._advertisement_wake_times.popleft()
