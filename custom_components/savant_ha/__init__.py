"""The Savant Home Assistant integration.

Hand-setup orchestration: create the hub (and its background client task) for each
config entry, and forward platform setup.  Protocol knowledge is reconstructed from
live observation — see ``PROTOCOL.md`` and the sibling ``savant-app-re`` project.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_UID, DOMAIN, new_uid
from .hub import SavantHub

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.COVER,
    Platform.FAN,
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.SCENE,
]
SERVICE_SET_MEDIA_SERVER_ZONES = "set_media_server_zones"
ATTR_ZONE_ENTITY_IDS = "zone_entity_ids"
SET_MEDIA_SERVER_ZONES_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_ZONE_ENTITY_IDS): vol.All(cv.ensure_list, [cv.entity_id]),
    }
)


def _get_hub(hass: HomeAssistant, entry: ConfigEntry) -> SavantHub:
    return hass.data[DOMAIN][entry.entry_id]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    # YAML setup is not supported; configuration is via the UI only.
    async def _async_set_media_server_zones(
        call: ServiceCall,
    ) -> dict[str, object] | None:
        entity_id = call.data[ATTR_ENTITY_ID]
        registry_entry = er.async_get(hass).async_get(entity_id)
        if registry_entry is None:
            if hass.states.get(entity_id) is None:
                raise ServiceValidationError(f"Unknown entity: {entity_id}")
            raise ServiceValidationError(f"Entity is not managed by Savant: {entity_id}")
        if registry_entry.platform != DOMAIN or not registry_entry.config_entry_id:
            raise ServiceValidationError(f"Entity is not managed by Savant: {entity_id}")
        hub = hass.data.get(DOMAIN, {}).get(registry_entry.config_entry_id)
        entity = hub.media_entity(entity_id) if hub is not None else None
        if entity is None:
            raise ServiceValidationError(f"Entity is not a Savant media player: {entity_id}")

        zone_entity_ids = list(dict.fromkeys(call.data[ATTR_ZONE_ENTITY_IDS]))
        registry = er.async_get(hass)
        for zone_entity_id in zone_entity_ids:
            zone_entry = registry.async_get(zone_entity_id)
            if zone_entry is None:
                if hass.states.get(zone_entity_id) is None:
                    raise ServiceValidationError(f"Unknown entity: {zone_entity_id}")
                raise ServiceValidationError(
                    f"Entity is not managed by Savant: {zone_entity_id}"
                )
            if (
                zone_entry.platform != DOMAIN
                or zone_entry.config_entry_id != registry_entry.config_entry_id
            ):
                raise ServiceValidationError(
                    f"Entity is not managed by the same Savant host: {zone_entity_id}"
                )
        if call.context.user_id is not None:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise ServiceValidationError("Unknown Home Assistant user")
            for controlled_entity_id in (entity_id, *zone_entity_ids):
                if not user.permissions.check_entity(
                    controlled_entity_id, POLICY_CONTROL
                ):
                    raise ServiceValidationError(
                        f"Not authorized to control {controlled_entity_id}"
                    )
        result = await entity.async_set_media_server_zones(zone_entity_ids)
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MEDIA_SERVER_ZONES,
        _async_set_media_server_zones,
        schema=SET_MEDIA_SERVER_ZONES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Ensure a stable client identity is persisted with the entry (PROTOCOL.md §2:
    # ``uid`` is constant per install).
    if not entry.data.get(CONF_UID):
        data = dict(entry.data)
        data[CONF_UID] = new_uid()
        hass.config_entries.async_update_entry(entry, data=data)

    hub = SavantHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hub.coordinator.async_config_entry_first_refresh()
    await hub.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_stop(_event: Event) -> None:
        await hub.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    # Re-read the advanced options (credentials/rooms) when they change.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hub = _get_hub(hass, entry)
        await hub.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
