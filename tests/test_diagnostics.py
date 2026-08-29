from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.govee_ble_air_purifier.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.govee_ble_air_purifier.models import FanMode, PurifierState


@pytest.mark.asyncio
async def test_entry_diagnostics_include_custom_auto_and_preserve_redaction(
    hass,
) -> None:
    custom_auto = {
        "exposed": False,
        "enabled": False,
        "controller_present": False,
        "task_counts": {"actor": 0, "sample": 0, "timer": 0, "command": 0},
    }
    custom_auto_snapshot = Mock(return_value=custom_auto)
    client = SimpleNamespace(
        status="disconnected",
        is_ready=False,
        diagnostic_snapshot=Mock(return_value={"secret_material": "[redacted]"}),
    )
    coordinator = SimpleNamespace(
        data=PurifierState(power=False, fan_mode=FanMode.AUTO),
        last_update_success=False,
        client=client,
        custom_auto_diagnostic_snapshot=custom_auto_snapshot,
    )
    entry = SimpleNamespace(
        runtime_data=coordinator,
        as_dict=lambda: {
            "title": "Bedroom secret",
            "unique_id": "AA:BB:CC:DD:EE:FF",
            "data": {"address": "AA:BB:CC:DD:EE:FF", "model": "H7129"},
            "options": {},
        },
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["custom_auto"] == custom_auto
    custom_auto_snapshot.assert_called_once_with()
    serialized = json.dumps(diagnostics)
    assert "AA:BB:CC:DD:EE:FF" not in serialized
    assert "Bedroom secret" not in serialized
    assert "session_key" not in serialized
    assert "negotiation_secret" not in serialized
