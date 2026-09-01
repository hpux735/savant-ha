"""Pure helpers for building archive-derived ``service/request`` payloads.

The HA platform modules (``light``, ``climate``, ``cover``, ``media_player``) import
``homeassistant``, which is not installed in the lightweight test venv.  This module
holds the payload-construction logic — the core of the reverse-engineered, live-verified
control surface — so it can be unit-tested without HA (see ``tests/test_control.py``).
"""

from __future__ import annotations

import re
from typing import Any

from .const import HVAC_STATE_PREFIX, SVC_AV_SAVANTMUSIC, SVC_ENV_HVAC, VERB_DIMMER_SET

# ``stateName`` for a thermostat names only the current-temperature key; the unit suffix
# (``_1``, ``_2``, …) and the component/logical prefix are derived around this marker.
TEMP_MARKER = "ThermostatCurrentTemperature"


def split_addresses(addresses: str) -> list[str]:
    """Split a comma-separated ``addresses`` field, stripping whitespace."""
    return [part.strip() for part in addresses.split(",")] if addresses else []


def light_address_args(addresses: str) -> dict[str, str]:
    """Build ``Address1..6`` with ``(null)`` padding (PROTOCOL.md §7.3 DimmerSet)."""
    parts = split_addresses(addresses)
    args: dict[str, str] = {}
    for index, value in enumerate(parts[:6], start=1):
        args[f"Address{index}"] = value
    for index in range(len(parts) + 1, 7):
        args[f"Address{index}"] = "(null)"
    args.setdefault("Address1", "(null)")
    return args


def is_switch(device: dict[str, Any]) -> bool:
    """True when a light load is a switch (``entityType == "Switch"``), not a dimmer."""
    control = device.get("control")
    return bool(isinstance(control, dict) and control.get("entity_type") == "Switch")


def is_color_light(device: dict[str, Any]) -> bool:
    """True when the archive state key carries RGBW color state."""
    state_name = str(device.get("state_name") or "")
    return "CurrentColor" in state_name or "CurrentBleColor" in state_name


def dimmer_command(device: dict[str, Any]) -> str:
    """The load's dimmer verb from the archive, defaulting to ``DimmerSet``."""
    control = device.get("control")
    command = control.get("dimmer_command") if isinstance(control, dict) else ""
    return command or VERB_DIMMER_SET


def dimmer_args(
    device: dict[str, Any],
    level: int,
    rgbw_color: tuple[int, int, int, int] | None = None,
    use_last_dimmer_value: bool = False,
) -> dict[str, Any]:
    """Build the full ``DimmerSet`` ``requestArgs``.

    Standard dimmers retain the archive-derived flat color fields. Color-capable loads
    use the observed nested ``bleColor`` map, allowing an RGBW update without erasing
    the existing color when Home Assistant only changes brightness (PROTOCOL.md §6).
    """
    control = device.get("control") if isinstance(device.get("control"), dict) else {}
    args: dict[str, Any] = light_address_args(str(device.get("addresses") or ""))
    args["DimmerLevel"] = level
    args["useLastDimmerValue"] = use_last_dimmer_value
    fade_time = control.get("fade_time")
    delay_time = control.get("delay_time")
    args["FadeTime"] = fade_time if fade_time is not None else "0.5"
    args["DelayTime"] = delay_time if delay_time is not None else "0"
    args["Curve"] = control.get("technology") or "Custom 1"
    if is_color_light(device):
        if rgbw_color is not None:
            red, green, blue, white = (max(0, min(255, int(value))) for value in rgbw_color)
            args["bleColor"] = {
                "red": red,
                "green": green,
                "blue": blue,
                "white": white,
                "kelvin": 0,
            }
        return args
    args.update(
        {
            "bleColorRed": 0,
            "bleColorGreen": 0,
            "bleColorBlue": 0,
            "bleColorWhite": 0,
            "kelvin": 0,
        }
    )
    return args


def state_name_component(state_name: str) -> str:
    """The component name from a dotted ``stateName`` (``<component>.<logical>...``)."""
    return state_name.split(".", 1)[0] if "." in state_name else ""


def state_name_logical(state_name: str) -> str:
    """The logical-component name from a dotted ``stateName``."""
    parts = state_name.split(".")
    return parts[1] if len(parts) > 1 else ""


def suffix_address(suffix: str) -> str:
    """``_1`` -> ``"1"`` (PROTOCOL.md §7.4: ThermostatAddress is a string index)."""
    return suffix.lstrip("_")


def climate_identity(state_name: str, suffix: str) -> tuple[str, str, str, str]:
    """Derive a thermostat's ``(state_prefix, unit_suffix, component, logical_component)``.

    The component/logical names come from ``stateName`` (e.g.
    ``CLIW220.HVAC_controller.…`` / ``New Thermostat.HVAC_controller.…``), not from a
    fixed constant; when ``stateName`` is absent it falls back to the observed default
    prefix and the caller-supplied suffix.
    """
    if TEMP_MARKER in state_name:
        state_prefix, unit_suffix = state_name.split(TEMP_MARKER, 1)
    else:
        state_prefix, unit_suffix = HVAC_STATE_PREFIX, suffix
    parts = state_prefix.rstrip(".").split(".")
    component = parts[0] if len(parts) >= 2 else "HVAC Controller"
    logical_component = parts[1] if len(parts) >= 2 else "HVAC_controller"
    return state_prefix, unit_suffix, component, logical_component


def climate_scope(component: str, logical_component: str) -> dict[str, str]:
    """The HVAC ``service/request`` scope (archive-derived, empty zone)."""
    return {
        "component": component,
        "service_type": SVC_ENV_HVAC,
        "zone": "",
        "logical_component": logical_component,
        "variant_id": "1",
    }


