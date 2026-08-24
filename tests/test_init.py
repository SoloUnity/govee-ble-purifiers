"""Tests for integration lifecycle cleanup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components import bluetooth as ha_bluetooth
from homeassistant.const import CONF_ADDRESS

from custom_components.govee_ble_air_purifier import async_remove_entry


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
