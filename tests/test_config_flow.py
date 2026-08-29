"""Tests for the Govee BLE Air Purifier config flow."""

import asyncio
import json
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_air_purifier import (
    config_flow as config_flow_module,
)
from custom_components.govee_ble_air_purifier import (
    discovery as discovery_module,
)
from custom_components.govee_ble_air_purifier import (
    setup_validation as setup_validation_module,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, DOMAIN
from custom_components.govee_ble_air_purifier.custom_auto_options import (
    CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES,
    CONF_CUSTOM_AUTO_ENABLED,
    CONF_CUSTOM_AUTO_PM25_BOUNDARIES,
    CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS,
    CustomAutoOptionsError,
)
from custom_components.govee_ble_air_purifier.discovery import (
    DiscoveredPurifier,
    PurifierDiscoveryService,
)
from custom_components.govee_ble_air_purifier.profiles import (
    ProfileError,
    get_profile_registry,
)
from custom_components.govee_ble_air_purifier.setup_validation import (
    PurifierSetupValidator,
)


@pytest.fixture(autouse=True)
def _prepare_bluetooth_test_environment(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load the custom flow without starting host Bluetooth hardware."""
    with (
        patch(
            "homeassistant.config_entries.async_process_deps_reqs",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_setup",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(discovery_module, "MANUAL_DISCOVERY_SCAN_DURATION", 0.0),
        patch.object(
            discovery_module,
            "SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT",
            1,
        ),
        patch.object(
            discovery_module,
            "SELECTED_ADVERTISEMENT_CHECK_INTERVAL",
            0.0,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_register_callback",
            return_value=MagicMock(),
            create=True,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_ble_device_from_address",
            return_value=None,
            create=True,
        ),
    ):
        yield


pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_invalid_profile_aborts_before_setup_scan(
    hass: HomeAssistant,
) -> None:
    """Broken bundled data is permanent and cannot start Bluetooth discovery."""
    with (
        patch.object(
            config_flow_module,
            "async_get_profile_registry",
            new=AsyncMock(side_effect=ProfileError("bad profile")),
        ),
        patch.object(
            PurifierDiscoveryService,
            "async_discover_purifiers",
            new_callable=AsyncMock,
        ) as discover,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "model_profile_invalid"
    discover.assert_not_awaited()


async def test_explicit_validation_waits_for_ready_and_always_shuts_down(
    hass: HomeAssistant,
) -> None:
    """The setup wizard remains strict after runtime startup becomes non-blocking."""
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
        async_wait_until_ready=AsyncMock(),
        async_shutdown=AsyncMock(),
    )

    validator = PurifierSetupValidator(hass, get_profile_registry())
    with patch.object(
        setup_validation_module,
        "GoveeDataUpdateCoordinator",
        return_value=coordinator,
    ):
        await validator.async_validate(
            address="AA:BB:CC:DD:EE:FF",
            model="H7129",
            name="ihoment_H7129_TEST",
        )

    coordinator.async_start.assert_awaited_once_with()
    coordinator.async_wait_until_ready.assert_awaited_once_with()
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_explicit_validation_failure_still_shuts_down(
    hass: HomeAssistant,
) -> None:
    """A failed readiness wait cannot leave a validation client running."""
    coordinator = SimpleNamespace(
        async_start=AsyncMock(),
        async_wait_until_ready=AsyncMock(side_effect=RuntimeError("not ready")),
        async_shutdown=AsyncMock(),
    )

    validator = PurifierSetupValidator(hass, get_profile_registry())
    with (
        patch.object(
            setup_validation_module,
            "GoveeDataUpdateCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(RuntimeError, match="not ready"),
    ):
        await validator.async_validate(
            address="AA:BB:CC:DD:EE:FF",
            model="H7124",
            name="GVH7124TEST",
        )

    coordinator.async_shutdown.assert_awaited_once_with()


def _service_info(
    name: str | None,
    address: str,
    rssi: int,
    *,
    connectable: bool = True,
    advertisement_time: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        address=address,
        rssi=rssi,
        connectable=connectable,
        time=time.monotonic() if advertisement_time is None else advertisement_time,
    )


async def test_user_flow_selects_discovered_purifier_without_address_entry(
    hass: HomeAssistant,
) -> None:
    """Manual setup scans once, chooses a discovery, and infers its model."""
    discovery = _service_info(
        "ihoment_H7129_BEDROOM",
        "AA:BB:CC:DD:EE:FF",
        -48,
    )
    call_order: list[str] = []

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        assert duration == 0.0
        call_order.append("active_scan")
        discovery.time = time.monotonic()

    def discoveries(*_: object, **__: object) -> tuple[SimpleNamespace, ...]:
        call_order.append("discovery_cache")
        return (discovery,)

    async def process_advertisements(
        _: HomeAssistant,
        predicate: object,
        match_dict: dict[str, object],
        mode: object,
        timeout: int,
    ) -> SimpleNamespace:
        assert callable(predicate)
        stale = _service_info(
            discovery.name,
            discovery.address,
            discovery.rssi,
            advertisement_time=time.monotonic() - 1,
        )
        assert predicate(stale) is False
        fresh = _service_info(discovery.name, discovery.address, discovery.rssi)
        assert predicate(fresh) is True
        assert match_dict == {
            "address": discovery.address,
            "connectable": True,
        }
        assert mode is discovery_module.bluetooth.BluetoothScanningMode.ACTIVE
        assert timeout == 1
        return fresh

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            side_effect=active_scan,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            side_effect=discoveries,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery."
            "bluetooth.async_process_advertisements",
            new_callable=AsyncMock,
            side_effect=process_advertisements,
        ) as process_advertisements,
        patch(
            "custom_components.govee_ble_air_purifier.discovery."
            "bluetooth.async_last_service_info",
            return_value=discovery,
        ),
        patch.object(
            PurifierSetupValidator,
            "async_validate",
            new_callable=AsyncMock,
        ) as validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["data_schema"]({CONF_ADDRESS: discovery.address}) == {
            CONF_ADDRESS: discovery.address
        }
        with pytest.raises(vol.Invalid):
            result["data_schema"]({CONF_ADDRESS: "00:00:00:00:00:00"})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: discovery.address},
        )
        assert result["step_id"] == "enable_custom_auto"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == discovery.name
    assert result["data"] == {
        CONF_ADDRESS: discovery.address,
        CONF_MODEL: "H7129",
    }
    assert result["options"] == {CONF_CUSTOM_AUTO_ENABLED: False}
    validate.assert_awaited_once_with(
        address=discovery.address,
        model="H7129",
        name=discovery.name,
    )
    request_active_scan.assert_awaited_once_with(hass, duration=0.0)
    process_advertisements.assert_awaited_once()
    process_call = process_advertisements.await_args
    assert process_call.args[0] is hass
    assert process_call.args[2] == {
        "address": discovery.address,
        "connectable": True,
    }
    assert (
        process_call.args[3]
        is discovery_module.bluetooth.BluetoothScanningMode.ACTIVE
    )
    assert process_call.args[4] == 1
    assert call_order == [
        "discovery_cache",
        "active_scan",
        "discovery_cache",
    ]


async def test_user_flow_excludes_device_not_seen_during_active_scan(
    hass: HomeAssistant,
) -> None:
    """A completed sweep cannot offer an old Home Assistant cache entry."""
    stale_discovery = _service_info(
        "GVH7124STALE",
        "AA:BB:CC:DD:EE:FF",
        -87,
        advertisement_time=time.monotonic() - 194,
    )

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(stale_discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
    request_active_scan.assert_awaited_once_with(hass, duration=0.0)


async def test_user_flow_keeps_observing_when_active_scan_fails(
    hass: HomeAssistant,
) -> None:
    """A scan API failure still permits a device seen during the window."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)

    async def failed_scan(_: HomeAssistant, *, duration: float) -> None:
        assert duration == 0.0
        discovery.time = time.monotonic()
        raise RuntimeError("scanner unavailable")

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=failed_scan),
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    request_active_scan.assert_awaited_once_with(hass, duration=0.0)


async def test_user_flow_failed_scan_rejects_stale_cache(
    hass: HomeAssistant,
) -> None:
    """The compatibility fallback cannot offer an unreachable stale device."""
    stale_discovery = _service_info(
        "GVH7124STALE",
        "AA:BB:CC:DD:EE:FF",
        -87,
        advertisement_time=time.monotonic() - 194,
    )

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=RuntimeError("scanner unavailable")),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(stale_discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_rejects_registration_cache_replay(
    hass: HomeAssistant,
) -> None:
    """A synchronous callback replay cannot satisfy a new setup scan."""
    stale_discovery = _service_info(
        "GVH7124STALE",
        "AA:BB:CC:DD:EE:FF",
        -68,
        advertisement_time=time.monotonic() - 60,
    )
    cancel_callback = MagicMock()

    def register_callback(
        _: HomeAssistant,
        callback: object,
        *__: object,
        **___: object,
    ) -> MagicMock:
        assert callable(callback)
        callback(stale_discovery)
        return cancel_callback

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_register_callback",
            side_effect=register_callback,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(stale_discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
    cancel_callback.assert_called_once_with()


async def test_user_flow_waits_full_window_when_active_scan_returns_early(
    hass: HomeAssistant,
) -> None:
    """An early HA scheduler return cannot expose the cached list immediately."""
    discovery = _service_info(
        "GVH7124BEDROOM",
        "AA:BB:CC:DD:EE:FF",
        -52,
        advertisement_time=time.monotonic() - 60,
    )

    async def observe_during_window() -> None:
        await asyncio.sleep(0.01)
        discovery.time = time.monotonic()

    observer = hass.async_create_task(observe_during_window())
    started_at = time.monotonic()
    with (
        patch.object(discovery_module, "MANUAL_DISCOVERY_SCAN_DURATION", 0.05),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    elapsed = time.monotonic() - started_at
    await observer

    assert result["type"] is FlowResultType.FORM
    assert elapsed >= 0.045
    request_active_scan.assert_awaited_once_with(hass, duration=0.05)


async def test_each_user_flow_starts_a_new_active_scan(
    hass: HomeAssistant,
) -> None:
    """Each blue Add device flow owns a separate discovery sweep."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
    ):
        first = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        second = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert first["type"] is FlowResultType.FORM
    assert second["type"] is FlowResultType.FORM
    assert request_active_scan.await_count == 2
    assert request_active_scan.await_args_list[0].kwargs == {"duration": 0.0}
    assert request_active_scan.await_args_list[1].kwargs == {"duration": 0.0}


async def test_user_flow_retains_valid_name_seen_during_scan(
    hass: HomeAssistant,
) -> None:
    """A later nameless sighting cannot erase a valid purifier identity."""
    address = "AA:BB:CC:DD:EE:FF"
    named = _service_info("ihoment_H7129_BEDROOM", address, -61)
    nameless = _service_info(None, address, -58)
    callbacks: list[object] = []
    cancel_callback = MagicMock()

    def register_callback(
        _: HomeAssistant,
        callback: object,
        match_dict: dict[str, object],
        mode: object,
        **__: object,
    ) -> MagicMock:
        assert match_dict == {"connectable": True}
        assert mode is discovery_module.bluetooth.BluetoothScanningMode.PASSIVE
        callbacks.append(callback)
        return cancel_callback

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        callback = callbacks[-1]
        assert callable(callback)
        callback(named)
        callback(nameless)

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_register_callback",
            side_effect=register_callback,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(nameless,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({CONF_ADDRESS: address}) == {
        CONF_ADDRESS: address
    }
    option_validator = next(iter(result["data_schema"].schema.values()))
    assert option_validator.container[address] == "ihoment_H7129_BEDROOM (Near)"
    cancel_callback.assert_called_once_with()


async def test_user_flow_combines_stale_identity_with_fresh_nameless_sighting(
    hass: HomeAssistant,
) -> None:
    """A prior supported name can identify the same address seen fresh."""
    address = "AA:BB:CC:DD:EE:FF"
    historical = _service_info(
        "ihoment_H7129_BEDROOM",
        address,
        -70,
        advertisement_time=time.monotonic() - 120,
    )
    fresh_nameless = _service_info(None, address, -58)
    cache_reads = 0

    def discoveries(*_: object, **__: object) -> tuple[SimpleNamespace, ...]:
        nonlocal cache_reads
        cache_reads += 1
        return (historical,) if cache_reads == 1 else (fresh_nameless,)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        fresh_nameless.time = time.monotonic()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            side_effect=discoveries,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    option_validator = next(iter(result["data_schema"].schema.values()))
    assert option_validator.container[address] == (
        "ihoment_H7129_BEDROOM (Near)"
    )


async def test_user_flow_uses_ble_device_name_for_fresh_nameless_sighting(
    hass: HomeAssistant,
) -> None:
    """Home Assistant's BLEDevice name may supply the known identity."""
    address = "AA:BB:CC:DD:EE:FF"
    nameless = _service_info(None, address, -61)
    ble_device = SimpleNamespace(name="GVH7124BEDROOM")

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        nameless.time = time.monotonic()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(nameless,),
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_ble_device_from_address",
            return_value=ble_device,
        ) as ble_device_from_address,
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    option_validator = next(iter(result["data_schema"].schema.values()))
    assert option_validator.container[address] == "GVH7124BEDROOM (Near)"
    assert ble_device_from_address.call_count >= 1


async def test_user_flow_retains_identity_across_setup_flows(
    hass: HomeAssistant,
) -> None:
    """A name learned by one blue-button flow survives later nameless packets."""
    address = "AA:BB:CC:DD:EE:FF"
    named = _service_info("GVH7124BEDROOM", address, -65)
    nameless = _service_info(None, address, -60)
    current_info = [named]

    def discoveries(*_: object, **__: object) -> tuple[SimpleNamespace, ...]:
        return (current_info[0],)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        current_info[0].time = time.monotonic()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            side_effect=discoveries,
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
    ):
        first = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        current_info[0] = nameless
        second = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert first["type"] is FlowResultType.FORM
    assert second["type"] is FlowResultType.FORM
    option_validator = next(iter(second["data_schema"].schema.values()))
    assert option_validator.container[address] == "GVH7124BEDROOM (Near)"


async def test_user_flow_reports_fresh_devices_without_supported_name(
    hass: HomeAssistant,
) -> None:
    """Fresh nameless traffic is distinguished from seeing no devices."""
    nameless = _service_info(None, "AA:BB:CC:DD:EE:FF", -58)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        nameless.time = time.monotonic()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(nameless,),
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "devices_seen_without_supported_name"


async def test_user_flow_does_not_infer_model_from_nameless_address(
    hass: HomeAssistant,
) -> None:
    """A model-like Bluetooth address cannot replace a supported name."""
    address_only = _service_info(
        "5C:E7:53:F9:6A:7D",
        "5C:E7:53:F9:6A:7D",
        -55,
    )

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        address_only.time = time.monotonic()

    with (
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(address_only,),
        ),
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "devices_seen_without_supported_name"


async def test_user_flow_requires_fresh_selected_device_advertisement(
    hass: HomeAssistant,
) -> None:
    """Selection stays on the form when the purifier is no longer visible."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_process_advertisements",
            new=AsyncMock(side_effect=TimeoutError),
        ) as process_advertisements,
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_last_service_info",
            return_value=discovery,
        ),
        patch.object(
            PurifierSetupValidator,
            "async_validate",
            new_callable=AsyncMock,
        ) as validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: discovery.address},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_discovered"}
    process_advertisements.assert_awaited_once()
    validate.assert_not_awaited()


async def test_user_flow_retains_valid_name_when_fresh_packet_is_nameless(
    hass: HomeAssistant,
) -> None:
    """Fresh reachability need not repeat the name learned during the scan."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)
    fresh_nameless = _service_info(None, discovery.address, -49)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_process_advertisements",
            new=AsyncMock(return_value=fresh_nameless),
        ),
        patch.object(
            PurifierSetupValidator,
            "async_validate",
            new_callable=AsyncMock,
        ) as validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: discovery.address},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == discovery.name
    validate.assert_awaited_once_with(
        address=discovery.address,
        model="H7124",
        name=discovery.name,
    )


