"""Async client for the reconstructed Savant RPM protocol.

Everything in this module is derived from live black-box observation archived in the
sibling ``savant-app-re`` project — see this repo's ``PROTOCOL.md`` (which condenses the
sibling ``PROTOCOL.md``). No vendor documentation, firmware, or source code is used.

Transport summary (PROTOCOL.md §1):

* WSS to ``wss://<host>:<dynamic-port>/`` with subprotocol ``rpm-protocol``.
* TLS is **not** validated (the host uses a generic factory cert; the app pins nothing).
* Each logical message is a single WebSocket binary frame holding a MessagePack map
  ``{messages: [...], URI, uid, user}``.
* client -> host frames are plaintext msgpack; host -> client frames are gzip-compressed.
* Keepalive: WS ``ping``/``pong`` with a single byte ``0x45`` every ~2s.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import msgpack
from aiohttp import WSMsgType, hdrs

from . import uiconfig
from .const import (
    DASHBOARD_REQUEST_APPLY_SCENE,
    DASHBOARD_STATE_RECENT_SERVICES,
    DEVICE_APP,
    DEVICE_MAKE,
    DEVICE_MODEL,
    DEVICE_NAME,
    DEVICE_OS,
    DEVICE_TYPE,
    DISCOVERY_PORT_CONTROL,
    DISCOVERY_SERVICE_CONTROL,
    ENVELOPE_KEY_MESSAGES,
    ENVELOPE_KEY_UID,
    ENVELOPE_KEY_URI,
    ENVELOPE_KEY_USER,
    HVAC_STATE_PREFIX,
    KEEPALIVE_BYTE,
    KEEPALIVE_INTERVAL,
    LOGGER,
    MUSIC_ZONE_PREFIX,
    RPM_SUBPROTOCOL,
    SCENE_VERSION,
    SCENES_STATE_KEY,
    URI_AUTH_REQUEST,
    URI_AUTH_RESPONSE,
    URI_DASHBOARD_REGISTER,
    URI_DASHBOARD_REQUEST,
    URI_DASHBOARD_UPDATE,
    URI_DCM_REQUEST,
    URI_DEVICE_PRESENT,
    URI_DEVICE_RECOGNIZED,
    URI_FEATURE_LOCK,
    URI_FILE_DOWNLOAD,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
    URI_USER_DATA_REGISTER,
    USER_DATA_IMAGE_STATES,
    USER_DATA_INITIAL_STATES,
    build_default_subscribe_keys,
    new_uid,
    room_from_state_key,
)

_StateCallback = Callable[[str, Any], None]
_StatusCallback = Callable[[bool], None]
_RoomsCallback = Callable[[set[str]], None]
_ScenesCallback = Callable[[dict[str, dict[str, Any]]], None]
_MdnsHostsCallback = Callable[[], Awaitable[list[str]]]

# How long to wait for the host to authorize us before registering state anyway.
AUTH_TIMEOUT = 5.0
ARTWORK_TIMEOUT = 3.0
FILE_TRANSFER_TIMEOUT = 15.0
MUSIC_BROWSE_TIMEOUT = 10.0
MUSIC_SEARCH_READY_TIMEOUT = 5.0
SCENE_ACTIVATION_TIMEOUT = 5.0
# Resource guards for malformed/untrusted LAN input, not observed protocol maxima.
_MAX_TRANSFER_BODY = 64 * 1024 * 1024
_MAX_TRANSFER_LABEL = 4096
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Message keys that must never be written to logs (PII per AGENTS.md).  Includes the
# host identity/credential fields carried in deviceRecognized (hostSecret/hostUID/
# homeInfo/postalCode), the local-account user list, and the username.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "hostToken",
        "secretKey",
        "cloudToken",
        "configurationID",
        "homeId",
        "onboardKey",
        "userHostName",
        "UID",
        "hostSecret",
        "hostUID",
        "hostName",
        "homeInfo",
        "hostTimeZone",
        "postalCode",
        "users",
        "user",
    }
)


def _redact(obj: Any) -> Any:
    """Return a log-safe copy of a msgpack-decoded object (PII keys masked)."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in _SENSITIVE_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


@dataclass(frozen=True)
class FileTransferMessage:
    """One decoded shared file/image transfer message (sibling PROTOCOL.md §13.0)."""

    final: bool
    declared_length: int
    label: bytes
    body: bytes


@dataclass
class _TransferState:
    """Accumulated transfer state correlated by raw label on one connection."""

    future: asyncio.Future[bytes | None] | None
    declared_length: int | None = None
    body: bytearray = field(default_factory=bytearray)
    invalid: bool = False


def parse_file_transfer_message(data: bytes) -> FileTransferMessage | None:
    """Decode the shared transfer header without interpreting its raw label."""
    if len(data) < 14 or data[0] != 0x01 or data[1] not in (0x01, 0x81):
        return None
    declared_length = int.from_bytes(data[2:10], "big")
    label_length = int.from_bytes(data[10:14], "big")
    if (
        declared_length > _MAX_TRANSFER_BODY
        or label_length > _MAX_TRANSFER_LABEL
        or label_length > len(data) - 14
    ):
        return None
    payload_start = 14 + label_length
    return FileTransferMessage(
        final=data[1] == 0x81,
        declared_length=declared_length,
        label=data[14:payload_start],
        body=data[payload_start:],
    )


def image_content_type(data: bytes) -> str | None:
    """Return the capture-backed media type for a complete image body."""
    if data.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    return None


