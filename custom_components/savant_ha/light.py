"""Light platform: one entity per configured/discovered Savant room.

Room-level on/off is implemented via the observed ``__RoomSetBrightness`` verb
(PROTOCOL.md §6).  Per-load dimming/colour (``DimmerSet``) requires load ``Address*``
values that are not surfaced by the state bus yet, so lights are on/off-only for now;
``BrightnessLevel`` is still exposed as a read-only attribute.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_NAME,
    DOMAIN,
    ROOM_BRIGHTNESS,
    ROOM_LIGHTS_ON,
    SVC_ENV_LIGHTING,
    VERB_ROOM_BRIGHTNESS,
)
from .entity import SavantEntity
from .hub import SavantHub


def _discovered_rooms(hub: SavantHub) -> set[str]:
    # hub.rooms = user-supplied rooms + rooms derived from scenes / startZone / state
    # keys (PROTOCOL.md §6.1).
    return set(hub.rooms)


class SavantLight(SavantEntity, LightEntity):
    """Room-level lighting (on/off; brightness attribute read-only)."""

    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, hub: SavantHub, room: str) -> None:
        super().__init__(hub)
        self._room = room
        self._attr_unique_id = f"{hub.uid}_light_{room}"
        self._attr_name = room
        self._attr_color_mode = ColorMode.ONOFF

    def _key(self, attr: str) -> str:
        return f"{self._room}.{attr}"

    @property
    def is_on(self) -> bool:
        return bool(self._state(self._key(ROOM_LIGHTS_ON)))

    @property
    def brightness(self) -> int | None:
        level = self._state(self._key(ROOM_BRIGHTNESS))
        if isinstance(level, (int, float)):
            return max(0, min(255, int(float(level) / 100 * 255)))
        return 255 if self.is_on else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"brightness_level": self._state(self._key(ROOM_BRIGHTNESS))}

    def _component(self) -> str:
        # The lighting ``component`` is the host/project name (PROTOCOL.md §6).  It is
        # captured from the discovery record during config flow; if absent, commands
        # cannot be formed.
        return self.hub.entry.data.get(CONF_NAME, "")

    async def async_turn_on(self, **kwargs: Any) -> None:
        # kwargs brightness is not used: room-level control only supports 0/100 today.
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 100, "useLastDimmerValue": True},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 0, "useLastDimmerValue": True},
        )


def _build_entities(hub: SavantHub) -> list[SavantLight]:
    return [SavantLight(hub, room) for room in sorted(_discovered_rooms(hub))]


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
