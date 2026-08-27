from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__package__)


class AddressOwnershipState(StrEnum):
    """Lifecycle state for one process-wide purifier address owner."""

    OWNED = "owned"
    CANCELLATION_REQUESTED = "cancellation_requested"
    LATE_CLEANUP = "late_cleanup"
    STANDALONE_CLEANUP = "standalone_cleanup"


@dataclass(frozen=True, slots=True)
class AddressOwnershipToken:
    """Unforgeable capability for mutating one address ownership record."""

    address: str
    serial: int


@dataclass(slots=True)
class _AddressOwnershipRecord:
    token: AddressOwnershipToken
    state: AddressOwnershipState = AddressOwnershipState.OWNED
    started_at: float = field(default_factory=time.monotonic)
    release_requested: bool = False
    cleanup_required: bool = False
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class AddressOwnershipRegistry:
    """Process-level ownership and quarantine for purifier addresses."""

    def __init__(self) -> None:
        self._records: dict[str, _AddressOwnershipRecord] = {}
        self._serial = 0

    @staticmethod
    def normalize(address: str) -> str:
        return address.strip().upper()

    def claim(self, address: str) -> AddressOwnershipToken | None:
        normalized = self.normalize(address)
        existing = self._records.get(normalized)
        if existing is not None:
            self._release_if_quiescent(existing)
        if normalized in self._records:
            return None
        self._serial += 1
        token = AddressOwnershipToken(normalized, self._serial)
        self._records[normalized] = _AddressOwnershipRecord(token=token)
        return token

    def is_current(self, token: AddressOwnershipToken | None) -> bool:
        if token is None:
            return False
        record = self._records.get(token.address)
        return record is not None and record.token is token

    def is_owned(self, address: str) -> bool:
        normalized = self.normalize(address)
        record = self._records.get(normalized)
        if record is not None:
            self._release_if_quiescent(record)
        return normalized in self._records

    def mark_cancellation_requested(self, token: AddressOwnershipToken) -> bool:
        record = self._record(token)
        if record is None:
            return False
        record.state = AddressOwnershipState.CANCELLATION_REQUESTED
        record.cleanup_required = True
        return True

    def mark_late_cleanup(self, token: AddressOwnershipToken) -> bool:
        record = self._record(token)
        if record is None:
            return False
        record.state = AddressOwnershipState.LATE_CLEANUP
        record.cleanup_required = True
        return True

    async def async_run_standalone_cleanup(
        self,
        address: str,
        action: Callable[[], Coroutine[Any, Any, Any]],
        *,
        timeout: float,
        cancellation_timeout: float,
    ) -> dict[str, object]:
        """Run setup/removal cleanup under process-wide address ownership."""
        token = self.claim(address)
        if token is None:
            return {
                "acquired": False,
                "success": False,
                "retained": False,
                "error": "address ownership is already active",
                "ownership": self.snapshot(address=address),
            }

        record = self._record(token)
        assert record is not None
        record.state = AddressOwnershipState.STANDALONE_CLEANUP
        record.cleanup_required = True
        task = asyncio.create_task(
            action(), name=f"govee-standalone-cleanup-{token.serial}"
        )
        self.track_task(token, task)
        success = False
        retained = False
        error: str | None = None
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if done:
                success, error = self._standalone_task_result(task)
            else:
                task.cancel()
                done, _ = await asyncio.wait(
                    (task,), timeout=cancellation_timeout
                )
                if done:
                    _success, task_error = self._standalone_task_result(task)
                    error = task_error or "cleanup cancelled after deadline"
                else:
                    retained = True
                    error = "cleanup deadline and cancellation tail exceeded"
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await asyncio.wait((task,), timeout=cancellation_timeout)
            raise
        finally:
            self.request_release(token)
            self.finish_cleanup(token)

        return {
            "acquired": True,
            "success": success,
            "retained": retained,
            "error": error,
            "ownership": self.snapshot(token=token, address=address),
        }

    def track_task(
        self, token: AddressOwnershipToken, task: asyncio.Task[Any]
    ) -> bool:
        record = self._record(token)
        if record is None:
            return False
        record.tasks.add(task)
        task.add_done_callback(lambda done: self._task_done(token, done))
        return True

    def request_release(self, token: AddressOwnershipToken) -> None:
        record = self._record(token)
        if record is None:
            return
        record.release_requested = True
        self._release_if_quiescent(record)

    def finish_cleanup(self, token: AddressOwnershipToken) -> None:
        record = self._record(token)
        if record is None:
            return
        record.cleanup_required = False
        self._release_if_quiescent(record)

    def snapshot(
        self,
        *,
        token: AddressOwnershipToken | None = None,
        address: str | None = None,
    ) -> dict[str, object] | None:
        record = self._record(token) if token is not None else None
        if record is None and address is not None:
            record = self._records.get(self.normalize(address))
        if record is None:
            return None
        self._release_if_quiescent(record)
        if self._records.get(record.token.address) is not record:
            return None
        return {
            "state": record.state.value,
            "cancellation_requested": (
                record.state is AddressOwnershipState.CANCELLATION_REQUESTED
            ),
            "pending_tasks": sum(not task.done() for task in record.tasks),
            "elapsed_seconds": round(max(0.0, time.monotonic() - record.started_at), 3),
            "release_requested": record.release_requested,
            "cleanup_required": record.cleanup_required,
            "token_serial": record.token.serial,
        }

    def _record(
        self, token: AddressOwnershipToken | None
    ) -> _AddressOwnershipRecord | None:
        if token is None:
            return None
        record = self._records.get(token.address)
        if record is None or record.token is not token:
            return None
        return record

    def _task_done(
        self, token: AddressOwnershipToken, task: asyncio.Task[Any]
    ) -> None:
        error: BaseException | None = None
        if not task.cancelled():
            try:
                error = task.exception()
            except Exception as err:  # noqa: BLE001
                error = err
            if error is not None:
                _LOGGER.debug(
                    "Retained Bluetooth ownership task failed: address=%s "
                    "task=%s cause=%s: %s",
                    token.address,
                    task.get_name(),
                    type(error).__name__,
                    error,
                )
        record = self._record(token)
        if record is None:
            return
        record.tasks.discard(task)
        self._release_if_quiescent(record)

    @staticmethod
    def _standalone_task_result(
        task: asyncio.Task[Any],
    ) -> tuple[bool, str | None]:
        if task.cancelled():
            return False, "cleanup task cancelled"
        try:
            task.result()
        except Exception as err:  # noqa: BLE001
            return False, f"{type(err).__name__}: {err}"
        return True, None

    def _release_if_quiescent(self, record: _AddressOwnershipRecord) -> None:
        if (
            record.release_requested
            and not record.cleanup_required
            and not any(not task.done() for task in record.tasks)
        ):
            self._records.pop(record.token.address, None)


ADDRESS_OWNERSHIP = AddressOwnershipRegistry()
