"""Tests for the protocol client (pure framing/decoding — no network or HA imports)."""

from __future__ import annotations

import asyncio
import gzip
import re

import msgpack
import pytest

from custom_components.savant_ha import savant_client as sc
from custom_components.savant_ha.const import (
    DASHBOARD_STATE_RECENT_SERVICES,
    ENVELOPE_KEY_MESSAGES,
    ENVELOPE_KEY_UID,
    ENVELOPE_KEY_URI,
    ENVELOPE_KEY_USER,
    SVC_ENV_HVAC,
    URI_DEVICE_PRESENT,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
    audio_zone_state_keys,
    build_default_subscribe_keys,
    device_state_keys,
    new_uid,
    room_from_state_key,
    room_state_keys,
)
from custom_components.savant_ha.savant_client import (
    SavantClient,
    SavantHostInfo,
    probe_host,
    rooms_from_scene_messages,
)


def _pack(obj):
    return msgpack.packb(obj, use_bin_type=True)


def _frame(obj, gzipped: bool = True) -> bytes:
    payload = _pack(obj)
    if gzipped:
        return gzip.compress(payload)
    return payload


def test_host_info_from_record_extracts_known_fields():
    record = {
        "name": "Living Room Host",
        "port": 35299,
        "homeId": "<HOME_ID>",
        "UID": "<DEVICE_UID>",
        "scheme": "wss",
        "hostModel": "X2",
        "buildVersion": "9.0",
        "onboardKey": "ignore-me-not",
    }
    info = SavantHostInfo.from_record("10.0.0.5", record)
    assert info.host == "10.0.0.5"
    assert info.port == 35299
    assert info.name == "Living Room Host"
    assert info.home_id == "<HOME_ID>"
    assert info.uid == "<DEVICE_UID>"
    assert info.scheme == "wss"
    assert info.host_model == "X2"
    assert info.build_version == "9.0"
    # unlisted keys are preserved in `extra`
    assert info.extra["onboardKey"] == "ignore-me-not"


def test_new_uid_is_not_uuid_shaped():
    # The host ignores devicePresent for UUID-shaped uids (32-hex substring), so the
    # generated uid must not look like a UUID.
    for _ in range(20):
        uid = new_uid()
        assert uid.startswith("homeassistant-")
        assert not re.search(r"[0-9a-fA-F]{32}", uid)


def test_hvac_service_type_is_available_to_platforms():
    assert SVC_ENV_HVAC == "SVC_ENV_HVAC"


def test_client_defaults_to_non_uuid_uid():
    client = SavantClient("10.0.0.5", 12345)
    assert client._uid.startswith("homeassistant-")
    assert not re.search(r"[0-9a-fA-F]{32}", client._uid)


def test_handle_frame_decodes_gzipped_state_update():
    client = SavantClient("10.0.0.5", 12345)
    seen: list[tuple[str, object]] = []
    client.on_state_update = lambda key, value: seen.append((key, value))

    envelope = {
        ENVELOPE_KEY_MESSAGES: [
            {"state": "Living Room.CurrentVolume", "value": 30}
        ],
        ENVELOPE_KEY_URI: URI_STATE_UPDATE,
        ENVELOPE_KEY_UID: "<DEVICE_UID>",
        ENVELOPE_KEY_USER: "iPhone",
    }
    client._handle_frame(_frame(envelope, gzipped=True))

    assert seen == [("Living Room.CurrentVolume", 30)]


def test_handle_frame_tolerates_uncompressed_frame():
    # featureLock is un-compressed msgbpack (PROTOCOL.md §1).
    client = SavantClient("10.0.0.5", 12345)
    client._handle_frame(
        _frame({ENVELOPE_KEY_URI: "featureLock", ENVELOPE_KEY_MESSAGES: []}, gzipped=False)
    )


def test_handle_frame_ignores_non_state_messages():
    client = SavantClient("10.0.0.5", 12345)
    seen: list[tuple[str, object]] = []
    client.on_state_update = lambda key, value: seen.append((key, value))
    client._handle_frame(
        _frame({ENVELOPE_KEY_URI: URI_STATE_UPDATE, ENVELOPE_KEY_MESSAGES: [{"bad": 1}]})
    )
    assert seen == []


def test_pack_encodes_long_strings_with_legacy_raw16():
    # Live-verified against the target host: its msgpack parser drops any frame
    # containing a new-spec str8 (0xd9) string, so strings longer than 31 chars (every
    # long dotted state key) must be encoded with legacy raw16 (0xda). A 40-char key
    # encodes as da 00 28.
    data = sc._pack({"state": "Savant.Lighting.CurrentDimmerLevel_1_001"})
    assert b"\xd9" not in data
    assert b"\xda\x00\x28" in data
    assert msgpack.unpackb(data, raw=False) == {
        "state": "Savant.Lighting.CurrentDimmerLevel_1_001"
    }


