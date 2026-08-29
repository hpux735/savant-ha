"""Base entity for all Savant platforms."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .hub import SavantCoordinator, SavantHub


class SavantEntity(CoordinatorEntity[SavantCoordinator], Entity):
    """A coordinator-driven entity that reads from the hub's flat state store.

    Each entity belongs to a *sub-device* (a room, a thermostat, or an audio zone) so
    the user-approved area can be applied per device via ``suggested_area``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        hub: SavantHub,
        *,
        device_key: str = "",
        device_name: str = "",
        area: str = "",
    ) -> None:
        super().__init__(hub.coordinator)
        self.hub = hub
        self._device_key = device_key
        self._device_name = device_name
        self._area = area

    @property
    def available(self) -> bool:
        # Reflect the live connection, not just the last coordinator update.
        return self.hub.client.connected

    @property
    def device_info(self) -> DeviceInfo:
        identifiers = {(DOMAIN, self.hub.entry.entry_id)}
        name = self.hub.entry.title
        if self._device_key:
            identifiers = {(DOMAIN, self.hub.entry.entry_id, self._device_key)}
            name = self._device_name or name
        info = DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="Savant",
        )
        if self._area:
            info["suggested_area"] = self._area
        return info

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
