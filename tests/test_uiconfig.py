"""Tests for the uiconfig.tar.gz parsing (framing + sqlite device enumeration)."""

from __future__ import annotations

import gzip
import io
import sqlite3
import tarfile

from custom_components.savant_ha import uiconfig


def _make_sqlite_bytes() -> bytes:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE Rooms (id INTEGER PRIMARY KEY, name TEXT, roomID TEXT)")
    conn.execute(
        "CREATE TABLE Zones (id INTEGER PRIMARY KEY, name TEXT, type TEXT,"
        " serviceID TEXT, logicalComponent TEXT)"
    )
    conn.execute("CREATE TABLE ZoneRoomMap (zoneID INTEGER, roomID INTEGER)")
    conn.execute(
        "CREATE TABLE LightEntities (id INTEGER PRIMARY KEY, name TEXT, addresses TEXT,"
        " stateName TEXT, zoneID INTEGER, entityType TEXT)"
    )
    conn.execute(
        "CREATE TABLE HVACEntities (id INTEGER PRIMARY KEY, name TEXT, zoneID INTEGER)"
    )
    conn.execute(
        "CREATE TABLE ServiceImplementationServiceResources"
        " (id INTEGER PRIMARY KEY, zone TEXT, component TEXT, logicalComponent TEXT,"
        " serviceType TEXT, serviceNameAlias TEXT, service TEXT, serviceVariantID TEXT,"
        " alias TEXT, pathOrder INTEGER)"
    )
    conn.execute(
        "CREATE VIEW ServiceImplementationZonedService AS SELECT * "
        "FROM ServiceImplementationServiceResources WHERE pathOrder = 0"
    )
    conn.execute(
        "CREATE TABLE ServiceImplementationRequests "
        "(id INTEGER PRIMARY KEY, request TEXT)"
    )
    conn.execute(
        "CREATE TABLE ServiceImplementationRequestMap "
        "(id INTEGER PRIMARY KEY, ServiceImplementationZonedService_id INTEGER, "
        "ServiceImplementationRequests_id INTEGER)"
    )
    conn.execute("INSERT INTO Rooms VALUES (1,'Kitchen','r1'),(2,'Living Room','r2')")
    conn.execute(
        "INSERT INTO Zones VALUES (10,'Kitchen','Environmental',"
        "'SVC_ENV_LIGHTING','Host'),(11,'Kitchen','Environmental',"
        "'SVC_ENV_HVAC','HVAC_controller')"
    )
    # A shared HVAC zone maps to multiple rooms. Its Environmental zone name, rather
    # than the last ZoneRoomMap row, must determine the imported device's area.
    conn.execute("INSERT INTO ZoneRoomMap VALUES (10,1),(11,1),(11,2)")
    conn.execute(
        "INSERT INTO LightEntities VALUES"
        " (1,'Kitchen Recessed','002,1,(null)',"
        "'Proj.Host.CurrentDimmerLevel_1_002',10,'Dimmer'),"
        " (2,'AUX B','002,2,(null)',"
        "'Proj.Host.CurrentLEDState_2_002',10,'Scene'),"
        " (3,'Kitchen Fan','002,3,(null)',"
        "'Proj.Host.CurrentDimmerLevel_3_002',10,'Switch')"
    )
    conn.execute("INSERT INTO HVACEntities VALUES (1,'Main Thermostat',11)")
    conn.execute(
        "INSERT INTO ServiceImplementationServiceResources VALUES"
        " (1,'Kitchen','Music','Audio Zone 1','SVC_AV_SAVANTMUSIC','Music',"
        "'Kitchen-Music-Audio-Zone-1','1','Music',0),"
        " (2,'Kitchen','Music','Audio Zone 1','SVC_SETTINGS_EQUALIZER','Kitchen EQ',"
        "'Kitchen-EQ','1','Kitchen EQ',0),"
        " (3,'Kitchen','Living Room Sound Bar','AVB Stream 2','SVC_AV_SAVANTMUSIC',"
        "'Living Room Sound Bar','Kitchen-Living-Room-Sound-Bar-AVB-Stream-2','2',"
        "'Living Room Sound Bar',0),"
        " (4,'Kitchen','Music','Audio Zone 1','SVC_AV_SAVANTMUSIC','Transport hop',"
        "'Kitchen-Living-Room-Sound-Bar-AVB-Stream-2','2','Transport hop',1),"
        " (5,'Kitchen','AppleTV1','Media_server','SVC_AV_APPLEREMOTEMEDIASERVER',"
        "'AppleTV1 Control','Kitchen-AppleTV1-Media-server-1','1','AppleTV1 Control',0)"
    )
    conn.execute(
        "INSERT INTO ServiceImplementationRequests VALUES"
        " (1,'PowerOn'),(2,'PowerOff'),(3,'SetVolume'),(4,'Play'),(5,'Pause')"
    )
    conn.execute(
        "INSERT INTO ServiceImplementationRequestMap VALUES"
        " (1,5,1),(2,5,2),(3,5,3),(4,5,4),(5,5,5)"
    )
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
    assert "media_player" in by_type

    light = next(d for d in devices if d.device_type == "light")
    assert light.name == "Kitchen Recessed"
    assert light.room == "Kitchen"
    assert light.addresses == "002,1,(null)"
    assert light.state_name == "Proj.Host.CurrentDimmerLevel_1_002"
    assert light.entity_id == "light:1"
    assert light.extra["entity_type"] == "Dimmer"
    assert all(device.name != "AUX B" for device in devices)
    switch = next(d for d in devices if d.name == "Kitchen Fan")
    assert switch.extra["entity_type"] == "Switch"

    climate = next(d for d in devices if d.device_type == "climate")
    assert climate.name == "Main Thermostat"
    assert climate.room == "Kitchen"

    media = [d for d in devices if d.device_type == "media_player"]
    assert [(d.name, d.room, d.component, d.zone) for d in media] == [
        ("Music", "Kitchen", "Music", "Audio Zone 1"),
        ("Living Room Sound Bar", "Kitchen", "Living Room Sound Bar", "AVB Stream 2"),
        ("AppleTV1 Control", "Kitchen", "AppleTV1", "Media_server"),
    ]
    assert [d.extra["variant_id"] for d in media] == ["1", "2", "1"]
    apple_tv = media[-1]
    assert apple_tv.extra["service_type"] == "SVC_AV_APPLEREMOTEMEDIASERVER"
    assert apple_tv.extra["requests"] == ["PowerOn", "PowerOff", "SetVolume", "Play", "Pause"]


def test_reassemble_ignores_non_archive_frames():
    frames = _frame_archive(gzip.compress(b"hello world"))
    frames.insert(0, b"\x80\x01not-an-archive-frame")
    assert uiconfig.reassemble_archive(frames)[:2] == b"\x1f\x8b"


def test_parse_archive_empty():
    assert uiconfig.parse_archive(b"") == []
    assert uiconfig.parse_archive(gzip.compress(b"not a tar")) == []
