"""Tests for the Govee BLE Air Purifier config flow."""

import asyncio
import time
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_air_purifier import config_flow as config_flow_module
from custom_components.govee_ble_air_purifier.config_flow import (
    _discover_purifiers,
    _discovery_options,
    _model_from_name,
)
from custom_components.govee_ble_air_purifier.const import CONF_MODEL, DOMAIN


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
        patch.object(config_flow_module, "MANUAL_DISCOVERY_SCAN_DURATION", 0.0),
        patch.object(
            config_flow_module,
            "SELECTED_DEVICE_ADVERTISEMENT_TIMEOUT",
            1,
        ),
    ):
        yield


pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _service_info(
    name: str,
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
        assert mode is config_flow_module.bluetooth.BluetoothScanningMode.ACTIVE
        assert timeout == 1
        return fresh

    with (
        patch.object(
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            side_effect=active_scan,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
            "async_discovered_service_info",
            side_effect=discoveries,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "bluetooth.async_process_advertisements",
            new_callable=AsyncMock,
            side_effect=process_advertisements,
        ) as process_advertisements,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "bluetooth.async_last_service_info",
            return_value=discovery,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "_async_validate_purifier",
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == discovery.name
    assert result["data"] == {
        CONF_ADDRESS: discovery.address,
        CONF_MODEL: "H7129",
    }
    validate.assert_awaited_once_with(
        hass,
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
        is config_flow_module.bluetooth.BluetoothScanningMode.ACTIVE
    )
    assert process_call.args[4] == 1
    assert call_order == ["active_scan", "discovery_cache"]


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
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
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
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=failed_scan),
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
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
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=RuntimeError("scanner unavailable")),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
            "async_discovered_service_info",
            return_value=(stale_discovery,),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


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
        patch.object(config_flow_module, "MANUAL_DISCOVERY_SCAN_DURATION", 0.05),
        patch.object(
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new_callable=AsyncMock,
            create=True,
        ) as request_active_scan,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
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


async def test_user_flow_requires_fresh_selected_device_advertisement(
    hass: HomeAssistant,
) -> None:
    """Selection stays on the form when the purifier is no longer visible."""
    discovery = _service_info("GVH7124BEDROOM", "AA:BB:CC:DD:EE:FF", -52)

    async def active_scan(_: HomeAssistant, *, duration: float) -> None:
        discovery.time = time.monotonic()

    with (
        patch.object(
            config_flow_module.bluetooth,
            "async_request_active_scan",
            new=AsyncMock(side_effect=active_scan),
            create=True,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
            "async_discovered_service_info",
            return_value=(discovery,),
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
            "async_process_advertisements",
            new=AsyncMock(side_effect=TimeoutError),
        ) as process_advertisements,
        patch(
            "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
            "async_last_service_info",
            return_value=discovery,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "_async_validate_purifier",
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


async def test_user_flow_aborts_when_no_supported_device_is_visible(
    hass: HomeAssistant,
) -> None:
    """Manual setup never falls back to a typed Bluetooth address."""
    with patch(
        "custom_components.govee_ble_air_purifier.config_flow.bluetooth."
        "async_discovered_service_info",
        return_value=(),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_duplicate_bluetooth_discovery_is_aborted(hass: HomeAssistant) -> None:
    """A discovered address can belong to only one config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aa:bb:cc:dd:ee:ff",
        data={CONF_ADDRESS: "aa:bb:cc:dd:ee:ff", CONF_MODEL: "H7124"},
    )
    entry.add_to_hass(hass)

    discovery = MagicMock()
    discovery.address = "AA:BB:CC:DD:EE:FF"
    discovery.name = "GVH7124ABCD"
    discovery.connectable = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=discovery,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_flow_confirms_and_infers_model(
    hass: HomeAssistant,
) -> None:
    """A narrow future Bluetooth matcher can use the confirmation flow."""
    discovery = MagicMock()
    discovery.address = "AA:BB:CC:DD:EE:FF"
    discovery.name = "ihoment_H7129_ABCD"
    discovery.connectable = True

    with patch(
        "custom_components.govee_ble_air_purifier.config_flow."
        "_async_validate_purifier",
        new_callable=AsyncMock,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=discovery,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "H7129"
    assert result["data"][CONF_ADDRESS] == "AA:BB:CC:DD:EE:FF"


def test_model_inference_accepts_only_supported_advertised_names() -> None:
    """Model inference follows the two narrow manifest matcher families."""
    assert _model_from_name("GVH7124ABCD") == "H7124"
    assert _model_from_name("ihoment_H7129_ABCD") == "H7129"
    assert _model_from_name("Govee H7124 1234") is None
    assert _model_from_name("Generic Govee device") is None


def test_discovery_options_rank_near_and_far_without_showing_addresses() -> None:
    """The strongest device is Near and Bluetooth addresses stay hidden."""
    discoveries = _discover_purifiers(
        (
            _service_info("ihoment_H7129_BASEMENT", "22:22:22:22:22:22", -82),
            _service_info("GVH7124BEDROOM", "11:11:11:11:11:11", -41),
            _service_info("Other device", "33:33:33:33:33:33", -20),
        )
    )

    assert [device.name for device in discoveries] == [
        "GVH7124BEDROOM",
        "ihoment_H7129_BASEMENT",
    ]
    assert _discovery_options(discoveries) == {
        "11:11:11:11:11:11": "GVH7124BEDROOM (Near)",
        "22:22:22:22:22:22": "ihoment_H7129_BASEMENT (Far)",
    }
