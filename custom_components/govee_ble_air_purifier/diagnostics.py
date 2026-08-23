"""Diagnostics support for Govee BLE Air Purifier."""

from __future__ import annotations

from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import GoveeConfigEntry

_TO_REDACT = {CONF_ADDRESS, "unique_id", "title"}
_STATE_FIELDS = (
    "power",
    "fan_mode",
    "light_power",
    "light_brightness",
    "light_rgb",
    "pm25",
    "filter_life",
)
_RUNTIME_FIELDS = ("last_update_success",)


def _diagnostic_value(value: Any) -> Any:
    """Convert cached values to diagnostics-safe primitives."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GoveeConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a purifier config entry."""
    coordinator = entry.runtime_data
    state = coordinator.data

    return {
        "entry": async_redact_data(entry.as_dict(), _TO_REDACT),
        "runtime": {
            **{
                field: _diagnostic_value(getattr(coordinator, field, None))
                for field in _RUNTIME_FIELDS
            },
            "client_status": _diagnostic_value(coordinator.client.status),
            "client_ready": coordinator.client.is_ready,
        },
        "cached_state": {
            field: _diagnostic_value(getattr(state, field, None))
            for field in _STATE_FIELDS
        },
    }
