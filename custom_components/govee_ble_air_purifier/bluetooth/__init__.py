"""Internal Bluetooth package for scanner access and GATT transport."""

from .cleanup import async_close_stale_connections
from .environment import HomeAssistantBluetoothEnvironment
from .errors import (
    BluetoothUnavailableError,
    GattTransportError,
    exception_chain_detail,
    exception_detail,
)
from .settings import (
    BluetoothRuntimeSettings,
    CleanupSettings,
    ConnectionSettings,
    GattEndpoints,
    GattOperationSettings,
    RouteSelectionSettings,
)
from .transport import GattTransport

__all__ = [
    "BluetoothRuntimeSettings",
    "BluetoothUnavailableError",
    "CleanupSettings",
    "ConnectionSettings",
    "GattEndpoints",
    "GattOperationSettings",
    "GattTransport",
    "GattTransportError",
    "HomeAssistantBluetoothEnvironment",
    "RouteSelectionSettings",
    "async_close_stale_connections",
    "exception_chain_detail",
    "exception_detail",
]
