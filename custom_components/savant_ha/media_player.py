"""Media player platform: one entity per Savant audio zone."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    MUSIC_ZONE_PREFIX,
    SVC_AV_SAVANTMUSIC,
    VERB_PAUSE,
    VERB_PLAY,
    VERB_POWER_OFF,
    VERB_POWER_ON,
    VERB_SEEK,
    VERB_SET_VOLUME,
    VERB_SKIP_DOWN,
    VERB_SKIP_UP,
)
from .control import audio_zone_logical_component, parse_media_time, zone_state_prefix
from .entity import SavantEntity
from .hub import SavantHub

_SONG = "CurrentSongName"
_ARTIST = "CurrentArtistName"
_ALBUM = "CurrentAlbumName"
_SERVICE = "CurrentStreamingService"
_PAUSED = "CurrentPauseStatus"
_ELAPSED = "CurrentElapsedTime"
_REMAINING = "CurrentRemainingTime"
_SEEK_DISABLED = "SeekDisabled"
_ARTWORK = "CurrentArtworkPath"


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
        self._last_media_position: float | None = None
        self._media_position_updated_at: datetime | None = None
        self._artwork_key: str | None = None
        self._artwork: bytes | None = None

    def _key(self, attr: str) -> str:
        return f"{zone_state_prefix(self._component, self._logical_component)}{attr}"

    def _value(self, attr: str) -> object:
        # The current host uses an unqualified audio-zone state namespace; retain the
        # service-qualified state form captured on the original host as a fallback.
        key = f"{self._component}.{self._logical_component}.{attr}"
        return self._state(key, self._state(self._key(attr)))

    def _handle_coordinator_update(self) -> None:
        position = self.media_position
        if position != self._last_media_position:
            self._last_media_position = position
            self._media_position_updated_at = dt_util.utcnow() if position is not None else None
        super()._handle_coordinator_update()

    # ------------------------------------------------------------ media player

    @property
    def state(self) -> MediaPlayerState:
        has_media = bool(self._value(_SONG) or self._value(_ARTIST))
        if has_media:
            if self._value(_PAUSED) is False:
                return MediaPlayerState.PLAYING
            if self._value(_PAUSED) is True:
                return MediaPlayerState.PAUSED
            return MediaPlayerState.ON
        return MediaPlayerState.OFF

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
        )
        if self._value(_SEEK_DISABLED) is False:
            features |= MediaPlayerEntityFeature.SEEK
        return features

    @property
    def media_title(self) -> str | None:
        value = self._value(_SONG)
        return value if isinstance(value, str) and value else None

    @property
    def media_artist(self) -> str | None:
        value = self._value(_ARTIST)
        return value if isinstance(value, str) and value else None

    @property
    def media_album_name(self) -> str | None:
        value = self._value(_ALBUM)
        return value if isinstance(value, str) and value else None

    @property
    def media_content_type(self) -> str | None:
        value = self._value(_SERVICE)
        return value if isinstance(value, str) and value else None

    @property
    def volume_level(self) -> float | None:
        value = self._value("CurrentVolume")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value) / 100.0))
        return None

    @property
    def media_position(self) -> float | None:
        return parse_media_time(self._value(_ELAPSED))

    @property
    def media_duration(self) -> float | None:
        elapsed = parse_media_time(self._value(_ELAPSED))
        remaining = parse_media_time(self._value(_REMAINING))
        if elapsed is not None and remaining is not None:
            return elapsed + remaining
        return None

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self._media_position_updated_at

    @property
    def media_image_hash(self) -> str | None:
        value = self._value(_ARTWORK)
        return value if isinstance(value, str) and value else None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        key = self.media_image_hash
        if key is None:
            self._artwork_key = None
            self._artwork = None
            return None, None
        if self._artwork_key != key:
            artwork = await self.hub.client.async_get_artwork(
                self._component, self._logical_component, key
            )
            if artwork is not None:
                self._artwork_key = key
                self._artwork = artwork
            else:
                self._artwork_key = None
                self._artwork = None
        return self._artwork, "image/jpeg" if self._artwork is not None else None

    async def _media_request(
        self, request: str, request_args: dict[str, int] | None = None
    ) -> None:
        await self._service_request(
            request,
            component=self._component,
            service_type=SVC_AV_SAVANTMUSIC,
            zone=self._room,
            logical_component=self._logical_component,
            variant_id="1",
            request_args=request_args,
        )

    async def async_turn_on(self) -> None:
        await self._media_request(VERB_POWER_ON)

    async def async_turn_off(self) -> None:
        await self._media_request(VERB_POWER_OFF)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._media_request(VERB_SET_VOLUME, {"VolumeValue": int(round(volume * 100))})

    async def async_media_play(self) -> None:
        await self._media_request(VERB_PLAY)

    async def async_media_pause(self) -> None:
        await self._media_request(VERB_PAUSE)

    async def async_media_next_track(self) -> None:
        await self._media_request(VERB_SKIP_UP)

    async def async_media_previous_track(self) -> None:
        await self._media_request(VERB_SKIP_DOWN)

    async def async_media_seek(self, position: float) -> None:
        duration = self.media_duration
        if duration is None or duration <= 0:
            return
        progress = max(0, min(100, round(position / duration * 100)))
        await self._media_request(VERB_SEEK, {"ProgressValue": progress})


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
