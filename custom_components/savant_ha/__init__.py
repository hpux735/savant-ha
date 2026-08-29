"""The Savant Home Assistant integration.

Hand-setup orchestration: create the hub (and its background client task) for each
config entry, and forward platform setup.  Protocol knowledge is reconstructed from
live observation — see ``PROTOCOL.md`` and the sibling ``savant-app-re`` project.
"""

from __future__ import annotations

import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_UID, DOMAIN
from .hub import SavantHub

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.SENSOR,
]


def _get_hub(hass: HomeAssistant, entry: ConfigEntry) -> SavantHub:
    return hass.data[DOMAIN][entry.entry_id]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    # YAML setup is not supported; configuration is via the UI only.
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Ensure a stable client identity is persisted with the entry (PROTOCOL.md §2:
    # ``uid`` is constant per install).
    if not entry.data.get(CONF_UID):
        data = dict(entry.data)
        data[CONF_UID] = uuid.uuid4().hex
        hass.config_entries.async_update_entry(entry, data=data)

    hub = SavantHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hub.coordinator.async_config_entry_first_refresh()
    await hub.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.stop)
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
