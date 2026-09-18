"""CoolMasterNet coexistence helpers.

CoolMaster state is identified by the observed two-address HVAC suffix documented in
``PROTOCOL.md`` §5.1.  The built-in Home Assistant integration owns direct CoolMaster
control, so only exact name matches are removed from the Savant inventory.
"""

from __future__ import annotations

from typing import Any

_TEMPERATURE_STATE = "ThermostatCurrentTemperature"


def is_coolmaster_climate(device: dict[str, Any]) -> bool:
    """Return whether a Savant climate device has the observed CoolMaster key shape."""
    if device.get("type") != "climate":
        return False
    state_name = str(device.get("state_name") or "")
    if _TEMPERATURE_STATE not in state_name:
        return False
    _, suffix = state_name.split(_TEMPERATURE_STATE, 1)
    return suffix.count("_") >= 2


def exclude_duplicate_coolmaster_devices(
    devices: list[dict[str, Any]], coolmaster_names: set[str]
) -> list[dict[str, Any]]:
    """Keep Savant devices except direct CoolMasterNet climate-name duplicates."""
    names = {_normalize_name(name) for name in coolmaster_names}
    return [
        device
        for device in devices
        if not (
            is_coolmaster_climate(device)
            and _normalize_name(str(device.get("name") or "")) in names
        )
    ]


def _normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold()
