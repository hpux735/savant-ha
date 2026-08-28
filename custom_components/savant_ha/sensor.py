"""Sensor platform: temperature and humidity read-outs.

Reads the observed state keys (PROTOCOL.md §5): ``global.CurrentTemperature``,
``<room>.RoomCurrentTemperature``, and the HVAC humidity point.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ROOMS,
    DOMAIN,
    GLOBAL_CURRENT_TEMPERATURE,
    HVAC_STATE_PREFIX,
    ROOM_CURRENT_TEMPERATURE,
)
from .entity import SavantEntity
from .hub import SavantHub

_HVAC_HUMIDITY = f"{HVAC_STATE_PREFIX}ThermostatCurrentHumidity_1"


class SavantSensor(SavantEntity, SensorEntity):
    """A numeric sensor reading a single dotted state key."""

    def __init__(
        self,
        hub: SavantHub,
        *,
        unique_suffix: str,
        name: str,
        key: str,
        device_class: SensorDeviceClass,
        unit: str,
    ) -> None:
        super().__init__(hub)
        self._key = key
        self._attr_unique_id = f"{hub.uid}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        value = self.hub.get(self._key)
        if isinstance(value, (int, float)):
            return float(value)
        return None


def _discovered_rooms(hub: SavantHub) -> set[str]:
    rooms: set[str] = set(hub.entry.data.get(CONF_ROOMS) or [])
    suffix = f".{ROOM_CURRENT_TEMPERATURE}"
    for key in hub.states:
        if key.endswith(suffix):
            rooms.add(key[: -len(suffix)])
    return rooms


def _build_entities(hub: SavantHub) -> list[SavantSensor]:
    # ASSUMPTION: Fahrenheit, matching the observed SchedulerSettings scale
    # (PROTOCOL.md §5.4).
    entities: list[SavantSensor] = [
        SavantSensor(
            hub,
            unique_suffix="sensor_global_temperature",
            name="Global Temperature",
            key=GLOBAL_CURRENT_TEMPERATURE,
            device_class=SensorDeviceClass.TEMPERATURE,
            unit=UnitOfTemperature.FAHRENHEIT,
        ),
        SavantSensor(
            hub,
            unique_suffix="sensor_hvac_humidity",
            name="Thermostat Humidity",
            key=_HVAC_HUMIDITY,
            device_class=SensorDeviceClass.HUMIDITY,
            unit=PERCENTAGE,
        ),
    ]
    for room in sorted(_discovered_rooms(hub)):
        entities.append(
            SavantSensor(
                hub,
                unique_suffix=f"sensor_room_temperature_{room}",
                name=f"{room} Temperature",
                key=f"{room}.{ROOM_CURRENT_TEMPERATURE}",
                device_class=SensorDeviceClass.TEMPERATURE,
                unit=UnitOfTemperature.FAHRENHEIT,
            )
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: SavantHub = hass.data[DOMAIN][entry.entry_id]

    def _add() -> None:
        entities = [e for e in _build_entities(hub) if not hub.is_created(e.unique_id)]
        if entities:
            hub.mark_created([e.unique_id for e in entities])
            async_add_entities(entities)

    _add()
    hub.add_platform_callback(_add)
