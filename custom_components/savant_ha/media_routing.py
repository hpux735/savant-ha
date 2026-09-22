"""Shared-media-server topology and atomic route replacement."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from typing import Any, Protocol


def opaque_model_id(kind: str, *parts: str) -> str:
    """Return a non-identifying stable ID from exact Savant model identifiers."""
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"{kind}:{digest}"


class MediaRouteEndpoint(Protocol):
    """The endpoint surface required by the route manager."""

    entity_id: str | None
    route_id: str
    savant_media_server_id: str

    def _is_active_service(self) -> bool: ...

    def _route_selection_state(self) -> bool | None: ...

    async def _async_set_route_selected(self, selected: bool) -> None: ...


class MediaRouteError(Exception):
    """A shared-media-server route could not reach its requested final state."""

    def __init__(self, message: str, selected: list[str]) -> None:
        super().__init__(message)
        self.selected = selected


class MediaRouteManager:
    """Track projected endpoints and serialize exact route replacement per server."""

    def __init__(self, verify_timeout: float = 5.0) -> None:
        self._routes: dict[str, MediaRouteEndpoint] = {}
        self._fallbacks: dict[str, MediaRouteEndpoint] = {}
        self._entities: dict[str, MediaRouteEndpoint] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._changed = asyncio.Event()
        self._verify_timeout = verify_timeout

    def register_hidden(self, endpoint: MediaRouteEndpoint) -> None:
        self._fallbacks[endpoint.route_id] = endpoint
        self._routes.setdefault(endpoint.route_id, endpoint)

    def register(self, entity: MediaRouteEndpoint) -> None:
        if entity.entity_id is None:
            raise ValueError("Projected media endpoints require an entity ID")
        self._entities[entity.entity_id] = entity
        self._routes[entity.route_id] = entity

    def unregister(self, entity: MediaRouteEndpoint) -> None:
        if entity.entity_id:
            self._entities.pop(entity.entity_id, None)
        fallback = self._fallbacks.get(entity.route_id)
        if fallback is not None:
            self._routes[entity.route_id] = fallback
        else:
            self._routes.pop(entity.route_id, None)

    def entity(self, entity_id: str) -> MediaRouteEndpoint | None:
        return self._entities.get(entity_id)

    def server_entities(self, media_server_id: str) -> list[MediaRouteEndpoint]:
        return [
            entity
            for entity in self._entities.values()
            if entity.savant_media_server_id == media_server_id
        ]

    def _server_routes(self, media_server_id: str) -> list[MediaRouteEndpoint]:
        return [
            endpoint
            for endpoint in self._routes.values()
            if endpoint.savant_media_server_id == media_server_id
        ]

    def selected(self, media_server_id: str) -> list[str]:
        """Return selected projected HA entity IDs for state and responses."""
        return sorted(
            entity.entity_id
            for entity in self.server_entities(media_server_id)
            if entity.entity_id is not None and entity._is_active_service()
        )

    def _selected_routes(self, media_server_id: str) -> set[str]:
        return self._route_snapshot(media_server_id)[0]

    def _route_snapshot(self, media_server_id: str) -> tuple[set[str], set[str]]:
        selected: set[str] = set()
        unknown: set[str] = set()
        for endpoint in self._server_routes(media_server_id):
            state = endpoint._route_selection_state()
            if state is None:
                unknown.add(endpoint.route_id)
            elif state:
                selected.add(endpoint.route_id)
        return selected, unknown

    def notify_state_changed(self) -> None:
        self._changed.set()

    async def join(
        self, anchor: MediaRouteEndpoint, group_members: list[str]
    ) -> dict[str, Any]:
        server_id = anchor.savant_media_server_id
        async with self._locks[server_id]:
            await self._wait_until_known(server_id)
            desired = set(self.selected(server_id))
            if anchor.entity_id is not None:
                desired.add(anchor.entity_id)
            desired.update(group_members)
            return await self._replace_locked(
                anchor,
                sorted(desired),
                extra_desired_routes=self._selected_routes(server_id),
            )

    async def unjoin(self, anchor: MediaRouteEndpoint) -> dict[str, Any]:
        server_id = anchor.savant_media_server_id
        async with self._locks[server_id]:
            await self._wait_until_known(server_id)
            desired = set(self.selected(server_id))
            desired.discard(anchor.entity_id)
            projected_routes = {
                entity.route_id for entity in self.server_entities(server_id)
            }
            hidden_routes = self._selected_routes(server_id) - projected_routes
            return await self._replace_locked(
                anchor, sorted(desired), extra_desired_routes=hidden_routes
            )

    async def command(self, endpoint: MediaRouteEndpoint, selected: bool) -> None:
        """Serialize ordinary endpoint power changes with route replacements."""
        async with self._locks[endpoint.savant_media_server_id]:
            if endpoint._is_active_service() == selected:
                return
            await endpoint._async_set_route_selected(selected)
            await self._wait_for_endpoint(endpoint, selected)

    async def replace(
        self, anchor: MediaRouteEndpoint, zone_entity_ids: list[str]
    ) -> dict[str, Any]:
        async with self._locks[anchor.savant_media_server_id]:
            return await self._replace_locked(anchor, zone_entity_ids)

    async def _replace_locked(
        self,
        anchor: MediaRouteEndpoint,
        zone_entity_ids: list[str],
        *,
        extra_desired_routes: set[str] | None = None,
    ) -> dict[str, Any]:
        server_id = anchor.savant_media_server_id
        desired_ids = sorted(set(zone_entity_ids))
        desired_entities: dict[str, MediaRouteEndpoint] = {}
        for entity_id in desired_ids:
            entity = self.entity(entity_id)
            if entity is None:
                raise ValueError(f"Unknown Savant media-player entity: {entity_id}")
            if entity.savant_media_server_id != server_id:
                raise ValueError(
                    f"{entity_id} belongs to media server "
                    f"{entity.savant_media_server_id}, not {server_id}"
                )
            desired_entities[entity_id] = entity

        await self._wait_until_known(server_id)

        before_entities = set(self.selected(server_id))
        desired_routes = {
            entity.route_id for entity in desired_entities.values()
        } | (extra_desired_routes or set())
        before_routes = self._selected_routes(server_id)
        removal_routes = sorted(before_routes - desired_routes)
        addition_routes = sorted(desired_routes - before_routes)
        self._changed.clear()
        try:
            for route_id in removal_routes:
                await self._routes[route_id]._async_set_route_selected(False)
            for route_id in addition_routes:
                await self._routes[route_id]._async_set_route_selected(True)
            await self._verify(server_id, desired_routes)
        except Exception as err:
            self._changed.clear()
            try:
                await asyncio.wait_for(
                    self._changed.wait(), self._verify_timeout
                )
            except TimeoutError:
                pass
            selected = self.selected(server_id)
            raise MediaRouteError(
                f"Savant media routing failed; currently selected: {selected}", selected
            ) from err

        selected = self.selected(server_id)
        return {
            "media_server_id": server_id,
            "requested_zone_entity_ids": desired_ids,
            "selected_zone_entity_ids": selected,
            "deselected_zone_entity_ids": sorted(before_entities - set(selected)),
            "unchanged_zone_entity_ids": sorted(before_entities & set(desired_ids)),
            "verified": True,
        }

    async def _verify(self, media_server_id: str, desired_routes: set[str]) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout
        while self._route_snapshot(media_server_id) != (desired_routes, set()):
            self._changed.clear()
            if self._route_snapshot(media_server_id) == (desired_routes, set()):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Savant did not confirm the requested media route")
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError as err:
                raise TimeoutError(
                    "Savant did not confirm the requested media route"
                ) from err

    async def _wait_until_known(self, media_server_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout
        while self._route_snapshot(media_server_id)[1]:
            self._changed.clear()
            if not self._route_snapshot(media_server_id)[1]:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MediaRouteError(
                    "Savant media routing state is incomplete; wait for host state",
                    self.selected(media_server_id),
                )
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError as err:
                raise MediaRouteError(
                    "Savant media routing state is incomplete; wait for host state",
                    self.selected(media_server_id),
                ) from err

    async def _wait_for_endpoint(
        self, endpoint: MediaRouteEndpoint, selected: bool
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout
        while endpoint._is_active_service() != selected:
            self._changed.clear()
            if endpoint._is_active_service() == selected:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MediaRouteError(
                    f"Savant did not confirm endpoint {endpoint.entity_id} power state",
                    self.selected(endpoint.savant_media_server_id),
                )
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError as err:
                raise MediaRouteError(
                    f"Savant did not confirm endpoint {endpoint.entity_id} power state",
                    self.selected(endpoint.savant_media_server_id),
                ) from err
