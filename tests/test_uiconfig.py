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
        "CREATE TABLE HVACEntities (id INTEGER PRIMARY KEY, name TEXT, zoneID INTEGER,"
        " addresses TEXT, stateName TEXT, heat INTEGER, cool INTEGER, auto INTEGER,"
        " temperatureSetPoints INTEGER, tempMinRange REAL, tempMaxRange REAL,"
        " tempBuffer REAL, isCelsius INTEGER)"
    )
    conn.execute(
        "CREATE TABLE ShadeEntities (id INTEGER PRIMARY KEY, name TEXT, addresses TEXT,"
        " stateName TEXT, zoneID INTEGER, pressCommand TEXT, dimmerCommand TEXT)"
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
        "'SVC_ENV_HVAC','HVAC_controller'),(12,'Living Room','Environmental',"
        "'SVC_ENV_HVAC','HVAC_controller')"
    )
    # A shared HVAC zone maps to multiple rooms, so it has no safe suggested area.
    conn.execute("INSERT INTO ZoneRoomMap VALUES (10,1),(11,1),(11,2),(12,2)")
    conn.execute(
        "INSERT INTO LightEntities VALUES"
        " (1,'Kitchen Recessed','002,1,(null)',"
        "'Proj.Host.CurrentDimmerLevel_1_002',10,'Dimmer'),"
        " (2,'AUX B','002,2,(null)',"
        "'Proj.Host.CurrentLEDState_2_002',10,'Scene'),"
        " (3,'Kitchen Fan','002,3,(null)',"
        "'Proj.Host.CurrentDimmerLevel_3_002',10,'Switch')"
    )
    conn.execute(
        "INSERT INTO HVACEntities VALUES"
        " (1,'Main Thermostat',11,'1,',"
        "'CLIW220.HVAC_controller.ThermostatCurrentTemperature_1',1,1,1,2,50,90,4,0),"
        " (2,'Living Thermostat',12,'1,',"
        "'Living HVAC.HVAC_controller.ThermostatCurrentTemperature_1',1,0,0,1,50,80,4,0)"
    )
    conn.execute(
        "INSERT INTO ShadeEntities VALUES"
        " (1,'Kitchen Shade','002,4,(null)',"
        "'Proj.Host.ShadeLevel_4_002',10,'ShadeSet',''),"
        " (2,'Kitchen Lutron Shade','5',"
        "'Lighting.Lighting_controller.DimmerLevel_5',10,'','RFShadeSet'),"
        " (3,'Unsupported Shade','6',"
        "'Lighting.Lighting_controller.DimmerLevel_6',10,'ShadeSet','ShadeSet')"
    )
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


def test_parse_completed_archive_and_devices():
    sqlite_bytes = _make_sqlite_bytes()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        info = tarfile.TarInfo(name="serviceImplementation.sqlite")
        info.size = len(sqlite_bytes)
        tar.addfile(info, io.BytesIO(sqlite_bytes))
    gz = gzip.compress(tar_bytes.getvalue())

    devices = uiconfig.parse_archive(gz)
    by_type = {d.device_type for d in devices}
    assert "light" in by_type
    assert "climate" in by_type
    assert "media_player" in by_type
    assert "cover" in by_type

    light = next(d for d in devices if d.device_type == "light")
    assert light.name == "Kitchen Recessed"
    assert light.room == "Kitchen"
    assert light.addresses == "002,1,(null)"
    assert light.state_name == "Proj.Host.CurrentDimmerLevel_1_002"
    assert light.entity_id == "light:1"
    assert light.extra["entity_type"] == "Dimmer"
    assert all(device.name != "AUX B" for device in devices)
    assert all(device.name != "Kitchen Fan" for device in devices)

    covers = [d for d in devices if d.device_type == "cover"]
    assert [d.name for d in covers] == ["Kitchen Shade", "Kitchen Lutron Shade"]
    assert [d.extra["shade_command"] for d in covers] == ["ShadeSet", "RFShadeSet"]

    climate = next(d for d in devices if d.device_type == "climate")
    assert climate.name == "Main Thermostat"
    assert climate.room == ""
    assert climate.zone == ""
    assert climate.extra == {
        "heat": 1,
        "cool": 1,
        "auto": 1,
        "temperatureSetPoints": 2,
        "tempMinRange": 50.0,
        "tempMaxRange": 90.0,
        "tempBuffer": 4.0,
        "isCelsius": 0,
    }
    scoped_climate = next(d for d in devices if d.name == "Living Thermostat")
    assert scoped_climate.room == "Living Room"
    assert scoped_climate.extra["zone_scope"] == "Living Room"

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


def test_parse_archive_empty():
    assert uiconfig.parse_archive(b"") == []
    assert uiconfig.parse_archive(gzip.compress(b"not a tar")) == []
