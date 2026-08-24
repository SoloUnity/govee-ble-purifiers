"""Tests for quiet coordinator availability transitions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.govee_ble_air_purifier import coordinator as coordinator_module
from custom_components.govee_ble_air_purifier.coordinator import (
    GoveeDataUpdateCoordinator,
)
from custom_components.govee_ble_air_purifier.models import PurifierState


def _coordinator() -> GoveeDataUpdateCoordinator:
    coordinator = object.__new__(GoveeDataUpdateCoordinator)
    coordinator.name = "Bedroom purifier"
    coordinator.data = PurifierState()
    coordinator._client_available = True
    coordinator.client = SimpleNamespace(state=PurifierState(power=True))
    coordinator.async_update_listeners = Mock()
    coordinator.async_set_updated_data = Mock()
    return coordinator


def test_unavailable_transition_notifies_without_update_error() -> None:
    """Expected weak-signal recovery only changes entity availability."""
    coordinator = _coordinator()

    with patch.object(coordinator_module._LOGGER, "error") as error_log:
        coordinator._availability_updated(False, ConnectionError("weak signal"))

    assert not coordinator.client_available
    error_log.assert_not_called()
    coordinator.async_update_listeners.assert_called_once_with()
    coordinator.async_set_updated_data.assert_not_called()


def test_ready_transition_publishes_initialized_state() -> None:
    """Successful recovery republishes state and restores availability."""
    coordinator = _coordinator()
    coordinator._client_available = False

    coordinator._availability_updated(True, None)

    assert coordinator.client_available
    coordinator.async_set_updated_data.assert_called_once_with(
        coordinator.client.state
    )
    coordinator.async_update_listeners.assert_not_called()


def test_unexpected_recovery_failure_remains_visible() -> None:
    """Quiet link recovery cannot hide an unexpected implementation failure."""
    coordinator = _coordinator()

    with patch.object(coordinator_module._LOGGER, "error") as error_log:
        coordinator._availability_updated(False, RuntimeError("unexpected"))

    error_log.assert_called_once()
    coordinator.async_update_listeners.assert_called_once_with()
