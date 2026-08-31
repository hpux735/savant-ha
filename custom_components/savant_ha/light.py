"""Light platform: one entity per Savant lighting load (from the config archive).

Each ``LightEntities`` row in ``serviceImplementation.sqlite`` is a single load with an
``addresses`` field (the ``DimmerSet`` ``Address*`` args) and a ``stateName`` field (the
per-load dimmer/colour state key).  Loads without those fields fall back to room-level
on/off via ``__RoomSetBrightness`` (PROTOCOL.md §6).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_TYPE_LIGHT,
    DOMAIN,
    ROOM_BRIGHTNESS,
    ROOM_LIGHTS_ON,
    SVC_ENV_LIGHTING,
    VERB_ROOM_BRIGHTNESS,
)
from .control import (
    dimmer_args,
    dimmer_command,
    is_switch,
    light_address_args,
    parse_light_state,
    state_name_component,
    state_name_logical,
)
from .entity import SavantEntity
from .hub import SavantHub


class SavantLight(SavantEntity, LightEntity):
    """A single Savant lighting load."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hub: SavantHub, device: dict[str, Any]) -> None:
        super().__init__(
            hub,
            device_key=f"light:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._room = device.get("room", "")
        self._state_name = device.get("state_name", "")
        self._addresses = device.get("addresses", "")
        self._device = device
        self._is_switch = is_switch(device)
        # Optimistic state: the host does not push lighting state back on this build, so
        # remember the last commanded on/brightness to make the power toggle behave like
        # a normal HA light (instant feedback + correct on/off toggling).
        self._assumed_on: bool | None = None
        self._assumed_brightness: int | None = None
        if self._is_switch:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
        self._attr_unique_id = f"{hub.uid}_light_{device['id']}"
        if not self._is_switch:
            self._attr_color_mode = ColorMode.BRIGHTNESS

    def _component(self) -> str:
        # Per-load: derive component/logical from stateName "<component>.<logical>...".
        component = state_name_component(self._state_name)
        if component:
            return component
        # Room-level: the lighting component is the host/project name (PROTOCOL.md §6).
        return self.hub.entry.data.get(CONF_NAME, "")

    def _logical_component(self) -> str:
        return state_name_logical(self._state_name)

    def _address_args(self) -> dict[str, Any]:
        return light_address_args(self._addresses)

    def _dimmer_args(self, level: int) -> dict[str, Any]:
        return dimmer_args(self._device, level)

    def _parsed_state(self) -> tuple[bool | None, int | None]:
        if not self._state_name:
            return None, None
        return parse_light_state(self._state_name, self._state(self._state_name))

    @property
    def is_on(self) -> bool:
        real_on, _ = self._parsed_state()
        if real_on is not None:
            return real_on
        if self._assumed_on is not None:
            return self._assumed_on
        return bool(self._state(f"{self._room}.{ROOM_LIGHTS_ON}"))

    @property
    def brightness(self) -> int | None:
        if self._is_switch:
            return None
        _, real_brightness = self._parsed_state()
        if real_brightness is not None:
            return real_brightness
        if self._assumed_brightness is not None:
            return self._assumed_brightness
        level = self._state(f"{self._room}.{ROOM_BRIGHTNESS}")
        if isinstance(level, (int, float)):
            return max(0, min(255, int(float(level) / 100 * 255)))
        return 255 if self.is_on else 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._is_switch:
            await self._service_request(
                "SwitchOn",
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._address_args(),
            )
            self._assumed_on = True
            self.async_write_ha_state()
            return
        brightness = kwargs.get("brightness")
        if self._state_name and self._addresses:
            level = round(brightness / 255 * 100) if isinstance(brightness, int) else 100
            await self._service_request(
                dimmer_command(self._device),
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._dimmer_args(level),
            )
            self._assumed_on = True
            self._assumed_brightness = brightness if isinstance(brightness, int) else 255
            self.async_write_ha_state()
            return
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 100},
        )
        self._assumed_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._is_switch:
            await self._service_request(
                "SwitchOff",
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._address_args(),
            )
            self._assumed_on = False
            self.async_write_ha_state()
            return
        if self._state_name and self._addresses:
            await self._service_request(
                dimmer_command(self._device),
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._dimmer_args(0),
            )
            self._assumed_on = False
            self._assumed_brightness = 0
            self.async_write_ha_state()
            return
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 0},
        )
        self._assumed_on = False
        self.async_write_ha_state()


def _build_entities(hub: SavantHub) -> list[SavantLight]:
    if hub.devices is not None:
        return [
            SavantLight(hub, device)
            for device in hub.devices
            if device.get("type") == DEVICE_TYPE_LIGHT
        ]
    # Legacy fallback: one room-level light per room that reports lighting.
    return [
        SavantLight(hub, {"id": room, "name": room, "room": room})
        for room in sorted(hub.rooms)
        if f"{room}.{ROOM_LIGHTS_ON}" in hub.states
        or f"{room}.{ROOM_BRIGHTNESS}" in hub.states
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
