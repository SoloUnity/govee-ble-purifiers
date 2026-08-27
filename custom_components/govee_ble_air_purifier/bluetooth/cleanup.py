"""Address-level Bluetooth connection cleanup."""

from __future__ import annotations

import logging

from bleak_retry_connector import close_stale_connections_by_address

_LOGGER = logging.getLogger(__package__)


async def async_close_stale_connections(address: str, *, reason: str) -> None:
    """Close local BlueZ connections for an address through HA's BLE library."""
    _LOGGER.debug(
        "Closing stale Bluetooth connections by address: address=%s reason=%s",
        address,
        reason,
    )
    await close_stale_connections_by_address(address)
