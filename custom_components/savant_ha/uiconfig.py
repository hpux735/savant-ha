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
    # roomID -> name
    rooms: dict[Any, str] = {}
    room_cols = _table_columns(conn, "Rooms")
    if room_cols:
        id_col = _pick(room_cols, ("roomID", "roomId", "id"))
        name_col = _pick(room_cols, ("name",))
        if id_col and name_col:
            for row in conn.execute(f'SELECT "{id_col}", "{name_col}" FROM Rooms'):
                rooms[row[0]] = str(row[1])

    # zoneID -> roomID
    zone_room: dict[Any, Any] = {}
    zrm_cols = _table_columns(conn, "ZoneRoomMap")
    if zrm_cols:
        z_col = _pick(zrm_cols, ("zoneID", "zoneId", "zone_id"))
        r_col = _pick(zrm_cols, ("roomID", "roomId", "room_id"))
        if z_col and r_col:
            for row in conn.execute(f'SELECT "{z_col}", "{r_col}" FROM ZoneRoomMap'):
                zone_room[row[0]] = row[1]

    # zoneID -> zone name
    zone_name: dict[Any, str] = {}
    zone_cols = _table_columns(conn, "Zones")
    if zone_cols:
        zid_col = _pick(zone_cols, ("zoneID", "zoneId", "id"))
        zname_col = _pick(zone_cols, ("name",))
        if zid_col and zname_col:
            for row in conn.execute(f'SELECT "{zid_col}", "{zname_col}" FROM Zones'):
                zone_name[row[0]] = str(row[1])

    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]

    # DEBUG: dump the entity-table schema so the column heuristics can be verified
    # against the real host (the schema is host-specific and undocumented in detail).
    entity_tables = [t for t in tables if t.endswith("Entities")]
    LOGGER.debug(
        "Savant uiconfig entity tables: %s",
        {t: _table_columns(conn, t) for t in sorted(entity_tables)},
    )

    devices: list[SavantDevice] = []
    for table in sorted(tables):
        device_type = None
        for prefix, dtype in _ENTITY_TABLE_TYPES.items():
            if table.endswith("Entities") and table.startswith(prefix):
                device_type = dtype
                break
        if device_type is None:
            continue

        cols = _table_columns(conn, table)
        if not cols:
            continue
        name_col = _pick(cols, ("name",))
        addr_col = _pick(cols, ("addresses", "address"))
        state_col = _pick(cols, ("stateName", "state_name", "state"))
        zone_col = _pick(cols, ("zoneID", "zoneId", "zone_id"))
        room_col = _pick(cols, ("roomID", "roomId", "room_id"))
        id_col = _pick(cols, ("entityID", "entityId", "id", "uuid", "UID"))

        select = ", ".join(f'"{c}"' for c in cols)
        cursor = conn.execute(f"SELECT {select} FROM {table}")
        for row in cursor:
            d = _row_to_dict(cursor, row)
            name = str(d.get(name_col) or "") if name_col else ""
            room = ""
            # direct room column
            if room_col and d.get(room_col) is not None:
                room = rooms.get(d[room_col], str(d[room_col]))
            # via zone link
            if not room and zone_col and d.get(zone_col) is not None:
                room_id = zone_room.get(d[zone_col])
                room = rooms.get(room_id, str(room_id or ""))
            if not room and zone_col and d.get(zone_col) is not None:
                zone = zone_name.get(d[zone_col], "")
                if zone:
                    room = zone.split("-", 1)[0].strip()

            if not name:
                continue
            entity_id = str(d.get(id_col) or "") if id_col else ""
            devices.append(
                SavantDevice(
                    device_type=device_type,
                    name=name,
                    room=room,
                    entity_id=entity_id,
                    addresses=str(d.get(addr_col) or "") if addr_col else "",
                    state_name=str(d.get(state_col) or "") if state_col else "",
                    zone=zone_name.get(d.get(zone_col), "") if zone_col else "",
                )
            )

    # AV / media zones are stored in the Zones table (type "User"), not an Entities
    # table.  Expose them as media players.
    if zone_cols:
        zid_col = _pick(zone_cols, ("zoneID", "zoneId", "id"))
        zname_col = _pick(zone_cols, ("name",))
        ztype_col = _pick(zone_cols, ("type", "zoneType", "zone_type"))
        zsvc_col = _pick(zone_cols, ("serviceID", "serviceId", "service_id"))
        select = ", ".join(f'"{c}"' for c in zone_cols)
        cursor = conn.execute(f"SELECT {select} FROM Zones")
        for row in cursor:
            d = _row_to_dict(cursor, row)
            if ztype_col and d.get(ztype_col) not in (None, "User"):
                continue
            zone_id = d.get(zid_col)
            name = str(d.get(zname_col) or "") if zname_col else ""
            if not name:
                continue
            room_id = zone_room.get(zone_id)
            room = rooms.get(room_id, str(room_id or ""))
            service = str(d.get(zsvc_col) or "") if zsvc_col else ""
            devices.append(
                SavantDevice(
                    device_type="media_player",
                    name=name,
                    room=room,
                    entity_id=str(zone_id or ""),
                    zone=name,
                    extra={"service_id": service},
                )
            )

    return devices
