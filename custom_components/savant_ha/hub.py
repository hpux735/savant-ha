"""Hub + coordinator: the per-config-entry object graph.

The hub owns a :class:`~.savant_client.SavantClient`, buffers the flat state store
(``states``), and bridges protocol pushes to a :class:`DataUpdateCoordinator` that the
platform entities subscribe to.  Platforms register callbacks here so they can
materialize new entities the first time a matching state key appears.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_CLOUD_TOKEN,
    CONF_CONFIGURATION_ID,
    CONF_DEVICES,
    CONF_HOME_ID,
    CONF_HOST,
    CONF_HOST_TOKEN,
    CONF_HOST_UID,
    CONF_MEDIA_TOPOLOGY,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_ROOMS,
    CONF_USERNAME,
    DOMAIN,
    LOGGER,
    SVC_AV_SAVANTMUSIC,
    VERB_POWER_OFF,
    VERB_POWER_ON,
    audio_zone_state_keys,
    build_default_subscribe_keys,
    device_state_keys,
    new_uid,
    room_from_state_key,
    room_state_keys,
)
from .mdns import async_savant_mdns_hosts
from .media_routing import MediaRouteManager, opaque_model_id
from .savant_client import SavantClient


class _ConfiguredMediaEndpoint:
    """A non-entity endpoint retained so exact routing includes unimported zones."""

    entity_id = None

    def __init__(self, hub: SavantHub, device: dict[str, Any]) -> None:
        self.hub = hub
        self.route_id = str(device["id"])
        self._component = str(device.get("component") or "Music")
        self._logical_component = str(device.get("zone") or "")
        self._room = str(device.get("room") or "")
        control = device.get("control")
        self._control = control if isinstance(control, dict) else {}
        self._service_id = str(self._control.get("service_id") or "")
        self._service_type = str(
            self._control.get("service_type") or SVC_AV_SAVANTMUSIC
        )
        self._variant_id = str(self._control.get("variant_id") or "1")
        self._requests = set(self._control.get("requests") or ())
        if self._service_type == SVC_AV_SAVANTMUSIC and not self._requests:
            self._requests = {VERB_POWER_ON, VERB_POWER_OFF}
        self.savant_media_server_id = str(
            self._control.get("media_server_id") or ""
        ) or opaque_model_id("server", self._component, self._service_type)

    def _is_active_service(self) -> bool:
        return self._route_selection_state() is True

    def _route_selection_state(self) -> bool | None:
        services: set[str] = set()
        for attribute in ("ActiveService", "ActiveServices"):
            key = f"{self._room}.{attribute}"
            if key not in self.hub.states:
                return None
            value = self.hub.get(key)
            if isinstance(value, str):
                services.update(part.strip() for part in value.split(",") if part.strip())
        return self._service_id in services if self._service_id else bool(services)

    async def _async_set_route_selected(self, selected: bool) -> None:
        verb = VERB_POWER_ON if selected else VERB_POWER_OFF
        if verb not in self._requests:
            raise RuntimeError(f"Savant endpoint does not support {verb}")
        await self.hub.client.service_request(
            verb,
            component=self._component,
            service_type=self._service_type,
            zone=self._room,
            logical_component=self._logical_component,
            variant_id=self._variant_id,
            include_request_id=True,
        )


class SavantCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-driven coordinator (no polling) — refreshed by the hub on state pushes."""

    def __init__(self, hass: HomeAssistant, hub: SavantHub) -> None:
        super().__init__(hass, LOGGER, name=f"{DOMAIN}_coordinator", update_interval=None)
        self.hub = hub

    async def _async_update_data(self) -> dict[str, Any]:
        return self.hub.snapshot()

    @callback
    def async_set_push_data(self, data: dict[str, Any]) -> None:
        """Publish a host push without the coordinator's noisy manual-update debug log."""
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()