async def test_user_flow_accepts_fresh_cache_when_callback_is_deduplicated(
    hass: HomeAssistant,
) -> None:
    """A refreshed HA cache timestamp can prove selected-address freshness."""
    discovery = _service_info("ihoment_H7129_BEDROOM", "AA:BB:CC:DD:EE:FF", -62)
    cached_calls = 0

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    async def no_callback(*_: object, **__: object) -> None:
        await asyncio.Event().wait()

    def refreshed_cache(*_: object, **__: object) -> SimpleNamespace:
        nonlocal cached_calls
        cached_calls += 1
        if cached_calls > 1:
            discovery.time = time.monotonic()
        return discovery

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_process_advertisements",
            side_effect=no_callback,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_last_service_info",
            side_effect=refreshed_cache,
        ),
        patch.object(
            PurifierSetupValidator,
            "async_validate",
            new_callable=AsyncMock,
        ) as validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        # Make the first selected-address cache lookup older than its cutoff.
        discovery.time = time.monotonic() - 1
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: discovery.address},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cached_calls >= 2
    validate.assert_awaited_once()


def _custom_auto_form_values(
    boundaries: Sequence[object],
    *,
    upshift: object = 3,
    downshifts: Sequence[object] | None = None,
) -> dict[str, object]:
    """Return scalar settings in the Home Assistant form shape."""
    delays = downshifts or [7, 5, 5, 5]
    return {
        "pm25_boundary_sleep_low": boundaries[0],
        "pm25_boundary_low_medium": boundaries[1],
        "pm25_boundary_medium_high": boundaries[2],
        "pm25_boundary_high_turbo": boundaries[3],
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: upshift,
        "downshift_low_sleep_minutes": delays[0],
        "downshift_medium_low_minutes": delays[1],
        "downshift_high_medium_minutes": delays[2],
        "downshift_turbo_high_minutes": delays[3],
    }


