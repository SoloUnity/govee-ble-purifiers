"""Tests for integration-owned Custom Auto memory."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.core import CoreState

from custom_components.govee_ble_air_purifier import custom_auto_memory
from custom_components.govee_ble_air_purifier.custom_auto_memory import (
    CustomAutoMemory,
    CustomAutoMemoryError,
)


class FakeStore:
    """Controllable public Store boundary with shared durable content."""

    durable: object = None
    load_error: Exception | None = None
    save_error: Exception | None = None
    remove_error: Exception | None = None
    suppress_save = False
    suppress_remove = False
    save_started: asyncio.Event | None = None
    allow_save: asyncio.Event | None = None
    remove_started: asyncio.Event | None = None
    allow_remove: asyncio.Event | None = None
    load_started: asyncio.Event | None = None
    allow_load: asyncio.Event | None = None
    mutation_delay = 0.0
    mutations = 0
    max_mutations = 0
    instances: list[FakeStore] = []
    saved_payloads: list[object] = []

    def __init__(
        self,
        hass: object,
        version: int,
        key: str,
        *,
        atomic_writes: bool,
    ) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.atomic_writes = atomic_writes
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        """Reset shared fake storage and controls."""
        cls.durable = None
        cls.load_error = None
        cls.save_error = None
        cls.remove_error = None
        cls.suppress_save = False
        cls.suppress_remove = False
        cls.save_started = None
        cls.allow_save = None
        cls.remove_started = None
        cls.allow_remove = None
        cls.load_started = None
        cls.allow_load = None
        cls.mutation_delay = 0.0
        cls.mutations = 0
        cls.max_mutations = 0
        cls.instances = []
        cls.saved_payloads = []

    async def async_load(self) -> object:
        """Load shared durable content."""
        cls = type(self)
        if cls.load_started is not None:
            cls.load_started.set()
        if cls.allow_load is not None:
            await cls.allow_load.wait()
        if self.load_error is not None:
            raise self.load_error
        return self.durable

    async def async_save(self, data: object) -> None:
        """Optionally save shared durable content."""
        cls = type(self)
        cls.mutations += 1
        cls.max_mutations = max(cls.max_mutations, cls.mutations)
        try:
            cls.saved_payloads.append(data)
            if cls.save_started is not None:
                cls.save_started.set()
            if cls.allow_save is not None:
                await cls.allow_save.wait()
            if cls.mutation_delay:
                await asyncio.sleep(cls.mutation_delay)
            if cls.save_error is not None:
                raise cls.save_error
            if not cls.suppress_save:
                cls.durable = data
        finally:
            cls.mutations -= 1

    async def async_remove(self) -> None:
        """Optionally remove shared durable content."""
        cls = type(self)
        cls.mutations += 1
        cls.max_mutations = max(cls.max_mutations, cls.mutations)
        try:
            if cls.remove_started is not None:
                cls.remove_started.set()
            if cls.allow_remove is not None:
                await cls.allow_remove.wait()
            if cls.mutation_delay:
                await asyncio.sleep(cls.mutation_delay)
            if cls.remove_error is not None:
                raise cls.remove_error
            if not cls.suppress_remove:
                cls.durable = None
        finally:
            cls.mutations -= 1


@pytest.fixture(autouse=True)
def fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Home Assistant Store at its public boundary."""
    FakeStore.reset()
    monkeypatch.setattr(custom_auto_memory, "Store", FakeStore)


def memory(state: CoreState = CoreState.running) -> CustomAutoMemory:
    """Create memory with the public Home Assistant state used by mutations."""
    hass = Mock()
    hass.state = state
    return CustomAutoMemory(hass, "entry-123")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [({"active": True}, True), ({"active": False}, False)],
)
async def test_load_restores_exact_booleans(stored: object, expected: bool) -> None:
    FakeStore.durable = stored

    assert await memory().async_load_active() is expected
    assert len(FakeStore.instances) == 1
    store = FakeStore.instances[0]
    assert store.key == "govee_ble_air_purifier.custom_auto.entry-123"
    assert store.version == 1
    assert store.atomic_writes is True


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {},
        [],
        {"wrong": True},
        {"active": True, "extra": False},
        {"active": 1},
        {"active": 0},
        {"active": "true"},
    ],
)
async def test_missing_or_malformed_load_fails_safe_without_write(stored: Any) -> None:
    FakeStore.durable = stored

    assert await memory().async_load_active() is False
    assert FakeStore.saved_payloads == []


@pytest.mark.parametrize(
    "error",
    [OSError("unreadable"), NotImplementedError("future store version")],
)
async def test_unreadable_or_incompatible_load_fails_safe_without_write(
    error: Exception,
) -> None:
    FakeStore.load_error = error

    assert await memory().async_load_active() is False
    assert FakeStore.saved_payloads == []


async def test_set_saves_only_active_and_verifies_with_fresh_store() -> None:
    await memory().async_set_active(True)

    assert FakeStore.saved_payloads == [{"active": True}]
    assert FakeStore.durable == {"active": True}
    assert len(FakeStore.instances) == 2
    assert FakeStore.instances[0] is not FakeStore.instances[1]


