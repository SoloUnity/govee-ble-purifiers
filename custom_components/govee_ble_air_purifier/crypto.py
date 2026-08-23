"""H7129 application-session frame transform.

Govee transforms every frame independently: the first sixteen bytes use one
AES-128-ECB block and bytes 16-19 use the first four bytes of an RC4-compatible
keystream.  The RC4 state is therefore re-created for every call.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from .frame import FRAME_LENGTH, build_frame, validate_frame

COMMUNICATION_KEY = b"MakingLifeSmarte"
AES_BLOCK_LENGTH = 16


class CryptoError(ValueError):
    """Raised for invalid encryption inputs or negotiation frames."""


def _validate_key(key: bytes | bytearray | memoryview) -> bytes:
    key = bytes(key)
    if len(key) != AES_BLOCK_LENGTH:
        raise CryptoError(f"key is {len(key)} bytes; expected 16")
    return key


def rc4_keystream(key: bytes | bytearray | memoryview, length: int) -> bytes:
    """Generate *length* bytes with the protocol's RC4-compatible transform."""

    key = _validate_key(key)
    if length < 0:
        raise ValueError("length must not be negative")

    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]

    output = bytearray(length)
    i = j = 0
    for offset in range(length):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        output[offset] = state[(state[i] + state[j]) & 0xFF]
    return bytes(output)


def _xor_tail(tail: bytes, key: bytes) -> bytes:
    stream = rc4_keystream(key, len(tail))
    return bytes(value ^ mask for value, mask in zip(tail, stream, strict=True))


def _aes_ecb_block(block: bytes, key: bytes, *, encrypt: bool) -> bytes:
    """Transform one AES block using Home Assistant's cryptography runtime."""

    try:
        from cryptography.hazmat.primitives.ciphers import (  # noqa: PLC0415
            Cipher,
            algorithms,
            modes,
        )
    except ImportError as err:  # pragma: no cover - HA always provides it
        raise CryptoError("the cryptography package is required for H7129") from err

    context = (
        Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        if encrypt
        else Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    )
    return context.update(block) + context.finalize()


def encrypt_frame(
    plaintext: bytes | bytearray | memoryview,
    key: bytes | bytearray | memoryview,
) -> bytes:
    """Validate and encrypt one independent twenty-byte application frame."""

    plaintext = validate_frame(plaintext)
    key = _validate_key(key)
    encrypted_head = _aes_ecb_block(plaintext[:AES_BLOCK_LENGTH], key, encrypt=True)
    return encrypted_head + _xor_tail(plaintext[AES_BLOCK_LENGTH:], key)


def decrypt_frame(
    ciphertext: bytes | bytearray | memoryview,
    key: bytes | bytearray | memoryview,
) -> bytes:
    """Decrypt and checksum-validate one independent H7129 wire frame."""

    ciphertext = bytes(ciphertext)
    if len(ciphertext) != FRAME_LENGTH:
        raise CryptoError(
            f"encrypted frame is {len(ciphertext)} bytes; expected {FRAME_LENGTH}"
        )
    key = _validate_key(key)
    plaintext_head = _aes_ecb_block(ciphertext[:AES_BLOCK_LENGTH], key, encrypt=False)
    plaintext = plaintext_head + _xor_tail(ciphertext[AES_BLOCK_LENGTH:], key)
    return validate_frame(plaintext)


def create_negotiation_frame(
    step: int,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> bytes:
    """Create a checksum-valid ``e7 01`` or ``e7 02`` frame with random padding."""

    if step not in (0x01, 0x02):
        raise CryptoError("negotiation step must be 0x01 or 0x02")
    padding = random_bytes(17)
    if len(padding) != 17:
        raise CryptoError("random source must return exactly 17 bytes")
    return build_frame(bytes((0xE7, step)) + padding)


def extract_session_key(frame: bytes | bytearray | memoryview) -> bytes:
    """Extract bytes 2-17 from a validated plaintext ``e7 01`` response."""

    frame = validate_frame(frame)
    if frame[:2] != b"\xe7\x01":
        raise CryptoError("session key is only present in an e7 01 response")
    return frame[2:18]