@pytest.mark.parametrize(
    ("model", "boundaries"),
    [("H7124", [3, 5, 9, 15]), ("H7129", [7, 9, 13, 19])],
)
async def test_options_enable_uses_profile_defaults_and_normalizes_values(
    hass: HomeAssistant, model: str, boundaries: list[int]
) -> None:
    """Both model baselines populate scalar forms and store only shared keys."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: model},
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({}) == {}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: True}
    )
    assert result["step_id"] == "custom_auto_settings"
    defaults = result["data_schema"]({})
    for key, value in zip(
        (
            "pm25_boundary_sleep_low",
            "pm25_boundary_low_medium",
            "pm25_boundary_medium_high",
            "pm25_boundary_high_turbo",
        ),
        boundaries,
        strict=True,
    ):
        assert defaults[key] == value

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _custom_auto_form_values(boundaries)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: boundaries,
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 3,
        CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [7, 5, 5, 5],
    }


@pytest.mark.parametrize(
    ("values", "error"),
    [
        (_custom_auto_form_values([3, 5, 5, 15]), "boundaries_not_ascending"),
        (_custom_auto_form_values([3, 5.5, 9, 15]), "invalid_integer"),
        (_custom_auto_form_values([3, 5, 9, 1000]), "value_out_of_range"),
        (_custom_auto_form_values([3, 5, 9, 15], upshift=301), "value_out_of_range"),
    ],
)
async def test_options_reject_invalid_custom_auto_settings(
    hass: HomeAssistant, values: dict[str, object], error: str
) -> None:
    """The shared parser supplies stable localized validation outcomes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7124"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: True}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


