"""Cover platform: one entity per Savant shade (from the config archive).

Shade state is read from ``<room>.RoomShadesAreOpen``; the open/close command verbs are
not yet in the observed catalog (PROTOCOL.md §6/§7), so these entities are read-only
for now.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEVICE_TYPE_COVER, DOMAIN, SVC_ENV_SHADE
from .control import shade_address_args, shade_component_logical
from .entity import SavantEntity
from .hub import SavantHub


class SavantCover(SavantEntity, CoverEntity):
    """A single archive-derived Savant shade."""

    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, hub: SavantHub, device: dict[str, Any]) -> None:
        super().__init__(
            hub,
            device_key=f"cover:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._room = device.get("room", "")
        self._state_name = str(device.get("state_name") or "")
        self._addresses = str(device.get("addresses") or "")
        self._component, self._logical_component = shade_component_logical(self._state_name)
        self._attr_unique_id = f"{hub.uid}_cover_{device['id']}"

    @property
    def is_closed(self) -> bool | None:
        value = self._state(self._state_name)
        if isinstance(value, (int, float)):
            # ASSUMPTION: the observed ShadeLevel uses 0 for closed and 100 for open.
            return value <= 0
        return None

    def _address_args(self, count: int = 5) -> dict[str, str]:
        return shade_address_args(self._addresses, count)

    async def _shade_request(self, request: str) -> None:
        await self._service_request(
            request,
            component=self._component,
            service_type=SVC_ENV_SHADE,
            zone=self._room,
            logical_component=self._logical_component,
            variant_id="1",
            request_args=self._address_args(),
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._shade_request("ShadeUp")

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._shade_request("ShadeDown")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._shade_request("ShadeStop")


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
