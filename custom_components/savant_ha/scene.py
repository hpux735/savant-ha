"""Savant dashboard scenes as native Home Assistant scene entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import SavantEntity
from .hub import SavantHub
from .savant_client import SavantError


class SavantScene(SavantEntity, Scene):
    """A saved Savant dashboard scene, attached directly to the hub device."""

    def __init__(self, hub: SavantHub, scene_id: str, name: str) -> None:
        super().__init__(hub)
        self._scene_id = scene_id
        self._attr_name = name
        self._attr_unique_id = f"{hub.uid}_scene_{scene_id}"

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the scene through the observed Savant ApplyScene RPC."""
        try:
            await self.hub.client.activate_scene(self._scene_id)
        except SavantError as err:
            raise HomeAssistantError(str(err)) from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: SavantHub = hass.data[DOMAIN][entry.entry_id]
    scenes: dict[str, SavantScene] = {}
    registry = er.async_get(hass)
    unique_prefix = f"{hub.uid}_scene_"

    def _marker(scene_id: str) -> str:
        return f"scene_entity:{hub.uid}_scene_{scene_id}"

    def _reconcile() -> None:
        if hub.scenes is None:
            return
        current_ids = set(hub.scenes)
        removed_ids = set(scenes) - current_ids
        for scene_id in removed_ids:
            unique_id = scenes[scene_id].unique_id
            entity_id = registry.async_get_entity_id("scene", DOMAIN, unique_id)
            if entity_id:
                registry.async_remove(entity_id)
            hub.unmark_created([_marker(scene_id)])
            del scenes[scene_id]

        # Migrate previous releases' button entities and remove scene entries the host no
        # longer lists.  The dashboard update is a complete authoritative inventory.
        for registry_entry in list(registry.entities.values()):
            if (
                registry_entry.config_entry_id != entry.entry_id
                or registry_entry.platform != DOMAIN
                or not registry_entry.unique_id.startswith(unique_prefix)
            ):
                continue
            scene_id = registry_entry.unique_id[len(unique_prefix):]
            if registry_entry.entity_id.startswith("button.") or scene_id not in current_ids:
                registry.async_remove(registry_entry.entity_id)

        new_scenes = [
            SavantScene(hub, scene_id, scene["name"])
            for scene_id, scene in hub.scenes.items()
            if scene_id not in scenes and not hub.is_created(_marker(scene_id))
        ]
        if new_scenes:
            for scene in new_scenes:
                scenes[scene._scene_id] = scene
            hub.mark_created([_marker(scene._scene_id) for scene in new_scenes])
            async_add_entities(new_scenes)

    _reconcile()
    hub.add_scene_callback(_reconcile)
