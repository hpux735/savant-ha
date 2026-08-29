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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import msgpack
from aiohttp import WSMsgType

from .const import (
    DASHBOARD_REQUEST_SCENES,
    DEVICE_APP,
    DEVICE_MAKE,
    DEVICE_TYPE,
    DISCOVERY_PORT_CONTROL,
    DISCOVERY_PORT_PRESENCE,
    DISCOVERY_SERVICE_CONTROL,
    DISCOVERY_SERVICE_PRESENCE,
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
    URI_AUTH_REQUEST,
    URI_AUTH_RESPONSE,
    URI_DASHBOARD_REGISTER,
    URI_DASHBOARD_REQUEST,
    URI_DASHBOARD_UPDATE,
    URI_DEVICE_PRESENT,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
    build_default_subscribe_keys,
    room_from_state_key,
)

_StateCallback = Callable[[str, Any], None]
_StatusCallback = Callable[[bool], None]
_RoomsCallback = Callable[[set[str]], None]

# How long to wait for the host to authorize us before registering state anyway.
AUTH_TIMEOUT = 5.0


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
    """The discoverable device surface collected during setup (PROTOCOL.md §6.1).

    Rooms come from scene definitions / ``startZone``; HVAC units and audio zones from
    the subscribed state keys.
    """

    rooms: set[str] = field(default_factory=set)
    hvac_suffixes: set[str] = field(default_factory=set)
    zones: set[int] = field(default_factory=set)
    authorized: bool = False


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
    await client._connect()
    receive = asyncio.ensure_future(client._receive_loop())
    try:
        await asyncio.sleep(timeout)
    finally:
        receive.cancel()
        with suppress(Exception):
            await receive
        await client._disconnect()
    info.authorized = client.authorized
    return info


def _pack(obj: Any) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def _encode_discovery(service: str) -> bytes:
    return _pack({"service": service})