def test_send_handshake_uses_expected_envelope():
    client = SavantClient(
        "10.0.0.5",
        12345,
        uid="client-uid",
        home_id="home-123",
        cloud_token="cloud-token",
        configuration_id="config-1",
        host_token="host-token",
        subscribe_keys=["a.b"],
    )
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))

        client.request = fake_request  # type: ignore[assignment]
        await client._send_device_present()
        await client._send_auth_request()
        await client._send_state_register()

    asyncio.run(run())

    device_uri, [device_msg] = sent[0]
    assert device_uri == URI_DEVICE_PRESENT
    assert device_msg["protocolVersion"] == "4"
    assert device_msg["homeId"] == "home-123"
    assert device_msg["cloudToken"] == "cloud-token"
    assert device_msg["configurationID"] == "config-1"
    assert device_msg["messageFormat"] == 2
    assert device_msg["device"]["UID"] == "client-uid"

    auth_uri, [auth_msg] = sent[1]
    assert auth_uri == "session/authenticationRequest"
    assert auth_msg == {"hostToken": "host-token"}

    reg_uri, reg_messages = sent[2]
    assert reg_uri == URI_STATE_REGISTER
    assert reg_messages == [{"state": "a.b"}]


def test_post_auth_matches_observed_state_startup_order():
    client = SavantClient("10.0.0.5", 12345, subscribe_keys=["a.b"])
    client._authorized = True
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))

        client.request = fake_request  # type: ignore[assignment]
        await client._post_auth()

    asyncio.run(run())

    assert sent == [
        ("session/fileDownload", [{"filePath": "uiconfig.tar.gz"}]),
        ("dis/dashboard/register", [{"state": DASHBOARD_STATE_RECENT_SERVICES}]),
        (URI_STATE_REGISTER, [{"state": "global.SonosGroups"}]),
        ("dis/userData/register", [{"state": "local.setting.update"}]),
        ("dis/userData/register", [{"state": "user.setting.update"}]),
        ("dis/userData/register", [{"state": "global.setting.update"}]),
        (URI_STATE_REGISTER, [{"state": "a.b"}]),
        ("dcm/request", [{"request": "getMode"}]),
        ("dis/userData/register", [{"state": "global.image.update"}]),
        ("dis/userData/register", [{"state": "user.image.update"}]),
        ("dis/dashboard/request", [{"request": "GetAVAutomationScenes"}]),
    ]


def test_subscribe_keys_include_every_category():
    keys = build_default_subscribe_keys(rooms=["Living Room"])
    assert any(k == "HVAC Controller.HVAC_controller.ThermostatCurrentTemperature_1" for k in keys)
    assert any(k == "Music.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName" for k in keys)
    assert "global.CurrentTemperature" in keys
    assert "Living Room.RoomLightsAreOn" in keys
    assert "Living Room.RoomCurrentTemperature" in keys


def test_device_state_keys_uses_archive_state_name_and_hvac_scope():
    assert device_state_keys(
        {"type": "light", "state_name": "Savant.Lighting.CurrentDimmerLevel_1_00C"}
    ) == ["Savant.Lighting.CurrentDimmerLevel_1_00C"]

    keys = device_state_keys(
        {
            "type": "climate",
            "state_name": "New Thermostat.HVAC_controller.ThermostatCurrentTemperature_2",
        }
    )
    assert "New Thermostat.HVAC_controller.ThermostatCurrentTemperature_2" in keys
    assert "New Thermostat.HVAC_controller.ThermostatCurrentCoolPoint_2" in keys
    assert "New Thermostat.HVAC_controller.IsCurrentHVACModeHeat_2" in keys
    assert "New Thermostat.HVAC_controller.ThermostatCurrentRemoteTemperature_2" in keys


def test_device_state_keys_passthrough_for_non_climate():
    assert device_state_keys(
        {"type": "cover", "state_name": "Bond Bridge.Lighting_controller.ShadeLevel_abc"}
    ) == ["Bond Bridge.Lighting_controller.ShadeLevel_abc"]
    assert device_state_keys({"type": "media_player"}) == []


