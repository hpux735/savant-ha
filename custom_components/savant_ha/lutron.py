"""Conservative import suggestions, not proof of shared Lutron hardware identity.

No mapping between Savant addresses and Caseta serial numbers is established. Match
only one-to-one light names within the same named area and let the installer override.
"""

from __future__ import annotations

from typing import Any


def find_duplicate_lutron_lights(
    devices: list[dict[str, Any]], lights: list[dict[str, Any]]
) -> dict[str, str]:
    """Suggest Savant device IDs mapped to matching direct Lutron entity IDs."""
    # ASSUMPTION: matching names and areas suggest duplicates, not hardware identity.
    candidates: dict[str, set[str]] = {}
    for device in devices:
        if device.get("type") != "light":
            continue
        area = " ".join(str(device.get("area") or "").split()).casefold()
        names = _names_in_area([str(device.get("name") or "")], area)
        if not area or not names:
            continue
        matches = {
            light["entity_id"]
            for light in lights
            if area == " ".join(light["area"].split()).casefold()
            and names & _names_in_area(light["names"], area)
        }
        if matches:
            candidates[device["id"]] = matches
    # A match in either direction must be unique, including across multiple bridges.
    return {
        device_id: next(iter(matches))
        for device_id, matches in candidates.items()
        if len(matches) == 1
        and sum(bool(matches & other) for other in candidates.values()) == 1
    }


def _names_in_area(names: list[str], area: str) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        name = " ".join(name.split()).casefold()
        if name:
            normalized.add(name)
            # HA's Caseta integration prefixes the original light name with its area.
            if area and name.startswith(f"{area} "):
                normalized.add(name[len(area) + 1:])
    return normalized
