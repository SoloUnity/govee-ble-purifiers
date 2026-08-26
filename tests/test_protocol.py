"""Tests for typed purifier commands, events, and transaction matching."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.frame import FrameError, build_frame
from custom_components.govee_ble_air_purifier.models import (
    AirQualityEvent,
    DeviceProfile,
    DeviceStateEvent,
    FanMode,
    FanModeEvent,
    Model,
    NightLightColorEvent,
    NightLightStateEvent,
    ProtocolCommand,
    QueryAirQuality,
    QueryDeviceState,
    QueryNightLightColor,
    QueryNightLightState,
    RefreshRequestedEvent,
    SetFanMode,
    SetNightLightBrightness,
    SetNightLightColor,
    SetNightLightPower,
    SetPower,
    StartupFanModeEvent,
)
from custom_components.govee_ble_air_purifier.protocol import (
    GoveePurifierProtocol,
    MatchResult,
    ProtocolError,
)


def _protocol(model: Model = Model.H7124) -> GoveePurifierProtocol:
    return GoveePurifierProtocol(DeviceProfile.for_model(model))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SetPower(False), "330100" + "00" * 16 + "32"),
        (SetPower(True), "330101" + "00" * 16 + "33"),
        (SetFanMode(FanMode.LOW), "3a050101" + "00" * 15 + "3f"),
        (SetFanMode(FanMode.MEDIUM), "3a050102" + "00" * 15 + "3c"),
        (SetFanMode(FanMode.HIGH), "3a050103" + "00" * 15 + "3d"),
        (SetFanMode(FanMode.SLEEP), "3a050500" + "00" * 15 + "3a"),
        (SetFanMode(FanMode.AUTO), "3a0503000014" + "00" * 13 + "28"),
        (SetFanMode(FanMode.TURBO), "3a050700" + "00" * 15 + "38"),
        (SetNightLightPower(False), "3a1b010100" + "00" * 14 + "21"),
        (SetNightLightPower(True), "3a1b010101" + "00" * 14 + "20"),
        (SetNightLightBrightness(50), "3a1b010232" + "00" * 14 + "10"),
        (SetNightLightColor(255, 0, 0), "3a1b050dff" + "00" * 14 + "d6"),
    ],
)
def test_h7124_documented_control_vectors(
    command: ProtocolCommand, expected: str
) -> None:
    """Typed controls reproduce the reference's exact twenty-byte frames."""

    assert _protocol().encode(command) == bytes.fromhex(expected)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (QueryDeviceState(), "aa01" + "00" * 17 + "ab"),
        (QueryNightLightState(), "aa1b01" + "00" * 16 + "b0"),
        (QueryNightLightColor(), "aa1b05" + "00" * 16 + "b4"),
        (QueryAirQuality(), "aa19" + "00" * 17 + "b3"),
    ],
)
def test_documented_query_vectors(command: ProtocolCommand, expected: str) -> None:
    """The only steady poll and the on-demand state queries match captures."""

    assert _protocol().encode(command) == bytes.fromhex(expected)


@pytest.mark.parametrize(
    ("content", "selector", "mode_code", "manual_level", "value", "auto"),
    [
        ("aa 05 00 01 03", 0x00, 0x01, 0x03, None, None),
        ("aa 05 01 04", 0x01, None, None, 0x04, None),
        ("aa 05 03 00 00 12", 0x03, None, None, None, 0x12),
    ],
)
def test_decode_startup_fan_mode_selector_layouts(
    content: str,
    selector: int,
    mode_code: int | None,
    manual_level: int | None,
    value: int | None,
    auto: int | None,
) -> None:
    """The typed aa-05 event preserves each observed selector layout."""

    frame = build_frame(bytes.fromhex(content))
    event = _protocol().decode(frame)

    assert isinstance(event, StartupFanModeEvent)
    assert event.frame == frame
    assert event.selector == selector
    assert event.mode_code == mode_code
    assert event.manual_level == manual_level
    assert event.level_or_configuration == value
    assert event.auto_parameter == auto


def test_h7129_auto_uses_model_specific_parameter() -> None:
    """H7129 Auto uses 0x12 rather than H7124's 0x14."""

    assert _protocol(Model.H7129).encode(SetFanMode(FanMode.AUTO)) == bytes.fromhex(
        "3a 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 2e"
    )


@pytest.mark.parametrize("percent", [0, 101])
def test_reject_invalid_brightness(percent: int) -> None:
    """The protocol documents only whole percentages from one through 100."""

    with pytest.raises(ProtocolError):
        _protocol().encode(SetNightLightBrightness(percent))


