"""Constants for the Savant Home Assistant integration.

Protocol constants here are reconstructed from live black-box observation, not vendor
docs — see the sibling project's ``PROTOCOL.md`` (condensed locally in this repo's
``PROTOCOL.md``). Each constant carries a note pointing at the section that documents
where it was observed.
"""

from __future__ import annotations

import logging

DOMAIN = "savant_ha"

LOGGER = logging.getLogger(__package__)

# ---- Config flow fields ---------------------------------------------------
CONF_HOST = "host"  # <HOST_IP> — user-supplied
CONF_PORT = "port"  # optional explicit control port (overrides UDP discovery)
CONF_USERNAME = "username"  # local user name for the host (optional)
CONF_PASSWORD = "password"  # local user password for the host (optional)
CONF_CLOUD_TOKEN = "cloud_token"  # optional; observed key in devicePresent
CONF_CONFIGURATION_ID = "configuration_id"  # optional; observed key in devicePresent
CONF_HOST_TOKEN = "host_token"  # optional; explicit session hostToken
CONF_HOME_ID = "home_id"  # discovered from the 9101 control record
CONF_UID = "uid"  # generated client device UUID
CONF_NAME = "name"  # discovered host name
CONF_ROOMS = "rooms"  # user-supplied room names (list[str])
CONF_DEVICES = "devices"  # user-approved device list (list[dict]) from the picker step

# Device kinds in the approved device list.
DEVICE_TYPE_ROOM = "room"
DEVICE_TYPE_HVAC = "hvac"
DEVICE_TYPE_AUDIO_ZONE = "audio_zone"

# ---- Protocol (PROTOCOL.md §1) --------------------------------------------
# WebSocket subprotocol negotiated on the control channel.
RPM_SUBPROTOCOL = "rpm-protocol"  # PROTOCOL.md §1
# Keepalive byte observed in WS ping/pong frames (PROTOCOL.md §1).
KEEPALIVE_BYTE = b"\x45"
# Interval observed between keepalive pings (~2s). Used as a starting value.
KEEPALIVE_INTERVAL = 2.0

# UDP discovery ports (PROTOCOL.md §1.1).
DISCOVERY_PORT_CONTROL = 9101  # _control_.ws
DISCOVERY_PORT_PRESENCE = 9103  # _presence_.ws
DISCOVERY_SERVICE_CONTROL = "_control_.ws"
DISCOVERY_SERVICE_PRESENCE = "_presence_.ws"

# Envelope keys present on every message (PROTOCOL.md §2).
ENVELOPE_KEY_MESSAGES = "messages"
ENVELOPE_KEY_URI = "URI"
ENVELOPE_KEY_UID = "uid"
ENVELOPE_KEY_USER = "user"

# ---- Session endpoints (PROTOCOL.md §3 / §4) ------------------------------
URI_DEVICE_PRESENT = "session/devicePresent"
URI_AUTH_REQUEST = "session/authenticationRequest"
URI_AUTH_RESPONSE = "session/authenticationResponse"
URI_STATE_REGISTER = "state/register"
URI_STATE_UNREGISTER = "state/unregister"
URI_STATE_UPDATE = "state/update"
URI_SERVICE_REQUEST = "service/request"
URI_DCM_REQUEST = "dcm/request"
URI_DASHBOARD_REGISTER = "dis/dashboard/register"
URI_DASHBOARD_UPDATE = "dis/dashboard/update"
URI_DASHBOARD_REQUEST = "dis/dashboard/request"

# Dashboard RPC verb that returns the scene list (which embeds room names).
DASHBOARD_REQUEST_SCENES = "GetAVAutomationScenes"
# State key the host pushes the scene list under (PROTOCOL.md §9).
SCENES_STATE_KEY = "scenesAndFoldersReduced"

