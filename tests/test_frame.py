"""Tests for Govee twenty-byte application frames."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.frame import (
    ApplicationFrame,
    FrameChecksumError,
    FrameLengthError,
    build_frame,
    is_valid_frame,
    validate_frame,
    xor_checksum,
)


def test_build_documented_power_on_frame() -> None:
    """Padding and XOR produce the documented power-on vector."""

    frame = build_frame(bytes.fromhex("33 01 01"))

    assert frame == bytes.fromhex(
        "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33"
    )
    assert xor_checksum(frame[:19]) == frame[19]
    assert validate_frame(frame) == frame


def test_application_frame_properties() -> None:
    """The immutable wrapper exposes the validated frame fields."""

    frame = ApplicationFrame.build(bytes.fromhex("aa 19"))

    assert frame.prefix == 0xAA
    assert frame.command == 0x19
    assert frame.payload == bytes(17)
    assert frame.checksum == 0xB3
    assert bytes(frame) == bytes.fromhex(
        "aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3"
    )


def test_reject_invalid_checksum() -> None:
    """A damaged frame is never passed to the protocol decoder."""

    damaged = bytearray(build_frame(b"\xaa\x01"))
    damaged[19] ^= 0x01

    with pytest.raises(FrameChecksumError):
        validate_frame(damaged)
    assert not is_valid_frame(damaged)


@pytest.mark.parametrize("length", [0, 19, 21])
def test_reject_invalid_frame_length(length: int) -> None:
    """Wire frames must contain exactly twenty bytes."""

    with pytest.raises(FrameLengthError):
        validate_frame(bytes(length))


def test_reject_oversized_frame_content() -> None:
    """The content builder reserves byte 19 for the checksum."""

    with pytest.raises(FrameLengthError):
        build_frame(bytes(20))