def thermostat_args(addresses: str, suffix: str) -> dict[str, str]:
    """Build ``ThermostatAddress``/``ThermostatAddress2`` from the archive record.

    Both keys are required by the archive's HVAC request definitions; a single-point
    thermostat reports ``ThermostatAddress2: "(null)"``.
    """
    parts = split_addresses(addresses)
    address = parts[0] if parts and parts[0] else suffix_address(suffix)
    address_2 = parts[1] if len(parts) > 1 and parts[1] else "(null)"
    return {"ThermostatAddress": address, "ThermostatAddress2": address_2}


def shade_address_args(addresses: str, count: int = 5) -> dict[str, str]:
    """Build ``Address1..N`` (default 5) for shade commands (``ShadeUp/Down/Stop``)."""
    parts = [part.strip() or "(null)" for part in addresses.split(",")] if addresses else []
    return {f"Address{index}": value for index, value in enumerate(parts[:count], start=1)}


def shade_set_args(
    addresses: str,
    position: int,
    fade_time: Any = 0,
    delay_time: Any = 0,
    preset_number: Any = 0,
    scene_number: Any = 0,
) -> dict[str, str]:
    """Build native ``ShadeSet`` arguments (sibling PROTOCOL.md §7.5).

    Controller shades include their explicit ``(null)`` address padding, while Bond
    shades have only ``Address1``. Do not add a synthetic Address6.
    """
    parts = [part.strip() for part in addresses.split(",")] if addresses else []
    while parts and not parts[-1]:
        parts.pop()
    args = {f"Address{index}": value for index, value in enumerate(parts, start=1)}
    args.update(
        {
            "ShadeLevel": str(position),
            "FadeTime": str(fade_time or 0),
            "DelayTime": str(delay_time or 0),
            "PresetNumber": str(preset_number or 0),
            "SceneNumber": str(scene_number or 0),
        }
    )
    return args


def shade_component_logical(state_name: str) -> tuple[str, str]:
    """The (component, logical_component) for a shade, from its ``stateName``."""
    prefix = state_name.rsplit(".", 1)[0] if "." in state_name else ""
    parts = prefix.split(".")
    return (
        parts[0] if len(parts) >= 2 else "",
        parts[1] if len(parts) >= 2 else "",
    )


def audio_zone_logical_component(device: dict[str, Any]) -> str | None:
    """The audio zone's logical component (``Audio Zone N``), if the device has one."""
    zone = str(device.get("zone") or "")
    if zone.startswith("Audio Zone "):
        return zone
    match = re.search(r"Audio Zone\s+(\d+)", str(device.get("name") or ""))
    if match:
        return f"Audio Zone {match.group(1)}"
    return None


def zone_state_prefix(component: str, logical_component: str) -> str:
    """The dotted state-key prefix for one audio zone.

    ``<component>.<logical>.SVC_AV_SAVANTMUSIC.`` (zone numbers are per-component).
    """
    return f"{component}.{logical_component}.{SVC_AV_SAVANTMUSIC}."


def parse_media_time(value: Any) -> float | None:
    """Convert observed ``MM:SS``/``HH:MM:SS`` media state strings to seconds."""
    if not isinstance(value, str):
        return None
    try:
        parts = [int(part) for part in value.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(part < 0 for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def parse_light_state(state_name: str, value: Any) -> tuple[bool | None, int | None]:
    """Interpret a light load's state value as ``(is_on, brightness 0-255)``.

    The per-load value format depends on the state attribute (live-verified):

    - ``CurrentDimmerLevel_*`` -> int ``0-100``.
    - ``CurrentColor_*`` / ``CurrentBleColor_*`` -> the string
      ``"R,G,B,W,<level>,<level>|kelvin,<level>,<level>|<curve>"`` (e.g.
      ``"083,079,245,000,096,096|6000,096,096|Custom 1"``).

    Returns ``(None, None)`` for unrecognised values so callers can fall back (to room
    state or an optimistic assumption).
    """
    if "CurrentDimmerLevel" in state_name:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            level = int(value)
            return level > 0, _dimmer_brightness(level)
        return None, None
    if "CurrentColor" in state_name or "CurrentBleColor" in state_name:
        if isinstance(value, str) and value:
            return _parse_color(value)
        return None, None
    return None, None


def parse_light_color(state_name: str, value: Any) -> tuple[int, int, int, int] | None:
    """Return the RGBW channels from a ``CurrentColor``/``CurrentBleColor`` state."""
    if (
        ("CurrentColor" not in state_name and "CurrentBleColor" not in state_name)
        or not isinstance(value, str)
    ):
        return None
    parts = value.split("|", 1)[0].split(",")
    if len(parts) < 4:
        return None
    try:
        channels = [max(0, min(255, int(part.strip()))) for part in parts[:4]]
    except ValueError:
        return None
    return channels[0], channels[1], channels[2], channels[3]


def coerce_number(value: Any) -> float | None:
    """Coerce a state value to float (the host reports temperatures as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _dimmer_brightness(level_0_100: int) -> int:
    return max(0, min(255, round(level_0_100 / 100 * 255)))


def _parse_color(value: str) -> tuple[bool, int]:
    head = value.split("|", 1)[0]
    parts = [part.strip() for part in head.split(",")]

    def _int(part: str) -> int:
        try:
            return int(part)
        except ValueError:
            return 0

    channels = [_int(part) for part in parts[:4]]
    level = _int(parts[4]) if len(parts) >= 5 else 0
    # Color loads retain their RGBW channels while off. When present, the duplicated
    # level field is therefore authoritative for on/off and brightness.
    if len(parts) >= 5:
        return level > 0, _dimmer_brightness(level)
    if any(channel > 0 for channel in channels):
        return True, max(0, min(255, max(channels)))
    return False, 0
