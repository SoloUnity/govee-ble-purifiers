"""Home Assistant Bluetooth scanner, cache, and route access."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .errors import exception_detail
from .settings import BluetoothRuntimeSettings

_LOGGER = logging.getLogger(__package__)


class HomeAssistantBluetoothEnvironment:
    """Expose Home Assistant's shared scanner without owning a scanner.

    A BLEDevice is deliberately looked up again for every connection attempt.
    Home Assistant can then choose the currently best local adapter or proxy.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        settings: BluetoothRuntimeSettings,
    ) -> None:
        self._hass = hass
        self.address = address
        self.settings = settings
        self._advertisement_event = asyncio.Event()
        self._cancel_advertisement: Callable[[], None] | None = None
        self._last_callback_time: float | None = None
        self._last_callback_advertisement_time: float | None = None
        self._live_service_info: Any = None
        self._fresh_after: float | None = None
        self._fresh_advertisements = 0
        self._last_selected_advertisement_time: float | None = None
        self._last_route_selection: str | None = None

    async def async_start(self) -> None:
        """Listen passively for a fresh route to the configured address."""
        if self._cancel_advertisement is not None:
            return

        callback_kwargs: dict[str, Any] = {}
        if replay_type := getattr(bluetooth, "BluetoothCallbackReplay", None):
            callback_kwargs["replay"] = replay_type.DISABLED
        self._cancel_advertisement = bluetooth.async_register_callback(
            self._hass,
            self._advertisement_received,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.PASSIVE,
            **callback_kwargs,
        )

    async def async_stop(self) -> None:
        """Stop listening for advertisements."""
        if self._cancel_advertisement is not None:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        self._advertisement_event.set()

    def _advertisement_received(self, service_info: Any, *_: Any) -> None:
        received_at = time.monotonic()
        advertisement_time = getattr(service_info, "time", None)
        self._last_callback_time = received_at
        self._last_callback_advertisement_time = (
            advertisement_time if isinstance(advertisement_time, int | float) else None
        )
        fresh_after = self._fresh_after
        # Registration replay is synchronous and happens before a connection
        # wait begins. Any callback received after this cutoff is therefore a
        # live scanner delivery, even if the scanner timestamp is coarse.
        is_live = fresh_after is not None and received_at >= fresh_after
        _LOGGER.debug(
            "Advertisement received for %s: name=%s source=%s rssi=%s "
            "connectable=%s live_for_current_wait=%s",
            self.address,
            getattr(service_info, "name", None),
            getattr(service_info, "source", None),
            getattr(service_info, "rssi", None),
            getattr(service_info, "connectable", None),
            is_live,
        )
        if is_live:
            self._live_service_info = service_info
            self._fresh_advertisements += 1
        # Recovery backoff also listens to this event. Signal every callback,
        # not only callbacks belonging to an active route-selection wait.
        self._advertisement_event.set()

    def get_connectable_device(self) -> BLEDevice | None:
        """Return the best currently reachable connectable route."""
        device = bluetooth.async_ble_device_from_address(
            self._hass, self.address, connectable=True
        )
        route = self.route_diagnostics()
        _LOGGER.debug(
            "Resolved Bluetooth route for %s: device=%s source=%s rssi=%s",
            self.address,
            device.name if device is not None else None,
            route["source"],
            route["rssi"],
        )
        return device

    def route_diagnostics(self) -> dict[str, Any]:
        """Return secret-free details for the best current HA Bluetooth route."""
        try:
            service_info = bluetooth.async_last_service_info(
                self._hass,
                self.address,
                connectable=True,
            )
        except Exception as err:
            # Diagnostics must never prevent an otherwise valid connection.
            _LOGGER.debug(
                "Unable to inspect the Home Assistant Bluetooth route for %s: %s",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return {
                "present": None,
                "name": None,
                "source": None,
                "rssi": None,
                "tx_power": None,
                "advertisement_age_seconds": None,
                "callback_age_seconds": self._callback_age(),
                "callback_advertisement_age_seconds": (
                    self._callback_advertisement_age()
                ),
                "fresh_advertisements": self._fresh_advertisements,
                "last_route_selection": self._last_route_selection,
                "selected_advertisement_age_seconds": (
                    self._selected_advertisement_age()
                ),
                "error": exception_detail(err),
            }
        if service_info is None:
            return {
                "present": False,
                "name": None,
                "source": None,
                "rssi": None,
                "tx_power": None,
                "advertisement_age_seconds": None,
                "callback_age_seconds": self._callback_age(),
                "callback_advertisement_age_seconds": (
                    self._callback_advertisement_age()
                ),
                "fresh_advertisements": self._fresh_advertisements,
                "last_route_selection": self._last_route_selection,
                "selected_advertisement_age_seconds": (
                    self._selected_advertisement_age()
                ),
            }
        advertisement_time = getattr(service_info, "time", None)
        advertisement_age = (
            max(0.0, time.monotonic() - advertisement_time)
            if isinstance(advertisement_time, int | float)
            else None
        )
        return {
            "present": True,
            "name": getattr(service_info, "name", None),
            "source": getattr(service_info, "source", None),
            "rssi": getattr(service_info, "rssi", None),
            "tx_power": getattr(service_info, "tx_power", None),
            "advertisement_age_seconds": (
                round(advertisement_age, 3) if advertisement_age is not None else None
            ),
            "callback_age_seconds": self._callback_age(),
            "callback_advertisement_age_seconds": (self._callback_advertisement_age()),
            "fresh_advertisements": self._fresh_advertisements,
            "last_route_selection": self._last_route_selection,
            "selected_advertisement_age_seconds": (self._selected_advertisement_age()),
        }

    def _selected_advertisement_age(self) -> float | None:
        """Return the age of the advertisement used for the previous route."""
        if self._last_selected_advertisement_time is None:
            return None
        return round(
            max(0.0, time.monotonic() - self._last_selected_advertisement_time),
            3,
        )

    def _callback_age(self) -> float | None:
        """Return seconds since this integration directly saw an advertisement."""
        if self._last_callback_time is None:
            return None
        return round(max(0.0, time.monotonic() - self._last_callback_time), 3)

    def _callback_advertisement_age(self) -> float | None:
        """Return the age encoded by the last callback's service information."""
        if self._last_callback_advertisement_time is None:
            return None
        return round(
            max(0.0, time.monotonic() - self._last_callback_advertisement_time),
            3,
        )

    def _usable_service_info(self, started_at: float) -> tuple[Any, str] | None:
        """Return a recent first route or evidence newer than the previous route."""
        if self._live_service_info is not None:
            return self._live_service_info, "live_callback"

        service_info = bluetooth.async_last_service_info(
            self._hass,
            self.address,
            connectable=True,
        )
        advertisement_time = (
            getattr(service_info, "time", None) if service_info is not None else None
        )
        if service_info is None or not isinstance(advertisement_time, int | float):
            return None
        if (
            advertisement_time
            < started_at
            - self.settings.route_selection.recent_cached_advertisement_max_age
        ):
            return None

        previous_time = self._last_selected_advertisement_time
        if previous_time is None:
            return service_info, "recent_cache"
        if advertisement_time > previous_time:
            return service_info, "newer_cache"
        return None

    def _record_selected_route(self, service_info: Any, source: str) -> None:
        """Remember the route evidence so a retry cannot reuse the same packet."""
        advertisement_time = getattr(service_info, "time", None)
        if isinstance(advertisement_time, int | float):
            previous_time = self._last_selected_advertisement_time
            self._last_selected_advertisement_time = (
                advertisement_time
                if previous_time is None
                else max(previous_time, advertisement_time)
            )
        self._last_route_selection = source

    def reachability_diagnostics(self) -> str | None:
        """Return Home Assistant's human-readable connection route diagnosis."""
        diagnose = getattr(bluetooth, "async_address_reachability_diagnostics", None)
        intent_type = getattr(bluetooth, "BluetoothReachabilityIntent", None)
        if diagnose is None or intent_type is None:
            return None
        try:
            return str(diagnose(self._hass, self.address, intent_type.CONNECTION))
        except Exception as err:
            _LOGGER.debug(
                "Unable to obtain Bluetooth reachability diagnostics for %s: %s",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return f"unavailable: {exception_detail(err)}"

    def address_is_present(self) -> bool:
        """Return whether a connectable scanner can currently see the address."""
        return bluetooth.async_address_present(
            self._hass, self.address, connectable=True
        )

    def has_recent_advertisement(self, max_age: float) -> bool:
        """Return whether HA has recent connectable evidence for this address."""
        try:
            service_info = bluetooth.async_last_service_info(
                self._hass,
                self.address,
                connectable=True,
            )
        except Exception as err:
            _LOGGER.debug(
                "Unable to inspect advertisement recency for %s: %s",
                self.address,
                exception_detail(err),
                exc_info=True,
            )
            return False
        advertisement_time = (
            getattr(service_info, "time", None) if service_info is not None else None
        )
        return (
            isinstance(advertisement_time, int | float)
            and max(0.0, time.monotonic() - advertisement_time) <= max_age
        )

    async def async_wait_for_advertisement_after(self, cutoff: float) -> None:
        """Wait until this integration receives a connectable advertisement."""
        while self._last_callback_time is None or self._last_callback_time <= cutoff:
            self._advertisement_event.clear()
            # Close the callback/check/clear race before blocking.
            if (
                self._last_callback_time is not None
                and self._last_callback_time > cutoff
            ):
                return
            await self._advertisement_event.wait()

    async def async_wait_for_fresh_device(self, timeout: float) -> BLEDevice | None:
        """Resolve a recent route, or wait for evidence newer than the last route."""
        started_at = time.monotonic()
        deadline = started_at + timeout
        self._fresh_after = started_at
        self._live_service_info = None
        self._advertisement_event.clear()
        _LOGGER.debug(
            "Waiting for a recent or new connectable advertisement for %s: "
            "timeout=%.1fs recent_cache_limit=%.1fs previous_age=%s",
            self.address,
            timeout,
            self.settings.route_selection.recent_cached_advertisement_max_age,
            self._selected_advertisement_age(),
        )

        try:
            while (remaining := deadline - time.monotonic()) > 0:
                route_candidate = self._usable_service_info(started_at)
                if route_candidate is not None:
                    service_info, selection_source = route_candidate
                    advertisement_time = getattr(service_info, "time", None)
                    device = self.get_connectable_device()
                    if device is None:
                        device = getattr(service_info, "device", None)
                    if device is not None:
                        self._record_selected_route(service_info, selection_source)
                        _LOGGER.debug(
                            "Bluetooth route selected for %s after %.3fs: "
                            "selection=%s source=%s rssi=%s "
                            "advertisement_age=%s",
                            self.address,
                            time.monotonic() - started_at,
                            selection_source,
                            getattr(service_info, "source", None),
                            getattr(service_info, "rssi", None),
                            (
                                round(
                                    max(0.0, time.monotonic() - advertisement_time),
                                    3,
                                )
                                if isinstance(advertisement_time, int | float)
                                else None
                            ),
                        )
                        return device

                self._advertisement_event.clear()
                # Close the check/clear race before blocking again.
                if self._usable_service_info(started_at) is not None:
                    continue

                try:
                    async with asyncio.timeout(
                        min(
                            self.settings.route_selection.advertisement_check_interval,
                            remaining,
                        )
                    ):
                        await self._advertisement_event.wait()
                except TimeoutError:
                    pass
        finally:
            self._fresh_after = None

        if time.monotonic() >= deadline:
            _LOGGER.debug(
                "No recent or new connectable advertisement for %s within %.1f "
                "seconds; route=%s reachability=%s",
                self.address,
                timeout,
                self.route_diagnostics(),
                self.reachability_diagnostics(),
            )
        return None