class SavantHub:
    """Owns the client lifecycle and the state store for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.states: dict[str, Any] = {}
        # None until the dashboard subscription delivers its authoritative full list.
        self.scenes: dict[str, dict[str, Any]] | None = None
        self._callbacks: dict[str, list[Callable[[], None]]] = defaultdict(list)
        self._scene_callbacks: list[Callable[[], None]] = []
        self._created: set[str] = set()
        self._flush_scheduled = False
        self._task: asyncio.Task | None = None
        self._media_routes = MediaRouteManager()

        data = dict(entry.data)
        options = dict(entry.options or {})
        self.uid = data.get("uid") or new_uid()
        # The approved device list from the config-flow picker (None for legacy entries
        # that predate the picker — platforms then fall back to dynamic discovery).
        self.devices: list[dict[str, Any]] | None = data.get(CONF_DEVICES)
        self.media_topology_complete = CONF_MEDIA_TOPOLOGY in data
        self._media_topology: list[dict[str, Any]] = data.get(CONF_MEDIA_TOPOLOGY) or []
        # Known rooms: the room each approved device lives in + user-supplied override
        # rooms + rooms discovered at runtime (PROTOCOL.md §6.1).
        self.rooms: set[str] = set(options.get(CONF_ROOMS) or [])
        if self.devices is not None:
            for device in self.devices:
                if device.get("room"):
                    self.rooms.add(device["room"])
        for device in self._media_topology:
            if device.get("room"):
                self.rooms.add(device["room"])
        self._non_room_state_prefixes = {
            f"{device['component']}.{device['zone']}"
            for device in [*(self.devices or []), *self._media_topology]
            if device.get("type") == "media_player"
            and device.get("component")
            and device.get("zone")
        }

        has_archive_inventory = bool(
            (self.devices or self._media_topology)
            and any(
                device.get("state_name") or device.get("component")
                for device in [*(self.devices or []), *self._media_topology]
            )
        )
        subscribe_keys = build_default_subscribe_keys(
            list(self.rooms), include_legacy_defaults=not has_archive_inventory
        )
        if self.devices is not None:
            for device in self.devices:
                subscribe_keys.extend(device_state_keys(device))
                if (
                    device.get("type") == "media_player"
                    and device.get("component")
                    and device.get("zone")
                    and isinstance(device.get("control"), dict)
                    and device["control"].get("service_type", SVC_AV_SAVANTMUSIC)
                    == SVC_AV_SAVANTMUSIC
                ):
                    subscribe_keys.extend(
                        audio_zone_state_keys(
                            device["component"],
                            device["zone"],
                            str(device["control"].get("variant_id") or "1"),
                        )
                    )

        async def _async_mdns_hosts() -> list[str]:
            return await async_savant_mdns_hosts(hass)

        self.client = SavantClient(
            host=data.get(CONF_HOST, ""),
            port=int(data.get(CONF_PORT) or 0),
            host_uid=data.get(CONF_HOST_UID, ""),
            uid=self.uid,
            home_id=data.get(CONF_HOME_ID, ""),
            cloud_token=options.get(CONF_CLOUD_TOKEN, ""),
            configuration_id=options.get(CONF_CONFIGURATION_ID, ""),
            host_token=options.get(CONF_HOST_TOKEN) or None,
            username=options.get(CONF_USERNAME) or data.get(CONF_USERNAME, ""),
            password=options.get(CONF_PASSWORD) or data.get(CONF_PASSWORD, ""),
            subscribe_keys=list(dict.fromkeys(subscribe_keys)),
            mdns_hosts=_async_mdns_hosts,
        )
        self.client.on_state_update = self._on_state_update
        self.client.on_status = self._on_status
        self.client.on_rooms_discovered = self._on_rooms_discovered
        self.client.on_scenes_update = self._on_scenes_update
        self.coordinator = SavantCoordinator(hass, self)
        for device in self._media_topology:
            self._media_routes.register_hidden(_ConfiguredMediaEndpoint(self, device))

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        endpoint = await self.client.async_resolve_endpoint()
        if endpoint is not None and endpoint.uid and not self.entry.data.get(CONF_HOST_UID):
            # Retire legacy endpoint data after discovery confirms the stable host UID.
            data = dict(self.entry.data)
            data[CONF_HOST_UID] = endpoint.uid
            data.pop(CONF_HOST, None)
            data.pop(CONF_PORT, None)
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        self._task = asyncio.create_task(self.client.run_forever())

    async def stop(self) -> None:
        await self.client.stop()
        if self._task is not None:
            self._task.cancel()
            # CancelledError is a BaseException — suppress it explicitly too.
            with suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    # ------------------------------------------------------------- state store

    def snapshot(self) -> dict[str, Any]:
        return dict(self.states)

    def get(self, key: str, default: Any = None) -> Any:
        return self.states.get(key, default)

    @callback
    def _on_state_update(self, state: str, value: Any) -> None:
        self.states[state] = value
        if state.endswith((".ActiveService", ".ActiveServices")):
            self._media_routes.notify_state_changed()
        # Derive new rooms from per-room state keys and subscribe to their other keys
        # (PROTOCOL.md §6.1: rooms are the first segments of per-room keys).
        room = room_from_state_key(state, self._non_room_state_prefixes)
        if room and room not in self.rooms:
            self._on_rooms_discovered({room})
        self._schedule_flush()

    @callback
    def _on_rooms_discovered(self, rooms: set[str]) -> None:
        new_rooms = rooms - self.rooms
        if not new_rooms:
            return
        self.rooms |= new_rooms
        LOGGER.info("Savant discovered %d new room(s)", len(new_rooms))
        # Subscribe to each new room's per-room state keys.
        self.hass.loop.create_task(self.client.register_state_keys(room_state_keys(new_rooms)))
        self._schedule_flush()

    @callback
    def _on_scenes_update(self, scenes: dict[str, dict[str, Any]]) -> None:
        """Replace the scene inventory from the dashboard's full-list push."""
        self.scenes = scenes
        for callback_fn in list(self._scene_callbacks):
            callback_fn()
        self._schedule_flush()

    @callback
    def _on_status(self, connected: bool) -> None:
        LOGGER.info("Savant host %s", "connected" if connected else "disconnected")
        # Availability changes must be re-rendered even if no state changed.
        self._schedule_flush()

    @callback
    def _schedule_flush(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        self.hass.loop.call_soon(self._flush)

    @callback
    def _flush(self) -> None:
        self._flush_scheduled = False
        self.coordinator.async_set_push_data(self.snapshot())
        for callback_list in self._callbacks.values():
            for fn in list(callback_list):
                fn()

    # ------------------------------------------------------------- entity discovery

    def add_platform_callback(self, fn: Callable[[], None]) -> None:
        self._callbacks["_all"].append(fn)

    def add_scene_callback(self, fn: Callable[[], None]) -> None:
        """Register a callback for authoritative dashboard scene-list updates only."""
        self._scene_callbacks.append(fn)

    def is_created(self, unique_id: str) -> bool:
        return unique_id in self._created

    def mark_created(self, unique_ids: list[str]) -> None:
        self._created.update(unique_ids)

    def unmark_created(self, unique_ids: list[str]) -> None:
        self._created.difference_update(unique_ids)

    # ------------------------------------------------------------- media topology

    def register_media_entity(self, entity: Any) -> None:
        """Register one live projected media endpoint by its exact HA entity ID."""
        self._media_routes.register(entity)

    def unregister_media_entity(self, entity: Any) -> None:
        self._media_routes.unregister(entity)

    def media_entity(self, entity_id: str) -> Any | None:
        return self._media_routes.entity(entity_id)

    def media_server_entities(self, media_server_id: str) -> list[Any]:
        return self._media_routes.server_entities(media_server_id)

    def selected_media_zone_entity_ids(self, media_server_id: str) -> list[str]:
        """Derive shared-server membership centrally from authoritative room state."""
        return self._media_routes.selected(media_server_id)

    async def async_set_media_server_zones(
        self, anchor: Any, zone_entity_ids: list[str]
    ) -> dict[str, Any]:
        """Replace one physical media server's complete projected endpoint set."""
        if not self.media_topology_complete:
            raise ValueError(
                "Exact Savant media routing requires reconfiguring this integration "
                "to refresh its complete media topology"
            )
        try:
            return await self._media_routes.replace(anchor, zone_entity_ids)
        finally:
            self.coordinator.async_set_push_data(self.snapshot())

    async def async_join_media_server_zones(
        self, anchor: Any, zone_entity_ids: list[str]
    ) -> None:
        if not self.media_topology_complete:
            raise ValueError("Savant media grouping requires refreshed media topology")
        try:
            await self._media_routes.join(anchor, zone_entity_ids)
        finally:
            self.coordinator.async_set_push_data(self.snapshot())

    async def async_unjoin_media_server_zone(self, anchor: Any) -> None:
        if not self.media_topology_complete:
            raise ValueError("Savant media grouping requires refreshed media topology")
        try:
            await self._media_routes.unjoin(anchor)
        finally:
            self.coordinator.async_set_push_data(self.snapshot())

    async def async_set_media_endpoint_power(self, endpoint: Any, selected: bool) -> None:
        await self._media_routes.command(endpoint, selected)