def test_initialization_and_refresh_order() -> None:
    """The public descriptors reproduce the documented 23/24 and short sweeps."""

    h7124 = _protocol(Model.H7124)
    h7129 = _protocol(Model.H7129)
    base_prefixes = [
        "33b2",
        "33b5",
        "aa01",
        "aa0500",
        "aa0501",
        "aa0503",
        "aa1b01",
        "aa1b05",
        "aa1e0102",
        "aa10",
        "aa08",
        "aa26",
        "aa16",
        "aa17",
        "aa19",
        "aa0710",
        "aa0711",
        "aa0706",
        "aa0720",
        "aa1f",
        "ab0102",
        "ab0105",
        "ab0104",
    ]

    h7124_requests = h7124.initialization_requests()
    h7129_requests = h7129.initialization_requests()
    assert len(h7124_requests) == 23
    assert len(h7129_requests) == 24
    for descriptor, prefix in zip(h7124_requests, base_prefixes, strict=True):
        assert descriptor.frame.hex().startswith(prefix)
    assert tuple(request.frame for request in h7129_requests[:23]) == tuple(
        request.frame for request in h7124_requests
    )
    assert h7129_requests[23].frame == bytes.fromhex(
        "ab 02 02 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 aa"
    )

    refresh = h7129.refresh_requests()
    assert len(refresh) == 14
    assert tuple(request.frame for request in refresh) == tuple(
        request.frame for request in h7124_requests[1:15]
    )
    assert h7129.device_state_poll().frame == build_frame(b"\xaa\x01")


def test_multipart_response_completion_and_duplicate() -> None:
    """Nine metadata fragments complete only at the documented ab-ff terminator."""

    descriptor = _protocol().initialization_requests()[22]
    matcher = _protocol().new_response_matcher(descriptor)

    for fragment in range(8):
        result = matcher.feed(build_frame(bytes((0xAB, fragment))))
        assert result is MatchResult.ACCEPTED
        assert not matcher.complete
        if fragment == 3:
            assert (
                matcher.feed(build_frame(bytes((0xAB, fragment))))
                is MatchResult.ACCEPTED
            )

    assert matcher.feed(build_frame(b"\xab\xff")) is MatchResult.COMPLETE
    assert matcher.complete
    assert len(matcher.frames) == 9


def test_h7129_additional_metadata_matching() -> None:
    """The additional response must contain its four-byte selector."""

    protocol = _protocol(Model.H7129)
    matcher = protocol.new_response_matcher(protocol.initialization_requests()[23])

    assert matcher.feed(build_frame(b"\xab\x00\x02\x02\x00\x02")) is MatchResult.IGNORED
    assert (
        matcher.feed(build_frame(b"\xab\x00\x55\x02\x02\x00\x01"))
        is MatchResult.COMPLETE
    )


def test_unrelated_notification_does_not_complete_transaction() -> None:
    """Unsolicited status remains useful but cannot finish another request."""

    descriptor = _protocol().initialization_requests()[3]  # aa-05-00
    matcher = _protocol().new_response_matcher(descriptor)

    assert matcher.feed(build_frame(b"\xee\x19")) is MatchResult.IGNORED
    assert matcher.feed(build_frame(b"\xaa\x05\x01")) is MatchResult.IGNORED
    assert matcher.feed(build_frame(b"\xaa\x05\x00")) is MatchResult.COMPLETE


def test_exact_echo_response_boundary() -> None:
    """H7124's exact-echo capability request rejects a differing payload."""

    requests = _protocol().initialization_requests()
    exact_echo = _protocol().new_response_matcher(requests[8])

    assert exact_echo.feed(build_frame(b"\xaa\x1e\x01\x03")) is MatchResult.IGNORED
    assert exact_echo.feed(requests[8].frame) is MatchResult.COMPLETE


def test_h7129_capability_1e_accepts_observed_response() -> None:
    """H7129 completes aa-1e only on its observed non-echo response."""

    protocol = _protocol(Model.H7129)
    descriptor = protocol.initialization_requests()[8]
    observed_response = bytes.fromhex(
        "aa 1e 03 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b6"
    )

    assert descriptor.frame == build_frame(b"\xaa\x1e\x01\x02")
    assert (
        protocol.new_response_matcher(descriptor).feed(observed_response)
        is MatchResult.COMPLETE
    )
    assert (
        protocol.new_response_matcher(descriptor).feed(descriptor.frame)
        is MatchResult.IGNORED
    )
    assert (
        protocol.new_response_matcher(descriptor).feed(
            build_frame(b"\xaa\x1e\x03\x01\x01")
        )
        is MatchResult.IGNORED
    )


