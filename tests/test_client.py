"""Tests for the protocol client (pure framing/decoding — no network or HA imports)."""

from __future__ import annotations

import asyncio
import gzip

import msgpack
import pytest

from custom_components.savant_ha import savant_client as sc
from custom_components.savant_ha.const import (
    ENVELOPE_KEY_MESSAGES,
    ENVELOPE_KEY_UID,
    ENVELOPE_KEY_URI,
    ENVELOPE_KEY_USER,
    URI_DEVICE_PRESENT,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
    build_default_subscribe_keys,
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


def test_subscribe_keys_include_every_category():
    keys = build_default_subscribe_keys(rooms=["Living Room"])
    assert any(k == "HVAC Controller.HVAC_controller.ThermostatCurrentTemperature_1" for k in keys)
    assert any(k == "Music.Audio Zone 1.SVC_AV_SAVANTMUSIC.CurrentSongName" for k in keys)
    assert "global.CurrentTemperature" in keys
    assert "Living Room.RoomLightsAreOn" in keys
    assert "Living Room.RoomCurrentTemperature" in keys


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
