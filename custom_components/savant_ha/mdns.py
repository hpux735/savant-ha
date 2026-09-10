"""mDNS helpers used to locate a Savant host for directed UDP discovery."""

from __future__ import annotations

import asyncio

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser

# PROTOCOL.md §1.1: Savant hosts advertise this service; UDP remains authoritative.
_SAVANT_MDNS_TYPE = "_soapi_sdo._tcp.local."


async def async_savant_mdns_hosts(hass: HomeAssistant) -> list[str]:
    """Return mDNS host addresses for directed UDP control discovery."""
    service_names: set[str] = set()

    def _service_changed(
        _zeroconf: object,
        _service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            service_names.add(name)

    try:
        aiozc = await zeroconf.async_get_async_instance(hass)
        browser = AsyncServiceBrowser(
            aiozc.zeroconf, _SAVANT_MDNS_TYPE, handlers=[_service_changed]
        )
        try:
            await asyncio.sleep(1)
            infos = await asyncio.gather(
                *(
                    aiozc.async_get_service_info(_SAVANT_MDNS_TYPE, name, timeout=1000)
                    for name in service_names
                )
            )
        finally:
            await browser.async_cancel()
    except Exception:  # noqa: BLE001 - mDNS is only a UDP-discovery fallback
        return []
    return list(
        dict.fromkeys(address for info in infos if info for address in info.parsed_addresses())
    )
