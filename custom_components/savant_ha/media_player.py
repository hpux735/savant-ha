"""Media player platform: one entity per selectable Savant music source."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_player.errors import BrowseError, SearchError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    VERB_STOP_REPEAT,
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
_RAW_VOLUME_MAX = 50  # PROTOCOL.md §5.4 / sibling PROTOCOL.md §6.1, §7.2.
_MUSIC_POWER_TIMEOUT = 5.0
_NON_MEDIA_ROOT_TITLES = frozenset({"settings", "savant music", "select server"})


class SavantMediaPlayer(SavantEntity, MediaPlayerEntity):
    """A single archive-derived AV endpoint in one room."""

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
        control = device.get("control")
        control = control if isinstance(control, dict) else {}
        self._variant_id = str(control.get("variant_id") or "1")
        self._service_id = str(control.get("service_id") or "")
        self._service_type = str(control.get("service_type") or SVC_AV_SAVANTMUSIC)
        self._requests = set(control.get("requests") or ())
        if self._service_type == SVC_AV_SAVANTMUSIC and not self._requests:
            self._requests = {
                VERB_POWER_ON,
                VERB_POWER_OFF,
                VERB_SET_VOLUME,
                VERB_PLAY,
                VERB_PAUSE,
                VERB_SKIP_UP,
                VERB_SKIP_DOWN,
                VERB_SEEK,
            }
        self._attr_unique_id = f"{hub.uid}_media_{device['id']}"
        self._last_media_position: float | None = None
        self._media_position_updated_at: datetime | None = None
        self._artwork_key: str | None = None
        self._artwork: bytes | None = None
        self._power_off_requested = False
        self._optimistic_state: MediaPlayerState | None = None
        self._optimistic_volume: float | None = None
        self._browse_nodes: dict[str, dict[str, object]] = {}
        self._browse_artwork: dict[str, bytes] = {}
        self._music_active = asyncio.Event()

    def _key(self, attr: str) -> str:
        return f"{zone_state_prefix(self._component, self._logical_component)}{attr}"

    def _value(self, attr: str) -> object:
        # The current host uses an unqualified audio-zone state namespace; retain the
        # service-qualified state form captured on the original host as a fallback.
        key = f"{self._component}.{self._logical_component}.{attr}"
        return self._state(key, self._state(self._key(attr)))

    def _handle_coordinator_update(self) -> None:
        # A nonempty room service confirms a later external/native power-on after an
        # optimistic PowerOff. Empty ActiveService is the host's authoritative idle state.
        active_service = str(self._state(f"{self._room}.ActiveService") or "")
        if self._service_type == SVC_AV_SAVANTMUSIC and self._is_active_service(active_service):
            self._power_off_requested = False
            self._music_active.set()
        elif self._service_type == SVC_AV_SAVANTMUSIC:
            self._music_active.clear()
        position = self.media_position
        if position != self._last_media_position:
            self._last_media_position = position
            self._media_position_updated_at = dt_util.utcnow() if position is not None else None
        super()._handle_coordinator_update()

    # ------------------------------------------------------------ media player

    @property
    def state(self) -> MediaPlayerState | None:
        if self._service_type != SVC_AV_SAVANTMUSIC:
            active_service = str(self._state(f"{self._room}.ActiveService") or "")
            if active_service:
                if active_service != self._service_id:
                    return MediaPlayerState.OFF
                return self._optimistic_state or MediaPlayerState.ON
            # ASSUMPTION: no Apple TV-specific state has been captured. Retain the last
            # requested state until the room's authoritative ActiveService changes.
            return self._optimistic_state or MediaPlayerState.OFF
        if self._power_off_requested or self._state(f"{self._room}.ActiveService") == "":
            return MediaPlayerState.OFF
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
        features = MediaPlayerEntityFeature(0)
        if VERB_POWER_ON in self._requests:
            features |= MediaPlayerEntityFeature.TURN_ON
        if VERB_POWER_OFF in self._requests:
            features |= MediaPlayerEntityFeature.TURN_OFF
        if VERB_SET_VOLUME in self._requests:
            features |= MediaPlayerEntityFeature.VOLUME_SET
        if VERB_PLAY in self._requests:
            features |= MediaPlayerEntityFeature.PLAY
        if VERB_PAUSE in self._requests:
            features |= MediaPlayerEntityFeature.PAUSE
        if VERB_SKIP_UP in self._requests:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if VERB_SKIP_DOWN in self._requests:
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        if VERB_SEEK in self._requests and self._value(_SEEK_DISABLED) is False:
            features |= MediaPlayerEntityFeature.SEEK
        if self._service_type == SVC_AV_SAVANTMUSIC:
            features |= (
                MediaPlayerEntityFeature.BROWSE_MEDIA
                | MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.SEARCH_MEDIA
            )
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
        value = self._state(f"{self._room}.CurrentVolume", self._value("CurrentVolume"))
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value) / _RAW_VOLUME_MAX))
        return self._optimistic_volume

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
        if self._service_type != SVC_AV_SAVANTMUSIC:
            return None, None
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
            service_type=self._service_type,
            zone=self._room,
            logical_component=self._logical_component,
            variant_id=self._variant_id,
            request_args=request_args,
        )

    async def async_turn_on(self) -> None:
        if VERB_POWER_ON not in self._requests:
            return
        await self._media_request(VERB_POWER_ON)
        self._power_off_requested = False
        self._optimistic_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        if VERB_POWER_OFF not in self._requests:
            return
        await self._media_request(VERB_POWER_OFF)
        self._power_off_requested = True
        self._optimistic_state = MediaPlayerState.OFF
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        if VERB_SET_VOLUME not in self._requests:
            return
        await self._media_request(
            VERB_SET_VOLUME,
            {"VolumeValue": int(round(volume * _RAW_VOLUME_MAX))},
        )
        self._optimistic_volume = volume
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        if VERB_PLAY not in self._requests:
            return
        await self._media_request(VERB_PLAY)
        if self._service_type != SVC_AV_SAVANTMUSIC:
            # The native app immediately cancels repeat after Apple TV Play (PROTOCOL.md §5.4).
            await self._media_request(VERB_STOP_REPEAT)
        self._power_off_requested = False
        self._optimistic_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        if VERB_PAUSE not in self._requests:
            return
        await self._media_request(VERB_PAUSE)
        self._optimistic_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_next_track(self) -> None:
        if VERB_SKIP_UP not in self._requests:
            return
        await self._media_request(VERB_SKIP_UP)

    async def async_media_previous_track(self) -> None:
        if VERB_SKIP_DOWN not in self._requests:
            return
        await self._media_request(VERB_SKIP_DOWN)

    async def async_media_seek(self, position: float) -> None:
        if VERB_SEEK not in self._requests:
            return
        duration = self.media_duration
        if duration is None or duration <= 0:
            return
        progress = max(0, min(100, round(position / duration * 100)))
        await self._media_request(VERB_SEEK, {"ProgressValue": progress})

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Return the capture-backed Savant Music folder tree (sibling PROTOCOL.md §8.3)."""
        if self._service_type != SVC_AV_SAVANTMUSIC:
            raise BrowseError("Savant media browsing is available only for Savant Music")
        if media_content_id is None:
            self._browse_nodes.clear()
            self._browse_artwork.clear()
            result = await self.hub.client.async_browse_music(
                self._component, self._logical_component, operation="getRoot"
            )
            return self._browse_result(result, "Savant Music")
        node = self._browse_nodes.get(media_content_id)
        if node is None:
            raise BrowseError("Savant media item is no longer available; browse again")
        try:
            if node.get("query") in {"browse", "browseSearch"}:
                result = await self.hub.client.async_follow_music_node(
                    self._component, self._logical_component, node
                )
            else:
                result = await self.hub.client.async_browse_music(
                    self._component, self._logical_component, operation="browse", node=node
                )
        except ValueError as err:
            raise BrowseError("Savant media item cannot be opened") from err
        return self._browse_result(result, str(node.get("title") or "Savant Music"))

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """Search captured Savant Music catalogs with the supported all-service scope."""
        if self._service_type != SVC_AV_SAVANTMUSIC:
            raise SearchError("Savant media search is available only for Savant Music")
        try:
            result = await self.hub.client.async_search_music(
                self._component, self._logical_component, query.search_query
            )
        except ValueError as err:
            raise SearchError("Savant media search failed") from err
        nodes = result.get("nodes")
        return SearchMedia(
            result=[
                self._browse_node(node)
                for node in nodes
                if isinstance(node, dict) and node.get("displayType") == "searchList"
            ]
            if isinstance(nodes, list)
            else []
        )

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: object
    ) -> None:
        """Submit a captured action-node request; host state confirms playback."""
        if self._service_type != SVC_AV_SAVANTMUSIC or media_type != MediaType.MUSIC:
            return
        node = self._browse_nodes.get(media_id)
        if node is None or node.get("actionType") != "action":
            return
        if self._is_active_service(str(self._state(f"{self._room}.ActiveService") or "")):
            self._music_active.set()
        if not self._music_active.is_set():
            if VERB_POWER_ON not in self._requests:
                raise HomeAssistantError("Savant Music cannot be turned on for playback")
            await self.async_turn_on()
            try:
                await asyncio.wait_for(self._music_active.wait(), _MUSIC_POWER_TIMEOUT)
            except TimeoutError as err:
                raise HomeAssistantError("Savant Music did not turn on for playback") from err
        try:
            await self.hub.client.async_follow_music_node(
                self._component, self._logical_component, node
            )
        except ValueError:
            return

    async def async_get_browse_image(
        self,
        media_content_type: str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Fetch a browse node thumbnail (sibling PROTOCOL.md §8.2)."""
        if self._service_type != SVC_AV_SAVANTMUSIC:
            return None, None
        node = self._browse_nodes.get(media_content_id)
        artwork_key = node.get("artworkKey") if node is not None else None
        if not isinstance(artwork_key, str) or not artwork_key:
            return None, None
        artwork = self._browse_artwork.get(artwork_key)
        if artwork is None:
            artwork = await self.hub.client.async_get_artwork(
                self._component,
                self._logical_component,
                artwork_key,
                artwork_type="thumbnailArtwork",
            )
            if artwork is not None:
                self._browse_artwork[artwork_key] = artwork
        return artwork, "image/jpeg" if artwork is not None else None

    def _browse_result(self, result: dict[str, object], title: str) -> BrowseMedia:
        nodes = result.get("nodes")
        children = (
            [
                self._browse_node(node)
                for node in nodes
                if isinstance(node, dict) and self._is_media_browse_node(node)
            ]
            if isinstance(nodes, list)
            else []
        )
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="root",
            media_content_type=MediaType.MUSIC,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_node(self, node: dict[str, object]) -> BrowseMedia:
        """Translate one opaque Savant UI node without exposing its provider metadata."""
        node_id = uuid.uuid4().hex
        self._browse_nodes[node_id] = node
        browsable = node.get("actionType") == "browsable"
        title = str(node.get("title") or node.get("subtitle") or "Savant Music")
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=node_id,
            media_content_type=MediaType.MUSIC,
            title=title,
            can_play=node.get("actionType") == "action",
            can_expand=browsable,
            thumbnail=(
                self.get_browse_image_url(MediaType.MUSIC, node_id)
                if isinstance(node.get("artworkKey"), str) and node["artworkKey"]
                else None
            ),
        )

    @staticmethod
    def _is_media_browse_node(node: dict[str, object]) -> bool:
        """Hide captured navigation/status entries that cannot select media."""
        title = str(node.get("title") or "").strip().casefold()
        if title in _NON_MEDIA_ROOT_TITLES or title.startswith("connected to"):
            return False
        return node.get("actionType") in {"browsable", "action"}

    def _is_active_service(self, active_service: str) -> bool:
        """Return whether the room's active service is this Music endpoint."""
        return active_service == self._service_id if self._service_id else bool(active_service)


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
    return str(device.get("zone") or "") or audio_zone_logical_component(device)


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