def _log_tls_info(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Log the negotiated TLS version/cipher for diagnostics."""
    try:
        ssl_object = ws._writer.transport.get_extra_info("ssl_object")  # noqa: SLF001
    except (AttributeError, RuntimeError):
        return
    if ssl_object is not None:
        try:
            LOGGER.info(
                "Savant TLS version=%s cipher=%s", ssl_object.version(), ssl_object.cipher()
            )
        except (AttributeError, ValueError):
            pass


class SavantError(Exception):
    """Base error for the Savant client."""


class SavantConnectionError(SavantError):
    """Raised when the control channel is not connected."""


@dataclass
class SavantHostInfo:
    """The decoded discovery record for a host (PROTOCOL.md §1.1)."""

    host: str
    port: int
    name: str = ""
    home_id: str = ""
    uid: str = ""
    scheme: str = "wss"
    host_model: str = ""
    build_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, host: str, record: dict[str, Any]) -> SavantHostInfo:
        # Only the keys observed in the discovery record are surfaced; the rest is kept
        # intact in ``extra`` for diagnostics.
        return cls(
            host=host,
            port=int(record.get("port", 0)),
            name=str(record.get("name", "")),
            home_id=str(record.get("homeId", "")),
            uid=str(record.get("UID", "")),
            scheme=str(record.get("scheme", "wss")),
            host_model=str(record.get("hostModel", "")),
            build_version=str(record.get("buildVersion", "")),
            extra={k: v for k, v in record.items() if k not in _RECORD_FIELDS},
        )


_RECORD_FIELDS = frozenset(
    {"port", "name", "homeId", "UID", "scheme", "hostModel", "buildVersion"}
)

# Discovery-record keys that must never appear in logs (PII per AGENTS.md): homeId,
# device UIDs, onboarding keys, and the operator's host/project names.
_REDACT_KEYS = frozenset({"UID", "onboardKey", "homeId", "userHostName"})


@dataclass
class SavantDeviceInfo:
    """The discoverable device surface collected during setup.

    ``devices`` is the authoritative inventory from the ``uiconfig.tar.gz`` download
    (PROTOCOL.md §13); the room/hvac/zone sets are fallbacks derived from the state bus.
    """

    rooms: set[str] = field(default_factory=set)
    hvac_suffixes: set[str] = field(default_factory=set)
    zones: set[int] = field(default_factory=set)
    authorized: bool = False
    auth_response_seen: bool = False
    devices: list[uiconfig.SavantDevice] = field(default_factory=list)


async def probe_host(
    host: str,
    port: int,
    *,
    uid: str | None = None,
    home_id: str = "",
    username: str = "",
    password: str = "",
    host_token: str | None = None,
    cloud_token: str = "",
    configuration_id: str = "",
    timeout: float = 15.0,
) -> SavantDeviceInfo:
    """One-shot probe: connect, authenticate, and collect the device surface.

    Runs the normal handshake (devicePresent -> auth -> state/dashboard subscription)
    for ``timeout`` seconds, then disconnects.  Used by the config flow to build the
    device picker.
    """
    client = SavantClient(
        host,
        port,
        uid=uid,
        home_id=home_id,
        username=username,
        password=password,
        host_token=host_token,
        cloud_token=cloud_token,
        configuration_id=configuration_id,
        subscribe_keys=build_default_subscribe_keys([]),
    )
    info = SavantDeviceInfo()

    def _on_rooms(rooms: set[str]) -> None:
        info.rooms.update(rooms)

    def _on_state(key: str, value: Any) -> None:  # noqa: ARG001 - value unused here
        room = room_from_state_key(key)
        if room:
            info.rooms.add(room)
        marker = f"{HVAC_STATE_PREFIX}ThermostatCurrentTemperature"
        if key.startswith(marker):
            info.hvac_suffixes.add(key[len(marker):])
        if key.startswith(MUSIC_ZONE_PREFIX):
            rest = key[len(MUSIC_ZONE_PREFIX):]
            if "." in rest:
                try:
                    info.zones.add(int(rest.split(".", 1)[0]))
                except ValueError:
                    pass

    client.on_rooms_discovered = _on_rooms
    client.on_state_update = _on_state
    LOGGER.info("Savant probe: connecting to collect devices")
    receive: asyncio.Task[None] | None = None
    try:
        await client._connect()
        receive = asyncio.ensure_future(client._receive_loop())
        await asyncio.sleep(timeout)
    finally:
        if receive is not None:
            receive.cancel()
            # NOTE: asyncio.CancelledError is a BaseException (not Exception), so it must
            # be suppressed explicitly or it escapes and crashes the config flow.
            with suppress(asyncio.CancelledError, Exception):
                await receive
        # Capture the auth state BEFORE _disconnect() resets _authorized.
        info.authorized = client.authorized
        info.auth_response_seen = client.auth_response_seen
        # Parse the downloaded config archive into the authoritative device inventory.
        if client.uiconfig_archive:
            try:
                info.devices = uiconfig.parse_archive(client.uiconfig_archive)
            except Exception:  # noqa: BLE001 - archive parsing is best-effort
                LOGGER.exception("Savant: failed to parse uiconfig archive")
        await client._disconnect()
    LOGGER.info(
        "Savant probe: authorized=%s auth_response=%s rooms=%d hvac=%d zones=%d devices=%d",
        info.authorized,
        info.auth_response_seen,
        len(info.rooms),
        len(info.hvac_suffixes),
        len(info.zones),
        len(info.devices),
    )
    return info


def _pack(obj: Any) -> bytes:
    # Live-verified against the target host: its msgpack parser does NOT accept the
    # new-spec ``str8`` (0xd9) format. Any envelope containing a string longer than 31
    # chars (e.g. every long dotted state key such as
    # ``Savant.Lighting.CurrentDimmerLevel_1_001``) is silently dropped — including the
    # whole ``state/register`` frame — which is why state never arrived while auth,
    # controls, and short-key registrations (all fixstr) worked. Encoding strings with
    # the legacy raw formats (fixstr / raw16; msgpack-python's ``use_bin_type=False``)
    # is accepted, matching the reverse-engineering demo's verified client
    # (PROTOCOL.md §1).
    return msgpack.packb(obj, use_bin_type=False)


def _encode_discovery(service: str) -> bytes:
    return _pack({"service": service})


async def discover_hosts(
    host: str | None = None, timeout: float = 3.0
) -> list[SavantHostInfo]:
    """Return all control records found through UDP discovery (PROTOCOL.md §1.1).

    Broadcasts the observed msgpack queries to UDP 9101/9103, then reads each host's
    reply.  A unicast probe is included when ``host`` is available during initial setup.
    """

    class _Proto(asyncio.DatagramProtocol):
        def __init__(self) -> None:
            self.results: list[tuple[str, dict[str, Any]]] = []

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
            try:
                obj = msgpack.unpackb(data, raw=False)
            except Exception:  # noqa: BLE001 - tolerate malformed replies
                return
            if isinstance(obj, dict):
                self.results.append((addr[0], obj))

        def error_received(self, exc: Exception) -> None:
            pass

    loop = asyncio.get_running_loop()
    proto = _Proto()
    # SO_BROADCAST is required to send to 255.255.255.255; without it the broadcast
    # send silently fails on some platforms (and the unicast probe is the fallback).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    transport, _ = await loop.create_datagram_endpoint(lambda: proto, sock=sock)
    # Only the 9101 control record carries the authoritative WSS endpoint.
    targets = [("255.255.255.255", DISCOVERY_PORT_CONTROL, DISCOVERY_SERVICE_CONTROL)]
    if host:
        targets.extend(
            [
                (host, DISCOVERY_PORT_CONTROL, DISCOVERY_SERVICE_CONTROL),
            ]
        )
    try:
        for target_host, port, service in targets:
            try:
                transport.sendto(_encode_discovery(service), (target_host, port))
            except OSError:
                pass
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    LOGGER.debug("Savant discovery: %d control reply(ies)", len(proto.results))
    if not proto.results:
        LOGGER.debug("Savant discovery: no reply on UDP 9101 for %s", host or "broadcast")
    def _record_port(record: dict[str, Any]) -> int:
        try:
            return int(record.get("port", 0))
        except (TypeError, ValueError):
            return 0

    records: list[SavantHostInfo] = []
    seen: set[tuple[str, str, int]] = set()
    for addr, record in proto.results:
        if _record_port(record) > 0:
            info = SavantHostInfo.from_record(addr, record)
            key = (info.host, info.uid, info.port)
            if key not in seen:
                seen.add(key)
                records.append(info)
    return records


async def discover_host(host: str | None, timeout: float = 3.0) -> SavantHostInfo | None:
    """Discover a host's current control endpoint (PROTOCOL.md §1.1)."""
    records = await discover_hosts(host, timeout)
    if host:
        matching_records = [info for info in records if info.host == host]
        for info in matching_records:
            if info.uid:
                return info
        if matching_records:
            return matching_records[0]
    return records[0] if records else None


async def discover_host_by_uid(host_uid: str, timeout: float = 3.0) -> SavantHostInfo | None:
    """Resolve a stable host UID to its current DHCP-assigned endpoint."""
    for info in await discover_hosts(timeout=timeout):
        if info.uid == host_uid:
            return info
    return None


class SavantClient:
    """A single RPM session, driven to completion by :meth:`run_forever`.

    Owns the WSS connection, the auth handshake, the state subscription, keepalive
    pings, and the receive loop.  Emits state pushes via :attr:`on_state_update` and
    connection changes via :attr:`on_status`.  :meth:`run_forever` reconnects with
    exponential backoff so the hub can run it as one background task.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        host_uid: str = "",
        uid: str | None = None,
        home_id: str = "",
        cloud_token: str = "",
        configuration_id: str = "",
        host_token: str | None = None,
        username: str = "",
        password: str = "",
        subscribe_keys: list[str] | None = None,
        mdns_hosts: _MdnsHostsCallback | None = None,
        reconnect_delay: float = 5.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        self._host_uid = host_uid
        # Client device identity, generated once per config entry and then persisted.
        self._uid = uid or new_uid()
        self._home_id = home_id
        self._cloud_token = cloud_token
        self._configuration_id = configuration_id
        self._host_token = host_token
        self._username = username
        self._password = password
        # Credentials for the LOCAL LOGIN path (PROTOCOL.md §4.1/§4.9): the host issues
        # the hostToken in exchange for {user, password}, so no cloud tokens are needed.
        self._has_credentials = bool(self._host_token or (username and password))
        self._subscribe_keys = list(subscribe_keys or [])
        self._mdns_hosts = mdns_hosts
        self._subscribed_keys: set[str] = set(self._subscribe_keys)
        self._reconnect_delay = reconnect_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._stopping = False
        self._authorized = False
        self._auth_response_seen = False
        self._device_recognized = False
        self._authentication_required = True
        self._post_auth_done = False
        self._port_warned = False
        self._auth_task: asyncio.Task | None = None
        self._uiconfig_archive: bytes | None = None
        # File and image replies share one binary framing and correlate by raw label.
        # Serialize our requests because same-label transfers have no sequence ID.
        self._file_transfer_lock = asyncio.Lock()
        self._file_transfers: dict[bytes, _TransferState] = {}
        self._pending_scene_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_music_requests: dict[
            tuple[str, str], asyncio.Future[dict[str, Any]]
        ] = {}
        self._pending_music_search_refresh: dict[
            tuple[str, str], asyncio.Future[None]
        ] = {}

        self.on_state_update: _StateCallback | None = None
        self.on_status: _StatusCallback | None = None
        self.on_rooms_discovered: _RoomsCallback | None = None
        self.on_scenes_update: _ScenesCallback | None = None

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def authorized(self) -> bool:
        return self._authorized

    @property
    def auth_response_seen(self) -> bool:
        return self._auth_response_seen

    @property
    def uiconfig_archive(self) -> bytes | None:
        return self._uiconfig_archive

    async def run_forever(self) -> None:
        """Connect and process frames, reconnecting with backoff on any failure.

        The control port is re-discovered before every (re)connect because it changes
        when the host reboots (PROTOCOL.md §1.1/§3.2); this also self-heals an entry
        whose port was not found at setup time.
        """
        delay = self._reconnect_delay
        while not self._stopping:
            try:
                await self._refresh_discovery()
                if self._port > 0:
                    await self._connect()
                    await self._receive_loop()
            except (TimeoutError, aiohttp.ClientError, OSError, SavantError) as exc:
                LOGGER.debug("Savant connection lost: %s", exc)
            except Exception:  # noqa: BLE001 - keep the client alive
                LOGGER.exception("Unexpected error in Savant client")
            finally:
                await self._disconnect()
            if self._stopping:
                break
            LOGGER.debug("Reconnecting to Savant host in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect_max_delay)

    async def async_resolve_endpoint(self) -> SavantHostInfo | None:
        """Resolve the configured host identity to its current network endpoint."""
        try:
            if self._host_uid:
                info = await discover_host_by_uid(self._host_uid, timeout=3.0)
                if info is None and self._mdns_hosts is not None:
                    for host in await self._mdns_hosts():
                        candidate = await discover_host(host, timeout=3.0)
                        if candidate is not None and candidate.uid == self._host_uid:
                            info = candidate
                            break
            else:
                info = await discover_host(self._host or None, timeout=3.0)
        except (TimeoutError, OSError):
            return None
        if info is None:
            if not self._port_warned:
                LOGGER.warning(
                    "Savant host endpoint unknown for %s (UDP discovery found nothing); "
                    "will keep retrying",
                    self._host_uid or self._host,
                )
                self._port_warned = True
            return None
        self._port_warned = False
        if info.host != self._host:
            LOGGER.info("Savant host endpoint changed")
            self._host = info.host
        if info.port != self._port:
            LOGGER.info("Savant control port changed %s -> %s", self._port, info.port)
        self._port = info.port
        if info.home_id:
            self._home_id = info.home_id
        if info.uid:
            self._host_uid = info.uid
        return info

    async def _refresh_discovery(self) -> None:
        await self.async_resolve_endpoint()

    async def stop(self) -> None:
        self._stopping = True
        await self._disconnect()

    # ------------------------------------------------------------------ control

    async def request(self, uri: str, messages: list[dict[str, Any]]) -> None:
        """Send an envelope of ``messages`` to ``uri`` (PROTOCOL.md §2)."""
        if self._ws is None or self._ws.closed:
            raise SavantConnectionError("not connected")
        envelope = {
            ENVELOPE_KEY_MESSAGES: messages,
            ENVELOPE_KEY_URI: uri,
            ENVELOPE_KEY_UID: self._uid,
            ENVELOPE_KEY_USER: DEVICE_TYPE,
        }
        LOGGER.debug("Savant -> %s (%d message(s))", uri, len(messages))
        await self._ws.send_bytes(_pack(envelope))

    async def _request_music(
        self,
        uri: str,
        messages: list[dict[str, Any]],
        *,
        include_identity: bool,
    ) -> None:
        """Send a Music RPC with its capture-specific envelope variant."""
        if include_identity:
            await self.request(uri, messages)
            return
        if self._ws is None or self._ws.closed:
            raise SavantConnectionError("not connected")
        await self._ws.send_bytes(
            _pack({ENVELOPE_KEY_MESSAGES: messages, ENVELOPE_KEY_URI: uri})
        )

    async def service_request(
        self,
        request: str,
        *,
        component: str | None,
        service_type: str,
        zone: str = "",
        logical_component: str | None = "",
        variant_id: str | None = None,
        request_args: dict[str, Any] | None = None,
        include_request_id: bool = False,
    ) -> None:
        """Emit a ``service/request`` control message (PROTOCOL.md §6)."""
        message: dict[str, Any] = {
            "serviceType": service_type,
            "zone": zone,
            "request": request,
        }
        if component is not None:
            message["component"] = component
        if logical_component is not None:
            message["logicalComponent"] = logical_component
        if variant_id is not None:
            message["variantID"] = variant_id
        if request_args:
            message["requestArgs"] = request_args
        if include_request_id:
            message["requestId"] = uuid.uuid4().hex
        await self.request("service/request", [message])

    async def activate_scene(self, scene_id: str) -> None:
        """Apply a saved dashboard scene and wait for its correlated RPC result."""
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_scene_requests[request_id] = future
        try:
            await self.request(
                URI_DASHBOARD_REQUEST,
                [
                    {
                        "request": DASHBOARD_REQUEST_APPLY_SCENE,
                        "requestId": request_id,
                        "requestArgs": {"id": scene_id, "version": SCENE_VERSION},
                    }
                ],
            )
            result = await asyncio.wait_for(future, SCENE_ACTIVATION_TIMEOUT)
            if not result.get("success") or result.get("errorCode") != 0:
                raise SavantError(
                    f"Savant scene activation failed (error {result.get('errorCode', 'unknown')})"
                )
        except TimeoutError as err:
            raise SavantError("Savant scene activation timed out") from err
        finally:
            self._pending_scene_requests.pop(request_id, None)

    async def _download_file_transfer(
        self,
        messages: list[dict[str, Any]],
        *,
        expected_label: bytes,
        timeout: float,
    ) -> bytes | None:
        """Request one transfer and correlate its complete body by raw label."""
        async with self._file_transfer_lock:
            if expected_label in self._file_transfers:
                LOGGER.debug("Savant transfer label is still draining after a timeout")
                return None
            future: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
            state = _TransferState(future=future)
            self._file_transfers[expected_label] = state
            request_sent = False
            try:
                await self.request(URI_FILE_DOWNLOAD, messages)
                request_sent = True
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except TimeoutError:
                LOGGER.debug("Savant file transfer timed out")
                state.future = None
                future.cancel()
                return None
            except asyncio.CancelledError:
                if request_sent:
                    state.future = None
                    future.cancel()
                else:
                    self._file_transfers.pop(expected_label, None)
                raise
            except (SavantConnectionError, aiohttp.ClientError, OSError):
                self._file_transfers.pop(expected_label, None)
                return None
            finally:
                if future.done() and state.future is future:
                    self._file_transfers.pop(expected_label, None)

    async def async_get_artwork(
        self,
        component: str,
        logical_component: str,
        key: str,
        artwork_type: str = "nowPlayingArtwork",
    ) -> bytes | None:
        """Fetch a complete JPEG or PNG artwork transfer (sibling PROTOCOL.md §8.2)."""
        if not key:
            return None
        body = await self._download_file_transfer(
            [
                {
                    "URI": f"avc/{component}/{logical_component}",
                    "payload": {"key": key, "type": artwork_type},
                }
            ],
            expected_label=key.encode(),
            timeout=ARTWORK_TIMEOUT,
        )
        return body if body is not None and image_content_type(body) is not None else None

    async def async_browse_music(
        self,
        component: str,
        logical_component: str,
        *,
        operation: str,
        node: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request an observed Savant Music root or folder listing (sibling PROTOCOL.md §8)."""
        if operation not in {"getRoot", "browse"}:
            raise ValueError(f"Unsupported Savant music operation: {operation}")
        return await self._async_music_request(
            component,
            logical_component,
            operation=operation,
            node=node,
            arguments=None,
            client_type="iPhone",
            include_identity=True,
        )

    async def async_search_music(
        self, component: str, logical_component: str, search_term: str
    ) -> dict[str, Any]:
        """Submit the captured typed Music search flow (sibling PROTOCOL.md §8.4)."""
        search_uuid = str(uuid.uuid4())
        arguments = {
            "filter": "all",
            "searchTerm": search_term,
            "services": ["plex", "tunein", "amazonmusic", "playlists"],
            "uuid": search_uuid,
        }
        prefix = f"{component}.{logical_component}."
        refresh: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_music_search_refresh[(prefix, search_uuid)] = refresh
        try:
            result = await self._async_music_request(
                component,
                logical_component,
                operation="search",
                node=None,
                arguments=arguments,
                client_type="android",
                include_identity=False,
            )
            screen_arguments = result.get("screenArguments")
            if isinstance(screen_arguments, dict) and screen_arguments.get("searchReady") is True:
                return result
            await asyncio.wait_for(refresh, MUSIC_SEARCH_READY_TIMEOUT)
            return await self._async_music_request(
                component,
                logical_component,
                operation="search",
                node=None,
                arguments=arguments,
                client_type="android",
                include_identity=False,
            )
        except TimeoutError as err:
            raise SavantError("Savant music search did not become ready") from err
        finally:
            self._pending_music_search_refresh.pop((prefix, search_uuid), None)

    async def async_follow_music_node(
        self, component: str, logical_component: str, node: dict[str, Any]
    ) -> dict[str, Any]:
        """Follow a captured typed-search result node (sibling PROTOCOL.md §8.4-8.5)."""
        operation = node.get("query") or "browse"
        if operation not in {"browse", "browseSearch"}:
            raise ValueError("Unsupported Savant Music result node")
        return await self._async_music_request(
            component,
            logical_component,
            operation=operation,
            node=node,
            arguments=None,
            client_type="android",
            include_identity=False,
        )

    async def _async_music_request(
        self,
        component: str,
        logical_component: str,
        *,
        operation: str,
        node: dict[str, Any] | None,
        arguments: dict[str, Any] | None,
        client_type: str,
        include_identity: bool,
    ) -> dict[str, Any]:
        """Send and correlate one observed Music RPC request."""
        request_id = uuid.uuid4().hex
        uri = f"music/{component}/{logical_component}/SVC_AV_SAVANTMUSIC/{operation}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_music_requests[(uri, request_id)] = future
        message: dict[str, Any] = {
            "clientType": client_type,
            "limit": 50,
            "offset": 0,
            "requestId": request_id,
            "version": 1,
            "node": node,
            "arguments": arguments,
        }
        try:
            await self._request_music(uri, [message], include_identity=include_identity)
            return await asyncio.wait_for(future, MUSIC_BROWSE_TIMEOUT)
        except TimeoutError as err:
            raise SavantError("Savant music browse timed out") from err
        finally:
            self._pending_music_requests.pop((uri, request_id), None)

    # ------------------------------------------------------------------ internals

    def _ssl_context(self) -> ssl.SSLContext:
        # The host presents a generic self-signed cert and the app validates nothing
        # (PROTOCOL.md §1).  Reproduce that: TLS for privacy, not authentication.
        # NOTE: ssl.create_default_context() eagerly loads the system trust store
        # (set_default_verify_paths) — blocking I/O that HA flags inside the event
        # loop.  Build a bare context instead, since we verify nothing.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _connect(self) -> None:
        # Match the observed upgrade request exactly (PROTOCOL.md §4.3): the app sends
        # only Host/Upgrade/Connection/Sec-WebSocket-* + Origin, with no User-Agent /
        # Accept / Accept-Encoding.  Strip aiohttp's auto headers so the host sees the
        # same minimal request the working reference client sends.
        self._session = aiohttp.ClientSession(
            skip_auto_headers={hdrs.USER_AGENT, hdrs.ACCEPT, hdrs.ACCEPT_ENCODING}
        )
        url = f"wss://{self._host}:{self._port}/"
        self._ws = await self._session.ws_connect(
            url,
            ssl=self._ssl_context(),
            protocols=[RPM_SUBPROTOCOL],
            # The app's upgrade request carries Origin: wss://<host>:<port>
            # (PROTOCOL.md §4.3); match it.
            origin=f"wss://{self._host}:{self._port}",
            receive_timeout=15.0,
        )
        _log_tls_info(self._ws)
        LOGGER.info(
            "Savant WS connected to %s:%s (subprotocol=%s, aiohttp=%s)",
            self._host,
            self._port,
            self._ws.protocol,
            aiohttp.__version__,
        )
        self._connected = True
        self._authorized = False
        self._auth_response_seen = False
        self._device_recognized = False
        self._authentication_required = True
        self._post_auth_done = False
        if self.on_status is not None:
            self.on_status(True)
        # Session handshake (PROTOCOL.md §4.0/§4.1): devicePresent MUST be sent first;
        # the host answers deviceRecognized (carrying the `authentication` flag) and only
        # then accepts authenticationRequest.  The auth flow is therefore deferred to
        # ``_run_auth_flow``, which waits for deviceRecognized before sending credentials.
        await self._send_device_present()
        self._auth_task = asyncio.ensure_future(self._run_auth_flow())

    async def _send_device_present(self) -> None:
        # A local-only session omits cloudToken/configurationID (PROTOCOL.md §4.1) —
        # include them only when the user actually supplied them.
        message: dict[str, Any] = {
            "protocolVersion": "4",
            "homeId": self._home_id,
            "device": {
                "UID": self._uid,
                "make": DEVICE_MAKE,
                "app": DEVICE_APP,
                "model": DEVICE_MODEL,
                "OS": DEVICE_OS,
                "type": DEVICE_TYPE,
                "name": DEVICE_NAME,
            },
            "messageFormat": 2,
        }
        if self._cloud_token:
            message["cloudToken"] = self._cloud_token
        if self._configuration_id:
            message["configurationID"] = self._configuration_id
        await self.request(URI_DEVICE_PRESENT, [message])

    async def _send_auth_request(self) -> None:
        # One of two observed forms (PROTOCOL.md §4.1): a cached hostToken, or the
        # local-login form {user, password}.
        if self._host_token:
            await self.request(URI_AUTH_REQUEST, [{"hostToken": self._host_token}])
        elif self._username and self._password:
            await self.request(
                URI_AUTH_REQUEST,
                [{"user": self._username, "password": self._password}],
            )

    async def _run_auth_flow(self) -> None:
        # PROTOCOL.md §4.0/§4.1: devicePresent -> (host) deviceRecognized ->
        # authenticationRequest (only if the `authentication` flag is set) ->
        # authenticationResponse -> state/dashboard registration.
        try:
            deadline = time.monotonic() + AUTH_TIMEOUT
            while not self._device_recognized and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if not self._device_recognized:
                LOGGER.warning(
                    "Savant: host did not answer devicePresent (no deviceRecognized)"
                )
                return
            if self._authentication_required:
                await self._send_auth_request()
                deadline = time.monotonic() + AUTH_TIMEOUT
                while not self._auth_response_seen and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                if not self._auth_response_seen:
                    LOGGER.warning("Savant: host did not answer authenticationRequest")
                    return
                if not self._authorized:
                    return
            elif not self._authorized:
                LOGGER.warning("Savant: deviceRecognized did not authorize the session")
                return
            await self._post_auth()
        except (SavantError, aiohttp.ClientError, OSError, TimeoutError):
            pass

    async def _request_uiconfig(self) -> None:
        # Download the authoritative config archive through the shared transfer framing.
        self._uiconfig_archive = await self._download_file_transfer(
            [{"filePath": "uiconfig.tar.gz"}],
            expected_label=b"uiconfig.tar.gz",
            timeout=FILE_TRANSFER_TIMEOUT,
        )
        if self._uiconfig_archive is None:
            LOGGER.warning("Savant config archive download did not complete")

    async def _post_auth(self) -> None:
        if self._post_auth_done:
            return
        self._post_auth_done = True
        # The native local-login startup requests the archive before subscribing, then
        # registers RecentServices before the state batch (sibling PROTOCOL.md §6.7).
        # Its causality is not yet isolated, but this replaces our prior undocumented
        # empty dashboard registration with the observed session sequence.
        if self._authorized:
            await self._request_uiconfig()
        await self._send_dashboard_register()
        # The native client registers this global key once before, and again in, its
        # main state batch (sibling PROTOCOL.md §6.7).
        await self.request(URI_STATE_REGISTER, [{"state": "global.SonosGroups"}])
        await self._send_user_data_register(USER_DATA_INITIAL_STATES)
        await self._send_state_register()
        await self.request(URI_DCM_REQUEST, [{"request": "getMode"}])
        await self._send_user_data_register(USER_DATA_IMAGE_STATES)

    async def _send_state_register(self) -> None:
        # Subscribe by explicit dotted key (PROTOCOL.md §3/§5).  Rooms are discovered
        # at runtime and subscribed to via :meth:`register_state_keys`.
        if self._subscribed_keys:
            await self.request(
                URI_STATE_REGISTER,
                [{"state": key} for key in sorted(self._subscribed_keys)],
            )

    async def register_state_keys(self, keys: list[str] | set[str]) -> None:
        """Subscribe to additional state keys (e.g. newly discovered rooms)."""
        new = [k for k in keys if k not in self._subscribed_keys]
        if not new or not self.connected:
            return
        self._subscribed_keys.update(new)
        try:
            await self.request(
                URI_STATE_REGISTER, [{"state": key} for key in sorted(new)]
            )
        except (SavantError, aiohttp.ClientError, OSError):
            # Reconnect will re-register from ``_subscribed_keys``.
            LOGGER.debug("Failed to register additional state keys (reconnecting?)")

    async def _send_dashboard_register(self) -> None:
        # ``scenesAndFoldersReduced`` delivers the full live scene inventory, while
        # ``RecentServices`` matches the native startup registration (PROTOCOL.md §9.1).
        await self.request(
            URI_DASHBOARD_REGISTER,
            [
                {"state": DASHBOARD_STATE_RECENT_SERVICES},
                {"state": SCENES_STATE_KEY},
            ],
        )

    async def _send_user_data_register(self, states: tuple[str, ...]) -> None:
        """Register the observed user-data update channels."""
        for state in states:
            await self.request(URI_USER_DATA_REGISTER, [{"state": state}])

    async def _disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._authorized = False
        for transfer in self._file_transfers.values():
            if transfer.future is not None and not transfer.future.done():
                transfer.future.set_result(None)
        self._file_transfers.clear()
        for future in self._pending_scene_requests.values():
            if not future.done():
                future.set_exception(SavantConnectionError("connection lost"))
        self._pending_scene_requests.clear()
        for future in self._pending_music_requests.values():
            if not future.done():
                future.set_exception(SavantConnectionError("connection lost"))
        self._pending_music_requests.clear()
        for future in self._pending_music_search_refresh.values():
            if not future.done():
                future.set_exception(SavantConnectionError("connection lost"))
        self._pending_music_search_refresh.clear()
        if self._auth_task is not None:
            self._auth_task.cancel()
            # CancelledError is a BaseException — suppress it explicitly too.
            with suppress(asyncio.CancelledError, Exception):
                await self._auth_task
            self._auth_task = None
        ws, self._ws = self._ws, None
        session, self._session = self._session, None
        if ws is not None and not ws.closed:
            with suppress(Exception):
                await ws.close()
        if session is not None:
            with suppress(Exception):
                await session.close()
        if was_connected and self.on_status is not None:
            self.on_status(False)

    async def _receive_loop(self) -> None:
        keepalive = asyncio.ensure_future(self._keepalive())
        ws = self._ws
        try:
            while ws is not None and not ws.closed:
                msg = await ws.receive()
                if msg.type is WSMsgType.BINARY:
                    try:
                        self._handle_frame(msg.data)
                    except Exception:  # noqa: BLE001 - never die on a bad frame
                        LOGGER.exception("Failed to decode Savant frame")
                elif msg.type is WSMsgType.TEXT:
                    LOGGER.warning("Savant <- unexpected TEXT frame (%d bytes)", len(msg.data))
                elif msg.type is WSMsgType.PING:
                    LOGGER.debug("Savant <- WS ping")
                elif msg.type is WSMsgType.PONG:
                    LOGGER.debug("Savant <- WS pong")
                elif msg.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                ):
                    LOGGER.info("Savant WS closing: %s", msg.type.name)
                    break
                else:
                    LOGGER.debug("Savant <- WS msg type=%s", msg.type.name)
        finally:
            keepalive.cancel()
            # CancelledError is a BaseException — suppress it explicitly too.
            with suppress(asyncio.CancelledError, Exception):
                await keepalive

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._ws is None or self._ws.closed:
                return
            await self._ws.ping(KEEPALIVE_BYTE)

    def _handle_frame(self, data: bytes) -> None:
        if data[:1] == b"\x01" and len(data) >= 2 and data[1] in (0x01, 0x81):
            transfer = parse_file_transfer_message(data)
            if transfer is None:
                LOGGER.warning("Savant <- malformed file-transfer message")
                return
            self._handle_file_transfer_message(transfer)
            return
        # Host maps are gzip-compressed MessagePack; transfers use the branch above.
        payload = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        obj = msgpack.unpackb(payload, raw=False)
        if not isinstance(obj, dict):
            LOGGER.warning("Savant <- undecodable frame: %r", _redact(obj))
            return
        uri = obj.get(ENVELOPE_KEY_URI, "")
        messages = obj.get(ENVELOPE_KEY_MESSAGES, []) or []
        LOGGER.debug("Savant <- %s (%d message(s))", uri, len(messages))

        if uri == URI_STATE_UPDATE:
            for message in messages:
                if isinstance(message, dict) and "state" in message:
                    state = message.get("state")
                    if isinstance(state, str) and self.on_state_update is not None:
                        self.on_state_update(state, message.get("value"))
                    if isinstance(state, str):
                        self._handle_music_search_refresh(state, message.get("value"))
        elif uri == URI_DEVICE_RECOGNIZED:
            self._handle_device_recognized(messages)
        elif uri == URI_AUTH_RESPONSE:
            self._handle_auth_response(messages)
        elif uri == URI_FEATURE_LOCK:
            # License/entitlement gating (PROTOCOL.md §12) — informational only.
            LOGGER.debug("Savant featureLock: %s", _redact(messages))
        elif uri in (URI_DASHBOARD_UPDATE, URI_DASHBOARD_REQUEST):
            self._emit_rooms(rooms_from_scene_messages(messages))
            if uri == URI_DASHBOARD_REQUEST:
                self._handle_scene_activation_response(messages)
            if uri == URI_DASHBOARD_UPDATE:
                scenes = scene_summaries_from_messages(messages)
                if scenes is not None and self.on_scenes_update is not None:
                    self.on_scenes_update(scenes)
        elif uri.startswith("music/"):
            self._handle_music_response(uri, messages)
        else:
            LOGGER.debug("Unhandled Savant URI %r", uri)

    def _handle_file_transfer_message(self, message: FileTransferMessage) -> None:
        """Append one correlated transfer message and enforce audited completion."""
        state = self._file_transfers.get(message.label)
        if state is None:
            LOGGER.debug("Savant <- ignored transfer with no matching request")
            return
        if state.declared_length is None:
            state.declared_length = message.declared_length
        elif state.declared_length != message.declared_length:
            state.invalid = True
        if not state.invalid:
            state.body.extend(message.body)
            if len(state.body) > message.declared_length:
                state.invalid = True
        if not message.final:
            return
        complete = (
            not state.invalid
            and state.declared_length is not None
            and len(state.body) == state.declared_length
        )
        result = bytes(state.body) if complete else None
        if state.future is not None and not state.future.done():
            state.future.set_result(result)
        elif state.future is None:
            self._file_transfers.pop(message.label, None)

    def _handle_device_recognized(self, messages: list[Any]) -> None:
        # Host's answer to devicePresent (PROTOCOL.md §4.0): carries the
        # `authentication` flag (true => the client must log in) and host metadata.
        self._device_recognized = True
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        self._authentication_required = bool(first.get("authentication", True))
        if not self._authentication_required:
            self._authorized = bool(first.get("authorized", False))
        LOGGER.info(
            "Savant deviceRecognized (authentication=%s)",
            self._authentication_required,
        )

    def _handle_auth_response(self, messages: list[Any]) -> None:
        self._auth_response_seen = True
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        self._authorized = bool(first.get("authorized", False))
        if self._authorized:
            LOGGER.info("Savant login successful")
        else:
            reason = first.get("errorReason") or first.get("errorCode")
            LOGGER.warning("Savant login denied: %s", reason or "unknown reason")
        # startZone names the room the session opens in (PROTOCOL.md §4.1/§6.1); it is
        # optional and may be absent.
        start_zone = first.get("startZone")
        if isinstance(start_zone, str) and start_zone:
            self._emit_rooms({start_zone})

    def _handle_scene_activation_response(self, messages: list[Any]) -> None:
        """Resolve observed ``ApplyScene`` dashboard RPC responses (PROTOCOL.md §9.3)."""
        for message in messages:
            if not isinstance(message, dict) or message.get("request") != DASHBOARD_REQUEST_APPLY_SCENE:
                continue
            request_id = message.get("requestId")
            if not isinstance(request_id, str):
                continue
            future = self._pending_scene_requests.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)

    def _handle_music_response(self, uri: str, messages: list[Any]) -> None:
        """Resolve a correlated Music RPC response on its request URI (PROTOCOL.md §8)."""
        for message in messages:
            if not isinstance(message, dict):
                continue
            request_id = message.get("requestId")
            if not isinstance(request_id, str):
                continue
            future = self._pending_music_requests.get((uri, request_id))
            if future is not None and not future.done():
                future.set_result(message)

    def _handle_music_search_refresh(self, state: str, value: Any) -> None:
        """Resolve typed-search readiness from the observed refresh state families."""
        if not (state.endswith(".refreshLMQ") or state.endswith(".refreshLMQ3")):
            return
        if not isinstance(value, str):
            return
        for (prefix, search_uuid), future in self._pending_music_search_refresh.items():
            if state.startswith(prefix) and search_uuid in value and not future.done():
                future.set_result(None)

    def _emit_rooms(self, rooms: set[str]) -> None:
        rooms = {r for r in rooms if isinstance(r, str) and r}
        if rooms and self.on_rooms_discovered is not None:
            self.on_rooms_discovered(rooms)


