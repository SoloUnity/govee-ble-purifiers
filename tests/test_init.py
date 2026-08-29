"""Tests for integration lifecycle cleanup."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryError

from custom_components.govee_ble_air_purifier import (
    _async_cleanup_address,
    _async_options_updated,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.govee_ble_air_purifier.bluetooth.ownership import (
    ADDRESS_OWNERSHIP,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, PLATFORMS
from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
)
from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import ProfileError


@pytest.fixture(autouse=True)
def _mock_entity_registry():
    """Keep lifecycle unit tests independent from Home Assistant storage."""
    with patch(
        "custom_components.govee_ble_air_purifier.er.async_get",
        return_value=MagicMock(),
    ):
        yield


def _setup_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
        async_wait_until_ready=AsyncMock(),
        async_shutdown=AsyncMock(),
        custom_auto_controller=None,
        async_deactivate_custom_auto=AsyncMock(),
    )
    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
        async_reload=AsyncMock(return_value=True),
    )
    bus = SimpleNamespace(async_listen_once=Mock(return_value=Mock()))
    hass = SimpleNamespace(config_entries=config_entries, bus=bus)
    entry = SimpleNamespace(
        data={
            CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_MODEL: Model.H7129.value,
        },
        options={},
        title="Bedroom purifier",
        entry_id="entry-id",
        async_on_unload=Mock(),
        add_update_listener=Mock(return_value=Mock()),
    )
    return coordinator, hass, entry


async def test_setup_cleans_address_before_start_and_registers_stop() -> None:
    """Setup clears crash leftovers and awaits shutdown on Home Assistant stop."""
    coordinator, hass, entry = _setup_objects()
    order: list[str] = []

    async def cleanup(address: str, *, reason: str) -> None:
        assert address == entry.data[CONF_ADDRESS]
        assert reason == "entry_setup"
        order.append("cleanup")

    async def start() -> None:
        order.append("start")

    coordinator.async_start.side_effect = start
    cancel_stop_listener = hass.bus.async_listen_once.return_value

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            side_effect=cleanup,
        ) as close_address,
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert order == ["cleanup", "start"]
    close_address.assert_awaited_once_with(
        entry.data[CONF_ADDRESS], reason="entry_setup"
    )
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS
    )
    coordinator.async_wait_until_ready.assert_not_awaited()
    hass.bus.async_listen_once.assert_called_once()
    event_type, stop_callback = hass.bus.async_listen_once.call_args.args
    assert event_type == EVENT_HOMEASSISTANT_STOP
    assert entry.async_on_unload.call_count == 2
    entry.async_on_unload.assert_any_call(cancel_stop_listener)
    entry.add_update_listener.assert_called_once_with(_async_options_updated)
    entry.async_on_unload.assert_any_call(
        entry.add_update_listener.return_value
    )

    await stop_callback(SimpleNamespace())
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_setup_cleanup_failure_does_not_prevent_normal_connection() -> None:
    """Early crash recovery remains best effort before verified transport cleanup."""
    coordinator, hass, entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            side_effect=RuntimeError("BlueZ unavailable"),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    coordinator.async_start.assert_awaited_once_with()
    coordinator.async_wait_until_ready.assert_not_awaited()


async def test_forward_failure_shuts_down_without_registering_listeners() -> None:
    """A platform-forward failure leaves no controller or entry listeners."""
    coordinator, hass, entry = _setup_objects()
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
        "forward failed"
    )

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.govee_ble_air_purifier."
            "_remove_custom_auto_switch_registry_entry"
        ) as remove_switch,
        pytest.raises(RuntimeError, match="forward failed"),
    ):
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    coordinator.async_shutdown.assert_awaited_once_with()
    entry.add_update_listener.assert_not_called()
    entry.async_on_unload.assert_not_called()
    remove_switch.assert_not_called()


async def test_invalid_profile_stops_setup_before_bluetooth_work() -> None:
    """A bundled artifact failure is permanent and cannot start recovery."""
    _coordinator, hass, entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_get_profile_registry",
            new_callable=AsyncMock,
            side_effect=ProfileError("invalid bundled request frame"),
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ) as cleanup,
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator"
        ) as coordinator_class,
    ):
        with pytest.raises(ConfigEntryError, match="Bundled purifier model profiles"):
            await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_not_awaited()
    coordinator_class.assert_not_called()


async def test_invalid_stored_options_stop_before_bluetooth_work() -> None:
    """Invalid mutable values fail before cleanup or coordinator creation."""
    _coordinator, hass, entry = _setup_objects()
    entry.options = {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [3, 5, 5, 15],
    }

    with (
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ) as cleanup,
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator"
        ) as coordinator_class,
        pytest.raises(ConfigEntryError, match="Stored Custom Auto options"),
    ):
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_not_awaited()
    coordinator_class.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_passes_effective_options_to_coordinator(enabled: bool) -> None:
    """Missing/disabled options create no policy; enabled options reach runtime."""
    coordinator, hass, entry = _setup_objects()
    if enabled:
        entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    effective = coordinator_class.call_args.kwargs["custom_auto_options"]
    assert effective.enabled is enabled
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS
    )


def _runtime_entry(
    *, enabled: bool, active: bool = False
) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    """Build a loaded entry and runtime for update-listener tests."""
    controller = (
        SimpleNamespace(snapshot=SimpleNamespace(active=active))
        if enabled
        else None
    )
    coordinator = SimpleNamespace(
        custom_auto_controller=controller,
        async_deactivate_custom_auto=AsyncMock(),
        async_set_fan_mode=AsyncMock(),
    )
    config_entries = SimpleNamespace(async_reload=AsyncMock(return_value=True))
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(
        data={
            CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_MODEL: Model.H7129.value,
        },
        options={CONF_CUSTOM_AUTO_ENABLED: enabled},
        entry_id="entry-id",
        runtime_data=coordinator,
    )
    return coordinator, hass, entry


@pytest.mark.parametrize("change", ["enable", "edit", "disable"])
async def test_options_update_reloads_exactly_once(change: str) -> None:
    """Every meaningful option transition performs one validated reload."""
    old_enabled = change != "enable"
    coordinator, hass, entry = _runtime_entry(
        enabled=old_enabled, active=change != "enable"
    )
    entry.options = (
        {CONF_CUSTOM_AUTO_ENABLED: False}
        if change == "disable"
        else {
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [8, 10, 14, 20],
        }
    )
    registry = MagicMock()

    with patch(
        "custom_components.govee_ble_air_purifier.er.async_get",
        return_value=registry,
    ):
        await _async_options_updated(hass, entry)  # type: ignore[arg-type]

    hass.config_entries.async_reload.assert_awaited_once_with("entry-id")
    if change == "enable":
        coordinator.async_deactivate_custom_auto.assert_not_awaited()
    else:
        coordinator.async_deactivate_custom_auto.assert_awaited_once_with()
    coordinator.async_set_fan_mode.assert_not_awaited()


async def test_active_disable_handoff_precedes_reload_and_registry_removal() -> None:
    """Disable yields ownership, reloads, then removes only the switch identity."""
    coordinator, hass, entry = _runtime_entry(enabled=True, active=True)
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: False}
    order: list[str] = []
    coordinator.async_deactivate_custom_auto.side_effect = lambda: order.append(
        "handoff"
    )
    hass.config_entries.async_reload.side_effect = lambda _: order.append(
        "reload"
    ) or True
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "switch.bedroom_custom_auto"
    registry.async_remove.side_effect = lambda _: order.append("remove")

    with patch(
        "custom_components.govee_ble_air_purifier.er.async_get",
        return_value=registry,
    ):
        await _async_options_updated(hass, entry)  # type: ignore[arg-type]

    assert order == ["handoff", "reload", "remove"]
    registry.async_get_entity_id.assert_called_once_with(
        "switch", "govee_ble_air_purifier", "aa:bb:cc:dd:ee:ff_custom_auto"
    )
    registry.async_remove.assert_called_once_with(
        "switch.bedroom_custom_auto"
    )


async def test_failed_disable_reload_preserves_switch_registry() -> None:
    """A failed reload cannot remove the prior entity-registry identity."""
    _coordinator, hass, entry = _runtime_entry(enabled=True, active=False)
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: False}
    hass.config_entries.async_reload.return_value = False
    registry = MagicMock()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        pytest.raises(ConfigEntryError, match="Could not reload purifier"),
    ):
        await _async_options_updated(hass, entry)  # type: ignore[arg-type]

    registry.async_remove.assert_not_called()


async def test_invalid_updated_options_do_not_handoff_or_reload() -> None:
    """Listener validation happens before changing active runtime ownership."""
    coordinator, hass, entry = _runtime_entry(enabled=True, active=True)
    entry.options = {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [7, 9, 9, 19],
    }

    with pytest.raises(ConfigEntryError, match="Stored Custom Auto options"):
        await _async_options_updated(hass, entry)  # type: ignore[arg-type]

    coordinator.async_deactivate_custom_auto.assert_not_awaited()
    hass.config_entries.async_reload.assert_not_awaited()


async def test_disabled_restart_removes_stale_switch_only_after_forward() -> None:
    """Successful disabled startup closes the reload/removal crash window."""
    coordinator, hass, entry = _setup_objects()
    order: list[str] = []
    hass.config_entries.async_forward_entry_setups.side_effect = (
        lambda *_: order.append("forward")
    )
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "switch.stale_custom_auto"
    registry.async_remove.side_effect = lambda _: order.append("remove")

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert order == ["forward", "remove"]
    registry.async_get_entity_id.assert_called_once_with(
        "switch", "govee_ble_air_purifier", "aa:bb:cc:dd:ee:ff_custom_auto"
    )
    registry.async_remove.assert_called_once_with("switch.stale_custom_auto")


async def test_enabled_restart_preserves_switch_registry() -> None:
    """Enabled setup never removes its stable switch registry entry."""
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.govee_ble_air_purifier."
            "_remove_custom_auto_switch_registry_entry"
        ) as remove_switch,
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    remove_switch.assert_not_called()


async def test_version_one_entry_data_is_not_mutated_during_setup() -> None:
    """Existing H7124/H7129 model values remain migration-free."""
    coordinator, hass, entry = _setup_objects()
    original_data = dict(entry.data)

    with (
        patch(
            "custom_components.govee_ble_air_purifier.async_close_stale_connections",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert entry.data == original_data


async def test_unload_shuts_down_after_platforms_unload() -> None:
    """A reload or integration update releases the active Bluetooth runtime."""
    coordinator, hass, entry = _setup_objects()
    entry.runtime_data = coordinator

    assert await async_unload_entry(hass, entry)  # type: ignore[arg-type]

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, PLATFORMS
    )
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_remove_entry_closes_stale_connection() -> None:
    """Removal performs address cleanup even after runtime data is gone."""
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(data={CONF_ADDRESS: address})

    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        new_callable=AsyncMock,
    ) as cleanup:
        await async_remove_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_awaited_once_with(address, reason="entry_removed")


async def test_standalone_cleanup_defers_to_existing_address_owner() -> None:
    """Setup/removal cleanup cannot race an existing runtime owner."""
    address = "AA:BB:CC:DD:EF:11"
    token = ADDRESS_OWNERSHIP.claim(address)
    assert token is not None

    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        new_callable=AsyncMock,
    ) as cleanup:
        await _async_cleanup_address(
            address,
            reason="entry_setup",
            timeout=0.01,
            cancellation_timeout=0.01,
        )

    cleanup.assert_not_awaited()
    assert ADDRESS_OWNERSHIP.is_current(token)
    ADDRESS_OWNERSHIP.request_release(token)
    ADDRESS_OWNERSHIP.finish_cleanup(token)


async def test_resistant_standalone_cleanup_is_bounded_and_retained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resistant top-level cleanup retains quarantine and observes failure."""
    address = "AA:BB:CC:DD:EF:12"
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def resistant_cleanup(_: str, *, reason: str) -> None:
        assert reason == "entry_removed"
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
        raise RuntimeError("late standalone cleanup failure")

    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth",
    )
    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        side_effect=resistant_cleanup,
    ):
        started = time.monotonic()
        await _async_cleanup_address(
            address,
            reason="entry_removed",
            timeout=0.01,
            cancellation_timeout=0.01,
        )
        assert time.monotonic() - started < 0.1

    await cancellation_seen.wait()
    assert ADDRESS_OWNERSHIP.is_owned(address)
    assert ADDRESS_OWNERSHIP.claim(address) is None

    release.set()
    for _ in range(100):
        if not ADDRESS_OWNERSHIP.is_owned(address):
            break
        await asyncio.sleep(0)

    assert not ADDRESS_OWNERSHIP.is_owned(address)
    await asyncio.sleep(0)
    assert "late standalone cleanup failure" in caplog.text
    replacement_token = ADDRESS_OWNERSHIP.claim(address)
    assert replacement_token is not None
    ADDRESS_OWNERSHIP.request_release(replacement_token)
    ADDRESS_OWNERSHIP.finish_cleanup(replacement_token)
