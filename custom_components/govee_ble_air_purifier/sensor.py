"""Sensor platform for Govee BLE Air Purifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

try:
    from homeassistant.const import UnitOfDensity, UnitOfRatio

    PM25_UNIT = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    FILTER_LIFE_UNIT = UnitOfRatio.PERCENTAGE
except ImportError:  # Home Assistant before the unit-enum migration
    from homeassistant.const import (
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        PERCENTAGE,
    )

    PM25_UNIT = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    FILTER_LIFE_UNIT = PERCENTAGE

from . import GoveeConfigEntry
from .entity import GoveePurifierEntity


@dataclass(frozen=True, kw_only=True)
class GoveePurifierSensorDescription(SensorEntityDescription):
    """Describe a purifier sensor backed by one state attribute."""

    state_attribute: str


SENSORS: tuple[GoveePurifierSensorDescription, ...] = (
    GoveePurifierSensorDescription(
        key="pm25",
        translation_key="pm25",
        state_attribute="pm25",
        device_class=SensorDeviceClass.PM25,
        native_unit_of_measurement=PM25_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    GoveePurifierSensorDescription(
        key="filter_life",
        translation_key="filter_life",
        state_attribute="filter_life",
        native_unit_of_measurement=FILTER_LIFE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GoveeConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up purifier sensors."""
    capabilities = entry.runtime_data.profile.capabilities
    supported = {
        "pm25": capabilities.pm25,
        "filter_life": capabilities.filter_life,
    }
    async_add_entities(
        GoveePurifierSensor(entry, description)
        for description in SENSORS
        if supported[description.key]
    )


class GoveePurifierSensor(GoveePurifierEntity, SensorEntity):
    """Representation of a cached purifier sensor."""

    entity_description: GoveePurifierSensorDescription

    def __init__(
        self,
        entry: GoveeConfigEntry,
        description: GoveePurifierSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the cached sensor value."""
        return getattr(self._state, self.entity_description.state_attribute)