# ---- Service/request verbs (PROTOCOL.md §6) -------------------------------
VERB_SET_VOLUME = "SetVolume"
VERB_POWER_ON = "PowerOn"
VERB_ROOM_BRIGHTNESS = "__RoomSetBrightness"
VERB_DIMMER_SET = "DimmerSet"
VERB_SET_COOL_POINT = "SetCoolPointTemperature"
VERB_SET_HEAT_POINT = "SetHeatPointTemperature"
VERB_HVAC_MODE_AUTO = "SetHVACModeAuto"
VERB_HVAC_MODE_COOL = "SetHVACModeCool"
VERB_HVAC_MODE_HEAT = "SetHVACModeHeat"
VERB_HVAC_MODE_OFF = "SetHVACModeOff"
VERB_FAN_MODE_ON = "SetFanModeOn"

# ---- Component/service-type scopes (PROTOCOL.md §6) -----------------------
SCOPE_HVAC = {
    "component": "HVAC Controller",
    "serviceType": "SVC_ENV_HVAC",
    "logicalComponent": "HVAC_controller",
    "variantID": "1",
    "zone": "",
}
SVC_AV_SAVANTMUSIC = "SVC_AV_SAVANTMUSIC"
SVC_AV_GENERALAUDIO = "SVC_AV_GENERALAUDIO"
SVC_ENV_LIGHTING = "SVC_ENV_LIGHTING"

# ---- State-key prefixes / attributes (PROTOCOL.md §5) ---------------------
HVAC_STATE_PREFIX = "HVAC Controller.HVAC_controller."
# Default unit/suffix index observed on HVAC state keys.
HVAC_UNIT_SUFFIX = "_1"

# Per-room attributes (PROTOCOL.md §5.2 / §6.1).
ROOM_CURRENT_VOLUME = "CurrentVolume"
ROOM_IS_MUTED = "IsMuted"
ROOM_LIGHTS_ON = "RoomLightsAreOn"
ROOM_BRIGHTNESS = "BrightnessLevel"
ROOM_CURRENT_TEMPERATURE = "RoomCurrentTemperature"

# The full observed set of per-room attributes (PROTOCOL.md §6.1).  A room name is the
# *first* dotted segment of any state key whose *second* segment is one of these — there
# is no dedicated "get rooms" endpoint, so the room list is derived from these keys and
# from scene definitions (§6.1).
ROOM_ATTRIBUTES = (
    "ActiveService",
    "ActiveServices",
    "LastActiveService",
    "CurrentVolume",
    "IsMuted",
    "RelativeVolumeOnly",
    "RoomLightsAreOn",
    "BrightnessLevel",
    "RoomFansAreOn",
    "RoomShadesAreOpen",
    "RoomCurrentTemperature",
    "SleepTimerActive",
    "SleepTimerRemainingTime",
)


def room_from_state_key(key: str) -> str | None:
    """Return the room name for a per-room state key, else ``None``.

    A room is the first dotted segment ``R`` of any key whose second segment is a
    per-room attribute (PROTOCOL.md §6.1), e.g. ``Living Room.RoomCurrentTemperature``
    -> ``"Living Room"``.
    """
    if "." not in key:
        return None
    first, rest = key.split(".", 1)
    second = rest.split(".", 1)[0]
    if second in ROOM_ATTRIBUTES:
        return first
    return None


def room_state_keys(rooms: set[str] | list[str]) -> list[str]:
    """Return the ``<room>.<attr>`` subscription keys for a set of room names."""
    return [f"{room}.{attr}" for room in rooms for attr in ROOM_ATTRIBUTES]

# Global attributes (PROTOCOL.md §5.4).
GLOBAL_CURRENT_TEMPERATURE = "global.CurrentTemperature"
GLOBAL_LIGHTS_ON = "global.LightsAreOn"

# Audio zones (PROTOCOL.md §5.3). The prefix includes the zone number; key is
# ``Music.Audio Zone N.SVC_AV_SAVANTMUSIC.<attr>``.
MUSIC_ZONE_PREFIX = "Music.Audio Zone "

# ---- State subscription defaults (PROTOCOL.md §5 / §6.1) ------------------
# These dotted keys are the *observed* surface from the sibling repo's
# ``savantre/schema.py``.  Room names are host-defined; there is no "get rooms"
# endpoint, so rooms are derived from scene definitions and per-room state keys
# (PROTOCOL.md §6.1) and subscribed to dynamically at runtime.

