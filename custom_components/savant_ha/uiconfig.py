"""Parse the host's ``uiconfig.tar.gz`` archive into a device inventory.

The authoritative room/load/device inventory is downloaded as a file, not pushed over
the state bus (PROTOCOL.md §13 of the sibling repo):

    URI: session/fileDownload   messages: [{ "filePath": "uiconfig.tar.gz" }]

The host replies with a shared framed binary transfer (parsed by ``savant_client``).
The completed body is a gzip'd tar whose members include
``serviceImplementation.sqlite`` — the complete system model (38 tables and one view).

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

# The member of the tar we care about.
_SQLITE_MEMBER = "serviceImplementation.sqlite"

# Entity table name -> HA device type (the tables are ``<Foo>Entities``).
_ENTITY_TABLE_TYPES = {
    "Light": "light",
    "Shade": "cover",
    "Fan": "fan",
    "HVAC": "climate",
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
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    ]

    rooms = {row[0]: str(row[1] or "") for row in conn.execute("SELECT id, name FROM Rooms")}
    zone_rooms: dict[Any, set[str]] = {}
    if "ZoneRoomMap" in tables:
        for zone_id, room_id in conn.execute("SELECT zoneID, roomID FROM ZoneRoomMap"):
            if room := rooms.get(room_id):
                zone_rooms.setdefault(zone_id, set()).add(room)

    entity_tables = [t for t in tables if t.endswith("Entities")]
    LOGGER.debug(
        "Savant uiconfig entity tables: %s",
        {t: _table_columns(conn, t) for t in sorted(entity_tables)},
    )

    def _room_for(zone_id: Any) -> str:
        # Suggest an area only when configuration has one unambiguous physical room.
        # Broad HVAC associations must not create areas from environmental-zone labels.
        associated = zone_rooms.get(zone_id, set())
        return next(iter(associated)) if len(associated) == 1 else ""

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
            if device_type == "light" and str(d.get(entity_type_col) or "") in {
                "Scene",
                "Switch",
            }:
                continue
            state_name = str(d.get(state_col) or "") if state_col else ""
            addresses = str(d.get(addr_col) or "") if addr_col else ""
            configured_commands = [
                str(d.get(column) or "")
                for name in ("pressCommand", "dimmerCommand")
                if (column := _pick(cols, (name,)))
            ]
            configured_command = next(
                (
                    command
                    for command in configured_commands
                    if command in {"ShadeSet", "RFShadeSet"}
                ),
                next((command for command in configured_commands if command), ""),
            )
            if device_type == "light" and (
                not addresses
                or not state_name
                or not any(
                    marker in state_name
                    for marker in ("CurrentDimmerLevel_", "CurrentColor_", "CurrentBleColor_", ".DimmerLevel_")
                )
            ):
                continue
            if device_type == "cover" and (
                not addresses
                or not any(marker in state_name for marker in ("ShadeLevel_", ".DimmerLevel_"))
                or (
                    ".DimmerLevel_" in state_name
                    and configured_command != "RFShadeSet"
                )
                or (
                    "ShadeLevel_" in state_name
                    and configured_command not in {"", "ShadeSet"}
                )
            ):
                continue
            zone_id = d.get(zone_col) if zone_col else None
            room = _room_for(zone_id) if zone_id is not None else ""
            extra: dict[str, Any] = {}
            if device_type == "light":
                extra = {
                    "entity_type": str(d.get(entity_type_col) or ""),
                    "dimmer_command": str(d.get(_pick(cols, ("dimmerCommand",))) or ""),
                    "fade_time": d.get(_pick(cols, ("fadeTime",))),
                    "delay_time": d.get(_pick(cols, ("delayTime",))),
                    "technology": str(d.get(_pick(cols, ("technology",))) or ""),
                }
            elif device_type == "cover":
                extra = {
                    "fade_time": d.get(_pick(cols, ("fadeTime",))),
                    "delay_time": d.get(_pick(cols, ("delayTime",))),
                    "preset_number": d.get(_pick(cols, ("presetNumber",))),
                    "scene_number": d.get(_pick(cols, ("sceneNumber",))),
                    "shade_command": str(
                        configured_command
                    ),
                }
            elif device_type == "climate":
                for key in (
                    "heat",
                    "cool",
                    "auto",
                    "temperatureSetPoints",
                    "tempMinRange",
                    "tempMaxRange",
                    "tempBuffer",
                    "isCelsius",
                ):
                    if column := _pick(cols, (key,)):
                        extra[key] = d.get(column)
                if room:
                    extra["zone_scope"] = room
            devices.append(
                SavantDevice(
                    device_type=device_type,
                    name=name,
                    room=room,
                    entity_id=f"{device_type}:{d.get('id')}",
                    addresses=addresses,
                    state_name=state_name,
                    zone=room,
                    extra=extra,
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
    # ServiceImplementationZonedService contains the canonical (pathOrder=0) endpoint
    # for every selectable music source. ServiceResources also contains intermediate
    # AVB transport hops, which must not become media players (PROTOCOL.md §13).
    sir = "ServiceImplementationServiceResources"
    zoned_service = "ServiceImplementationZonedService"
    source = zoned_service if zoned_service in tables else sir
    if source not in tables:
        return
    cols = _table_columns(conn, source)
    lc_col = _pick(cols, ("logicalComponent", "logical_component"))
    svc_col = _pick(cols, ("serviceType", "service_type"))
    zone_col = _pick(cols, ("zone",))
    comp_col = _pick(cols, ("component",))
    if not (lc_col and svc_col and zone_col):
        return
    path_order_col = _pick(cols, ("pathOrder", "path_order"))
    name_col = _pick(cols, ("serviceNameAlias", "alias", "name"))
    service_col = _pick(cols, ("service", "serviceID", "service_id"))
    variant_col = _pick(cols, ("serviceVariantID", "serviceVariantId", "variantID"))
    requests = _service_requests(conn, tables)
    seen: set[str | tuple[str, str, str]] = set()
    cursor = conn.execute(f'SELECT * FROM "{source}"')
    for row in cursor:
        d = _row_to_dict(cursor, row)
        service_type = str(d.get(svc_col) or "")
        if service_type != "SVC_AV_SAVANTMUSIC" and not service_type.startswith(
            "SVC_AV_APPLEREMOTEMEDIASERVER"
        ):
            continue
        if source == sir and path_order_col and str(d.get(path_order_col)) != "0":
            continue
        logical = str(d.get(lc_col) or "")
        component = str(d.get(comp_col) or "")
        room = str(d.get(zone_col) or "")
        if not (logical and component and room):
            continue
        service = str(d.get(service_col) or "") if service_col else ""
        key: str | tuple[str, str, str] = service or (room, component, logical)
        if key in seen:
            continue
        seen.add(key)
        name = str(d.get(name_col) or "") if name_col else ""
        devices.append(
            SavantDevice(
                device_type="media_player",
                name=name or component,
                room=room,
                entity_id=f"media_player:{d.get('id')}",
                zone=logical,
                component=component,
                extra={
                    "service_id": service,
                    "service_type": service_type,
                    "variant_id": str(d.get(variant_col) or "1") if variant_col else "1",
                    "requests": requests.get(str(d.get("id")), []),
                },
            )
        )


def _service_requests(conn: sqlite3.Connection, tables: list[str]) -> dict[str, list[str]]:
    """Return the archive-declared request names for each zoned-service endpoint."""
    request_map = "ServiceImplementationRequestMap"
    requests = "ServiceImplementationRequests"
    if request_map not in tables or requests not in tables:
        return {}
    map_cols = _table_columns(conn, request_map)
    request_cols = _table_columns(conn, requests)
    endpoint_col = _pick(map_cols, ("ServiceImplementationZonedService_id",))
    request_id_col = _pick(map_cols, ("ServiceImplementationRequests_id",))
    request_name_col = _pick(request_cols, ("request",))
    if not (endpoint_col and request_id_col and request_name_col):
        return {}
    try:
        cursor = conn.execute(
            f'''SELECT m."{endpoint_col}", r."{request_name_col}"
            FROM "{request_map}" m
            JOIN "{requests}" r ON r.id = m."{request_id_col}"'''
        )
    except sqlite3.Error:
        return {}
    result: dict[str, list[str]] = {}
    for endpoint_id, request in cursor:
        if request:
            result.setdefault(str(endpoint_id), []).append(str(request))
    return result