def test_h7129_refresh_uses_observed_capability_1e_response() -> None:
    """The active-session refresh reuses H7129's model-specific matcher."""

    protocol = _protocol(Model.H7129)
    descriptor = protocol.refresh_requests()[7]

    assert descriptor.name == "capability_1e_01_02"
    assert (
        protocol.new_response_matcher(descriptor).feed(
            build_frame(b"\xaa\x1e\x03\x01")
        )
        is MatchResult.COMPLETE
    )


def test_h7129_capability_10_accepts_observed_response() -> None:
    """H7129 completes aa-10 only on its observed capability response."""

    protocol = _protocol(Model.H7129)
    descriptor = protocol.initialization_requests()[9]
    observed_response = bytes.fromhex(
        "aa 10 00 ff ff ff 00 00 00 00 00 00 00 00 00 00 00 00 00 45"
    )

    assert descriptor.frame == build_frame(b"\xaa\x10")
    assert (
        protocol.new_response_matcher(descriptor).feed(observed_response)
        is MatchResult.COMPLETE
    )
    assert (
        protocol.new_response_matcher(descriptor).feed(descriptor.frame)
        is MatchResult.IGNORED
    )
    assert (
        protocol.new_response_matcher(descriptor).feed(
            build_frame(b"\xaa\x10\x00\xff\xff\xff\x01")
        )
        is MatchResult.IGNORED
    )


def test_h7129_refresh_uses_observed_capability_10_response() -> None:
    """The active-session refresh reuses H7129's aa-10 matcher."""

    protocol = _protocol(Model.H7129)
    descriptor = protocol.refresh_requests()[8]

    assert descriptor.name == "capability_10"
    assert (
        protocol.new_response_matcher(descriptor).feed(
            build_frame(b"\xaa\x10\x00\xff\xff\xff")
        )
        is MatchResult.COMPLETE
    )


@pytest.mark.parametrize("model", [Model.H7124, Model.H7129])
@pytest.mark.parametrize("value", [0x00, 0x01])
def test_capability_b2_accepts_observed_values(model: Model, value: int) -> None:
    """Both models complete capability B2 on either observed value."""

    protocol = _protocol(model)
    descriptor = protocol.initialization_requests()[0]
    matcher = protocol.new_response_matcher(descriptor)

    assert matcher.feed(build_frame(bytes((0x33, 0xB2, value)))) is MatchResult.COMPLETE
    assert matcher.complete


@pytest.mark.parametrize("model", [Model.H7124, Model.H7129])
def test_capability_b2_rejects_unobserved_response_shapes(model: Model) -> None:
    """The observed values do not broaden matching beyond their known shape."""

    protocol = _protocol(model)
    descriptor = protocol.initialization_requests()[0]

    wrong_prefix = protocol.new_response_matcher(descriptor)
    unknown_value = protocol.new_response_matcher(descriptor)
    nonzero_tail = protocol.new_response_matcher(descriptor)

    assert wrong_prefix.feed(build_frame(b"\x33\xb5\x01")) is MatchResult.IGNORED
    assert unknown_value.feed(build_frame(b"\x33\xb2\x02")) is MatchResult.IGNORED
    assert nonzero_tail.feed(build_frame(b"\x33\xb2\x01\x01")) is MatchResult.IGNORED

    invalid_checksum = bytearray(build_frame(b"\x33\xb2\x01"))
    invalid_checksum[-1] ^= 0x01
    with pytest.raises(FrameError):
        protocol.new_response_matcher(descriptor).feed(invalid_checksum)


def test_power_confirmation_waits_for_applied_aa01_state() -> None:
    """A local write/echo does not prove that purifier power was applied."""

    protocol = _protocol()
    matcher = protocol.new_response_matcher(protocol.command_request(SetPower(True)))

    assert matcher.feed(build_frame(b"\x33\x01\x01")) is MatchResult.IGNORED
    assert matcher.feed(build_frame(b"\xaa\x01\x00")) is MatchResult.IGNORED
    assert matcher.feed(build_frame(b"\xaa\x01\x01")) is MatchResult.COMPLETE


