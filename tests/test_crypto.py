"""Tests for the H7129 per-frame transform and session helpers."""

from __future__ import annotations

import pytest

from custom_components.govee_ble_air_purifier.crypto import (
    COMMUNICATION_KEY,
    CryptoError,
    create_negotiation_frame,
    decrypt_frame,
    encrypt_frame,
    extract_session_key,
    rc4_keystream,
)
from custom_components.govee_ble_air_purifier.frame import (
    FrameChecksumError,
    build_frame,
    validate_frame,
)


def test_aes_known_answer() -> None:
    """The AES block primitive agrees with the NIST AES-128 vector."""

    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plaintext_block = bytes.fromhex("00112233445566778899aabbccddeeff")
    plaintext_frame = build_frame(plaintext_block)

    assert encrypt_frame(plaintext_frame, key)[:16] == bytes.fromhex(
        "69c4e0d86a7b0430d8cdb78070b4c55a"
    )


def test_h7129_frame_round_trip_and_per_frame_reset() -> None:
    """AES plus the four-byte RC4-compatible tail round trips independently."""

    plaintext = build_frame(b"\xaa\x01")
    encrypted = encrypt_frame(plaintext, COMMUNICATION_KEY)

    assert len(encrypted) == 20
    assert encrypted != plaintext
    assert decrypt_frame(encrypted, COMMUNICATION_KEY) == plaintext
    # Per-frame RC4 state is reset, so equal inputs under one key stay equal.
    assert encrypt_frame(plaintext, COMMUNICATION_KEY) == encrypted


def test_rc4_keystream_is_deterministic_and_keyed() -> None:
    """The tail keystream is freshly derived from the supplied session key."""

    first = rc4_keystream(COMMUNICATION_KEY, 4)

    assert len(first) == 4
    assert rc4_keystream(COMMUNICATION_KEY, 4) == first
    assert rc4_keystream(bytes(range(16)), 4) != first


@pytest.mark.parametrize("step", [0x01, 0x02])
def test_negotiation_frame_uses_random_padding(step: int) -> None:
    """Both negotiation requests contain seventeen random padding bytes."""

    padding = bytes(range(17))
    frame = create_negotiation_frame(step, lambda length: padding)

    assert frame[:2] == bytes((0xE7, step))
    assert frame[2:19] == padding
    assert validate_frame(frame) == frame


def test_extract_session_key() -> None:
    """The e7-01 response carries its sixteen-byte session key at bytes 2-17."""

    session_key = bytes(range(16))
    response = build_frame(b"\xe7\x01" + session_key + b"\x55")

    assert extract_session_key(response) == session_key


def test_extract_session_key_rejects_wrong_step() -> None:
    """An e7-02 confirmation cannot be mistaken for key material."""

    with pytest.raises(CryptoError):
        extract_session_key(build_frame(b"\xe7\x02"))


def test_decryption_rejects_wrong_key() -> None:
    """A frame decrypted with a different key fails application validation."""

    encrypted = encrypt_frame(build_frame(b"\xaa\x01"), COMMUNICATION_KEY)

    with pytest.raises(FrameChecksumError):
        decrypt_frame(encrypted, bytes(range(16)))
