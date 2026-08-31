"""Media player platform: one entity per Savant audio zone.

Uses the archive-derived ``<component>.Audio Zone N.SVC_AV_SAVANTMUSIC.*`` state keys
for now-playing metadata and ``PowerOn`` / ``SetVolume`` for control (PROTOCOL.md
§5.3/§6/§13). Volume and mute are held on the *room*, not the zone, so volume control
is offered only when the zone's active room can be resolved via ``ZonesActiveIn``.
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
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    MUSIC_ZONE_PREFIX,
    ROOM_CURRENT_VOLUME,
    SVC_AV_SAVANTMUSIC,
    VERB_POWER_ON,
    VERB_SET_VOLUME,
)
from .control import audio_zone_logical_component, zone_state_prefix
from .entity import SavantEntity
from .hub import SavantHub

_ZONE_MARKER = "ZonesActiveIn"
_SONG = "CurrentSongName"
_ARTIST = "CurrentArtistName"
_ALBUM = "CurrentAlbumName"
_SERVICE = "CurrentStreamingService"


def _active_rooms(hub: SavantHub, component: str, logical_component: str) -> list[str]:
    # ZonesActiveIn maps zone -> room (§6.2); the exact shape is unconfirmed, so both a
    # mapping (values) and a list are accepted defensively.
    value = hub.get(f"{zone_state_prefix(component, logical_component)}{_ZONE_MARKER}")
    if isinstance(value, dict):
        return [r for r in value.values() if isinstance(r, str)]
    if isinstance(value, list):
        return [r for r in value if isinstance(r, str)]
    return []


class SavantMediaPlayer(SavantEntity, MediaPlayerEntity):
    """A single Savant audio zone."""

    def __init__(
        self,
        hub: SavantHub,
        device: dict[str, object],
        component: str,
        logical_component: str,
    ) -> None:
        super().__init__(
            hub,
            device_key=f"media:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        self._component = component
        self._logical_component = logical_component
        self._room = str(device.get("room") or "")
        self._attr_unique_id = f"{hub.uid}_media_{device['id']}"

    def _key(self, attr: str) -> str:
        return f"{zone_state_prefix(self._component, self._logical_component)}{attr}"

    def _active_rooms(self) -> list[str]:
        return _active_rooms(self.hub, self._component, self._logical_component)

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
        rooms = self._active_rooms()
        await self._service_request(
            VERB_POWER_ON,
            component=self._component,
            service_type=SVC_AV_SAVANTMUSIC,
            zone=rooms[0] if rooms else self._room,
            logical_component=self._logical_component,
            variant_id="1",
        )

    async def async_set_volume_level(self, volume: float) -> None:
        rooms = self._active_rooms()
        if not rooms:
            return
        await self._service_request(
            VERB_SET_VOLUME,
            component=self._component,
            service_type=SVC_AV_SAVANTMUSIC,
            zone=rooms[0],
            logical_component=self._logical_component,
            variant_id="1",
            request_args={"VolumeValue": int(round(volume * 100))},
        )


def _discovered_zones(hub: SavantHub) -> set[int]:
    zones: set[int] = set()
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


def _logical_component(device: dict[str, object]) -> str | None:
    return audio_zone_logical_component(device)


def _build_entities(hub: SavantHub) -> list[SavantMediaPlayer]:
    if hub.devices is not None:
        media = [d for d in hub.devices if d.get("type") == DEVICE_TYPE_MEDIA_PLAYER]
        entities: list[SavantMediaPlayer] = []
        for device in media:
            logical_component = _logical_component(device)
            component = str(device.get("component") or "Music")
            if logical_component:
                entities.append(
                    SavantMediaPlayer(hub, device, component, logical_component)
                )
        return entities
    return [
        SavantMediaPlayer(
            hub,
            {"id": str(z), "name": f"Audio Zone {z}"},
            "Music",
            f"Audio Zone {z}",
        )
        for z in sorted(_discovered_zones(hub))
    ]


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
