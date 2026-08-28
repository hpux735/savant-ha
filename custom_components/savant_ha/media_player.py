"""Media player platform: one entity per Savant audio zone.

Uses the observed ``Music.Audio Zone N.SVC_AV_SAVANTMUSIC.*`` state keys for now-playing
metadata and ``PowerOn`` / ``SetVolume`` for control (PROTOCOL.md §5.3/§6).  Volume and
mute are held on the *room*, not the zone, so volume control is offered only when the
zone's active room can be resolved via ``ZonesActiveIn``.
"""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_MUSIC_ZONES,
    DOMAIN,
    MUSIC_ZONE_PREFIX,
    ROOM_CURRENT_VOLUME,
    SVC_AV_SAVANTMUSIC,
    VERB_POWER_ON,
    VERB_SET_VOLUME,
)
from .entity import SavantEntity
from .hub import SavantHub

_ZONE_MARKER = "ZonesActiveIn"
_SONG = "CurrentSongName"
_ARTIST = "CurrentArtistName"
_ALBUM = "CurrentAlbumName"
_SERVICE = "CurrentStreamingService"


def _zone_prefix(zone: int) -> str:
    return f"{MUSIC_ZONE_PREFIX}{zone}.{SVC_AV_SAVANTMUSIC}."


def _active_rooms(hub: SavantHub, zone: int) -> list[str]:
    # ZonesActiveIn maps zone -> room (§6.2); the exact shape is unconfirmed, so both a
    # mapping (values) and a list are accepted defensively.
    value = hub.get(f"{_zone_prefix(zone)}{_ZONE_MARKER}")
    if isinstance(value, dict):
        return [r for r in value.values() if isinstance(r, str)]
    if isinstance(value, list):
        return [r for r in value if isinstance(r, str)]
    return []


class SavantMediaPlayer(SavantEntity, MediaPlayerEntity):
    """A single Savant audio zone."""

    def __init__(self, hub: SavantHub, zone: int) -> None:
        super().__init__(hub)
        self._zone = zone
        self._attr_unique_id = f"{hub.uid}_audio_zone_{zone}"
        self._attr_name = f"Audio Zone {zone}"

    def _key(self, attr: str) -> str:
        return f"{_zone_prefix(self._zone)}{attr}"

    def _active_rooms(self) -> list[str]:
        return _active_rooms(self.hub, self._zone)

    # ------------------------------------------------------------ media player

    @property
    def state(self) -> MediaPlayerState:
        rooms = self._active_rooms()
        playing = self._state(self._key(_SONG)) or self._state(self._key(_ARTIST))
        if rooms or playing:
            return MediaPlayerState.ON
        return MediaPlayerState.OFF

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = MediaPlayerEntityFeature.TURN_ON
        if self._active_rooms():
            features |= MediaPlayerEntityFeature.VOLUME_SET
        return features

    @property
    def media_title(self) -> str | None:
        return self._state(self._key(_SONG))

    @property
    def media_artist(self) -> str | None:
        return self._state(self._key(_ARTIST))

    @property
    def media_album_name(self) -> str | None:
        return self._state(self._key(_ALBUM))

    @property
    def media_content_type(self) -> str | None:
        return self._state(self._key(_SERVICE))

    @property
    def volume_level(self) -> float | None:
        rooms = self._active_rooms()
        if not rooms:
            return None
        value = self._state(f"{rooms[0]}.{ROOM_CURRENT_VOLUME}")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value) / 100.0))
        return None

    async def async_turn_on(self) -> None:
        zone = self._active_rooms()
        await self._service_request(
            VERB_POWER_ON,
            component="Music",
            service_type=SVC_AV_SAVANTMUSIC,
            zone=zone[0] if zone else "",
            logical_component=f"Audio Zone {self._zone}",
            variant_id="1",
        )

    async def async_set_volume_level(self, volume: float) -> None:
        zone = self._active_rooms()
        if not zone:
            return
        await self._service_request(
            VERB_SET_VOLUME,
            component="Music",
            service_type=SVC_AV_SAVANTMUSIC,
            zone=zone[0],
            logical_component=f"Audio Zone {self._zone}",
            variant_id="1",
            request_args={"VolumeValue": int(round(volume * 100))},
        )


def _discovered_zones(hub: SavantHub) -> set[int]:
    zones: set[int] = set(range(1, DEFAULT_MUSIC_ZONES + 1))
    marker = MUSIC_ZONE_PREFIX
    for key in hub.states:
        if key.startswith(marker):
            rest = key[len(marker):]
            if "." in rest:
                try:
                    zones.add(int(rest.split(".", 1)[0]))
                except ValueError:
                    continue
    return zones


def _build_entities(hub: SavantHub) -> list[SavantMediaPlayer]:
    return [SavantMediaPlayer(hub, zone) for zone in sorted(_discovered_zones(hub))]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: SavantHub = hass.data[DOMAIN][entry.entry_id]

    def _add() -> None:
        entities = [e for e in _build_entities(hub) if not hub.is_created(e.unique_id)]
        if entities:
            hub.mark_created([e.unique_id for e in entities])
            async_add_entities(entities)

    _add()
    hub.add_platform_callback(_add)
