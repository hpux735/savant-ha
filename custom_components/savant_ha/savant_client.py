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
import base64
import gzip
import socket
import ssl
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import msgpack
from aiohttp import WSMsgType

from .const import (
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
    KEEPALIVE_BYTE,
    KEEPALIVE_INTERVAL,
    LOGGER,
    RPM_SUBPROTOCOL,
    URI_AUTH_REQUEST,
    URI_DEVICE_PRESENT,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
)

_StateCallback = Callable[[str, Any], None]
_StatusCallback = Callable[[bool], None]


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
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto, local_addr=("0.0.0.0", 0), family=socket.AF_INET
    )
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

    # Prefer a record carrying a control port; fall back to any presence record.
    for addr, record in proto.results.items():
        if "port" in record:
            return SavantHostInfo.from_record(addr, record)
    for _, record in proto.results.items():
        if "homeId" in record or "name" in record:
            return SavantHostInfo.from_record(host, record)
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
        # ASSUMPTION (PROTOCOL.md §7.1): if no explicit hostToken is supplied but a
        # local username/password is, fall back to base64(username:password).  The true
        # hostToken derivation has not been observed.
        if self._host_token is None and username and password:
            self._host_token = base64.b64encode(
                f"{username}:{password}".encode()
            ).decode()
        self._subscribe_keys = list(subscribe_keys or [])
        self._reconnect_delay = reconnect_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._stopping = False
        self._authorized = False

        self.on_state_update: _StateCallback | None = None
        self.on_status: _StatusCallback | None = None

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def authorized(self) -> bool:
        return self._authorized

    async def run_forever(self) -> None:
        """Connect and process frames, reconnecting with backoff on any failure."""
        delay = self._reconnect_delay
        while not self._stopping:
            try:
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
            LOGGER.info("Reconnecting to Savant host in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._reconnect_max_delay)

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
        ctx = ssl.create_default_context()
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
        if self.on_status is not None:
            self.on_status(True)
        # Session handshake (PROTOCOL.md §3).
        await self._send_device_present()
        await self._send_auth_request()
        await self._send_state_register()

    async def _send_device_present(self) -> None:
        message = {
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
            "cloudToken": self._cloud_token,
            "configurationID": self._configuration_id,
        }
        await self.request(URI_DEVICE_PRESENT, [message])

    async def _send_auth_request(self) -> None:
        # Only present a hostToken if we have one (PROTOCOL.md §3/§7.1).
        if self._host_token:
            await self.request(URI_AUTH_REQUEST, [{"hostToken": self._host_token}])

    async def _send_state_register(self) -> None:
        # Subscribe by explicit dotted key (PROTOCOL.md §3/§5).  The key list is
        # supplied by the hub; unknown room names cannot be enumerated ahead of time.
        if self._subscribe_keys:
            await self.request(
                URI_STATE_REGISTER,
                [{"state": key} for key in self._subscribe_keys],
            )

    async def _disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._authorized = False
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
        elif uri == "session/authenticationResponse":
            first = messages[0] if messages and isinstance(messages[0], dict) else {}
            self._authorized = bool(first.get("authorized", False))
            LOGGER.debug("Savant authentication %s", "ok" if self._authorized else "denied")