@pytest.mark.parametrize("model", [Model.H7124, Model.H7129])
@pytest.mark.parametrize("mode", list(FanMode))
def test_fan_command_accepts_only_its_exact_echo(model: Model, mode: FanMode) -> None:
    """Both plaintext and decrypted sessions use the exact 3a-05 acknowledgement."""

    protocol = _protocol(model)
    descriptor = protocol.command_request(SetFanMode(mode))
    matcher = protocol.new_response_matcher(descriptor)

    assert matcher.feed(descriptor.frame) is MatchResult.COMPLETE
    assert matcher.feed(descriptor.frame) is MatchResult.IGNORED


@pytest.mark.parametrize("model", [Model.H7124, Model.H7129])
def test_fan_command_rejects_a_different_mode_echo(model: Model) -> None:
    """A stale or unrelated 3a-05 echo cannot confirm the pending mode."""

    protocol = _protocol(model)
    matcher = protocol.new_response_matcher(
        protocol.command_request(SetFanMode(FanMode.HIGH))
    )

    assert (
        matcher.feed(protocol.encode(SetFanMode(FanMode.MEDIUM)))
        is MatchResult.IGNORED
    )
    assert matcher.feed(build_frame(b"\xee\x05\x01\x03")) is MatchResult.COMPLETE


@pytest.mark.parametrize(
    ("mode", "notification"),
    [
        (FanMode.LOW, "ee050101"),
        (FanMode.AUTO, "ee0503000012"),
        (FanMode.SLEEP, "ee050503"),
        (FanMode.TURBO, "ee050703"),
    ],
)
def test_h7129_mode_confirmation_uses_authoritative_notification(
    mode: FanMode, notification: str
) -> None:
    """Mode matching handles H7129's retained byte 3 for Sleep and Turbo."""

    protocol = _protocol(Model.H7129)
    matcher = protocol.new_response_matcher(protocol.command_request(SetFanMode(mode)))

    assert (
        matcher.feed(build_frame(bytes.fromhex(notification))) is MatchResult.COMPLETE
    )


def test_rgb_fc_response_completes_query_without_replacing_color() -> None:
    """H7129's checksum-valid fc response has no usable RGB value."""

    protocol = _protocol(Model.H7129)
    frame = build_frame(bytes.fromhex("aa 1b 05 fc"))
    event = protocol.decode(frame)
    matcher = protocol.new_response_matcher(
        protocol.command_request(QueryNightLightColor())
    )

    assert isinstance(event, NightLightColorEvent)
    assert not event.color_available
    assert event.red is event.green is event.blue is None
    assert matcher.feed(frame) is MatchResult.COMPLETE


def test_decode_documented_notifications() -> None:
    """Authoritative physical changes and status updates become typed events."""

    protocol = _protocol(Model.H7129)
    state = protocol.decode(build_frame(bytes.fromhex("aa 01 01 00 81 00 05")))
    fan = protocol.decode(build_frame(bytes.fromhex("ee 05 05 03")))
    light = protocol.decode(build_frame(bytes.fromhex("ee 1b 01 01 64")))
    status = protocol.decode(
        bytes.fromhex("ee 19 81 00 03 01 00 49 00 00 00 00 00 00 00 00 00 00 00 3d")
    )
    refresh = protocol.decode(build_frame(b"\xee\xaa"))

    assert isinstance(state, DeviceStateEvent)
    assert state.power is True
    assert state.volatile_state == 5
    assert isinstance(fan, FanModeEvent)
    assert fan.mode is FanMode.SLEEP
    assert isinstance(light, NightLightStateEvent)
    assert light.power is True
    assert light.brightness == 100
    assert light.unsolicited
    assert isinstance(status, AirQualityEvent)
    assert status.pm25_ug_m3 == 3
    assert status.filter_life == 73
    assert status.unsolicited
    assert isinstance(refresh, RefreshRequestedEvent)


def test_unknown_auto_parameter_is_not_mislabeled() -> None:
    """Undocumented H7129 Auto variants remain unknown."""

    event = _protocol(Model.H7129).decode(
        build_frame(bytes.fromhex("ee 05 03 00 00 99"))
    )

    assert isinstance(event, FanModeEvent)
    assert event.mode is None


def test_pm25_sentinel_is_unavailable() -> None:
    """Values above 999, including ff-ff, are never published as PM2.5."""

    event = _protocol().decode(build_frame(bytes.fromhex("aa 19 81 ff ff")))

    assert isinstance(event, AirQualityEvent)
    assert event.raw_pm25 == 0xFFFF
    assert event.pm25_ug_m3 is None
