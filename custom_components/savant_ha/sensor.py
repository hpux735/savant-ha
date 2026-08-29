"""Sensor platform: temperature and humidity read-outs.

Sensors are only created for state keys the host actually reports (no speculative
entities): ``global.CurrentTemperature``, ``<room>.RoomCurrentTemperature``, and the
HVAC humidity point.  When the config-flow device picker approved a device list,
room/hvac sensors follow that list and inherit its area assignments.
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
    DEVICE_TYPE_HVAC,
    DEVICE_TYPE_ROOM,
    DOMAIN,
    GLOBAL_CURRENT_TEMPERATURE,
    HVAC_STATE_PREFIX,
    ROOM_CURRENT_TEMPERATURE,
)
from .entity import SavantEntity
from .hub import SavantHub

_HVAC_HUMIDITY_ATTR = "ThermostatCurrentHumidity"


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
        device_key: str = "",
        device_name: str = "",
        area: str = "",
    ) -> None:
        super().__init__(hub, device_key=device_key, device_name=device_name, area=area)
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


def _room_temperature_sensor(hub: SavantHub, room: str, area: str = "") -> SavantSensor:
    return SavantSensor(
        hub,
        unique_suffix=f"sensor_room_temperature_{room}",
        name=f"{room} Temperature",
        key=f"{room}.{ROOM_CURRENT_TEMPERATURE}",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.FAHRENHEIT,
        device_key=f"room:{room}",
        device_name=room,
        area=area,
    )


def _hvac_humidity_sensor(hub: SavantHub, suffix: str, area: str = "") -> SavantSensor:
    return SavantSensor(
        hub,
        unique_suffix=f"sensor_hvac_humidity_{suffix.lstrip('_')}",
        name="Thermostat Humidity",
        key=f"{HVAC_STATE_PREFIX}{_HVAC_HUMIDITY_ATTR}{suffix}",
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
        device_key=f"hvac:{suffix}",
        device_name="Thermostat",
        area=area,
    )


def _build_entities(hub: SavantHub) -> list[SavantSensor]:
    # ASSUMPTION: Fahrenheit, matching the observed SchedulerSettings scale
    # (PROTOCOL.md §5.4).
    entities: list[SavantSensor] = []
    if GLOBAL_CURRENT_TEMPERATURE in hub.states:
        entities.append(
            SavantSensor(
                hub,
                unique_suffix="sensor_global_temperature",
                name="Global Temperature",
                key=GLOBAL_CURRENT_TEMPERATURE,
                device_class=SensorDeviceClass.TEMPERATURE,
                unit=UnitOfTemperature.FAHRENHEIT,
            )
        )

    if hub.devices is not None:
        for device in hub.devices:
            device_type = device.get("type")
            area = device.get("area", "")
            if device_type == DEVICE_TYPE_ROOM:
                key = f"{device['id']}.{ROOM_CURRENT_TEMPERATURE}"
                if key in hub.states:
                    entities.append(
                        _room_temperature_sensor(hub, device["id"], area=area)
                    )
            elif device_type == DEVICE_TYPE_HVAC:
                key = f"{HVAC_STATE_PREFIX}{_HVAC_HUMIDITY_ATTR}{device['id']}"
                if key in hub.states:
                    entities.append(_hvac_humidity_sensor(hub, device["id"], area=area))
        return entities

    # Legacy entries (no approved device list): key-gated dynamic discovery.
    if f"{HVAC_STATE_PREFIX}{_HVAC_HUMIDITY_ATTR}_1" in hub.states:
        entities.append(_hvac_humidity_sensor(hub, "_1"))
    for room in sorted(hub.rooms):
        if f"{room}.{ROOM_CURRENT_TEMPERATURE}" in hub.states:
            entities.append(_room_temperature_sensor(hub, room))
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
