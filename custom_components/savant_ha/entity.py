"""Base entity for all Savant platforms."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .hub import SavantCoordinator, SavantHub


class SavantEntity(CoordinatorEntity[SavantCoordinator], Entity):
    """A coordinator-driven entity that reads from the hub's flat state store."""

    _attr_has_entity_name = True

    def __init__(self, hub: SavantHub) -> None:
        super().__init__(hub.coordinator)
        self.hub = hub

    @property
    def available(self) -> bool:
        # Reflect the live connection, not just the last coordinator update.
        return self.hub.client.connected

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.hub.entry.entry_id)},
            name=self.hub.entry.title,
            manufacturer="Savant",
        )

    def _state(self, key: str, default: Any = None) -> Any:
        return self.hub.get(key, default)

    async def _service_request(
        self,
        request: str,
        *,
        component: str,
        service_type: str,
        zone: str = "",
        logical_component: str = "",
        variant_id: str = "",
        request_args: dict[str, Any] | None = None,
    ) -> None:
        await self.hub.client.service_request(
            request,
            component=component,
            service_type=service_type,
            zone=zone,
            logical_component=logical_component,
            variant_id=variant_id,
            request_args=request_args,
        )

    async def _service_request(
        self,
        request: str,
        *,
        component: str,
        service_type: str,
        zone: str = "",
        logical_component: str = "",
        variant_id: str = "",
        request_args: dict[str, Any] | None = None,
    ) -> None:
        await self.hub.client.service_request(
            request,
            component=component,
            service_type=service_type,
            zone=zone,
            logical_component=logical_component,
            variant_id=variant_id,
            request_args=request_args,
        )