async def test_set_awaits_save_completion_before_fresh_readback() -> None:
    FakeStore.save_started = asyncio.Event()
    FakeStore.allow_save = asyncio.Event()

    task = asyncio.create_task(memory().async_set_active(True))
    await FakeStore.save_started.wait()
    await asyncio.sleep(0)
    assert not task.done()
    assert len(FakeStore.instances) == 1

    FakeStore.allow_save.set()
    await task
    assert len(FakeStore.instances) == 2


async def test_cancelled_save_settles_and_verifies_before_next_mutation() -> None:
    FakeStore.durable = {"active": False}
    FakeStore.save_started = asyncio.Event()
    FakeStore.allow_save = asyncio.Event()
    FakeStore.load_started = asyncio.Event()
    FakeStore.allow_load = asyncio.Event()
    owner = memory()

    first = asyncio.create_task(owner.async_set_active(True))
    await FakeStore.save_started.wait()
    first.cancel()
    second = asyncio.create_task(owner.async_set_active(False))
    await asyncio.sleep(0)

    assert not first.done()
    assert not second.done()
    assert len(FakeStore.instances) == 1

    FakeStore.allow_save.set()
    await FakeStore.load_started.wait()
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    assert len(FakeStore.instances) == 2

    FakeStore.allow_load.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second

    assert FakeStore.saved_payloads == [{"active": True}, {"active": False}]
    assert FakeStore.durable == {"active": False}


async def test_cancelled_clear_settles_and_verifies_before_next_mutation() -> None:
    FakeStore.durable = {"active": True}
    FakeStore.remove_started = asyncio.Event()
    FakeStore.allow_remove = asyncio.Event()
    FakeStore.load_started = asyncio.Event()
    FakeStore.allow_load = asyncio.Event()
    owner = memory()

    first = asyncio.create_task(owner.async_clear())
    await FakeStore.remove_started.wait()
    first.cancel()
    second = asyncio.create_task(owner.async_set_active(False))
    await asyncio.sleep(0)

    assert not first.done()
    assert not second.done()
    assert len(FakeStore.instances) == 1

    FakeStore.allow_remove.set()
    await FakeStore.load_started.wait()
    await asyncio.sleep(0)
    assert not first.done()
    assert not second.done()
    assert len(FakeStore.instances) == 2

    FakeStore.allow_load.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second

    assert FakeStore.saved_payloads == [{"active": False}]
    assert FakeStore.durable == {"active": False}


async def test_cancellation_while_waiting_for_mutation_lock_is_immediate() -> None:
    FakeStore.save_started = asyncio.Event()
    FakeStore.allow_save = asyncio.Event()
    owner = memory()

    first = asyncio.create_task(owner.async_set_active(True))
    await FakeStore.save_started.wait()
    waiting = asyncio.create_task(owner.async_set_active(False))
    await asyncio.sleep(0)
    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not first.done()
    assert FakeStore.saved_payloads == [{"active": True}]

    FakeStore.allow_save.set()
    await first


@pytest.mark.parametrize("durable", [None, {"active": False}])
async def test_suppressed_write_without_desired_postcondition_raises(
    durable: object,
) -> None:
    FakeStore.durable = durable
    FakeStore.suppress_save = True

    with pytest.raises(CustomAutoMemoryError, match="verification failed"):
        await memory().async_set_active(True)


async def test_suppressed_idempotent_write_may_succeed() -> None:
    FakeStore.durable = {"active": True}
    FakeStore.suppress_save = True

    await memory().async_set_active(True)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("save failed"), OSError("readback failed")],
)
async def test_save_or_readback_failure_raises_domain_error(
    failure: Exception,
) -> None:
    if isinstance(failure, RuntimeError):
        FakeStore.save_error = failure
    else:
        FakeStore.load_error = failure

    with pytest.raises(CustomAutoMemoryError) as raised:
        await memory().async_set_active(True)
    assert raised.value.__cause__ is failure
    assert "entry-123" in str(raised.value)


@pytest.mark.parametrize("state", [CoreState.stopping, CoreState.final_write])
async def test_stopping_states_reject_mutations(state: CoreState) -> None:
    owner = memory(state)

    with pytest.raises(CustomAutoMemoryError, match="Home Assistant is stopping"):
        await owner.async_set_active(True)
    with pytest.raises(CustomAutoMemoryError, match="Home Assistant is stopping"):
        await owner.async_clear()
    assert FakeStore.instances == []


async def test_clear_removes_and_verifies_absence_with_fresh_store() -> None:
    FakeStore.durable = {"active": True}

    await memory().async_clear()

    assert FakeStore.durable is None
    assert len(FakeStore.instances) == 2
    assert FakeStore.instances[0] is not FakeStore.instances[1]


@pytest.mark.parametrize("remove_raises", [False, True])
async def test_clear_reports_physical_removal_failure(remove_raises: bool) -> None:
    FakeStore.durable = {"active": True}
    if remove_raises:
        FakeStore.remove_error = OSError("remove failed")
    else:
        FakeStore.suppress_remove = True

    with pytest.raises(CustomAutoMemoryError, match="remov"):
        await memory().async_clear()
    assert len(FakeStore.instances) == 2


async def test_concurrent_mutations_are_serialized() -> None:
    FakeStore.durable = {"active": False}
    FakeStore.mutation_delay = 0.01
    owner = memory()

    await asyncio.gather(
        owner.async_set_active(True),
        owner.async_clear(),
        owner.async_set_active(False),
    )

    assert FakeStore.max_mutations == 1
    assert FakeStore.durable == {"active": False}