def rooms_from_scene_messages(messages: list[Any]) -> set[str]:
    """Extract room names from scene-list messages (PROTOCOL.md §6.1/§9).

    Scene objects embed room names in ``definition.power.rooms`` (a map keyed by every
    room — the most complete single source), ``power.lightingOff``,
    ``av.<zone>.rooms`` and ``lighting.<host>.rooms``.  This scans any ``rooms`` /
    ``lightingOff`` key found under a scene message; only strings are kept.
    """
    rooms: set[str] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("rooms", "lightingOff"):
                value = obj.get(key)
                if isinstance(value, dict):
                    rooms.update(r for r in value if isinstance(r, str))
                elif isinstance(value, list):
                    rooms.update(r for r in value if isinstance(r, str))
            for value in obj.values():
                _walk(value)
        elif isinstance(obj, list):
            for value in obj:
                _walk(value)

    for message in messages:
        _walk(message)
    return {r for r in rooms if r}


def scene_summaries_from_messages(messages: list[Any]) -> dict[str, dict[str, Any]] | None:
    """Return the complete dashboard scene list from a subscription update.

    The host pushes the list as the value of ``scenesAndFoldersReduced``.  An empty list
    is a valid authoritative inventory; ``None`` means this frame was unrelated or malformed.
    """
    for message in messages:
        if not isinstance(message, dict) or message.get("state") != SCENES_STATE_KEY:
            continue
        value = message.get("value")
        if not isinstance(value, list):
            return None
        return {
            scene_id: scene
            for scene in value
            if isinstance(scene, dict)
            and isinstance((scene_id := scene.get("id")), str)
            and scene_id
            and isinstance(scene.get("name"), str)
            and scene["name"]
        }
    return None
