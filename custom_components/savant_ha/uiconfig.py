"""Parse the host's ``uiconfig.tar.gz`` archive into a device inventory.

The authoritative room/load/device inventory is downloaded as a file, not pushed over
the state bus (PROTOCOL.md §13 of the sibling repo):

    URI: session/fileDownload   messages: [{ "filePath": "uiconfig.tar.gz" }]

The host replies with a sequence of WebSocket **binary** frames carrying a *framed gzip
archive* (not msgpack).  ``uiconfig.tar.gz`` is a gzip'd tar whose members include
``serviceImplementation.sqlite`` — the complete system model (39 tables).

The device model here is driven by introspection (``PRAGMA table_info`` / ``sqlite_master``)
rather than hard-coded column names, because the exact schema varies per host.  The
column-name heuristics are taken from the documented schema (PROTOCOL.md §13).
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .const import LOGGER

# Frame flags in the framed archive (PROTOCOL.md §13).
_ARCHIVE_MARKER = 0x01
_ARCHIVE_DATA = 0x01
_ARCHIVE_FINAL = 0x81

# The member of the tar we care about.
_SQLITE_MEMBER = "serviceImplementation.sqlite"

# Entity table name -> HA device type (the tables are ``<Foo>Entities``).
_ENTITY_TABLE_TYPES = {
    "Light": "light",
    "Shade": "cover",
    "Fan": "fan",
    "HVAC": "climate",
    "DoorLock": "lock",
    "Garage": "cover",
}


@dataclass
class SavantDevice:
    """One controllable device from the config archive."""

    device_type: str  # "light", "climate", "cover", "fan", ...
    name: str
    room: str
    entity_id: str = ""
    addresses: str = ""
    state_name: str = ""
    zone: str = ""
    component: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stable_id(self) -> str:
        """A stable, unique identifier for the device."""
        if self.entity_id:
            return self.entity_id
        import hashlib

        base = f"{self.device_type}|{self.name}|{self.addresses}|{self.state_name}|{self.room}"
        return hashlib.sha1(base.encode()).hexdigest()[:16]


def reassemble_archive(frames: list[bytes]) -> bytes:
    """Reassemble the framed gzip archive from raw WS binary frames (PROTOCOL.md §13).

    Frame layout: byte 0 = 0x01; byte 1 = 0x01 (data) / 0x81 (final); bytes 2-7
    reserved; bytes 8-9 = total gzip length (BE uint16); bytes 10-12 reserved;
    byte 13 = filename length; then the filename; then the gzip bytes.
    """
    parts: list[bytes] = []
    for frame in frames:
        if len(frame) < 14 or frame[0] != _ARCHIVE_MARKER:
            continue
        flag = frame[1]
        filename_len = frame[13]
        data = frame[14 + filename_len :]
        if data:
            parts.append(data)
        if flag == _ARCHIVE_FINAL:
            break
    return b"".join(parts)


def parse_archive(gz_bytes: bytes) -> list[SavantDevice]:
    """Decompress the tar and parse ``serviceImplementation.sqlite`` into devices."""
    if not gz_bytes:
        return []
    try:
        tar = tarfile.open(fileobj=io.BytesIO(gz_bytes), mode="r:gz")
    except (tarfile.TarError, OSError):
        return []
    with tar:
        sqlite_bytes: bytes | None = None
        for member in tar.getmembers():
            if member.name.endswith(_SQLITE_MEMBER) and member.isfile():
                extracted = tar.extractfile(member)
                if extracted is not None:
                    sqlite_bytes = extracted.read()
                break
    if not sqlite_bytes:
        return []
    return parse_sqlite(sqlite_bytes)


def _row_to_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    names = [d[0] for d in cursor.description]
    return dict(zip(names, row, strict=True))


def parse_sqlite(sqlite_bytes: bytes) -> list[SavantDevice]:
    """Read ``serviceImplementation.sqlite`` and enumerate devices."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        os.write(fd, sqlite_bytes)
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            devices = _parse_connection(conn)
        finally:
            conn.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.device_type] = counts.get(device.device_type, 0) + 1
    LOGGER.info("Savant uiconfig parsed %d device(s): %s", len(devices), counts)
    return devices


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _pick(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _parse_connection(conn: sqlite3.Connection) -> list[SavantDevice]:
    # PROTOCOL.md §13.1/§13.2: ``zoneID`` is an integer reference to ``Zones.id``.
    # Zones.id (int) -> (name, type, serviceID, logicalComponent)
    zones: dict[Any, tuple[str, str, str, str]] = {}
    for row in conn.execute(
        "SELECT id, name, type, serviceID, logicalComponent FROM Zones"
    ):
        zones[row[0]] = (
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or ""),
            str(row[4] or ""),
        )

    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]

    entity_tables = [t for t in tables if t.endswith("Entities")]
    LOGGER.debug(
        "Savant uiconfig entity tables: %s",
        {t: _table_columns(conn, t) for t in sorted(entity_tables)},
    )

    def _room_for(zone_id: Any) -> str:
        # The Environmental zone's name IS the room name (one zone per room per service,
        # e.g. "Office", "Dining Room").  ZoneRoomMap is NOT usable here — HVAC zones are
        # shared across every room (many-to-many), so a 1:1 zone->room map is wrong.
        return zones.get(zone_id, ("", "", "", ""))[0]

    devices: list[SavantDevice] = []
    for table in sorted(tables):
        device_type = _type_for(table)
        if device_type is None:
            continue
        cols = _table_columns(conn, table)
        if not cols:
            continue
        name_col = _pick(cols, ("name",))
        addr_col = _pick(cols, ("addresses",))
        state_col = _pick(cols, ("stateName", "state_name"))
        zone_col = _pick(cols, ("zoneID", "zoneId", "zone_id"))
        entity_type_col = _pick(cols, ("entityType", "entity_type"))

        cursor = conn.execute(f'SELECT * FROM "{table}"')
        for row in cursor:
            d = _row_to_dict(cursor, row)
            name = str(d.get(name_col) or "") if name_col else ""
            if not name:
                continue
            # LightEntities also contains keypad LEDs and scene indicators. They have
            # entityType "Scene" (often CurrentLEDState/IsSceneActive), not a
            # controllable lighting load, so importing them creates misleading entries
            # such as "AUX B" and "CCW" (PROTOCOL.md §13.2).
            if device_type == "light" and str(d.get(entity_type_col) or "") == "Scene":
                continue
            zone_id = d.get(zone_col) if zone_col else None
            devices.append(
                SavantDevice(
                    device_type=device_type,
                    name=name,
                    room=_room_for(zone_id) if zone_id is not None else "",
                    entity_id=f"{device_type}:{d.get('id')}",
                    addresses=str(d.get(addr_col) or "") if addr_col else "",
                    state_name=str(d.get(state_col) or "") if state_col else "",
                    zone=zones.get(zone_id, ("", "", "", ""))[0] if zone_id is not None else "",
                    extra=(
                        {
                            "entity_type": str(d.get(entity_type_col) or ""),
                            "dimmer_command": str(d.get(_pick(cols, ("dimmerCommand",))) or ""),
                            "fade_time": d.get(_pick(cols, ("fadeTime",))),
                            "delay_time": d.get(_pick(cols, ("delayTime",))),
                            "technology": str(d.get(_pick(cols, ("technology",))) or ""),
                        }
                        if device_type == "light"
                        else {}
                    ),
                )
            )

    _parse_media_zones(conn, tables, devices)
    return devices