def test_audio_zone_state_keys_keep_component_identity():
    keys = audio_zone_state_keys("Living Room Sound Bar", "Audio Zone 1")
    assert "Living Room Sound Bar.Audio Zone 1.CurrentSongName" in keys
    assert "Living Room Sound Bar.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName" in keys
    assert "Living Room Sound Bar.Audio Zone 1.SVC_AV_SAVANTMUSIC.ZonesActiveIn" in keys


def test_extract_jpeg_discards_raw_transfer_prefix_and_suffix():
    assert sc.extract_jpeg(b"header\x00\xff\xd8\xffimage\xff\xd9trailer") == (
        b"\xff\xd8\xffimage\xff\xd9"
    )
    assert sc.extract_jpeg(b"header\x00\xff\xd8\xffpartial") is None


def test_artwork_request_collects_jpeg():
    client = SavantClient("10.0.0.5", 12345)
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))
            client._handle_artwork_frame(b"prefix\xff\xd8\xffjpeg\xff\xd9")

        client.request = fake_request  # type: ignore[assignment]
        return await client.async_get_artwork("Music", "Audio Zone 1", "artwork-key")

    assert asyncio.run(run()) == b"\xff\xd8\xffjpeg\xff\xd9"
    assert sent == [
        (
            "session/fileDownload",
            [
                {
                    "URI": "avc/Music/Audio Zone 1",
                    "payload": {"key": "artwork-key", "type": "nowPlayingArtwork"},
                }
            ],
        )
    ]


def test_default_music_subscriptions_include_both_observed_key_shapes():
    keys = build_default_subscribe_keys()
    assert "Music.Audio Zone 1.CurrentSongName" in keys
    assert "Music.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName" in keys


def test_local_login_uses_user_password_form():
    client = SavantClient("10.0.0.5", 12345, username="bob", password="secret")
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))

        client.request = fake_request  # type: ignore[assignment]
        await client._send_auth_request()

    asyncio.run(run())

    uri, [auth_msg] = sent[0]
    assert uri == "session/authenticationRequest"
    assert auth_msg == {"user": "bob", "password": "secret"}


def test_auth_request_omitted_without_credentials():
    client = SavantClient("10.0.0.5", 12345)
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))

        client.request = fake_request  # type: ignore[assignment]
        await client._send_auth_request()

    asyncio.run(run())
    assert sent == []


def test_device_present_omits_empty_cloud_fields():
    client = SavantClient("10.0.0.5", 12345, uid="client-uid")
    sent: list[tuple[str, list]] = []

    async def run():
        async def fake_request(uri, messages):
            sent.append((uri, messages))

        client.request = fake_request  # type: ignore[assignment]
        await client._send_device_present()

    asyncio.run(run())

    _, [device_msg] = sent[0]
    assert "cloudToken" not in device_msg
    assert "configurationID" not in device_msg
    assert device_msg["messageFormat"] == 2


def test_room_from_state_key():
    assert room_from_state_key("Living Room.RoomCurrentTemperature") == "Living Room"
    assert room_from_state_key("Living Room.BrightnessLevel") == "Living Room"
    assert room_from_state_key("Music.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName") is None
    assert room_from_state_key("HVAC Controller.HVAC_controller.ThermostatMode_1") is None
    assert room_from_state_key("global.CurrentTemperature") is None


def test_room_state_keys_covers_all_attributes():
    keys = room_state_keys({"Living Room"})
    assert "Living Room.RoomLightsAreOn" in keys
    assert "Living Room.RoomCurrentTemperature" in keys
    assert "Living Room.RoomFansAreOn" in keys


def test_rooms_from_scene_messages():
    messages = [
        {
            "state": "scenesAndFoldersReduced",
            "value": [
                {
                    "definition": {
                        "power": {"rooms": {"Living Room": 0, "Kitchen": 1}},
                        "av": {"Audio Zone 1": {"rooms": ["Dining Room"]}},
                        "lighting": {"host": {"rooms": ["Primary Bedroom"]}},
                    }
                }
            ],
        }
    ]
    rooms = rooms_from_scene_messages(messages)
    assert rooms == {"Living Room", "Kitchen", "Dining Room", "Primary Bedroom"}


def test_auth_response_emits_start_zone():
    client = SavantClient("10.0.0.5", 12345)
    seen: list[set[str]] = []
    client.on_rooms_discovered = lambda rooms: seen.append(set(rooms))
    client._handle_frame(
        _frame(
            {
                ENVELOPE_KEY_URI: "session/authenticationResponse",
                ENVELOPE_KEY_MESSAGES: [
                    {"authorized": True, "startZone": "Living Room", "hostToken": "x"}
                ],
            }
        )
    )
    assert client.authorized is True
    assert seen == [{"Living Room"}]


