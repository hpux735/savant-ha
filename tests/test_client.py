"""Tests for the protocol client (pure framing/decoding — no network or HA imports)."""

from __future__ import annotations

import asyncio
import gzip

import msgpack
import pytest

from custom_components.savant_ha.const import (
    ENVELOPE_KEY_MESSAGES,
    ENVELOPE_KEY_UID,
    ENVELOPE_KEY_URI,
    ENVELOPE_KEY_USER,
    URI_DEVICE_PRESENT,
    URI_STATE_REGISTER,
    URI_STATE_UPDATE,
    build_default_subscribe_keys,
)
from custom_components.savant_ha.savant_client import (
    SavantClient,
    SavantHostInfo,
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


def test_username_password_derives_host_token():
    import base64

    client = SavantClient("10.0.0.5", 12345, username="bob", password="secret")
    assert client._host_token == base64.b64encode(b"bob:secret").decode()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
