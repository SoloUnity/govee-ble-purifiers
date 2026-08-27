"""Tests for integration lifecycle cleanup."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryError

from custom_components.govee_ble_air_purifier import (
    _async_cleanup_address,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.govee_ble_air_purifier.bluetooth.ownership import (
    ADDRESS_OWNERSHIP,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, PLATFORMS
from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import ProfileError


def _setup_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
        async_wait_until_ready=AsyncMock(),
        async_shutdown=AsyncMock(),
    )
    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    bus = SimpleNamespace(async_listen_once=Mock(return_value=Mock()))
    hass = SimpleNamespace(config_entries=config_entries, bus=bus)
    entry = SimpleNamespace(
        data={
            CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_MODEL: Model.H7129.value,
        },
        title="Bedroom purifier",
        async_on_unload=Mock(),
    )
    return coordinator, hass, entry


async def test_setup_cleans_address_before_start_and_registers_stop() -> None:
    """Setup clears crash leftovers and awaits shutdown on Home Assistant stop."""
    coordinator, hass, entry = _setup_objects()
    order: list[str] = []

    async def cleanup(address: str, *, reason: str) -> None:
        assert address == entry.data[CONF_ADDRESS]
        assert reason == "entry_setup"
        order.append("cleanup")

    async def start() -> None:
        order.append("start")

    coordinator.async_start.side_effect = start
    cancel_stop_listener = hass.bus.async_listen_once.return_value

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            side_effect=cleanup,
        ) as close_address,
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert order == ["cleanup", "start"]
    close_address.assert_awaited_once_with(
        entry.data[CONF_ADDRESS], reason="entry_setup"
    )
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS
    )
    coordinator.async_wait_until_ready.assert_not_awaited()
    hass.bus.async_listen_once.assert_called_once()
    event_type, stop_callback = hass.bus.async_listen_once.call_args.args
    assert event_type == EVENT_HOMEASSISTANT_STOP
    entry.async_on_unload.assert_called_once_with(cancel_stop_listener)

    await stop_callback(SimpleNamespace())
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_setup_cleanup_failure_does_not_prevent_normal_connection() -> None:
    """Early crash recovery remains best effort before verified transport cleanup."""
    coordinator, hass, entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            side_effect=RuntimeError("BlueZ unavailable"),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    coordinator.async_start.assert_awaited_once_with()
    coordinator.async_wait_until_ready.assert_not_awaited()


async def test_invalid_profile_stops_setup_before_bluetooth_work() -> None:
    """A bundled artifact failure is permanent and cannot start recovery."""
    _coordinator, hass, entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_get_profile_registry",
            new_callable=AsyncMock,
            side_effect=ProfileError("invalid bundled request frame"),
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ) as cleanup,
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator"
        ) as coordinator_class,
    ):
        with pytest.raises(ConfigEntryError, match="Bundled purifier model profiles"):
            await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_not_awaited()
    coordinator_class.assert_not_called()


async def test_version_one_entry_data_is_not_mutated_during_setup() -> None:
    """Existing H7124/H7129 model values remain migration-free."""
    coordinator, hass, entry = _setup_objects()
    original_data = dict(entry.data)

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert entry.data == original_data


async def test_unload_shuts_down_after_platforms_unload() -> None:
    """A reload or integration update releases the active Bluetooth runtime."""
    coordinator, hass, entry = _setup_objects()
    entry.runtime_data = coordinator

    assert await async_unload_entry(hass, entry)  # type: ignore[arg-type]

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, PLATFORMS
    )
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_remove_entry_closes_stale_connection() -> None:
    """Removal performs address cleanup even after runtime data is gone."""
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(data={CONF_ADDRESS: address})

    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        new_callable=AsyncMock,
    ) as cleanup:
        await async_remove_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_awaited_once_with(address, reason="entry_removed")


async def test_standalone_cleanup_defers_to_existing_address_owner() -> None:
    """Setup/removal cleanup cannot race an existing runtime owner."""
    address = "AA:BB:CC:DD:EF:11"
    token = ADDRESS_OWNERSHIP.claim(address)
    assert token is not None

    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        new_callable=AsyncMock,
    ) as cleanup:
        await _async_cleanup_address(
            address,
            reason="entry_setup",
            timeout=0.01,
            cancellation_timeout=0.01,
        )

    cleanup.assert_not_awaited()
    assert ADDRESS_OWNERSHIP.is_current(token)
    ADDRESS_OWNERSHIP.request_release(token)
    ADDRESS_OWNERSHIP.finish_cleanup(token)


async def test_resistant_standalone_cleanup_is_bounded_and_retained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resistant top-level cleanup retains quarantine and observes failure."""
    address = "AA:BB:CC:DD:EF:12"
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def resistant_cleanup(_: str, *, reason: str) -> None:
        assert reason == "entry_removed"
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
        raise RuntimeError("late standalone cleanup failure")

    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth",
    )
    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        side_effect=resistant_cleanup,
    ):
        started = time.monotonic()
        await _async_cleanup_address(
            address,
            reason="entry_removed",
            timeout=0.01,
            cancellation_timeout=0.01,
        )
        assert time.monotonic() - started < 0.1

    await cancellation_seen.wait()
    assert ADDRESS_OWNERSHIP.is_owned(address)
    assert ADDRESS_OWNERSHIP.claim(address) is None

    release.set()
    for _ in range(100):
        if not ADDRESS_OWNERSHIP.is_owned(address):
            break
        await asyncio.sleep(0)

    assert not ADDRESS_OWNERSHIP.is_owned(address)
    await asyncio.sleep(0)
    assert "late standalone cleanup failure" in caplog.text
    replacement_token = ADDRESS_OWNERSHIP.claim(address)
    assert replacement_token is not None
    ADDRESS_OWNERSHIP.request_release(replacement_token)
    ADDRESS_OWNERSHIP.finish_cleanup(replacement_token)
