"""Regression vectors for the local H7123/H712C extension."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.govee_ble_air_purifier.frame import build_frame
from custom_components.govee_ble_air_purifier.models import FanMode, SetFanMode
from custom_components.govee_ble_air_purifier.profiles import (
    DeviceProfile,
    load_profile_registry,
)
from custom_components.govee_ble_air_purifier.protocol import (
    GoveePurifierProtocol,
    ProtocolError,
)
from custom_components.govee_ble_air_purifier.state_reducer import PurifierStateReducer


@pytest.mark.parametrize(
    ("model", "mode", "wire"),
    [
        ("H7123", FanMode.LOW, "3a050101"),
        ("H7123", FanMode.AUTO, "3a0503000014"),
        ("H712C", FanMode.LOW, "33050101"),
        ("H712C", FanMode.MEDIUM, "33050102"),
        ("H712C", FanMode.HIGH, "33050103"),
    ],
)
def test_model_specific_source_vectors(model, mode, wire):
    assert GoveePurifierProtocol(DeviceProfile.for_model(model)).encode(
        SetFanMode(mode)
    ) == build_frame(bytes.fromhex(wire))


@pytest.mark.parametrize(
    ("model", "mode"), [("H7123", FanMode.TURBO), ("H712C", FanMode.AUTO)]
)
def test_reject_unsupported_modes(model, mode):
    with pytest.raises(ProtocolError):
        GoveePurifierProtocol(DeviceProfile.for_model(model)).encode(SetFanMode(mode))


@pytest.mark.parametrize(
    ("wire", "mode"),
    [
        ("aa 05 00 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 a8", FanMode.TURBO),
        ("aa 05 00 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 af", FanMode.LOW),
    ],
)
def test_h712c_observed_startup_vectors(wire, mode):
    profile = DeviceProfile.for_model("H712C")
    protocol = GoveePurifierProtocol(profile)
    reducer = PurifierStateReducer(profile)
    reducer.reduce_event(
        protocol.decode(bytes.fromhex(wire)),
        generation=1,
        matched_request="mode_data_00",
    )
    assert reducer.state.fan_mode is mode


def test_exact_selection_and_no_unverified_sensors():
    r = load_profile_registry()
    for name, model in [("GVH712Ctest", "H712C"), ("ihoment_H7123_test", "H7123")]:
        profile = r.match_name(name)
        assert profile.model == model
        assert not any(
            (
                profile.capabilities.light,
                profile.capabilities.pm25,
                profile.capabilities.filter_life,
            )
        )
    assert r.match_name("GVH712Xunknown") is None




@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "name"),
    [("H712C", "GVH712Ctest"), ("H7123", "ihoment_H7123_test")],
)
async def test_config_flow_accepts_string_model_and_skips_custom_auto(
    monkeypatch, model, name
):
    from custom_components.govee_ble_air_purifier.config_flow import (
        GoveeBleAirPurifierConfigFlow,
    )
    from custom_components.govee_ble_air_purifier.discovery import DiscoveredPurifier

    flow = GoveeBleAirPurifierConfigFlow()
    flow._registry = load_profile_registry()
    flow._manual_discoveries = (
        DiscoveredPurifier("target", name, model, -60),
    )
    flow._discovery = MagicMock()
    flow._discovery.async_wait_for_selected_advertisement = AsyncMock(
        return_value=SimpleNamespace(name=name, rssi=-60)
    )
    flow._discovery.model_from_name.return_value = model
    flow._validator = SimpleNamespace(async_validate=AsyncMock())
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None)
    monkeypatch.setattr(
        flow,
        "_async_create_pending_entry",
        lambda options: {"type": "create_entry", "options": options},
    )
    result = await flow.async_step_user({"address": "target"})
    assert result["type"] == "create_entry"
    assert result["options"] == {"custom_auto_enabled": False}


def test_h7123_encrypted_profile_and_observed_direct_fan_status():
    from custom_components.govee_ble_air_purifier.models import (
        FanModeEvent,
        SecurityMode,
    )
    from custom_components.govee_ble_air_purifier.protocol import MatchResult

    profile = DeviceProfile.for_model("H7123")
    assert profile.security is SecurityMode.H7129_SESSION
    protocol = GoveePurifierProtocol(profile)
    frame = bytes.fromhex("aa 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ac")
    event = protocol.decode(frame)
    assert isinstance(event, FanModeEvent)
    assert event.mode is FanMode.MEDIUM
    descriptor = protocol.command_request(SetFanMode(FanMode.MEDIUM))
    assert protocol.new_response_matcher(descriptor).feed(frame) is MatchResult.COMPLETE
    descriptor = protocol.command_request(SetFanMode(FanMode.LOW))
    assert protocol.new_response_matcher(descriptor).feed(frame) is MatchResult.IGNORED


def test_h7123_standard_initialization_handshake_vectors():
    from custom_components.govee_ble_air_purifier.protocol import MatchResult

    protocol = GoveePurifierProtocol(DeviceProfile.for_model("H7123"))
    requests = protocol.initialization_requests()
    assert requests[0].frame == build_frame(bytes.fromhex("33 b2"))
    assert requests[1].frame == build_frame(bytes.fromhex("33 b5"))
    assert (
        protocol.new_response_matcher(requests[0]).feed(
            build_frame(bytes.fromhex("33 b2 01"))
        )
        is MatchResult.COMPLETE
    )
    assert (
        protocol.new_response_matcher(requests[1]).feed(
            build_frame(bytes.fromhex("33 b5"))
        )
        is MatchResult.COMPLETE
    )


@pytest.mark.parametrize("model", ["H7123", "H712C"])
@pytest.mark.parametrize("wire", ["aa190001", "aa1b010164", "ee1b010164"])
def test_added_models_do_not_decode_unverified_sensor_or_light_fields(model, wire):
    from custom_components.govee_ble_air_purifier.models import UnknownEvent

    protocol = GoveePurifierProtocol(DeviceProfile.for_model(model))
    assert isinstance(protocol.decode(build_frame(bytes.fromhex(wire))), UnknownEvent)


@pytest.mark.parametrize("model", ["H7123", "H712C"])
def test_added_models_only_poll_power_state(model):
    profile = DeviceProfile.for_model(model)
    protocol = GoveePurifierProtocol(profile)
    assert protocol.device_state_poll().frame == build_frame(bytes.fromhex("aa01"))




@pytest.mark.parametrize("model", ["H7123", "H712C", "H7124", "H7129"])
def test_decoding_untrusted_frame_bytes_does_not_crash(model):
    """Exercise every prefix and command byte with arbitrary bounded payloads."""
    import random

    random_bytes = random.Random(7123)
    protocol = GoveePurifierProtocol(DeviceProfile.for_model(model))
    for field in (0, 1):
        for value in range(256):
            payload = bytearray(random_bytes.randbytes(19))
            payload[field] = value
            protocol.decode(build_frame(payload))
