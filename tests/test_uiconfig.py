"""Tests for the uiconfig.tar.gz parsing (framing + sqlite device enumeration)."""

from __future__ import annotations

import gzip
import io
import sqlite3
import tarfile

from custom_components.savant_ha import uiconfig


def _make_sqlite_bytes() -> bytes:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE Rooms (roomID TEXT, name TEXT)")
    conn.execute("CREATE TABLE Zones (zoneID TEXT, name TEXT, type TEXT)")
    conn.execute("CREATE TABLE ZoneRoomMap (zoneID TEXT, roomID TEXT)")
    conn.execute(
        "CREATE TABLE LightEntities (entityID TEXT, name TEXT, addresses TEXT,"
        " stateName TEXT, zoneID TEXT)"
    )
    conn.execute("CREATE TABLE HVACEntities (entityID TEXT, name TEXT, zoneID TEXT)")
    conn.execute("INSERT INTO Rooms VALUES ('r1','Kitchen'),('r2','Living Room')")
    conn.execute(
        "INSERT INTO Zones VALUES ('z1','Kitchen-Recessed','User'),"
        "('z2','Living Room-Lamp','User')"
    )
    conn.execute("INSERT INTO ZoneRoomMap VALUES ('z1','r1'),('z2','r2')")
    conn.execute(
        "INSERT INTO LightEntities VALUES ('l1','Kitchen Recessed','002,1,(null)',"
        "'Proj.Host.CurrentDimmerLevel_1_002','z1')"
    )
    conn.execute("INSERT INTO HVACEntities VALUES ('h1','Main Thermostat','z1')")
    data = conn.serialize()
    conn.close()
    return data


def _frame_archive(gz: bytes) -> list[bytes]:
    frames: list[bytes] = []
    total = len(gz)
    filename = b"uiconfig.tar.gz"
    chunk = 64
    for i in range(0, total, chunk):
        part = gz[i : i + chunk]
        flag = 0x81 if (i + chunk) >= total else 0x01
        header = (
            bytes([0x01, flag])
            + b"\x00" * 6
            + total.to_bytes(2, "big")
            + b"\x00" * 3
            + bytes([len(filename)])
            + filename
        )
        frames.append(header + part)
    return frames


def test_reassemble_and_parse_devices():
    sqlite_bytes = _make_sqlite_bytes()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        info = tarfile.TarInfo(name="serviceImplementation.sqlite")
        info.size = len(sqlite_bytes)
        tar.addfile(info, io.BytesIO(sqlite_bytes))
    gz = gzip.compress(tar_bytes.getvalue())

    frames = _frame_archive(gz)
    reassembled = uiconfig.reassemble_archive(frames)
    assert reassembled[:2] == b"\x1f\x8b"

    devices = uiconfig.parse_archive(reassembled)
    by_type = {d.device_type for d in devices}
    assert "light" in by_type
    assert "climate" in by_type

    light = next(d for d in devices if d.device_type == "light")
    assert light.name == "Kitchen Recessed"
    assert light.room == "Kitchen"
    assert light.addresses == "002,1,(null)"
    assert light.state_name == "Proj.Host.CurrentDimmerLevel_1_002"
    assert light.entity_id == "l1"

    climate = next(d for d in devices if d.device_type == "climate")
    assert climate.name == "Main Thermostat"
    assert climate.room == "Kitchen"


def test_reassemble_ignores_non_archive_frames():
    frames = _frame_archive(gzip.compress(b"hello world"))
    frames.insert(0, b"\x80\x01not-an-archive-frame")
    assert uiconfig.reassemble_archive(frames)[:2] == b"\x1f\x8b"


def test_parse_archive_empty():
    assert uiconfig.parse_archive(b"") == []
    assert uiconfig.parse_archive(gzip.compress(b"not a tar")) == []
