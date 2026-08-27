"""Bluetooth errors and safe exception formatting helpers."""

from __future__ import annotations


def exception_detail(error: BaseException) -> str:
    """Return a useful one-line exception description, including empty errors."""
    message = str(error).strip()
    return f"{type(error).__name__}: {message or repr(error)}"


def exception_chain_detail(error: BaseException) -> str:
    """Return the complete explicit/implicit exception chain on one line."""
    details: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        details.append(exception_detail(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " <- ".join(details)


class BluetoothUnavailableError(ConnectionError):
    """Raised when there is no usable Bluetooth route to the purifier."""


class GattTransportError(ConnectionError):
    """Raised when a GATT operation cannot be completed."""
