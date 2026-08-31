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
    VERB_DIMMER_SET,
    VERB_ROOM_BRIGHTNESS,
)
from .entity import SavantEntity
from .hub import SavantHub


def _split_addresses(addresses: str) -> list[str]:
    return [p.strip() for p in addresses.split(",")] if addresses else []


class SavantLight(SavantEntity, LightEntity):
    """A single Savant lighting load."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hub: SavantHub, device: dict[str, str]) -> None:
        super().__init__(
            hub,
            device_key=f"light:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._room = device.get("room", "")
        self._state_name = device.get("state_name", "")
        self._addresses = _split_addresses(device.get("addresses", ""))
        self._attr_unique_id = f"{hub.uid}_light_{device['id']}"
        self._attr_color_mode = ColorMode.BRIGHTNESS

    def _component(self) -> str:
        # Per-load: derive component/logical from stateName "<component>.<logical>...".
        if self._state_name and "." in self._state_name:
            return self._state_name.split(".", 1)[0]
        # Room-level: the lighting component is the host/project name (PROTOCOL.md §6).
        return self.hub.entry.data.get(CONF_NAME, "")

    def _logical_component(self) -> str:
        if self._state_name and "." in self._state_name:
            parts = self._state_name.split(".")
            return parts[1] if len(parts) > 1 else ""
        return ""

    def _dimmer_args(self, level: int) -> dict[str, Any]:
        args: dict[str, Any] = {"DimmerLevel": level, "useLastDimmerValue": False}
        for index, value in enumerate(self._addresses[:6], start=1):
            args[f"Address{index}"] = value
        for index in range(len(self._addresses) + 1, 7):
            args[f"Address{index}"] = "(null)"
        args.setdefault("Address1", "(null)")
        args["FadeTime"] = "0.5"
        args["Curve"] = "Custom 1"
        return args

    @property
    def is_on(self) -> bool:
        if self._state_name:
            level = self._state(self._state_name)
            if isinstance(level, (int, float)):
                return float(level) > 0
        return bool(self._state(f"{self._room}.{ROOM_LIGHTS_ON}"))

    @property
    def brightness(self) -> int | None:
        if self._state_name:
            level = self._state(self._state_name)
            if isinstance(level, (int, float)):
                return max(0, min(255, int(float(level) / 100 * 255)))
        level = self._state(f"{self._room}.{ROOM_BRIGHTNESS}")
        if isinstance(level, (int, float)):
            return max(0, min(255, int(float(level) / 100 * 255)))
        return 255 if self.is_on else 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._state_name and self._addresses:
            brightness = kwargs.get("brightness")
            level = round(brightness / 255 * 100) if isinstance(brightness, int) else 100
            await self._service_request(
                VERB_DIMMER_SET,
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._dimmer_args(level),
            )
            return
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 100, "useLastDimmerValue": True},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._state_name and self._addresses:
            await self._service_request(
                VERB_DIMMER_SET,
                component=self._component(),
                service_type=SVC_ENV_LIGHTING,
                zone=self._room,
                logical_component=self._logical_component(),
                request_args=self._dimmer_args(0),
            )
            return
        await self._service_request(
            VERB_ROOM_BRIGHTNESS,
            component=self._component(),
            service_type=SVC_ENV_LIGHTING,
            zone=self._room,
            request_args={"BrightnessLevel": 0, "useLastDimmerValue": True},
        )


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
