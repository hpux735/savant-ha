"""Cover platform: one entity per Savant shade (from the config archive).

Shade state is read from ``<room>.RoomShadesAreOpen``; the open/close command verbs are
not yet in the observed catalog (PROTOCOL.md §6/§7), so these entities are read-only
for now.
"""

from __future__ import annotations

from homeassistant.components.cover import CoverDeviceClass, CoverEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEVICE_TYPE_COVER, DOMAIN, ROOM_SHADES_OPEN
from .entity import SavantEntity
from .hub import SavantHub


class SavantCover(SavantEntity, CoverEntity):
    """A single Savant shade (read-only until open/close verbs are known)."""

    _attr_device_class = CoverDeviceClass.SHADE

    def __init__(self, hub: SavantHub, device: dict[str, str]) -> None:
        super().__init__(
            hub,
            device_key=f"cover:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._room = device.get("room", "")
        self._attr_unique_id = f"{hub.uid}_cover_{device['id']}"

    @property
    def is_closed(self) -> bool | None:
        value = self._state(f"{self._room}.{ROOM_SHADES_OPEN}")
        if isinstance(value, bool):
            return not value
        return None


def _build_entities(hub: SavantHub) -> list[SavantCover]:
    if hub.devices is None:
        return []
    return [
        SavantCover(hub, device)
        for device in hub.devices
        if device.get("type") == DEVICE_TYPE_COVER
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
