"""Manual purifier discovery on Home Assistant's shared Bluetooth scanner."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from .const import DOMAIN
from .profiles import ProfileRegistry

_LOGGER = logging.getLogger(__name__)

MANUAL_DISCOVERY_SCAN_DURATION = 10.0
SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT = 10
SELECTED_ADVERTISEMENT_CHECK_INTERVAL = 0.25
_SETUP_IDENTITY_CACHE = f"{DOMAIN}_setup_identity_cache"


@dataclass(frozen=True, slots=True)
class PurifierIdentity:
    """A supported purifier identity learned independently of reachability."""

    name: str
    model: str


@dataclass(frozen=True, slots=True)
class FreshBluetoothSighting:
    """Address-level evidence observed during the current setup scan."""

    address: str
    rssi: int
    name_missing: bool


@dataclass(frozen=True, slots=True)
class DiscoveredPurifier:
    """A supported connectable purifier currently known to Home Assistant."""

    address: str
    name: str
    model: str
    rssi: int


@dataclass(frozen=True, slots=True)
class PurifierDiscoveryResult:
    """Resolved purifiers and unresolved nameless Bluetooth observations."""

    purifiers: tuple[DiscoveredPurifier, ...]
    unnamed_devices_seen: bool


def unique_id_from_address(address: str) -> str:
    """Return the stable normalized registry identity for an address."""
    return format_mac(address)


class PurifierDiscoveryService:
    """Discover and revalidate purifiers through an injected profile registry."""

    def __init__(self, hass: HomeAssistant, registry: ProfileRegistry) -> None:
        """Bind discovery to one Home Assistant instance and profile snapshot."""
        self._hass = hass
        self._registry = registry

    def model_from_name(self, name: str | None) -> str | None:
        """Resolve only a supported advertised-name family."""
        profile = self._registry.match_name(name)
        return profile.model.value if profile is not None else None

    def discovery_options(
        self,
        discoveries: tuple[DiscoveredPurifier, ...],
    ) -> dict[str, str]:
        """Build address-backed labels without exposing addresses to the user."""
        return {
            device.address: f"{device.name} ({'Near' if index == 0 else 'Far'})"
            for index, device in enumerate(discoveries)
        }

    def _identity_cache(self) -> dict[str, PurifierIdentity]:
        """Return identities retained for the lifetime of Home Assistant."""
        return cast(
            dict[str, PurifierIdentity],
            self._hass.data.setdefault(_SETUP_IDENTITY_CACHE, {}),
        )

    def _remember_identity(
        self,
        identities: dict[str, PurifierIdentity],
        *,
        address: str,
        name: str | None,
    ) -> PurifierIdentity | None:
        """Retain a supported advertised identity without implying freshness."""
        model = self.model_from_name(name)
        if model is None or name is None:
            return identities.get(unique_id_from_address(address))
        identity = PurifierIdentity(name=name, model=model)
        identities[unique_id_from_address(address)] = identity
        return identity

    def _remember_ble_device_identity(
        self,
        identities: dict[str, PurifierIdentity],
        *,
        address: str,
    ) -> None:
        """Recover a valid name retained on Home Assistant's BLE device."""
        try:
            device = bluetooth.async_ble_device_from_address(
                self._hass,
                address,
                connectable=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Unable to inspect Home Assistant's BLE device name for %s: %s",
                address,
                err,
                exc_info=True,
            )
            return
        if device is not None:
            self._remember_identity(
                identities,
                address=address,
                name=getattr(device, "name", None),
            )

    @staticmethod
    def _name_is_missing(name: str | None, address: str) -> bool:
        """Return whether an observation lacks a useful advertised name."""
        return not name or name.casefold() == address.casefold()

    def _record_fresh_sighting(
        self,
        sightings: dict[str, FreshBluetoothSighting],
        identities: dict[str, PurifierIdentity],
        service_info: BluetoothServiceInfoBleak,
    ) -> None:
        """Record fresh reachability separately from any known identity."""
        if not service_info.connectable:
            return
        self._remember_identity(
            identities,
            address=service_info.address,
            name=service_info.name,
        )
        key = unique_id_from_address(service_info.address)
        previous = sightings.get(key)
        sightings[key] = FreshBluetoothSighting(
            address=service_info.address,
            rssi=max(
                service_info.rssi,
                previous.rssi if previous is not None else service_info.rssi,
            ),
            name_missing=(
                self._name_is_missing(service_info.name, service_info.address)
                or (previous.name_missing if previous is not None else False)
            ),
        )

    @staticmethod
    def _sorted_discoveries(
        discoveries: Iterable[DiscoveredPurifier],
    ) -> tuple[DiscoveredPurifier, ...]:
        """Order discoveries from strongest to weakest signal."""
        return tuple(
            sorted(
                discoveries,
                key=lambda device: (-device.rssi, device.name, device.address),
            )
        )

    def _resolve_discoveries(
        self,
        sightings: Iterable[FreshBluetoothSighting],
        identities: dict[str, PurifierIdentity],
    ) -> PurifierDiscoveryResult:
        """Combine current reachability with independently learned identities."""
        discoveries: list[DiscoveredPurifier] = []
        unnamed_devices_seen = False
        for sighting in sightings:
            identity = identities.get(unique_id_from_address(sighting.address))
            if identity is None:
                unnamed_devices_seen |= sighting.name_missing
                continue
            discoveries.append(
                DiscoveredPurifier(
                    address=sighting.address,
                    name=identity.name,
                    model=identity.model,
                    rssi=sighting.rssi,
                )
            )
        return PurifierDiscoveryResult(
            purifiers=self._sorted_discoveries(discoveries),
            unnamed_devices_seen=unnamed_devices_seen,
        )

    def _scanner_diagnostics(self) -> list[dict[str, Any]] | None:
        """Return best-effort read-only scanner details for setup diagnostics."""
        current_scanners = getattr(bluetooth, "async_current_scanners", None)
        if current_scanners is None:
            return None
        try:
            scanners = current_scanners(self._hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Unable to inspect Home Assistant Bluetooth scanners: %s",
                err,
                exc_info=True,
            )
            return None
        return [
            {
                "source": getattr(scanner, "source", None),
                "connectable": getattr(scanner, "connectable", None),
                "scanning": getattr(scanner, "scanning", None),
                "requested_mode": str(getattr(scanner, "requested_mode", None)),
                "current_mode": str(getattr(scanner, "current_mode", None)),
            }
            for scanner in scanners
        ]

    async def async_discover_purifiers(self) -> PurifierDiscoveryResult:
        """Collect supported purifiers during one complete setup scan window."""
        request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
        started_at = time.monotonic()
        api_available = request_active_scan is not None
        scan_error: Exception | None = None
        identities = self._identity_cache()
        sightings: dict[str, FreshBluetoothSighting] = {}
        accept_callbacks = False

        # Seed identity before the freshness cutoff. Cached names can identify
        # later nameless packets, but cannot make their addresses reachable.
        cached_before_scan = tuple(
            bluetooth.async_discovered_service_info(self._hass, connectable=True)
        )
        for service_info in cached_before_scan:
            if not service_info.connectable:
                continue
            self._remember_identity(
                identities,
                address=service_info.address,
                name=service_info.name,
            )
            self._remember_ble_device_identity(
                identities,
                address=service_info.address,
            )

        def _advertisement_received(
            service_info: BluetoothServiceInfoBleak,
            *_: Any,
        ) -> None:
            # Older HA releases synchronously replay cache during registration.
            if not accept_callbacks:
                return
            key = unique_id_from_address(service_info.address)
            previous = sightings.get(key)
            self._record_fresh_sighting(sightings, identities, service_info)
            current = sightings.get(key)
            identity = identities.get(key)
            if current is not None and current != previous:
                _LOGGER.debug(
                    "Observed connectable Bluetooth address during manual scan: "
                    "address=%s observed_name=%s known_name=%s model=%s "
                    "source=%s rssi=%s",
                    current.address,
                    service_info.name,
                    identity.name if identity is not None else None,
                    identity.model if identity is not None else None,
                    getattr(service_info, "source", None),
                    current.rssi,
                )

        callback_kwargs: dict[str, Any] = {}
        if replay_type := getattr(bluetooth, "BluetoothCallbackReplay", None):
            callback_kwargs["replay"] = replay_type.DISABLED
        cancel_callback: Callable[[], None] = bluetooth.async_register_callback(
            self._hass,
            _advertisement_received,
            {"connectable": True},
            bluetooth.BluetoothScanningMode.PASSIVE,
            **callback_kwargs,
        )
        accept_callbacks = True

        _LOGGER.debug(
            "Starting %.1f-second manual purifier discovery window: "
            "active_scan_api_available=%s scanners=%s",
            MANUAL_DISCOVERY_SCAN_DURATION,
            api_available,
            self._scanner_diagnostics(),
        )

        try:
            if request_active_scan is None:
                _LOGGER.debug(
                    "One-shot active Bluetooth scanning is unavailable; observing "
                    "Home Assistant's shared scanner for the complete setup window"
                )
            else:
                try:
                    await request_active_scan(
                        self._hass,
                        duration=MANUAL_DISCOVERY_SCAN_DURATION,
                    )
                except Exception as err:  # noqa: BLE001
                    scan_error = err
                    _LOGGER.warning(
                        "Home Assistant's one-shot active Bluetooth scan failed; "
                        "continuing the shared-scanner observation window: %s",
                        err,
                        exc_info=True,
                    )

            remaining = MANUAL_DISCOVERY_SCAN_DURATION - (
                time.monotonic() - started_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)

            # Shared history may refresh even when callback content is deduped.
            cached_after_scan = tuple(
                bluetooth.async_discovered_service_info(
                    self._hass,
                    connectable=True,
                )
            )
            for service_info in cached_after_scan:
                advertisement_time = getattr(service_info, "time", None)
                if not isinstance(advertisement_time, int | float):
                    continue
                if advertisement_time < started_at:
                    continue
                self._record_fresh_sighting(sightings, identities, service_info)

            for sighting in sightings.values():
                self._remember_ble_device_identity(
                    identities,
                    address=sighting.address,
                )
        finally:
            cancel_callback()

        result = self._resolve_discoveries(sightings.values(), identities)
        _LOGGER.debug(
            "Manual purifier discovery window completed in %.3f seconds: "
            "active_scan_api_available=%s active_scan_error=%s scanners=%s "
            "fresh_addresses=%d unnamed_unresolved=%s candidates=%s",
            time.monotonic() - started_at,
            api_available,
            repr(scan_error) if scan_error is not None else None,
            self._scanner_diagnostics(),
            len(sightings),
            result.unnamed_devices_seen,
            [
                {
                    "name": discovery.name,
                    "address": discovery.address,
                    "model": discovery.model,
                    "rssi": discovery.rssi,
                }
                for discovery in result.purifiers
            ],
        )
        return result

    async def async_wait_for_selected_advertisement(
        self,
        *,
        address: str,
    ) -> BluetoothServiceInfoBleak | None:
        """Wait for fresh connectable evidence for the selected purifier."""
        started_at = time.monotonic()

        def _is_fresh(service_info: BluetoothServiceInfoBleak) -> bool:
            advertisement_time = getattr(service_info, "time", None)
            return (
                isinstance(advertisement_time, int | float)
                and advertisement_time >= started_at
            )

        _LOGGER.debug(
            "Waiting up to %d seconds for a fresh advertisement from selected "
            "purifier %s",
            SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT,
            address,
        )
        process_task = self._hass.async_create_task(
            bluetooth.async_process_advertisements(
                self._hass,
                _is_fresh,
                {"address": address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE,
                SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT,
            )
        )
        deadline = started_at + SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT
        service_info: BluetoothServiceInfoBleak | None = None
        wait_error: Exception | None = None
        try:
            while service_info is None and time.monotonic() < deadline:
                if process_task.done():
                    try:
                        service_info = process_task.result()
                    except TimeoutError as err:
                        wait_error = err
                    except Exception as err:  # noqa: BLE001
                        wait_error = err
                        _LOGGER.warning(
                            "Selected-purifier advertisement processing failed "
                            "for %s: %s",
                            address,
                            err,
                            exc_info=True,
                        )
                    break

                cached_info = bluetooth.async_last_service_info(
                    self._hass,
                    address,
                    connectable=True,
                )
                cached_time = (
                    getattr(cached_info, "time", None)
                    if cached_info is not None
                    else None
                )
                if (
                    cached_info is not None
                    and isinstance(cached_time, int | float)
                    and cached_time >= started_at
                ):
                    service_info = cached_info
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.wait(
                    {process_task},
                    timeout=min(
                        SELECTED_ADVERTISEMENT_CHECK_INTERVAL,
                        remaining,
                    ),
                )
        finally:
            if not process_task.done():
                process_task.cancel()
                with suppress(asyncio.CancelledError):
                    await process_task
            elif not process_task.cancelled():
                process_task.exception()

        if service_info is None:
            cached_info = bluetooth.async_last_service_info(
                self._hass,
                address,
                connectable=True,
            )
            cached_time = (
                getattr(cached_info, "time", None)
                if cached_info is not None
                else None
            )
            cached_age = (
                round(max(0.0, time.monotonic() - cached_time), 3)
                if isinstance(cached_time, int | float)
                else None
            )
            _LOGGER.warning(
                "Selected purifier %s was not observed during its %.1f-second "
                "freshness check; cached_advertisement_age_seconds=%s "
                "advertisement_wait_error=%s scanners=%s",
                address,
                time.monotonic() - started_at,
                cached_age,
                repr(wait_error) if wait_error is not None else None,
                self._scanner_diagnostics(),
            )
            return None

        advertisement_time = getattr(service_info, "time", None)
        _LOGGER.debug(
            "Selected purifier %s produced a fresh advertisement after %.3f "
            "seconds: name=%s source=%s rssi=%s advertisement_age_seconds=%s",
            address,
            time.monotonic() - started_at,
            getattr(service_info, "name", None),
            getattr(service_info, "source", None),
            getattr(service_info, "rssi", None),
            (
                round(max(0.0, time.monotonic() - advertisement_time), 3)
                if isinstance(advertisement_time, int | float)
                else None
            ),
        )
        return service_info
