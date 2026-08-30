"""Durable ON/OFF memory for Custom Auto."""

from __future__ import annotations

import asyncio
from typing import Final, TypedDict

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORAGE_VERSION: Final = 1
_STOPPING_STATES: Final = frozenset({CoreState.stopping, CoreState.final_write})


class _CustomAutoPayload(TypedDict):
    """The complete Custom Auto storage payload."""

    active: bool


class CustomAutoMemoryError(HomeAssistantError):
    """Raised when a Custom Auto memory mutation is not durable."""


class CustomAutoMemory:
    """Own the single persisted Custom Auto selection for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize Custom Auto memory."""
        self._hass = hass
        self._entry_id = entry_id
        self._key = f"{DOMAIN}.custom_auto.{entry_id}"
        self._mutation_lock = asyncio.Lock()

    def _store(self) -> Store[_CustomAutoPayload]:
        """Create a Store instance for this entry."""
        return Store(
            self._hass,
            _STORAGE_VERSION,
            self._key,
            atomic_writes=True,
        )

    @staticmethod
    def _active_from_payload(payload: object) -> bool | None:
        """Strictly decode the complete storage payload."""
        if (
            type(payload) is not dict
            or set(payload) != {"active"}
            or type(payload["active"]) is not bool
        ):
            return None
        return payload["active"]

    async def async_load_active(self) -> bool:
        """Load the remembered selection, failing safely to OFF."""
        try:
            payload = await self._store().async_load()
        except Exception:  # noqa: BLE001
            return False

        active = self._active_from_payload(payload)
        return active if active is not None else False

    async def async_set_active(self, active: bool) -> None:
        """Persist and verify an exact ON/OFF selection."""
        if type(active) is not bool:
            raise TypeError("active must be a bool")

        async with self._mutation_lock:
            self._ensure_writable()
            mutation = asyncio.create_task(self._async_save_and_verify(active))
            await self._async_await_mutation(mutation)

    async def async_clear(self) -> None:
        """Remove and verify absence of this entry's memory."""
        async with self._mutation_lock:
            self._ensure_writable()
            mutation = asyncio.create_task(self._async_remove_and_verify())
            await self._async_await_mutation(mutation)

    async def _async_save_and_verify(self, active: bool) -> None:
        """Save and verify one exact selection."""
        desired: _CustomAutoPayload = {"active": active}
        try:
            await self._store().async_save(desired)
        except Exception as err:  # noqa: BLE001
            raise CustomAutoMemoryError(
                f"Failed to save Custom Auto memory for entry {self._entry_id}"
            ) from err

        try:
            persisted = await self._store().async_load()
        except Exception as err:  # noqa: BLE001
            raise CustomAutoMemoryError(
                "Failed to read back Custom Auto memory for entry "
                f"{self._entry_id}"
            ) from err

        if self._active_from_payload(persisted) is not active:
            raise CustomAutoMemoryError(
                "Custom Auto memory verification failed for entry "
                f"{self._entry_id}"
            )

    async def _async_remove_and_verify(self) -> None:
        """Remove and verify absence of the stored selection."""
        remove_error: Exception | None = None
        try:
            await self._store().async_remove()
        except Exception as err:  # noqa: BLE001
            remove_error = err

        try:
            persisted = await self._store().async_load()
        except Exception as err:  # noqa: BLE001
            raise CustomAutoMemoryError(
                "Failed to verify removal of Custom Auto memory for entry "
                f"{self._entry_id}"
            ) from err

        if remove_error is not None:
            raise CustomAutoMemoryError(
                f"Failed to remove Custom Auto memory for entry {self._entry_id}"
            ) from remove_error
        if persisted is not None:
            raise CustomAutoMemoryError(
                "Custom Auto memory removal verification failed for entry "
                f"{self._entry_id}"
            )

    @staticmethod
    async def _async_await_mutation(mutation: asyncio.Task[None]) -> None:
        """Keep a mutation alive and settled before propagating cancellation."""
        try:
            await asyncio.shield(mutation)
        except asyncio.CancelledError:
            while not mutation.done():
                try:
                    await asyncio.shield(mutation)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001
                    break
            if not mutation.cancelled():
                mutation.exception()
            raise

    def _ensure_writable(self) -> None:
        """Reject mutations that Home Assistant would defer to final write."""
        if self._hass.state in _STOPPING_STATES:
            raise CustomAutoMemoryError(
                "Cannot change Custom Auto memory while Home Assistant is stopping "
                f"(entry {self._entry_id})"
            )
