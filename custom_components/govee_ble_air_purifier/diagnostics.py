"""Diagnostics support for Govee BLE Air Purifier."""

from __future__ import annotations

import re
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
_ADDRESS_IN_TEXT = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:_-]){5}[0-9a-f]{2}(?![0-9a-f])"
)

_FRAME_IN_TEXT = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2} ){19}[0-9a-f]{2}(?![0-9a-f])"
)

def _redact_runtime_identifiers(value: Any, identifiers: tuple[str, ...]) -> Any:
    """Redact backend error strings and BlueZ paths as well as structured fields."""
    if isinstance(value, dict):
        return {
            key: _redact_runtime_identifiers(item, identifiers)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_runtime_identifiers(item, identifiers) for item in value]
    if isinstance(value, str):
        value = _FRAME_IN_TEXT.sub("**FRAME REDACTED**", value)
        value = _ADDRESS_IN_TEXT.sub("**REDACTED**", value)
        for identifier in identifiers:
            value = re.sub(re.escape(identifier), "**REDACTED**", value, flags=re.I)
    return value



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

    entry_data = entry.as_dict()
    identifiers = tuple(
        item for item in (
            entry_data.get("title"), entry_data.get("unique_id"),
            entry_data.get("data", {}).get(CONF_ADDRESS),
        ) if isinstance(item, str) and item
    )
    diagnostics = {
        "entry": async_redact_data(entry_data, _TO_REDACT),
        "runtime": {
            **{
                field: _diagnostic_value(getattr(coordinator, field, None))
                for field in _RUNTIME_FIELDS
            },
            "client_status": _diagnostic_value(coordinator.client.status),
            "client_ready": coordinator.client.is_ready,
            "client": coordinator.client.diagnostic_snapshot(),
        },
        "cached_state": {
            field: _diagnostic_value(getattr(state, field, None))
            for field in _STATE_FIELDS
        },
        "custom_auto": coordinator.custom_auto_diagnostic_snapshot(),
    }
    return _redact_runtime_identifiers(diagnostics, identifiers)