def test_options_flow_error_keys_have_translations() -> None:
    """Every error emitted by the options flow resolves in its namespace."""
    strings_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "govee_ble_air_purifier"
        / "strings.json"
    )
    strings = json.loads(strings_path.read_text())

    assert {
        "boundaries_not_ascending",
        "invalid_integer",
        "value_out_of_range",
        "invalid_custom_auto_settings",
        "stored_options_invalid",
    } <= strings["options"]["error"].keys()


async def test_options_missing_enable_disables_and_clears_mutable_values(
    hass: HomeAssistant,
) -> None:
    """Missing enable is disabled and writes one stable disabled shape."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7129"},
        options={
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [8, 10, 14, 20],
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CUSTOM_AUTO_ENABLED: False}


async def test_invalid_stored_options_can_be_repaired_by_disabling(
    hass: HomeAssistant,
) -> None:
    """An unloaded invalid entry remains accessible for immediate disable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7124"},
        options={
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [3, 5, 5, 15],
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "stored_options_invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CUSTOM_AUTO_ENABLED: False}


async def test_invalid_stored_options_can_be_replaced_completely(
    hass: HomeAssistant,
) -> None:
    """Profile defaults seed a complete valid replacement without trapping."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7129"},
        options={
            CONF_CUSTOM_AUTO_ENABLED: True,
            CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [7, 9, 9, 19],
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "stored_options_invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: True}
    )
    assert result["errors"] == {"base": "stored_options_invalid"}
    assert result["data_schema"]({})[
        "pm25_boundary_sleep_low"
    ] == 7

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _custom_auto_form_values([8, 10, 14, 20])
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: [8, 10, 14, 20],
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 3,
        CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [7, 5, 5, 5],
    }


@pytest.mark.parametrize(
    ("model", "boundaries"),
    [("H7124", [3, 5, 9, 15]), ("H7129", [7, 9, 13, 19])],
)
async def test_setup_enable_uses_profile_defaults_validates_and_normalizes(
    hass: HomeAssistant, model: str, boundaries: list[int]
) -> None:
    """The complete setup opt-in leg uses each model's profile defaults."""
    flow = config_flow_module.GoveeBleAirPurifierConfigFlow()
    flow.hass = hass
    flow._registry = get_profile_registry()  # noqa: SLF001
    flow._pending_title = f"Test {model}"  # noqa: SLF001
    flow._pending_data = {  # noqa: SLF001
        CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
        CONF_MODEL: model,
    }

    result = await flow.async_step_enable_custom_auto()
    assert result["step_id"] == "enable_custom_auto"
    result = await flow.async_step_enable_custom_auto(
        {CONF_CUSTOM_AUTO_ENABLED: True}
    )
    assert result["step_id"] == "custom_auto_settings"
    defaults = result["data_schema"]({})
    assert [
        defaults["pm25_boundary_sleep_low"],
        defaults["pm25_boundary_low_medium"],
        defaults["pm25_boundary_medium_high"],
        defaults["pm25_boundary_high_turbo"],
    ] == boundaries

    result = await flow.async_step_custom_auto_settings(
        _custom_auto_form_values([3, 5, 5, 15])
    )
    assert result["errors"] == {"base": "boundaries_not_ascending"}
    result = await flow.async_step_custom_auto_settings(
        _custom_auto_form_values(boundaries)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {
        CONF_CUSTOM_AUTO_ENABLED: True,
        CONF_CUSTOM_AUTO_PM25_BOUNDARIES: boundaries,
        CONF_CUSTOM_AUTO_UPSHIFT_CONFIRMATION_SECONDS: 3,
        CONF_CUSTOM_AUTO_DOWNSHIFT_DELAYS_MINUTES: [7, 5, 5, 5],
    }


async def test_flow_maps_unrecognized_parser_error_to_generic_key(
    hass: HomeAssistant,
) -> None:
    """Unexpected shared-parser messages retain the stable generic mapping."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7124"},
        options={},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CUSTOM_AUTO_ENABLED: True}
    )
    original_parser = config_flow_module.parse_custom_auto_options

    def parse_or_fail(raw, defaults):
        if raw.get(CONF_CUSTOM_AUTO_ENABLED) is True:
            raise CustomAutoOptionsError("unrecognized parser failure")
        return original_parser(raw, defaults)

    with patch.object(
        config_flow_module,
        "parse_custom_auto_options",
        side_effect=parse_or_fail,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _custom_auto_form_values([3, 5, 9, 15])
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_custom_auto_settings"}


async def test_user_flow_aborts_when_no_supported_device_is_visible(
    hass: HomeAssistant,
) -> None:
    """Manual setup never falls back to a typed Bluetooth address."""
    with patch(
        "custom_components.govee_ble_air_purifier.discovery.bluetooth."
        "async_discovered_service_info",
        return_value=(),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_excludes_already_configured_purifier(
    hass: HomeAssistant,
) -> None:
    """A configured address is not offered by a later manual scan."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aa:bb:cc:dd:ee:ff",
        data={CONF_ADDRESS: discovery.address, CONF_MODEL: "H7124"},
    )
    entry.add_to_hass(hass)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    with (
        patch.object(
            discovery_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.discovery.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


def test_discovery_service_accepts_only_supported_advertised_names(
    hass: HomeAssistant,
) -> None:
    """Model inference follows the two supported advertised-name families."""
    service = PurifierDiscoveryService(hass, get_profile_registry())

    assert service.model_from_name("GVH7124ABCD") == "H7124"
    assert service.model_from_name("ihoment_H7129_ABCD") == "H7129"
    assert service.model_from_name("Govee H7124 1234") is None
    assert service.model_from_name("Generic Govee device") is None


def test_discovery_service_options_rank_near_and_far_without_addresses(
    hass: HomeAssistant,
) -> None:
    """The strongest device is Near and Bluetooth addresses stay hidden."""
    service = PurifierDiscoveryService(hass, get_profile_registry())
    discoveries = (
        DiscoveredPurifier(
            address="11:11:11:11:11:11",
            name="GVH7124BEDROOM",
            model="H7124",
            rssi=-41,
        ),
        DiscoveredPurifier(
            address="22:22:22:22:22:22",
            name="ihoment_H7129_BASEMENT",
            model="H7129",
            rssi=-82,
        ),
    )
    assert service.discovery_options(discoveries) == {
        "11:11:11:11:11:11": "GVH7124BEDROOM (Near)",
        "22:22:22:22:22:22": "ihoment_H7129_BASEMENT (Far)",
    }


def test_manifest_disables_automatic_bluetooth_discovery() -> None:
    """The integration exposes only its explicit manual setup flow."""
    manifest_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "govee_ble_air_purifier"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())

    assert "bluetooth" not in manifest
    assert "bluetooth" in manifest["dependencies"]
