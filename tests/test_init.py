"""Tests for integration lifecycle cleanup."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_air_purifier import (
    _async_cleanup_address,
    _async_options_updated,
    async_remove_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.govee_ble_air_purifier.bluetooth.ownership import (
    ADDRESS_OWNERSHIP,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, DOMAIN, PLATFORMS
from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
)
from custom_components.govee_ble_air_purifier.models import Model
from custom_components.govee_ble_air_purifier.profiles import ProfileError


@pytest.fixture(autouse=True)
def _mock_entity_registry(request):
    """Keep lifecycle unit tests independent from Home Assistant storage."""
    if "hass" in request.fixturenames:
        yield
        return
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=False),
        async_set_active=AsyncMock(),
        async_clear=AsyncMock(),
    )
    with (
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
    ):
        yield


def _setup_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
        async_wait_until_ready=AsyncMock(),
        async_shutdown=AsyncMock(),
        custom_auto_controller=None,
        async_deactivate_custom_auto=AsyncMock(),
        async_quiesce_custom_auto=AsyncMock(),
        async_disable_custom_auto_and_clear=AsyncMock(),
        set_custom_auto_registry_allowed=Mock(),
        set_custom_auto_registry_listener_remover=Mock(),
    )
    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
        async_reload=AsyncMock(return_value=True),
    )
    bus = SimpleNamespace(
        async_listen_once=Mock(return_value=Mock()),
        async_listen=Mock(return_value=Mock()),
    )
    hass = SimpleNamespace(config_entries=config_entries, bus=bus, data={})
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


def _visible_custom_auto_registry() -> MagicMock:
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "switch.custom_auto"
    registry.async_get.return_value = SimpleNamespace(
        disabled_by=None, hidden_by=None
    )
    return registry


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


async def test_enabled_setup_loads_memory_before_coordinator_construction() -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    order: list[str] = []
    memory = SimpleNamespace(
        async_load_active=AsyncMock(side_effect=lambda: order.append("load") or True),
        async_set_active=AsyncMock(),
        async_clear=AsyncMock(),
    )

    def construct(*args, **kwargs):
        order.append("construct")
        assert kwargs["custom_auto_memory"] is memory
        assert kwargs["restore_custom_auto"] is True
        return coordinator

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ) as memory_class,
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            side_effect=construct,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=_visible_custom_auto_registry(),
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    assert order == ["load", "construct"]
    memory_class.assert_called_once_with(hass, "entry-id")
    memory.async_load_active.assert_awaited_once_with()
    memory.async_set_active.assert_not_awaited()


async def test_disabled_setup_does_not_restore_remembered_on() -> None:
    coordinator, hass, entry = _setup_objects()
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=True), async_clear=AsyncMock()
    )

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    memory.async_load_active.assert_not_awaited()
    assert coordinator_class.call_args.kwargs["restore_custom_auto"] is False


@pytest.mark.parametrize("registry_state", ["missing", "hidden", "disabled"])
async def test_blocked_registry_state_clears_memory_and_starts_off(
    registry_state: str,
) -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=True), async_clear=AsyncMock()
    )
    registry = MagicMock()
    if registry_state == "missing":
        registry.async_get_entity_id.return_value = None
    else:
        registry.async_get_entity_id.return_value = "switch.custom_auto"
        registry.async_get.return_value = SimpleNamespace(
            disabled_by=("user" if registry_state == "disabled" else None),
            hidden_by=("user" if registry_state == "hidden" else None),
        )

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    memory.async_load_active.assert_not_awaited()
    memory.async_clear.assert_awaited_once_with()
    assert coordinator_class.call_args.kwargs["restore_custom_auto"] is False
    assert coordinator_class.call_args.kwargs[
        "custom_auto_registry_allowed"
    ] is False


async def test_registry_listener_precedes_start_and_reread_closes_race() -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=True), async_clear=AsyncMock()
    )
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "switch.custom_auto"
    registry.async_get.side_effect = [
        SimpleNamespace(disabled_by=None, hidden_by=None),
        None,
    ]

    async def start() -> None:
        hass.bus.async_listen.assert_called_once()
        coordinator.set_custom_auto_registry_listener_remover.assert_called_once()
        coordinator.set_custom_auto_registry_allowed.assert_called_once_with(False)

    coordinator.async_start.side_effect = start
    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    memory.async_load_active.assert_awaited_once_with()
    memory.async_clear.assert_awaited_once_with()


@pytest.mark.parametrize(
    "clear_error",
    [RuntimeError("clear failed"), asyncio.CancelledError()],
)
async def test_second_registry_read_clear_failure_releases_all_owners(
    clear_error: BaseException,
) -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=True),
        async_clear=AsyncMock(side_effect=clear_error),
    )
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "switch.custom_auto"
    registry.async_get.side_effect = [
        SimpleNamespace(disabled_by=None, hidden_by=None),
        None,
    ]
    remove_listener = Mock()
    hass.bus.async_listen.return_value = remove_listener

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(type(clear_error)),
    ):
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    remove_listener.assert_called_once_with()
    coordinator.async_start.assert_not_awaited()
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_registry_listener_rereads_stable_identity_for_runtime_changes() -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    registry = _visible_custom_auto_registry()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    _, listener = hass.bus.async_listen.call_args.args
    coordinator.set_custom_auto_registry_allowed.reset_mock()

    with patch(
        "custom_components.govee_ble_air_purifier.er.async_get",
        return_value=registry,
    ):
        registry.async_get.return_value = SimpleNamespace(
            disabled_by="user", hidden_by=None
        )
        listener(SimpleNamespace())
        registry.async_get.return_value = SimpleNamespace(
            disabled_by=None, hidden_by="user"
        )
        listener(SimpleNamespace())
        registry.async_get_entity_id.return_value = None
        listener(SimpleNamespace())
        registry.async_get_entity_id.return_value = "switch.renamed_custom_auto"
        registry.async_get.return_value = SimpleNamespace(
            disabled_by=None, hidden_by=None
        )
        listener(SimpleNamespace())

    assert [
        call.args
        for call in coordinator.set_custom_auto_registry_allowed.call_args_list
    ] == [
        (False,),
        (False,),
        (False,),
        (True,),
    ]
    assert all(
        call.args
        == (
            "switch",
            "govee_ble_air_purifier",
            "aa:bb:cc:dd:ee:ff_custom_auto",
        )
        for call in registry.async_get_entity_id.call_args_list
    )


async def test_runtime_registry_listener_processes_real_ha_bus_event(
    hass: HomeAssistant,
) -> None:
    coordinator, _fake_hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    registry = _visible_custom_auto_registry()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
    ):
        assert await async_setup_entry(hass, entry)  # type: ignore[arg-type]
        coordinator.set_custom_auto_registry_allowed.reset_mock()
        registry.async_get.return_value = SimpleNamespace(
            disabled_by="user", hidden_by=None
        )
        hass.bus.async_fire("entity_registry_updated", {"action": "update"})
        await hass.async_block_till_done()

    coordinator.set_custom_auto_registry_allowed.assert_called_once_with(False)


async def test_disabled_unloaded_entry_registry_event_clears_memory(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain="govee_ble_air_purifier",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: Model.H7124.value},
        options={CONF_CUSTOM_AUTO_ENABLED: True},
    )
    entry.add_to_hass(hass)
    registry_entry = SimpleNamespace(
        platform="govee_ble_air_purifier",
        unique_id="aa:bb:cc:dd:ee:ff_custom_auto",
        config_entry_id=entry.entry_id,
        disabled_by=None,
    )
    registry = MagicMock()
    registry.async_get.return_value = registry_entry
    memory = SimpleNamespace(async_clear=AsyncMock())

    with (
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ) as memory_class,
    ):
        assert await async_setup(hass, {})
        hass.bus.async_fire(
            "entity_registry_updated",
            {"action": "update", "entity_id": "switch.custom_auto"},
        )
        await hass.async_block_till_done()
        memory.async_clear.assert_not_awaited()

        entry.disabled_by = ConfigEntryDisabler.USER
        registry_entry.disabled_by = er.RegistryEntryDisabler.CONFIG_ENTRY
        hass.bus.async_fire(
            "entity_registry_updated",
            {
                "action": "update",
                "entity_id": "switch.custom_auto",
                "changes": {"entity_id": "switch.old_custom_auto"},
            },
        )
        await hass.async_block_till_done()
        memory.async_clear.assert_not_awaited()

        hass.bus.async_fire(
            "entity_registry_updated",
            {
                "action": "update",
                "entity_id": "switch.custom_auto",
                "changes": {"disabled_by": None},
            },
        )
        await hass.async_block_till_done()

    memory_class.assert_called_once_with(hass, entry.entry_id)
    memory.async_clear.assert_awaited_once_with()


async def test_immediate_reenable_setup_joins_required_clear_before_restore(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: Model.H7124.value},
        options={CONF_CUSTOM_AUTO_ENABLED: True},
        title="Bedroom purifier",
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        DOMAIN,
        "aa:bb:cc:dd:ee:ff_custom_auto",
        suggested_object_id="custom_auto",
        config_entry=entry,
    )
    clear_started = asyncio.Event()
    clear_release = asyncio.Event()
    memory = SimpleNamespace(active=True, clear_calls=0)

    async def clear() -> None:
        memory.clear_calls += 1
        clear_started.set()
        await clear_release.wait()
        memory.active = False

    async def load() -> bool:
        return memory.active

    async def set_active(active: bool) -> None:
        memory.active = active

    memory.async_clear = AsyncMock(side_effect=clear)
    memory.async_load_active = AsyncMock(side_effect=load)
    memory.async_set_active = AsyncMock(side_effect=set_active)
    coordinator, _fake_hass, _fake_entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        assert await async_setup(hass, {})
        assert await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await clear_started.wait()
        assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)

        setup_task = asyncio.create_task(async_setup_entry(hass, entry))
        await asyncio.sleep(0)
        assert not setup_task.done()
        memory.async_load_active.assert_not_awaited()
        coordinator_class.assert_not_called()

        clear_release.set()
        assert await setup_task
        assert coordinator_class.call_args.kwargs["restore_custom_auto"] is False
        assert memory.clear_calls == 1

        await memory.async_set_active(True)
        await hass.async_block_till_done()
        assert memory.active
        assert memory.clear_calls == 1


async def test_exhausted_required_clear_blocks_setup_until_verified_retry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: Model.H7124.value},
        options={CONF_CUSTOM_AUTO_ENABLED: True},
        title="Bedroom purifier",
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        "aa:bb:cc:dd:ee:ff_custom_auto",
        suggested_object_id="custom_auto",
        config_entry=entry,
    )
    memory = SimpleNamespace(failing=True, active=True, clear_calls=0)

    async def clear() -> None:
        memory.clear_calls += 1
        if memory.failing:
            raise RuntimeError("storage failed")
        memory.active = False

    memory.async_clear = AsyncMock(side_effect=clear)
    memory.async_load_active = AsyncMock(side_effect=lambda: memory.active)
    memory.async_set_active = AsyncMock()
    coordinator, _fake_hass, _fake_entry = _setup_objects()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._CONFIG_ENTRY_CLEAR_BACKOFF",
            (0, 0),
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        assert await async_setup(hass, {})
        assert await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert await hass.config_entries.async_set_disabled_by(entry.entry_id, None)

        with pytest.raises(ConfigEntryError, match="safely clear"):
            await async_setup_entry(hass, entry)
        memory.async_load_active.assert_not_awaited()
        coordinator_class.assert_not_called()
        assert memory.clear_calls == 6

        memory.failing = False
        assert await async_setup_entry(hass, entry)
        assert memory.clear_calls == 7
        assert coordinator_class.call_args.kwargs["restore_custom_auto"] is False


async def test_restart_reenable_event_reconstructs_required_clear_before_reload(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: Model.H7124.value},
        options={CONF_CUSTOM_AUTO_ENABLED: True},
        title="Bedroom purifier",
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        "aa:bb:cc:dd:ee:ff_custom_auto",
        suggested_object_id="custom_auto",
        config_entry=entry,
    )
    memory = SimpleNamespace(failing=True, active=True, clear_calls=0)

    async def clear() -> None:
        memory.clear_calls += 1
        if memory.failing:
            raise RuntimeError("storage failed")
        memory.active = False

    async def set_active(active: bool) -> None:
        memory.active = active

    memory.async_clear = AsyncMock(side_effect=clear)
    memory.async_load_active = AsyncMock(side_effect=lambda: memory.active)
    memory.async_set_active = AsyncMock(side_effect=set_active)
    coordinator, _fake_hass, _fake_entry = _setup_objects()
    state_key = f"{DOMAIN}.custom_auto_required_clear"

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._CONFIG_ENTRY_CLEAR_BACKOFF",
            (0, 0),
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            new_callable=AsyncMock,
            return_value=True,
        ) as reload_mock,
    ):
        assert await async_setup(hass, {})
        assert await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert memory.clear_calls == 3

        # Model a fresh Home Assistant process: integration-owned memory and
        # its bus subscription are gone, while the entity registry still
        # records CONFIG_ENTRY as the entity's previous disabled owner.
        old_state = hass.data.pop(state_key)
        assert old_state.remove_listener is not None
        old_state.remove_listener()
        assert await async_setup(hass, {})

        async def reload_into_setup(_entry_id: str) -> bool:
            return await async_setup_entry(hass, entry)

        reload_mock.side_effect = reload_into_setup
        with pytest.raises(ConfigEntryError, match="safely clear"):
            await hass.config_entries.async_set_disabled_by(entry.entry_id, None)

        memory.async_load_active.assert_not_awaited()
        coordinator_class.assert_not_called()
        assert memory.clear_calls == 6

        with pytest.raises(ConfigEntryError, match="safely clear"):
            await async_setup_entry(hass, entry)
        assert memory.clear_calls == 9
        memory.async_load_active.assert_not_awaited()

        memory.failing = False
        assert await async_setup_entry(hass, entry)
        assert memory.clear_calls == 10
        assert coordinator_class.call_args.kwargs["restore_custom_auto"] is False

        await memory.async_set_active(True)
        await hass.async_block_till_done()
        assert memory.active
        assert memory.clear_calls == 10


async def test_startup_failure_does_not_overwrite_remembered_state() -> None:
    coordinator, hass, entry = _setup_objects()
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: True}
    coordinator.async_start.side_effect = RuntimeError("startup failed")
    memory = SimpleNamespace(
        async_load_active=AsyncMock(return_value=True),
        async_set_active=AsyncMock(),
        async_clear=AsyncMock(),
    )

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.er.async_get",
            return_value=_visible_custom_auto_registry(),
        ),
        pytest.raises(RuntimeError, match="startup failed"),
    ):
        await async_setup_entry(hass, entry)  # type: ignore[arg-type]

    memory.async_load_active.assert_awaited_once_with()
    memory.async_set_active.assert_not_awaited()


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
        async_quiesce_custom_auto=AsyncMock(),
        async_disable_custom_auto_and_clear=AsyncMock(),
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
        coordinator.async_quiesce_custom_auto.assert_not_awaited()
    elif change == "disable":
        coordinator.async_disable_custom_auto_and_clear.assert_awaited_once_with()
    else:
        coordinator.async_quiesce_custom_auto.assert_awaited_once_with()
    coordinator.async_set_fan_mode.assert_not_awaited()


async def test_active_disable_handoff_precedes_reload_and_registry_removal() -> None:
    """Disable yields ownership, reloads, then removes only the switch identity."""
    coordinator, hass, entry = _runtime_entry(enabled=True, active=True)
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: False}
    order: list[str] = []
    coordinator.async_disable_custom_auto_and_clear.side_effect = lambda: order.append(
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

    coordinator.async_quiesce_custom_auto.assert_not_awaited()
    hass.config_entries.async_reload.assert_not_awaited()


async def test_external_disable_cleanup_failure_does_not_reload() -> None:
    """A failed required cleanup cannot claim a successful reload."""
    coordinator, hass, entry = _runtime_entry(enabled=True, active=True)
    entry.options = {CONF_CUSTOM_AUTO_ENABLED: False}
    coordinator.async_disable_custom_auto_and_clear.side_effect = RuntimeError(
        "purifier unavailable"
    )

    with pytest.raises(RuntimeError, match="purifier unavailable"):
        await _async_options_updated(hass, entry)  # type: ignore[arg-type]

    coordinator.async_disable_custom_auto_and_clear.assert_awaited_once_with()
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
    coordinator.async_disable_custom_auto_and_clear.assert_not_awaited()


async def test_config_entry_disable_clears_before_unload() -> None:
    coordinator, hass, entry = _setup_objects()
    entry.runtime_data = coordinator
    entry.disabled_by = "user"
    order: list[str] = []
    coordinator.async_disable_custom_auto_and_clear.side_effect = (
        lambda: order.append("clear")
    )
    hass.config_entries.async_unload_platforms.side_effect = (
        lambda *_: order.append("platforms") or True
    )
    coordinator.async_shutdown.side_effect = lambda: order.append("shutdown")

    assert await async_unload_entry(hass, entry)  # type: ignore[arg-type]

    assert order == ["clear", "platforms", "shutdown"]


async def test_remove_entry_closes_stale_connection() -> None:
    """Removal performs address cleanup even after runtime data is gone."""
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(
        data={CONF_ADDRESS: address, CONF_MODEL: Model.H7129.value},
        entry_id="entry-id",
    )

    with patch(
        "custom_components.govee_ble_air_purifier.async_close_stale_connections",
        new_callable=AsyncMock,
    ) as cleanup:
        await async_remove_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_awaited_once_with(address, reason="entry_removed")


async def test_remove_entry_runs_address_cleanup_when_memory_clear_fails() -> None:
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(
        data={CONF_ADDRESS: address, CONF_MODEL: Model.H7129.value},
        entry_id="entry-id",
    )
    memory = SimpleNamespace(
        async_clear=AsyncMock(side_effect=RuntimeError("storage failed"))
    )

    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            new_callable=AsyncMock,
        ) as cleanup,
        pytest.raises(RuntimeError, match="storage failed"),
    ):
        await async_remove_entry(hass, entry)  # type: ignore[arg-type]

    cleanup.assert_awaited_once()


async def test_remove_entry_cancellation_settles_address_cleanup_then_propagates(
) -> None:
    address = "AA:BB:CC:DD:EE:FF"
    hass = SimpleNamespace()
    entry = SimpleNamespace(
        data={CONF_ADDRESS: address, CONF_MODEL: Model.H7129.value},
        entry_id="entry-id",
    )
    clear_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def clear() -> None:
        clear_started.set()
        await asyncio.Event().wait()

    async def cleanup(*args, **kwargs) -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    memory = SimpleNamespace(async_clear=AsyncMock(side_effect=clear))
    with (
        patch(
            "custom_components.govee_ble_air_purifier.CustomAutoMemory",
            return_value=memory,
        ),
        patch(
            "custom_components.govee_ble_air_purifier._async_cleanup_address",
            side_effect=cleanup,
        ) as cleanup_mock,
    ):
        removal = asyncio.create_task(async_remove_entry(hass, entry))  # type: ignore[arg-type]
        await clear_started.wait()
        removal.cancel()
        await cleanup_started.wait()
        removal.cancel()
        await asyncio.sleep(0)
        assert not removal.done()

        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await removal

    cleanup_mock.assert_awaited_once_with(
        address,
        reason="entry_removed",
        timeout=5.0,
        cancellation_timeout=1.0,
    )


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
