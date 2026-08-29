"""Hub + coordinator: the per-config-entry object graph.

The hub owns a :class:`~.savant_client.SavantClient`, buffers the flat state store
(``states``), and bridges protocol pushes to a :class:`DataUpdateCoordinator` that the
platform entities subscribe to.  Platforms register callbacks here so they can
materialize new entities the first time a matching state key appears.
"""

from __future__ import annotations

import asyncio
import uuid
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
    CONF_HOME_ID,
    CONF_HOST,
    CONF_HOST_TOKEN,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_ROOMS,
    CONF_USERNAME,
    DOMAIN,
    LOGGER,
    build_default_subscribe_keys,
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
        self._callbacks: dict[str, list[Callable[[], None]]] = defaultdict(list)
        self._created: set[str] = set()
        self._flush_scheduled = False
        self._task: asyncio.Task | None = None

        data = dict(entry.data)
        options = dict(entry.options or {})
        self.uid = data.get("uid") or uuid.uuid4().hex

        self.client = SavantClient(
            host=data[CONF_HOST],
            port=int(data.get(CONF_PORT) or 0),
            uid=self.uid,
            home_id=data.get(CONF_HOME_ID, ""),
            cloud_token=options.get(CONF_CLOUD_TOKEN, ""),
            configuration_id=options.get(CONF_CONFIGURATION_ID, ""),
            host_token=options.get(CONF_HOST_TOKEN) or None,
            username=options.get(CONF_USERNAME, ""),
            password=options.get(CONF_PASSWORD, ""),
            subscribe_keys=build_default_subscribe_keys(options.get(CONF_ROOMS) or []),
        )
        self.client.on_state_update = self._on_state_update
        self.client.on_status = self._on_status
        self.coordinator = SavantCoordinator(hass, self)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._task = asyncio.create_task(self.client.run_forever())

    async def stop(self) -> None:
        await self.client.stop()
        if self._task is not None:
            self._task.cancel()
            with suppress(Exception):
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
        self._schedule_flush()

    @callback
    def _on_status(self, connected: bool) -> None:  # noqa: ARG002 - signature kept for clarity
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

    def is_created(self, unique_id: str) -> bool:
        return unique_id in self._created

    def mark_created(self, unique_ids: list[str]) -> None:
        self._created.update(unique_ids)
