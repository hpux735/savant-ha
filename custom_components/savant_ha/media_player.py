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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
from .control import audio_zone_logical_component, coerce_number, parse_media_time
from .entity import SavantEntity
from .hub import SavantHub
from .media_routing import MediaRouteError, opaque_model_id
from .savant_client import SavantError, image_content_type

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
_SEARCH_NODE_ID = "savant-search"


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
        self._media_server_id = str(control.get("media_server_id") or "") or opaque_model_id(
            "server", component, self._service_type
        )
        self._zone_id = str(control.get("zone_id") or "") or opaque_model_id(
            "zone", self._room, self._service_id, logical_component, str(device["id"])
        )
        self.route_id = str(device["id"])
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hub.register_media_entity(self)
        # A later alias can change shared-server/group attributes on earlier aliases.
        for entity in self.hub.media_server_entities(self._media_server_id):
            entity.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        server_id = self._media_server_id
        self.hub.unregister_media_entity(self)
        for entity in self.hub.media_server_entities(server_id):
            entity.async_write_ha_state()
        await super().async_will_remove_from_hass()

    def _value(self, attr: str) -> object:
        return self._state(f"{self._component}.{self._logical_component}.{attr}")

    def _handle_coordinator_update(self) -> None:
        # A nonempty room service confirms a later external/native power-on after an
        # optimistic PowerOff. Empty ActiveService is the host's authoritative idle state.
        if self._service_type == SVC_AV_SAVANTMUSIC and self._is_active_service():
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
            active_services = self._active_services()
            if self._service_id in active_services:
                return self._optimistic_state or MediaPlayerState.ON
            if active_services:
                return MediaPlayerState.OFF
            # ASSUMPTION: no Apple TV-specific state has been captured. Retain the last
            # requested state until the room's authoritative ActiveService changes.
            return self._optimistic_state or MediaPlayerState.OFF
        if self._power_off_requested:
            return MediaPlayerState.OFF
        if self._activity_is_addressable and not self._is_active_service():
            return MediaPlayerState.OFF
        has_media = bool(self._value(_SONG) or self._value(_ARTIST))
        if has_media:
            if self._value(_PAUSED) is False:
                return MediaPlayerState.PLAYING
            if self._value(_PAUSED) is True:
                return MediaPlayerState.PAUSED
            return MediaPlayerState.ON
        return self._optimistic_state or MediaPlayerState.OFF

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
            features |= MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SEARCH_MEDIA
            if self._activity_is_addressable:
                features |= MediaPlayerEntityFeature.PLAY_MEDIA
        if self.savant_shared_media_server:
            features |= MediaPlayerEntityFeature.GROUPING
        return features

    @property
    def savant_media_server_id(self) -> str:
        return self._media_server_id

    @property
    def savant_zone_id(self) -> str:
        return self._zone_id

    @property
    def savant_selected_zone_entity_ids(self) -> list[str]:
        return self.hub.selected_media_zone_entity_ids(self._media_server_id)

    @property
    def savant_shared_media_server(self) -> bool:
        return self.hub.media_topology_complete and len(
            self.hub.media_server_entities(self._media_server_id)
        ) > 1

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "savant_media_server_id": self.savant_media_server_id,
            "savant_zone_id": self.savant_zone_id,
            "savant_selected_zone_entity_ids": self.savant_selected_zone_entity_ids,
            "savant_shared_media_server": self.savant_shared_media_server,
        }

    @property
    def group_members(self) -> list[str] | None:
        return self.savant_selected_zone_entity_ids

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
        if (number := coerce_number(value)) is not None:
            return max(0.0, min(1.0, number / _RAW_VOLUME_MAX))
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
        return (
            self._artwork,
            image_content_type(self._artwork) if self._artwork is not None else None,
        )

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
            include_request_id=True,
        )

    async def async_turn_on(self) -> None:
        if VERB_POWER_ON not in self._requests:
            return
        try:
            await self.hub.async_set_media_endpoint_power(self, True)
        except MediaRouteError as err:
            raise HomeAssistantError(str(err)) from err
        self._power_off_requested = False
        self._optimistic_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        if VERB_POWER_OFF not in self._requests:
            return
        try:
            await self.hub.async_set_media_endpoint_power(self, False)
        except MediaRouteError as err:
            raise HomeAssistantError(str(err)) from err
        self._power_off_requested = True
        self._optimistic_state = MediaPlayerState.OFF
        self._music_active.clear()
        self.async_write_ha_state()

    async def _async_set_route_selected(self, selected: bool) -> None:
        verb = VERB_POWER_ON if selected else VERB_POWER_OFF
        if verb not in self._requests:
            raise HomeAssistantError(f"Savant endpoint does not support {verb}")
        await self._media_request(verb)

    async def async_set_media_server_zones(
        self, zone_entity_ids: list[str]
    ) -> dict[str, object]:
        try:
            return await self.hub.async_set_media_server_zones(self, zone_entity_ids)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except MediaRouteError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_join_players(self, group_members: list[str]) -> None:
        try:
            await self.hub.async_join_media_server_zones(self, group_members)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except MediaRouteError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_unjoin_player(self) -> None:
        try:
            await self.hub.async_unjoin_media_server_zone(self)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except MediaRouteError as err:
            raise HomeAssistantError(str(err)) from err

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
        if media_content_id == _SEARCH_NODE_ID:
            return BrowseMedia(
                media_class=MediaClass.DIRECTORY,
                media_content_id=_SEARCH_NODE_ID,
                media_content_type=MediaType.MUSIC,
                title="Search Savant Music",
                can_play=False,
                can_expand=True,
                can_search=True,
                children=[],
            )
        if media_content_id is None:
            self._browse_nodes.clear()
            self._browse_artwork.clear()
            try:
                result = await self.hub.client.async_browse_music(
                    self._component, self._logical_component, operation="getRoot"
                )
            except (SavantError, ValueError) as err:
                raise BrowseError(str(err) or "Savant media browsing failed") from err
            return self._browse_result(result, "Savant Music", include_search=True)
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
        except (SavantError, ValueError) as err:
            raise BrowseError("Savant media item cannot be opened") from err
        return self._browse_result(result, str(node.get("title") or "Savant Music"))

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """Search captured Savant Music catalogs with the supported all-service scope."""
        if self._service_type != SVC_AV_SAVANTMUSIC:
            raise SearchError("Savant media search is available only for Savant Music")
        search_term = query.search_query.strip()
        if not search_term:
            return SearchMedia(result=[])
        # The captured endpoint supports only global filter:"all" search. Home Assistant
        # supplies the current opaque browse ID, but mapping that to a provider filter
        # would invent uncaptured behavior. Do not advertise media-class filters either.
        try:
            result = await self.hub.client.async_search_music(
                self._component, self._logical_component, search_term
            )
        except (SavantError, ValueError) as err:
            raise SearchError(str(err) or "Savant media search failed") from err
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
        if self._is_active_service():
            self._music_active.set()
        if not self._music_active.is_set():
            if VERB_POWER_ON not in self._requests:
                raise HomeAssistantError("Savant Music cannot be turned on for playback")
            try:
                await self.async_turn_on()
            except SavantError as err:
                raise HomeAssistantError(str(err)) from err
            try:
                await asyncio.wait_for(self._music_active.wait(), _MUSIC_POWER_TIMEOUT)
            except TimeoutError as err:
                raise HomeAssistantError("Savant Music did not turn on for playback") from err
        try:
            await self.hub.client.async_follow_music_node(
                self._component, self._logical_component, node
            )
        except (SavantError, ValueError) as err:
            raise HomeAssistantError(str(err) or "Savant media playback failed") from err

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
        return artwork, image_content_type(artwork) if artwork is not None else None

    def _browse_result(
        self, result: dict[str, object], title: str, *, include_search: bool = False
    ) -> BrowseMedia:
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
        if include_search:
            children.insert(
                0,
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=_SEARCH_NODE_ID,
                    media_content_type=MediaType.MUSIC,
                    title="Search Savant Music",
                    can_play=False,
                    can_expand=True,
                ),
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
        playable = node.get("actionType") == "action" and self._activity_is_addressable
        title = str(node.get("title") or node.get("subtitle") or "Savant Music")
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY if browsable else MediaClass.MUSIC,
            media_content_id=node_id,
            media_content_type=MediaType.MUSIC,
            title=title,
            can_play=playable,
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

    def _active_services(self) -> set[str]:
        """Return exact identifiers from both captured room service states."""
        services: set[str] = set()
        for attribute in ("ActiveService", "ActiveServices"):
            value = self._state(f"{self._room}.{attribute}")
            if isinstance(value, str):
                services.update(part.strip() for part in value.split(",") if part.strip())
        return services

    @property
    def _activity_is_addressable(self) -> bool:
        """Whether room activity can identify this exact configured service."""
        return bool(self._room and self._service_id)

    def _is_active_service(self) -> bool:
        """Return whether this endpoint is active in either room service state."""
        services = self._active_services()
        return self._service_id in services if self._service_id else bool(services)

    def _route_selection_state(self) -> bool | None:
        """Return exact membership, or None until room route state is known."""
        if not all(
            f"{self._room}.{attribute}" in self.hub.states
            for attribute in ("ActiveService", "ActiveServices")
        ):
            return None
        services = self._active_services()
        return self._service_id in services if self._service_id else bool(services)


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
