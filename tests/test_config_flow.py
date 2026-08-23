"""Tests for the Govee BLE Air Purifier config flow."""

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_air_purifier.config_flow import (
    _model_from_name,
    _normalize_address,
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
    ):
        yield


pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_user_flow_creates_one_entry_per_address(
    hass: HomeAssistant,
) -> None:
    """A visible connectable purifier creates an address-keyed entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    device = SimpleNamespace(name="Govee H7129")
    with (
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "bluetooth.async_ble_device_from_address",
            return_value=device,
        ),
        patch(
            "custom_components.govee_ble_air_purifier.config_flow."
            "_async_validate_purifier",
            new_callable=AsyncMock,
        ) as validate,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: "AA-BB-CC-DD-EE-FF", CONF_MODEL: "H7129"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Govee H7129"
    assert result["data"] == {
        CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
        CONF_MODEL: "H7129",
    }
    validate.assert_awaited_once()


async def test_user_flow_requires_current_connectable_discovery(
    hass: HomeAssistant,
) -> None:
    """Manual setup does not store an unreachable address."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.govee_ble_air_purifier.config_flow."
        "bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_MODEL: "H7124"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_discovered"}


async def test_duplicate_address_is_aborted(hass: HomeAssistant) -> None:
    """A normalized address can belong to only one config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aa:bb:cc:dd:ee:ff",
        data={CONF_ADDRESS: "aa:bb:cc:dd:ee:ff", CONF_MODEL: "H7124"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.govee_ble_air_purifier.config_flow."
        "bluetooth.async_ble_device_from_address",
        return_value=SimpleNamespace(name="Govee H7124"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ADDRESS: "AABBCCDDEEFF", CONF_MODEL: "H7124"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_flow_confirms_and_infers_model(
    hass: HomeAssistant,
) -> None:
    """A narrow future Bluetooth matcher can use the confirmation flow."""
    discovery = MagicMock()
    discovery.address = "AA:BB:CC:DD:EE:FF"
    discovery.name = "Govee H7129 ABCD"
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


def test_address_and_model_normalization() -> None:
    """Stable addresses and conservative name inference are deterministic."""
    assert _normalize_address("AA-BB-CC-DD-EE-FF") == "AA:BB:CC:DD:EE:FF"
    assert _model_from_name("Govee H7124 1234") == "H7124"
    assert _model_from_name("Generic Govee device") is None