def test_device_recognized_sets_authentication_flag():
    client = SavantClient("10.0.0.5", 12345)
    assert client._device_recognized is False
    client._handle_frame(
        _frame(
            {
                ENVELOPE_KEY_URI: "session/deviceRecognized",
                ENVELOPE_KEY_MESSAGES: [{"authentication": True}],
            }
        )
    )
    assert client._device_recognized is True
    assert client._authentication_required is True


def test_auth_denied_marks_seen_but_not_authorized():
    client = SavantClient("10.0.0.5", 12345)
    client._handle_frame(
        _frame(
            {
                ENVELOPE_KEY_URI: "session/authenticationResponse",
                ENVELOPE_KEY_MESSAGES: [
                    {"authorized": False, "errorReason": "Invalid password", "errorCode": 1}
                ],
            }
        )
    )
    assert client.authorized is False
    assert client.auth_response_seen is True


def test_refresh_discovery_updates_port(monkeypatch):
    client = SavantClient("10.0.0.5", 0)

    async def fake_discover(host, timeout):
        return SavantHostInfo(host=host, port=35299, home_id="home-1")

    monkeypatch.setattr(sc, "discover_host", fake_discover)
    asyncio.run(client._refresh_discovery())
    assert client._port == 35299
    assert client._home_id == "home-1"


def test_refresh_discovery_keeps_zero_port(monkeypatch):
    client = SavantClient("10.0.0.5", 0)

    async def fake_discover(host, timeout):
        return None

    monkeypatch.setattr(sc, "discover_host", fake_discover)
    asyncio.run(client._refresh_discovery())
    assert client._port == 0


def test_probe_host_collects_device_surface(monkeypatch):
    async def fake_connect(self):
        self._authorized = True
        self.on_rooms_discovered({"Living Room", "Kitchen"})
        self.on_state_update(
            "HVAC Controller.HVAC_controller.ThermostatCurrentTemperature_1", 72
        )
        self.on_state_update(
            "Music.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName", "Song"
        )

    async def fake_receive_loop(self):
        await asyncio.sleep(0.001)

    async def fake_disconnect(self):
        pass

    monkeypatch.setattr(sc.SavantClient, "_connect", fake_connect)
    monkeypatch.setattr(sc.SavantClient, "_receive_loop", fake_receive_loop)
    monkeypatch.setattr(sc.SavantClient, "_disconnect", fake_disconnect)

    info = asyncio.run(
        probe_host("10.0.0.5", 12345, username="u", password="p", timeout=0.05)
    )
    assert info.authorized is True
    assert info.rooms == {"Living Room", "Kitchen"}
    assert info.hvac_suffixes == {"_1"}
    assert info.zones == {1}


def test_probe_host_survives_cancelling_a_blocked_receive(monkeypatch):
    # Regression: cancelling the receive task raises asyncio.CancelledError, which is a
    # BaseException.  The probe must suppress it or the config flow crashes with HA's
    # generic "Unknown error occurred".
    release = asyncio.Event()

    async def fake_connect(self):
        self._authorized = True

    async def fake_receive_loop(self):
        await release.wait()

    async def fake_disconnect(self):
        pass

    monkeypatch.setattr(sc.SavantClient, "_connect", fake_connect)
    monkeypatch.setattr(sc.SavantClient, "_receive_loop", fake_receive_loop)
    monkeypatch.setattr(sc.SavantClient, "_disconnect", fake_disconnect)

    info = asyncio.run(
        probe_host("10.0.0.5", 12345, username="u", password="p", timeout=0.05)
    )
    assert info.authorized is True


def test_probe_host_captures_authorized_before_disconnect(monkeypatch):
    # Regression: _disconnect() resets _authorized; probe_host must capture the auth
    # state before disconnecting, or the config flow wrongly reports "login failed".
    async def fake_connect(self):
        self._authorized = True
        self._auth_response_seen = True

    async def fake_receive_loop(self):
        await asyncio.sleep(0.001)

    async def fake_disconnect(self):
        self._authorized = False  # mimic the real _disconnect

    monkeypatch.setattr(sc.SavantClient, "_connect", fake_connect)
    monkeypatch.setattr(sc.SavantClient, "_receive_loop", fake_receive_loop)
    monkeypatch.setattr(sc.SavantClient, "_disconnect", fake_disconnect)

    info = asyncio.run(
        probe_host("10.0.0.5", 12345, username="u", password="p", timeout=0.05)
    )
    assert info.authorized is True
    assert info.auth_response_seen is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
