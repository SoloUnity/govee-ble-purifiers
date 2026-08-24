"""Tests for integration lifecycle cleanup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components import bluetooth as ha_bluetooth
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP

from custom_components.govee_ble_air_purifier import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, PLATFORMS
from custom_components.govee_ble_air_purifier.models import Model


def _setup_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
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


async def test_unload_shuts_down_after_platforms_unload() -> None:
    """A reload or integration update releases the active Bluetooth runtime."""
    coordinator, hass, entry = _setup_objects()
    entry.runtime_data = coordinator

    assert await async_unload_entry(hass, entry)  # type: ignore[arg-type]

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, PLATFORMS
    )
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_remove_entry_closes_stale_connection_before_rediscovery() -> None:
    """Removal performs address cleanup even after runtime data is gone."""
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(data={CONF_ADDRESS: address})

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ) as cleanup,
        patch.object(
            ha_bluetooth,
            "async_rediscover_address",
            new_callable=Mock,
            create=True,
        ) as rediscover,
    ):
        await async_remove_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_awaited_once_with(address, reason="entry_removed")
    rediscover.assert_called_once_with(hass, address)