def _type_for(table: str) -> str | None:
    for prefix, dtype in _ENTITY_TABLE_TYPES.items():
        if table.endswith("Entities") and table.startswith(prefix):
            return dtype
    return None


def _parse_media_zones(
    conn: sqlite3.Connection,
    tables: list[str],
    devices: list[SavantDevice],
) -> None:
    # Audio zones are the music service's "Audio Zone N" zoned services in
    # ServiceImplementationServiceResources. The `zone` column is the room;
    # `component`/`logicalComponent` identify the audio zone (zone numbers are
    # per-component). A zone may be reached through a nonzero pathOrder, so deduplicate
    # the route rows instead of assuming the endpoint always has pathOrder 0.
    sir = "ServiceImplementationServiceResources"
    if sir not in tables:
        return
    cols = _table_columns(conn, sir)
    lc_col = _pick(cols, ("logicalComponent", "logical_component"))
    svc_col = _pick(cols, ("serviceType", "service_type"))
    zone_col = _pick(cols, ("zone",))
    comp_col = _pick(cols, ("component",))
    if not (lc_col and svc_col and zone_col):
        return
    seen: set[tuple[str, str]] = set()
    cursor = conn.execute(f'SELECT * FROM "{sir}"')
    for row in cursor:
        d = _row_to_dict(cursor, row)
        if str(d.get(svc_col) or "") != "SVC_AV_SAVANTMUSIC":
            continue
        logical = str(d.get(lc_col) or "")
        if not logical.startswith("Audio Zone"):
            continue
        component = str(d.get(comp_col) or "")
        room = str(d.get(zone_col) or "")
        key = (component, logical)
        if key in seen:
            continue
        seen.add(key)
        name = f"{component} {logical}".strip() if component else logical
        devices.append(
            SavantDevice(
                device_type="media_player",
                name=name,
                room=room,
                entity_id=f"media_player:{d.get('id')}",
                zone=logical,
                component=component,
                extra={"service_id": "SVC_AV_SAVANTMUSIC"},
            )
        )
