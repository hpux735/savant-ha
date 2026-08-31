"""Sensor platform: temperature read-outs.

Sensors are only created for state keys the host actually reports (no speculative
entities): ``global.CurrentTemperature`` and ``<room>.RoomCurrentTemperature``.  The
climate entity already exposes the HVAC temperature/humidity, so those aren't duplicated
here.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    GLOBAL_CURRENT_TEMPERATURE,
    ROOM_CURRENT_TEMPERATURE,
)
from .entity import SavantEntity
from .hub import SavantHub


class SavantSensor(SavantEntity, SensorEntity):
    """A numeric sensor reading a single dotted state key."""

    def __init__(
        self,
        hub: SavantHub,
        *,
        unique_suffix: str,
        name: str,
        key: str,
        device_key: str = "",
        device_name: str = "",
        area: str = "",
    ) -> None:
        super().__init__(hub, device_key=device_key, device_name=device_name, area=area)
        self._key = key
        self._attr_unique_id = f"{hub.uid}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        value = self.hub.get(self._key)
        if isinstance(value, (int, float)):
            return float(value)
        return None


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
            )
        )
    for room in sorted(hub.rooms):
        key = f"{room}.{ROOM_CURRENT_TEMPERATURE}"
        if key in hub.states:
            entities.append(
                SavantSensor(
                    hub,
                    unique_suffix=f"sensor_room_temperature_{room}",
                    name=f"{room} Temperature",
                    key=key,
                    device_key=f"room:{room}",
                    device_name=room,
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
