"""Fan platform: one entity per Savant fan (from the config archive).

Fan state is read from ``<room>.RoomFansAreOn``; the fan command verbs are not yet in
the observed catalog (PROTOCOL.md §6/§7), so these entities are read-only for now.
"""

from __future__ import annotations

from homeassistant.components.fan import FanEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEVICE_TYPE_FAN, DOMAIN
from .entity import SavantEntity
from .hub import SavantHub


class SavantFan(SavantEntity, FanEntity):
    """A single Savant fan (read-only until fan verbs are known)."""

    def __init__(self, hub: SavantHub, device: dict[str, str]) -> None:
        super().__init__(
            hub,
            device_key=f"fan:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._state_name = device.get("state_name", "")
        self._attr_unique_id = f"{hub.uid}_fan_{device['id']}"

    @property
    def is_on(self) -> bool | None:
        value = self._state(self._state_name) if self._state_name else None
        return value if isinstance(value, bool) else None


def _build_entities(hub: SavantHub) -> list[SavantFan]:
    if hub.devices is None:
        return []
    return [
        SavantFan(hub, device)
        for device in hub.devices
        if device.get("type") == DEVICE_TYPE_FAN
    ]


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
