"""Savant dashboard scene buttons.

Scene summaries are a push-updated dashboard inventory (PROTOCOL.md §9.1), so this
platform reconciles entities when scenes are created or removed on the host.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import SavantEntity
from .hub import SavantHub
from .savant_client import SavantSceneActivationUnsupported


class SavantSceneButton(SavantEntity, ButtonEntity):
    """A saved Savant dashboard scene, attached directly to the hub device."""

    def __init__(self, hub: SavantHub, scene_id: str, name: str) -> None:
        super().__init__(hub)
        self._scene_id = scene_id
        self._attr_name = name
        self._attr_unique_id = f"{hub.uid}_scene_{scene_id}"

    async def async_press(self) -> None:
        try:
            await self.hub.client.activate_scene(self._scene_id)
        except SavantSceneActivationUnsupported as err:
            raise HomeAssistantError(str(err)) from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: SavantHub = hass.data[DOMAIN][entry.entry_id]
    buttons: dict[str, SavantSceneButton] = {}
    registry = er.async_get(hass)
    unique_prefix = f"{hub.uid}_scene_"

    def _reconcile() -> None:
        if hub.scenes is None:
            return
        current_ids = set(hub.scenes)
        removed_ids = set(buttons) - current_ids
        for scene_id in removed_ids:
            unique_id = buttons[scene_id].unique_id
            entity_id = registry.async_get_entity_id("button", DOMAIN, unique_id)
            if entity_id:
                registry.async_remove(entity_id)
            hub.unmark_created([unique_id])
            del buttons[scene_id]

        # Remove scene registry entries omitted from the host's current full-list update.
        for registry_entry in list(registry.entities.values()):
            if (
                registry_entry.config_entry_id == entry.entry_id
                and registry_entry.platform == DOMAIN
                and registry_entry.unique_id.startswith(unique_prefix)
                and registry_entry.unique_id[len(unique_prefix):] not in current_ids
            ):
                registry.async_remove(registry_entry.entity_id)

        new_buttons = [
            SavantSceneButton(hub, scene_id, scene["name"])
            for scene_id, scene in hub.scenes.items()
            if scene_id not in buttons
            and not hub.is_created(f"{hub.uid}_scene_{scene_id}")
        ]
        if new_buttons:
            for button in new_buttons:
                buttons[button._scene_id] = button
            hub.mark_created([button.unique_id for button in new_buttons])
            async_add_entities(new_buttons)

    _reconcile()
    hub.add_scene_callback(_reconcile)
