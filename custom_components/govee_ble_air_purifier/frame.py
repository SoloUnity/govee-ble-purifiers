"""Twenty-byte Govee application-frame construction and validation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor

FRAME_LENGTH = 20
FRAME_CONTENT_LENGTH = FRAME_LENGTH - 1


class FrameError(ValueError):
    """Base class for malformed application frames."""


class FrameLengthError(FrameError):
    """Raised when a frame is not exactly twenty bytes."""


class FrameChecksumError(FrameError):
    """Raised when a frame's XOR checksum is invalid."""


def xor_checksum(content: bytes | bytearray | memoryview) -> int:
    """Return the XOR of all bytes in *content*."""

    return reduce(xor, content, 0)


def build_frame(content: bytes | bytearray | memoryview) -> bytes:
    """Pad up to nineteen content bytes and append their XOR checksum."""

    content = bytes(content)
    if len(content) > FRAME_CONTENT_LENGTH:
        raise FrameLengthError(
            f"frame content is {len(content)} bytes; maximum is {FRAME_CONTENT_LENGTH}"
        )
    padded = content.ljust(FRAME_CONTENT_LENGTH, b"\x00")
    return padded + bytes((xor_checksum(padded),))


def validate_frame(frame: bytes | bytearray | memoryview) -> bytes:
    """Validate and return an immutable twenty-byte application frame."""

    frame = bytes(frame)
    if len(frame) != FRAME_LENGTH:
        raise FrameLengthError(
            f"application frame is {len(frame)} bytes; expected {FRAME_LENGTH}"
        )
    expected = xor_checksum(frame[:FRAME_CONTENT_LENGTH])
    if frame[FRAME_CONTENT_LENGTH] != expected:
        raise FrameChecksumError(
            f"invalid checksum 0x{frame[19]:02x}; expected 0x{expected:02x}"
        )
    return frame


def is_valid_frame(frame: bytes | bytearray | memoryview) -> bool:
    """Return whether *frame* is a checksum-valid application frame."""

    try:
        validate_frame(frame)
    except FrameError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ApplicationFrame:
    """A validated immutable application frame."""

    data: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", validate_frame(self.data))

    @classmethod
    def build(cls, content: bytes | bytearray | memoryview) -> ApplicationFrame:
        """Build a frame from unpadded content bytes."""

        return cls(build_frame(content))

    @classmethod
    def from_bytes(cls, frame: bytes | bytearray | memoryview) -> ApplicationFrame:
        """Validate an existing frame."""

        return cls(bytes(frame))

    @property
    def prefix(self) -> int:
        return self.data[0]

    @property
    def command(self) -> int:
        return self.data[1]

    @property
    def payload(self) -> bytes:
        return self.data[2:19]

    @property
    def checksum(self) -> int:
        return self.data[19]

    def __bytes__(self) -> bytes:
        return self.data

    def __len__(self) -> int:
        return FRAME_LENGTH

    def __getitem__(self, index: int | slice) -> int | bytes:
        return self.data[index]
