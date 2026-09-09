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


@pytest.mark.asyncio
async def test_backend_errors_redact_identifiers_without_mutating_runtime(hass):
    runtime = {
        "last_error": "Failed for Bedroom secret at aa:bb:cc:dd:ee:ff",
        "transport": {
            "recent_connection_failures": [
                {"error": "BlueZ /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"},
                {"source": "11:22:33:44:55:66", "attempts": 3},
            ],
        },
    }
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            data=PurifierState(), last_update_success=False,
            client=SimpleNamespace(
                status="disconnected", is_ready=False,
                diagnostic_snapshot=lambda: runtime,
            ),
            custom_auto_diagnostic_snapshot=lambda: {},
        ),
        as_dict=lambda: {
            "title": "Bedroom secret", "unique_id": "AA:BB:CC:DD:EE:FF",
            "data": {"address": "AA:BB:CC:DD:EE:FF", "model": "H7123"},
        },
    )
    before = json.dumps(runtime)
    result = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result).casefold()
    for identifier in (
        "bedroom secret", "aa:bb:cc:dd:ee:ff", "aa_bb_cc_dd_ee_ff",
        "11:22:33:44:55:66",
    ):
        assert identifier not in serialized
    assert json.dumps(runtime) == before
    failures = result["runtime"]["client"]["transport"]["recent_connection_failures"]
    assert failures[1]["attempts"] == 3


def test_export_redacts_raw_frame_samples_but_keeps_timeout_counters():
    from custom_components.govee_ble_air_purifier.diagnostics import (
        _redact_runtime_identifiers,
    )

    sample = "ab 01 04 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f ff"
    result = _redact_runtime_identifiers(
        {"last_timeout_summary": f"attempt 1/3: ignored=1, ignored_sample={sample}"}, ()
    )
    assert sample not in result["last_timeout_summary"]
    assert "attempt 1/3: ignored=1" in result["last_timeout_summary"]
