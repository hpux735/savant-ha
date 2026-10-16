"""Conservative import suggestions, not proof of shared Lutron hardware identity.

No mapping between Savant addresses and Caseta serial numbers is established. Match
only one-to-one names within the same domain and area and let the installer override.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .const import LOGGER


def find_duplicate_lutron_devices(
    devices: list[dict[str, Any]], entities: list[dict[str, Any]]
) -> dict[str, str]:
    """Suggest Savant device IDs mapped to matching direct Lutron entity IDs."""
    # ASSUMPTION: matching names and areas suggest duplicates, not hardware identity.
    duplicates: dict[str, str] = {}
    for domain in ("light", "cover"):
        candidates: dict[str, set[str]] = {}
        counts: Counter[str] = Counter()
        lutron = [entity for entity in entities if entity["entity_id"].partition(".")[0] == domain]
        lutron_areas = {" ".join(entity["area"].split()).casefold() for entity in lutron}
        for device in devices:
            if device.get("type") != domain:
                continue
            counts["devices"] += 1
            area = " ".join(str(device.get("area") or "").split()).casefold()
            names = _names_in_area([str(device.get("name") or "")], area)
            if not area:
                counts["missing_area"] += 1
                continue
            if not names:
                counts["missing_name"] += 1
                continue
            if area not in lutron_areas:
                counts["no_matching_area"] += 1
                continue
            matches = {
                entity["entity_id"]
                for entity in lutron
                if area == " ".join(entity["area"].split()).casefold()
                and names & _names_in_area(entity["names"], area)
            }
            if matches:
                candidates[device["id"]] = matches
            else:
                counts["no_matching_name"] += 1
        # A match in either direction must be unique, including across multiple bridges.
        domain_duplicates = {
            device_id: next(iter(matches))
            for device_id, matches in candidates.items()
            if len(matches) == 1
            and sum(bool(matches & other) for other in candidates.values()) == 1
        }
        duplicates.update(domain_duplicates)
        LOGGER.debug(
            "Savant Lutron matching summary domain=%s savant_%ss=%d lutron_%ss=%d missing_area=%d "
            "missing_name=%d no_matching_area=%d no_matching_name=%d ambiguous=%d suggested=%d",
            domain, domain, counts["devices"], domain, len(lutron),
            counts["missing_area"], counts["missing_name"],
            counts["no_matching_area"], counts["no_matching_name"],
            len(candidates) - len(domain_duplicates), len(domain_duplicates),
        )
    return duplicates


def _names_in_area(names: list[str], area: str) -> set[str]:
    normalized: set[str] = set()
    for name in names:
        name = " ".join(name.split()).casefold()
        if name:
            normalized.add(name)
            # HA's Caseta integration prefixes the original entity name with its area.
            if area and name.startswith(f"{area} "):
                normalized.add(name[len(area) + 1:])
    return normalized
