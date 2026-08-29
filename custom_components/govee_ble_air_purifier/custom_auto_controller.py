"""Serialized asyncio actor owning Custom Auto policy lifecycle and I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from .custom_auto_options import CustomAutoOptions
from .custom_auto_policy import (
    CustomAutoPolicyResult,
    confirm_pending_target,
    evaluate_boundary,
    evaluate_observation,
    initial_policy_state,
    invalidate_policy_ownership,
    mark_command_failed,
)
from .models import FanMode
from .observations import (
    AirQualityObservation,
    CommandOrigin,
    FanModeObservation,
    ObservationSource,
)

SampleRequest = Callable[[], Awaitable[None]]
SampleCancel = Callable[[], Awaitable[None]]
FanModeCommand = Callable[[FanMode, CommandOrigin], Awaitable[None]]
SleepUntil = Callable[[float], Awaitable[None]]

_WorkKind = Literal["sample", "timer", "command"]
_ControlAction = Literal[
    "activate", "deactivate", "override", "reconfigure", "shutdown"
]


@dataclass(frozen=True, slots=True)
class _GenerationToken:
    configuration: int
    activation: int
    connection: int


@dataclass(frozen=True, slots=True)
class _Control:
    action: _ControlAction
    future: asyncio.Future[None]
    options: CustomAutoOptions | None = None


@dataclass(frozen=True, slots=True)
class _Connection:
    available: bool
    generation: int


@dataclass(frozen=True, slots=True)
class _Power:
    powered: bool


@dataclass(frozen=True, slots=True)
class _WorkDone:
    kind: _WorkKind
    serial: int
    token: _GenerationToken
    succeeded: bool
    cancelled: bool = False
    target: FanMode | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class FanProvenanceSnapshot:
    """Bounded semantic evidence for the latest fan observation of one kind."""

    mode: FanMode
    source: str
    purpose: str
    revision: int
    connection_generation: int
    command_origin: str | None


@dataclass(frozen=True, slots=True)
class UpshiftSnapshot:
    """Cached positive-delay upshift confirmation evidence."""

    revision: int
    started_at: float
    sample_requested: bool


@dataclass(frozen=True, slots=True)
class DownshiftSnapshot:
    """Cached independently timed downshift evidence."""

    target_level: int
    target_mode: FanMode
    revision: int
    started_at: float
    sample_requested: bool


@dataclass(frozen=True, slots=True)
class CustomAutoSnapshot:
    """Bounded immutable controller state suitable for entities/diagnostics."""

    active: bool
    available: bool
    powered: bool
    suspended: bool
    configuration_generation: int
    activation_generation: int
    connection_generation: int
    confirmed_level: int | None
    confirmed_mode: FanMode | None
    pending_target: FanMode | None
    last_pm25: int | None
    last_pm25_revision: int | None
    last_pm25_connection_generation: int | None
    last_pm25_observed_at: float | None
    pending_upshift: UpshiftSnapshot | None
    pending_downshifts: tuple[DownshiftSnapshot, ...]
    command_state: str
    last_error_type: str | None
    last_physical_fan: FanProvenanceSnapshot | None
    last_command_fan: FanProvenanceSnapshot | None
    auto_redirect_state: str
    controller_listeners: int
    actor_tasks: int
    sample_tasks: int
    timer_tasks: int
    command_tasks: int


StateListener = Callable[[CustomAutoSnapshot], None]


class CustomAutoController:
    """One-owner actor translating authoritative observations into callbacks."""

    _WORK_KINDS: Final = ("sample", "timer", "command")

    def __init__(
        self,
        options: CustomAutoOptions,
        *,
        request_sample: SampleRequest,
        cancel_sample: SampleCancel,
        send_fan_mode: FanModeCommand,
        clock: Callable[[], float],
        sleep_until: SleepUntil | None = None,
    ) -> None:
        if not options.enabled:
            raise ValueError("disabled Custom Auto options cannot own a controller")
        self._options = options
        self._request_sample_callback = request_sample
        self._cancel_sample_callback = cancel_sample
        self._send_fan_mode_callback = send_fan_mode
        self._clock = clock
        self._sleep_until_callback = sleep_until or self._default_sleep_until

        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._actor_task: asyncio.Task[None] | None = None
        self._workers: dict[_WorkKind, tuple[int, asyncio.Task[None]]] = {}
        self._work_serial = 0
        self._listeners: set[StateListener] = set()

        self._active = False
        self._available = False
        self._powered = False
        self._configuration_generation = 0
        self._activation_generation = 0
        self._connection_generation = 0
        self._policy = initial_policy_state(None, options)
        self._sample_barrier_revision: int | None = None
        self._sample_barrier_observed_at: float | None = None
        self._last_seen_air_revision: int | None = None
        self._last_pm25: int | None = None
        self._last_pm25_revision: int | None = None
        self._last_pm25_connection_generation: int | None = None
        self._last_pm25_observed_at: float | None = None
        self._last_physical_fan: FanProvenanceSnapshot | None = None
        self._last_command_fan: FanProvenanceSnapshot | None = None
        self._auto_redirect_state = "idle"
        self._command_state = "idle"
        self._last_error_type: str | None = None

    @property
    def snapshot(self) -> CustomAutoSnapshot:
        """Return cached state without scheduling work or performing I/O."""
        confirmed_mode = (
            self._options.modes[self._policy.confirmed_level - 1]
            if self._policy.confirmed_level is not None
            else None
        )
        return CustomAutoSnapshot(
            active=self._active,
            available=self._available,
            powered=self._powered,
            suspended=self._active and not (self._available and self._powered),
            configuration_generation=self._configuration_generation,
            activation_generation=self._activation_generation,
            connection_generation=self._connection_generation,
            confirmed_level=self._policy.confirmed_level,
            confirmed_mode=confirmed_mode,
            pending_target=self._policy.pending_target,
            last_pm25=self._last_pm25,
            last_pm25_revision=self._last_pm25_revision,
            last_pm25_connection_generation=(
                self._last_pm25_connection_generation
            ),
            last_pm25_observed_at=self._last_pm25_observed_at,
            pending_upshift=(
                UpshiftSnapshot(
                    revision=self._policy.upshift.revision,
                    started_at=self._policy.upshift.started_at,
                    sample_requested=self._policy.upshift.sample_requested,
                )
                if self._policy.upshift is not None
                else None
            ),
            pending_downshifts=tuple(
                DownshiftSnapshot(
                    target_level=dwell.target_level,
                    target_mode=self._options.modes[dwell.target_level - 1],
                    revision=dwell.revision,
                    started_at=dwell.started_at,
                    sample_requested=dwell.sample_requested,
                )
                for dwell in self._policy.downshifts
            ),
            command_state=self._command_state,
            last_error_type=self._last_error_type,
            last_physical_fan=self._last_physical_fan,
            last_command_fan=self._last_command_fan,
            auto_redirect_state=self._auto_redirect_state,
            controller_listeners=len(self._listeners),
            actor_tasks=int(
                self._actor_task is not None and not self._actor_task.done()
            ),
            sample_tasks=int("sample" in self._workers),
            timer_tasks=int("timer" in self._workers),
            command_tasks=int("command" in self._workers),
        )

    def add_state_listener(self, listener: StateListener) -> Callable[[], None]:
        """Register a synchronous cached-state listener and return removal."""
        self._listeners.add(listener)

        def remove() -> None:
            self._listeners.discard(listener)

        return remove

    async def start(self) -> None:
        """Start the single owned actor without activating policy."""
        if self._actor_task is None:
            self._actor_task = asyncio.create_task(
                self._run(), name="govee-custom-auto-actor"
            )

    async def activate(self) -> None:
        """Activate with a fresh-sample barrier and unknown ownership."""
        await self._control("activate")

    async def deactivate(self) -> None:
        """Invalidate active intent and await all operational work."""
        await self._control("deactivate")

    async def ha_override(self) -> None:
        """Atomically yield ownership before a Home Assistant command."""
        await self._control("override")

    async def reconfigure(self, options: CustomAutoOptions) -> None:
        """Invalidate old configuration work and apply enabled options."""
        if not options.enabled:
            raise ValueError("controller reconfiguration must remain enabled")
        await self._control("reconfigure", options)

    async def shutdown(self) -> None:
        """Invalidate intent, cancel all work, and stop the actor."""
        if self._actor_task is None:
            return
        await self._control("shutdown")
        await self._actor_task
        self._actor_task = None

    def set_connection(self, *, available: bool, generation: int) -> None:
        """Synchronously enqueue availability and connection generation."""
        self._enqueue(_Connection(available, generation))

    def set_powered(self, powered: bool) -> None:
        """Synchronously enqueue authoritative power state."""
        self._enqueue(_Power(powered))

    def observe_air_quality(self, observation: AirQualityObservation) -> None:
        """Synchronously enqueue an authoritative air-quality observation."""
        self._enqueue(observation)

    def observe_fan_mode(self, observation: FanModeObservation) -> None:
        """Synchronously enqueue an authoritative fan-mode observation."""
        self._enqueue(observation)

    async def _control(
        self, action: _ControlAction, options: CustomAutoOptions | None = None
    ) -> None:
        if self._actor_task is None or self._actor_task.done():
            raise RuntimeError("Custom Auto controller is not started")
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Control(action, future, options))
        await future

    def _enqueue(self, event: object) -> None:
        if self._actor_task is None or self._actor_task.done():
            raise RuntimeError("Custom Auto controller is not started")
        self._queue.put_nowait(event)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            stop = await self._handle(event)
            self._publish()
            if stop:
                return

    async def _handle(self, event: object) -> bool:
        if isinstance(event, _Control):
            try:
                stop = await self._handle_control(event)
            except Exception as err:
                if not event.future.done():
                    event.future.set_exception(err)
                return False
            if not event.future.done():
                event.future.set_result(None)
            return stop
        if isinstance(event, _Connection):
            await self._handle_connection(event)
        elif isinstance(event, _Power):
            await self._handle_power(event.powered)
        elif isinstance(event, AirQualityObservation):
            await self._handle_air_quality(event)
        elif isinstance(event, FanModeObservation):
            await self._handle_fan_mode(event)
        elif isinstance(event, _WorkDone):
            await self._handle_work_done(event)
        return False

    async def _handle_control(self, event: _Control) -> bool:
        if event.action == "activate":
            self._active = True
            await self._invalidate_operational(reset_policy=True)
            await self._request_fresh_if_usable()
            return False
        if event.action in {"deactivate", "override"}:
            self._active = False
            await self._invalidate_operational(reset_policy=True)
            return False
        if event.action == "reconfigure":
            assert event.options is not None
            self._configuration_generation += 1
            self._options = event.options
            await self._invalidate_operational(reset_policy=True)
            await self._request_fresh_if_usable()
            return False
        self._active = False
        await self._invalidate_operational(reset_policy=True)
        return True

    async def _handle_connection(self, event: _Connection) -> None:
        if event.generation < self._connection_generation:
            return
        changed_generation = event.generation != self._connection_generation
        recovering = event.available and not self._available
        if changed_generation or not event.available or recovering:
            if changed_generation:
                self._last_seen_air_revision = None
            self._connection_generation = event.generation
            self._available = event.available
            await self._invalidate_operational(reset_policy=True)
            await self._request_fresh_if_usable()
            return
        self._available = event.available

    async def _handle_power(self, powered: bool) -> None:
        if powered == self._powered:
            return
        self._powered = powered
        await self._invalidate_operational(reset_policy=True)
        await self._request_fresh_if_usable()

    async def _handle_air_quality(self, observation: AirQualityObservation) -> None:
        if observation.generation != self._connection_generation:
            return
        if (
            self._last_seen_air_revision is not None
            and observation.revision <= self._last_seen_air_revision
        ):
            return
        self._last_seen_air_revision = observation.revision
        if not self._active or not self._available or not self._powered:
            return
        if (
            self._sample_barrier_revision is not None
            and observation.revision <= self._sample_barrier_revision
        ):
            return
        if (
            self._sample_barrier_observed_at is not None
            and observation.observed_at < self._sample_barrier_observed_at
        ):
            return
        if (
            observation.pm25 is not None
            and not isinstance(observation.pm25, bool)
            and 0 <= observation.pm25 <= 999
        ):
            self._last_pm25 = observation.pm25
            self._last_pm25_revision = observation.revision
            self._last_pm25_connection_generation = observation.generation
            self._last_pm25_observed_at = observation.observed_at
        result = evaluate_observation(
            self._policy,
            self._options,
            pm25=observation.pm25,
            revision=observation.revision,
            observed_at=observation.observed_at,
        )
        if result.state == self._policy:
            return
        self._policy = result.state
        await self._apply_policy_result(result)

    async def _handle_fan_mode(self, observation: FanModeObservation) -> None:
        if observation.generation != self._connection_generation:
            return
        provenance = FanProvenanceSnapshot(
            mode=observation.mode,
            source=observation.source.value,
            purpose=observation.purpose.value,
            revision=observation.revision,
            connection_generation=observation.generation,
            command_origin=(
                observation.command_origin.value
                if observation.command_origin is not None
                else None
            ),
        )
        if observation.source is ObservationSource.PHYSICAL:
            self._last_physical_fan = provenance
        elif observation.source is ObservationSource.COMMAND:
            self._last_command_fan = provenance
        if not self._active:
            return
        if observation.source is ObservationSource.PHYSICAL:
            if observation.mode is FanMode.AUTO:
                self._auto_redirect_state = "pending"
                await self._invalidate_operational(reset_policy=True)
                await self._request_fresh_if_usable()
            else:
                self._auto_redirect_state = "idle"
                self._active = False
                await self._invalidate_operational(reset_policy=True)
            return
        # Command observations remain useful upstream diagnostics, but policy
        # confirmation belongs exclusively to this actor's serial/token-matched
        # worker completion. A late acknowledgement from cancelled work cannot
        # establish ownership for a replacement target.

    async def _handle_work_done(self, event: _WorkDone) -> None:
        current = self._workers.get(event.kind)
        if current is None or current[0] != event.serial:
            return
        self._workers.pop(event.kind, None)
        if event.cancelled or event.token != self._token():
            return
        if event.kind == "timer":
            result = evaluate_boundary(
                self._policy, self._options, now=self._clock()
            )
            self._policy = result.state
            await self._apply_policy_result(result)
            return
        if event.kind == "sample":
            if not event.succeeded:
                self._last_error_type = event.error_type
                if self._auto_redirect_state == "pending":
                    self._auto_redirect_state = "failed"
            return
        if event.target is not self._policy.pending_target:
            self._schedule_pending_command_if_usable()
            return
        if event.succeeded:
            assert event.target is not None
            self._policy = confirm_pending_target(
                self._policy, event.target, self._options
            )
            self._command_state = "confirmed"
            self._last_error_type = None
            if self._auto_redirect_state == "pending":
                self._auto_redirect_state = "confirmed"
            boundary = evaluate_boundary(
                self._policy, self._options, now=self._clock()
            )
            self._policy = boundary.state
            await self._apply_policy_result(boundary)
            return
        self._policy = mark_command_failed(self._policy)
        self._command_state = "failed"
        self._last_error_type = event.error_type
        if self._auto_redirect_state == "pending":
            self._auto_redirect_state = "failed"

    def _schedule_pending_command_if_usable(self) -> None:
        """Ensure a changed pending target cannot strand after old work exits."""
        target = self._policy.pending_target
        if (
            target is None
            or "command" in self._workers
            or not self._active
            or not self._available
            or not self._powered
        ):
            return
        self._command_state = "sending"
        self._last_error_type = None
        self._spawn(
            "command",
            lambda: self._send_fan_mode_callback(
                target, CommandOrigin.CUSTOM_AUTO
            ),
            target=target,
        )

    async def _apply_policy_result(self, result: CustomAutoPolicyResult) -> None:
        await self._cancel_kinds("timer")
        if result.target is not None and "command" not in self._workers:
            target = result.target
            self._command_state = "sending"
            self._last_error_type = None
            self._spawn(
                "command",
                lambda: self._send_fan_mode_callback(
                    target, CommandOrigin.CUSTOM_AUTO
                ),
                target=target,
            )
        if result.request_sample:
            self._request_sample()
        if result.next_boundary_at is not None:
            deadline = result.next_boundary_at
            self._spawn(
                "timer",
                lambda: self._sleep_until_callback(deadline),
            )

    async def _invalidate_operational(self, *, reset_policy: bool) -> None:
        self._activation_generation += 1
        self._sample_barrier_revision = self._last_seen_air_revision
        self._sample_barrier_observed_at = self._clock()
        await self._cancel_kinds(*self._WORK_KINDS)
        if reset_policy:
            self._policy = initial_policy_state(None, self._options)
        else:
            self._policy = invalidate_policy_ownership(self._policy)
        self._command_state = "idle"
        self._last_error_type = None

    async def _request_fresh_if_usable(self) -> None:
        if self._active and self._available and self._powered:
            self._request_sample()

    def _request_sample(self) -> None:
        current = self._workers.get("sample")
        if current is not None and not current[1].done():
            return
        if current is not None:
            self._workers.pop("sample", None)
        self._spawn("sample", self._request_sample_callback)

    def _spawn(
        self,
        kind: _WorkKind,
        awaitable_factory: Callable[[], Awaitable[None]],
        *,
        target: FanMode | None = None,
    ) -> None:
        if kind in self._workers:
            return
        self._work_serial += 1
        serial = self._work_serial
        token = self._token()

        async def run() -> None:
            try:
                await awaitable_factory()
            except asyncio.CancelledError:
                self._queue.put_nowait(
                    _WorkDone(kind, serial, token, False, cancelled=True, target=target)
                )
                raise
            except Exception as err:  # Callback failures are actor state, not leaks.
                self._queue.put_nowait(
                    _WorkDone(
                        kind,
                        serial,
                        token,
                        False,
                        target=target,
                        error_type=type(err).__name__,
                    )
                )
            else:
                self._queue.put_nowait(
                    _WorkDone(kind, serial, token, True, target=target)
                )

        task = asyncio.create_task(run(), name=f"govee-custom-auto-{kind}")
        self._workers[kind] = (serial, task)

    async def _cancel_kinds(self, *kinds: _WorkKind) -> None:
        tasks: list[asyncio.Task[None]] = []
        for kind in kinds:
            current = self._workers.pop(kind, None)
            if current is not None:
                if kind == "sample":
                    try:
                        await self._cancel_sample_callback()
                    except Exception as err:
                        self._last_error_type = type(err).__name__
                current[1].cancel()
                tasks.append(current[1])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _token(self) -> _GenerationToken:
        return _GenerationToken(
            self._configuration_generation,
            self._activation_generation,
            self._connection_generation,
        )

    async def _default_sleep_until(self, deadline: float) -> None:
        await asyncio.sleep(max(0.0, deadline - self._clock()))

    def _publish(self) -> None:
        snapshot = self.snapshot
        for listener in tuple(self._listeners):
            try:
                listener(snapshot)
            except Exception:
                # Entity listeners consume cached state and cannot own or stop
                # the controller actor.
                continue


__all__ = (
    "CustomAutoController",
    "CustomAutoSnapshot",
    "DownshiftSnapshot",
    "FanProvenanceSnapshot",
    "UpshiftSnapshot",
)
