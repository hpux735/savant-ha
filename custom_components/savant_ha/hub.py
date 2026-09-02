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
    CONF_PASSWORD,
    CONF_PORT,
    CONF_ROOMS,
    CONF_USERNAME,
    DOMAIN,
    LOGGER,
    SVC_AV_SAVANTMUSIC,
    audio_zone_state_keys,
    build_default_subscribe_keys,
    device_state_keys,
    new_uid,
    room_from_state_key,
    room_state_keys,
)
from .savant_client import SavantClient


class SavantCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-driven coordinator (no polling) — refreshed by the hub on state pushes."""

    def __init__(self, hass: HomeAssistant, hub: SavantHub) -> None:
        super().__init__(hass, LOGGER, name=f"{DOMAIN}_coordinator", update_interval=None)
        self.hub = hub

    async def _async_update_data(self) -> dict[str, Any]:
        return self.hub.snapshot()


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

        data = dict(entry.data)
        options = dict(entry.options or {})
        self.uid = data.get("uid") or new_uid()
        # The approved device list from the config-flow picker (None for legacy entries
        # that predate the picker — platforms then fall back to dynamic discovery).
        self.devices: list[dict[str, Any]] | None = data.get(CONF_DEVICES)
        # Known rooms: the room each approved device lives in + user-supplied override
        # rooms + rooms discovered at runtime (PROTOCOL.md §6.1).
        self.rooms: set[str] = set(options.get(CONF_ROOMS) or [])
        if self.devices is not None:
            for device in self.devices:
                if device.get("room"):
                    self.rooms.add(device["room"])

        subscribe_keys = build_default_subscribe_keys(list(self.rooms))
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
                        audio_zone_state_keys(device["component"], device["zone"])
                    )

        self.client = SavantClient(
            host=data[CONF_HOST],
            port=int(data.get(CONF_PORT) or 0),
            uid=self.uid,
            home_id=data.get(CONF_HOME_ID, ""),
            cloud_token=options.get(CONF_CLOUD_TOKEN, ""),
            configuration_id=options.get(CONF_CONFIGURATION_ID, ""),
            host_token=options.get(CONF_HOST_TOKEN) or None,
            username=options.get(CONF_USERNAME) or data.get(CONF_USERNAME, ""),
            password=options.get(CONF_PASSWORD) or data.get(CONF_PASSWORD, ""),
            subscribe_keys=list(dict.fromkeys(subscribe_keys)),
        )
        self.client.on_state_update = self._on_state_update
        self.client.on_status = self._on_status
        self.client.on_rooms_discovered = self._on_rooms_discovered
        self.client.on_scenes_update = self._on_scenes_update
        self.coordinator = SavantCoordinator(hass, self)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
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
        # Derive new rooms from per-room state keys and subscribe to their other keys
        # (PROTOCOL.md §6.1: rooms are the first segments of per-room keys).
        room = room_from_state_key(state)
        if room and room not in self.rooms:
            self._on_rooms_discovered({room})
        self._schedule_flush()

    @callback
    def _on_rooms_discovered(self, rooms: set[str]) -> None:
        new_rooms = rooms - self.rooms
        if not new_rooms:
            return
        self.rooms |= new_rooms
        LOGGER.info("Savant discovered %d new room(s): %s", len(new_rooms), sorted(new_rooms))
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
        self.coordinator.async_set_updated_data(self.snapshot())
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
