"""Tests for shared physical media-server route replacement."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.savant_ha.media_routing import MediaRouteError, MediaRouteManager


class FakeEndpoint:
    def __init__(
        self,
        manager: MediaRouteManager,
        entity_id: str,
        server_id: str,
        *,
        active: bool = False,
        fail: bool = False,
        delay: float = 0,
        known: bool = True,
        calls: list[tuple[str, bool]] | None = None,
        concurrency: dict[str, int] | None = None,
    ) -> None:
        self.manager = manager
        self.entity_id = entity_id
        self.route_id = entity_id
        self.savant_media_server_id = server_id
        self.active = active
        self.fail = fail
        self.delay = delay
        self.known = known
        self.calls = calls if calls is not None else []
        self.concurrency = concurrency

    def _is_active_service(self) -> bool:
        return self.active

    def _route_selection_state(self) -> bool | None:
        return self.active if self.known else None

    async def _async_set_route_selected(self, selected: bool) -> None:
        self.calls.append((self.entity_id, selected))
        if self.concurrency is not None:
            self.concurrency["current"] += 1
            self.concurrency["maximum"] = max(
                self.concurrency["maximum"], self.concurrency["current"]
            )
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("backend failure")
            self.active = selected
            self.known = True
            self.manager.notify_state_changed()
        finally:
            if self.concurrency is not None:
                self.concurrency["current"] -= 1


def _endpoint(
    manager: MediaRouteManager,
    entity_id: str,
    server_id: str = "server-1",
    **kwargs: object,
) -> FakeEndpoint:
    endpoint = FakeEndpoint(manager, entity_id, server_id, **kwargs)
    manager.register(endpoint)
    return endpoint


def test_exact_replacement_removes_adds_deduplicates_and_reports_response():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.1)
        calls: list[tuple[str, bool]] = []
        anchor = _endpoint(manager, "media_player.bath", active=True, calls=calls)
        _endpoint(manager, "media_player.dining", active=True, calls=calls)
        _endpoint(manager, "media_player.living", calls=calls)

        result = await manager.replace(
            anchor,
            ["media_player.living", "media_player.bath", "media_player.living"],
        )

        assert calls == [
            ("media_player.dining", False),
            ("media_player.living", True),
        ]
        assert result == {
            "media_server_id": "server-1",
            "requested_zone_entity_ids": ["media_player.bath", "media_player.living"],
            "selected_zone_entity_ids": ["media_player.bath", "media_player.living"],
            "deselected_zone_entity_ids": ["media_player.dining"],
            "unchanged_zone_entity_ids": ["media_player.bath"],
            "verified": True,
        }

        calls.clear()
        second = await manager.replace(
            anchor, ["media_player.bath", "media_player.living"]
        )
        assert calls == []
        assert second["verified"] is True

    asyncio.run(run())


def test_empty_selection_and_external_state_changes():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.1)
        anchor = _endpoint(manager, "media_player.bath", active=True)
        dining = _endpoint(manager, "media_player.dining", active=True)
        result = await manager.replace(anchor, [])
        assert result["selected_zone_entity_ids"] == []
        dining.active = True
        manager.notify_state_changed()
        assert manager.selected("server-1") == ["media_player.dining"]

    asyncio.run(run())


def test_exact_replacement_removes_selected_unprojected_endpoint():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.1)
        calls: list[tuple[str, bool]] = []
        anchor = _endpoint(manager, "media_player.bath", active=True, calls=calls)
        hidden = FakeEndpoint(
            manager,
            "hidden:dining",
            "server-1",
            active=True,
            calls=calls,
        )
        hidden.route_id = "route:dining"
        hidden.entity_id = None
        manager.register_hidden(hidden)

        result = await manager.replace(anchor, ["media_player.bath"])
        assert calls == [(None, False)]
        assert hidden.active is False
        assert result["selected_zone_entity_ids"] == ["media_player.bath"]

    asyncio.run(run())


def test_unknown_route_state_cannot_be_verified_as_inactive():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.01)
        anchor = _endpoint(manager, "media_player.bath", known=False)
        with pytest.raises(MediaRouteError, match="state is incomplete"):
            await manager.replace(anchor, [])

    asyncio.run(run())


def test_unknown_and_cross_server_entities_are_rejected():
    async def run() -> None:
        manager = MediaRouteManager()
        anchor = _endpoint(manager, "media_player.bath")
        _endpoint(manager, "media_player.soundbar", "server-2")
        with pytest.raises(ValueError, match="Unknown"):
            await manager.replace(anchor, ["media_player.missing"])
        with pytest.raises(ValueError, match="belongs to media server server-2"):
            await manager.replace(anchor, ["media_player.soundbar"])

    asyncio.run(run())


def test_join_and_unjoin_use_the_same_exact_route_path():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.1)
        anchor = _endpoint(manager, "media_player.bath")
        _endpoint(manager, "media_player.dining", active=True)
        _endpoint(manager, "media_player.living")
        _endpoint(manager, "media_player.soundbar", "server-2")

        joined = await manager.join(anchor, ["media_player.living"])
        assert joined["selected_zone_entity_ids"] == [
            "media_player.bath",
            "media_player.dining",
            "media_player.living",
        ]
        unjoined = await manager.unjoin(anchor)
        assert unjoined["selected_zone_entity_ids"] == [
            "media_player.dining",
            "media_player.living",
        ]
        with pytest.raises(ValueError, match="server-2"):
            await manager.join(anchor, ["media_player.soundbar"])

    asyncio.run(run())


def test_join_and_unjoin_preserve_active_unprojected_endpoints():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.1)
        anchor = _endpoint(manager, "media_player.bath", active=True)
        _endpoint(manager, "media_player.dining")
        hidden = FakeEndpoint(manager, "hidden", "server-1", active=True)
        hidden.entity_id = None
        hidden.route_id = "route:hidden"
        manager.register_hidden(hidden)

        await manager.join(anchor, ["media_player.dining"])
        assert hidden.active is True
        await manager.unjoin(anchor)
        assert hidden.active is True
        assert manager.selected("server-1") == ["media_player.dining"]

    asyncio.run(run())


def test_concurrent_requests_for_one_server_are_serialized():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.2)
        concurrency = {"current": 0, "maximum": 0}
        anchor = _endpoint(
            manager,
            "media_player.bath",
            delay=0.02,
            concurrency=concurrency,
        )
        _endpoint(
            manager,
            "media_player.dining",
            delay=0.02,
            concurrency=concurrency,
        )
        await asyncio.gather(
            manager.replace(anchor, ["media_player.bath"]),
            manager.replace(anchor, ["media_player.dining"]),
        )
        assert concurrency["maximum"] == 1
        assert manager.selected("server-1") == ["media_player.dining"]

    asyncio.run(run())


def test_concurrent_joins_compute_membership_inside_the_server_lock():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.2)
        anchor = _endpoint(manager, "media_player.bath", active=True, delay=0.01)
        _endpoint(manager, "media_player.dining", delay=0.01)
        _endpoint(manager, "media_player.living", delay=0.01)
        await asyncio.gather(
            manager.join(anchor, ["media_player.dining"]),
            manager.join(anchor, ["media_player.living"]),
        )
        assert manager.selected("server-1") == [
            "media_player.bath",
            "media_player.dining",
            "media_player.living",
        ]

    asyncio.run(run())


def test_power_command_holds_lock_until_delayed_state_confirmation():
    class DelayedEndpoint(FakeEndpoint):
        async def _async_set_route_selected(self, selected: bool) -> None:
            self.calls.append((self.entity_id, selected))

            async def apply() -> None:
                await asyncio.sleep(0.02)
                self.active = selected
                self.manager.notify_state_changed()

            asyncio.create_task(apply())

    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.2)
        anchor = _endpoint(manager, "media_player.bath", active=True)
        delayed = DelayedEndpoint(manager, "media_player.dining", "server-1")
        manager.register(delayed)

        power = asyncio.create_task(manager.command(delayed, True))
        await asyncio.sleep(0)
        replacement = asyncio.create_task(manager.replace(anchor, [anchor.entity_id]))
        await asyncio.gather(power, replacement)
        assert manager.selected("server-1") == ["media_player.bath"]

    asyncio.run(run())


def test_partial_backend_failure_reports_refreshed_actual_state():
    async def run() -> None:
        manager = MediaRouteManager(verify_timeout=0.05)
        calls: list[tuple[str, bool]] = []
        anchor = _endpoint(manager, "media_player.bath", active=True, calls=calls)
        _endpoint(manager, "media_player.dining", active=True, calls=calls)
        _endpoint(manager, "media_player.living", fail=True, calls=calls)
        with pytest.raises(MediaRouteError) as caught:
            await manager.replace(anchor, ["media_player.bath", "media_player.living"])
        assert calls == [
            ("media_player.dining", False),
            ("media_player.living", True),
        ]
        assert caught.value.selected == ["media_player.bath"]
        assert "media_player.bath" in str(caught.value)

    asyncio.run(run())