# HVAC attributes, joined as ``<HVAC_STATE_PREFIX><attr><HVAC_UNIT_SUFFIX>``.
HVAC_STATE_ATTRIBUTES = (
    "ThermostatCurrentTemperature",
    "ThermostatCurrentHeatPoint",
    "ThermostatCurrentCoolPoint",
    "ThermostatCurrentSetPoint",
    "ThermostatCurrentHumidity",
    "ThermostatCurrentHumiditySetPoint",
    "ThermostatMode",
    "ThermostatHVACState",
    "ThermostatFanMode",
    "ThermostatCurrentFanSpeed",
    "IsCurrentHVACModeOff",
    "IsCurrentHVACModeCool",
    "IsCurrentHVACModeHeat",
    "IsCurrentHVACModeAuto",
    "IsCurrentHVACModeEmergencyHeat",
    "IsCurrentHVACModeHumidify",
    "IsCurrentHVACModeDehumidify",
    "IsCurrentFanSpeedLow",
    "IsCurrentFanSpeedMidLow",
    "IsCurrentFanSpeedMid",
    "IsCurrentFanSpeedMidHigh",
    "IsCurrentFanSpeedHigh",
    "IsCurrentFanSpeedOff",
    "IsThermostatCurrentFanModeAuto",
    "IsThermostatCurrentFanModeOn",
    "IsThermostatCurrentFanModeOff",
    "IsThermostatHolding",
    "ThermostatAwayState",
    "ThermostatHoldUntil",
    "ThermostatIsSavingEnergy",
)

# Audio-zone attributes, joined as ``Music.Audio Zone <N>.SVC_AV_SAVANTMUSIC.<attr>``.
MUSIC_ZONE_ATTRIBUTES = (
    "ZonesActiveIn",
    "CurrentSongName",
    "CurrentArtistName",
    "CurrentAlbumName",
    "CurrentArtworkPath",
    "CurrentArtworkDeeplink",
    "NowPlayingSource",
    "CurrentStreamingService",
    "CurrentPauseStatus",
    "CurrentElapsedTime",
    "CurrentRemainingTime",
    "CurrentProgress",
    "TransportSet",
)

# Global keys that need no per-room prefix (PROTOCOL.md §5.4).
GLOBAL_STATE_KEYS = (
    "global.CurrentTemperature",
    "global.LightsAreOn",
    "Energy.Grid.IsAvailable",
)

# Number of Audio Zones to subscribe to by default.
DEFAULT_MUSIC_ZONES = 2


def build_default_subscribe_keys(rooms: list[str] | None = None) -> list[str]:
    """Return the default dotted state keys to subscribe to.

    ``rooms`` are user-supplied room names (optional); additional rooms are discovered
    at runtime from scene definitions and state keys (PROTOCOL.md §6.1).
    """
    keys: list[str] = [
        f"{HVAC_STATE_PREFIX}{attr}{HVAC_UNIT_SUFFIX}" for attr in HVAC_STATE_ATTRIBUTES
    ]
    for zone in range(1, DEFAULT_MUSIC_ZONES + 1):
        for attr in MUSIC_ZONE_ATTRIBUTES:
            keys.append(f"{MUSIC_ZONE_PREFIX}{zone}.{SVC_AV_SAVANTMUSIC}.{attr}")
    keys.extend(GLOBAL_STATE_KEYS)
    keys.extend(room_state_keys(set(rooms or [])))
    return keys


# ---- Defaults --------------------------------------------------------------
# Default/identity values sent in devicePresent until the user supplies real ones.
# ASSUMPTION: a well-behaved client must identify itself; the exact value of the make/
# app/model strings is unconstrained (only the host's own app has been observed).
DEVICE_MAKE = "Home Assistant"
DEVICE_APP = "savant_ha"
DEVICE_TYPE = "Home Assistant"
# client device UUID is generated per config entry at setup time.