async def discover_host(host: str, timeout: float = 3.0) -> SavantHostInfo | None:
    """Discover a host's control port / homeId over UDP (PROTOCOL.md §1.1).

    Broadcasts a tiny msgpack ``{"service": "_control_.ws"}`` query to UDP 9101 (and
    ``_presence_.ws`` to 9103), then reads the host's reply.  Unicast to ``host`` and a
    broadcast to ``255.255.255.255`` are both attempted because the target's broadcast
    address is not known ahead of time.
    """

    class _Proto(asyncio.DatagramProtocol):
        def __init__(self) -> None:
            self.results: dict[str, dict[str, Any]] = {}

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
            try:
                obj = msgpack.unpackb(data, raw=False)
            except Exception:  # noqa: BLE001 - tolerate malformed replies
                return
            if isinstance(obj, dict):
                self.results.setdefault(addr[0], obj)

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
    targets = {
        (host, DISCOVERY_PORT_CONTROL),
        (host, DISCOVERY_PORT_PRESENCE),
        ("255.255.255.255", DISCOVERY_PORT_CONTROL),
        ("255.255.255.255", DISCOVERY_PORT_PRESENCE),
    }
    try:
        for target in targets:
            for service in (DISCOVERY_SERVICE_CONTROL, DISCOVERY_SERVICE_PRESENCE):
                try:
                    transport.sendto(_encode_discovery(service), target)
                except OSError:
                    pass
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    LOGGER.debug(
        "Savant discovery: %d raw reply(ies) for %s: %s",
        len(proto.results),
        host,
        {addr: {k: v for k, v in rec.items() if k not in _REDACT_KEYS}
         for addr, rec in proto.results.items()},
    )
    if not proto.results:
        LOGGER.warning(
            "Savant discovery: no reply on UDP 9101/9103 for %s — is Home Assistant "
            "on the same network (L2) as the host?",
            host,
        )
    # Prefer the record from the queried host address (other Savant hosts/proxies may
    # answer the same broadcast); fall back to any record with a valid control port
    # (PROTOCOL.md §1.1).

    def _record_port(record: dict[str, Any]) -> int:
        try:
            return int(record.get("port", 0))
        except (TypeError, ValueError):
            return 0

    own = proto.results.get(host)
    if own is not None and _record_port(own) > 0:
        return SavantHostInfo.from_record(host, own)
    for addr, record in proto.results.items():
        if addr == host:
            continue
        if _record_port(record) > 0:
            return SavantHostInfo.from_record(addr, record)
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
        uid: str | None = None,
        home_id: str = "",
        cloud_token: str = "",
        configuration_id: str = "",
        host_token: str | None = None,
        username: str = "",
        password: str = "",
        subscribe_keys: list[str] | None = None,
        reconnect_delay: float = 5.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._host = host
        self._port = port
        # Client device identity — generated once per config entry.
        self._uid = uid or str(uuid.uuid4())
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
        self._subscribed_keys: set[str] = set(self._subscribe_keys)
        self._reconnect_delay = reconnect_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._stopping = False
        self._authorized = False
        self._post_auth_done = False
        self._port_warned = False
        self._auth_task: asyncio.Task | None = None

        self.on_state_update: _StateCallback | None = None
        self.on_status: _StatusCallback | None = None
        self.on_rooms_discovered: _RoomsCallback | None = None

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def authorized(self) -> bool:
        return self._authorized

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

    async def _refresh_discovery(self) -> None:
        try:
            info = await discover_host(self._host, timeout=3.0)
        except (TimeoutError, OSError):
            return
        if info is None:
            if not self._port_warned:
                LOGGER.warning(
                    "Savant control port unknown for %s (UDP discovery found nothing); "
                    "will keep retrying",
                    self._host,
                )
                self._port_warned = True
            return
        self._port_warned = False
        if info.port != self._port:
            LOGGER.info("Savant control port changed %s -> %s", self._port, info.port)
        self._port = info.port
        if info.home_id:
            self._home_id = info.home_id

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
        await self._ws.send_bytes(_pack(envelope))

    async def service_request(
        self,
        request: str,
        *,
        component: str,
        service_type: str,
        zone: str = "",
        logical_component: str = "",
        variant_id: str = "",
        request_args: dict[str, Any] | None = None,
    ) -> None:
        """Emit a ``service/request`` control message (PROTOCOL.md §6)."""
        message: dict[str, Any] = {
            "component": component,
            "serviceType": service_type,
            "zone": zone,
            "logicalComponent": logical_component,
            "variantID": variant_id,
            "request": request,
        }
        if request_args:
            message["requestArgs"] = request_args
        await self.request("service/request", [message])

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
        self._session = aiohttp.ClientSession()
        url = f"wss://{self._host}:{self._port}/"
        self._ws = await self._session.ws_connect(
            url,
            ssl=self._ssl_context(),
            protocols=[RPM_SUBPROTOCOL],
            receive_timeout=15.0,
        )
        self._connected = True
        self._authorized = False
        self._post_auth_done = False
        if self.on_status is not None:
            self.on_status(True)
        # Session handshake (PROTOCOL.md §3).  State registration follows the auth
        # response on the credentialed path (§3 flow), so it is deferred to
        # ``_post_auth``.
        await self._send_device_present()
        await self._send_auth_request()
        if self._has_credentials:
            self._auth_task = asyncio.ensure_future(self._wait_and_post_auth())
        else:
            await self._post_auth()

    async def _send_device_present(self) -> None:
        # A local-only session omits cloudToken/configurationID (PROTOCOL.md §4.9) —
        # include them only when the user actually supplied them.
        message: dict[str, Any] = {
            "protocolVersion": "4",
            "homeId": self._home_id,
            "device": {
                "UID": self._uid,
                "make": DEVICE_MAKE,
                "app": DEVICE_APP,
                "model": DEVICE_TYPE,
                "OS": DEVICE_TYPE,
                "type": DEVICE_TYPE,
                "name": DEVICE_TYPE,
            },
            "messageFormat": 2,
        }
        if self._cloud_token:
            message["cloudToken"] = self._cloud_token
        if self._configuration_id:
            message["configurationID"] = self._configuration_id
        await self.request(URI_DEVICE_PRESENT, [message])

    async def _send_auth_request(self) -> None:
        # One of two observed forms (PROTOCOL.md §4): a cached/cloud hostToken, or the
        # local-login form {user, password}.
        if self._host_token:
            await self.request(URI_AUTH_REQUEST, [{"hostToken": self._host_token}])
        elif self._username and self._password:
            await self.request(
                URI_AUTH_REQUEST,
                [{"user": self._username, "password": self._password}],
            )

    async def _wait_and_post_auth(self) -> None:
        try:
            deadline = time.monotonic() + AUTH_TIMEOUT
            while not self._authorized and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            await self._post_auth()
        except (SavantError, aiohttp.ClientError, OSError, TimeoutError):
            pass

    async def _post_auth(self) -> None:
        if self._post_auth_done:
            return
        self._post_auth_done = True
        await self._send_state_register()
        await self._send_dashboard_register()
        await self._send_scenes_request()

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
        # Subscribe to dashboard pushes — the scene list embeds every room name
        # (PROTOCOL.md §6.1).
        # ASSUMPTION: the dis/<service>/register payload is an empty message map; the
        # exact register-payload shape has not been captured, but the active
        # GetAVAutomationScenes request below is observed and is the primary source.
        await self.request(URI_DASHBOARD_REGISTER, [{}])

    async def _send_scenes_request(self) -> None:
        # Actively fetch the scene list; the response also embeds room names.
        await self.request(URI_DASHBOARD_REQUEST, [{"request": DASHBOARD_REQUEST_SCENES}])

    async def _disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._authorized = False
        if self._auth_task is not None:
            self._auth_task.cancel()
            with suppress(Exception):
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
                elif msg.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                ):
                    break
        finally:
            keepalive.cancel()
            with suppress(Exception):
                await keepalive

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._ws is None or self._ws.closed:
                return
            await self._ws.ping(KEEPALIVE_BYTE)

    def _handle_frame(self, data: bytes) -> None:
        # host -> client is gzip-compressed msgpack (PROTOCOL.md §1), except the
        # un-compressed ``featureLock`` map.
        payload = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        obj = msgpack.unpackb(payload, raw=False)
        if not isinstance(obj, dict):
            return
        uri = obj.get(ENVELOPE_KEY_URI, "")
        messages = obj.get(ENVELOPE_KEY_MESSAGES, []) or []

        if uri == URI_STATE_UPDATE:
            for message in messages:
                if isinstance(message, dict) and "state" in message:
                    state = message.get("state")
                    if isinstance(state, str) and self.on_state_update is not None:
                        self.on_state_update(state, message.get("value"))
        elif uri == URI_AUTH_RESPONSE:
            self._handle_auth_response(messages)
        elif uri in (URI_DASHBOARD_UPDATE, URI_DASHBOARD_REQUEST):
            self._emit_rooms(rooms_from_scene_messages(messages))
        else:
            LOGGER.debug("Unhandled Savant URI %r", uri)

    def _handle_auth_response(self, messages: list[Any]) -> None:
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        self._authorized = bool(first.get("authorized", False))
        LOGGER.debug("Savant authentication %s", "ok" if self._authorized else "denied")
        # startZone names the room the session opens in (PROTOCOL.md §4.1/§6.1).
        start_zone = first.get("startZone")
        if isinstance(start_zone, str) and start_zone:
            self._emit_rooms({start_zone})

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
