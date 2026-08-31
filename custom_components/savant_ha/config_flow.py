"""Config flow for the Savant integration.

Three steps, matching the standard device-import UX:

1. **user** — host address (+ optional explicit port). UDP discovery resolves the
   control port / ``homeId``.
2. **login** — the host-local account (``{user, password}``, PROTOCOL.md §4.1). The
   integration connects, authenticates, downloads the config archive, and enumerates
   the device inventory (PROTOCOL.md §13).
3. **devices** — the collected devices (lights / thermostats / shades / fans / AV
   zones / …), each with an area override. On accept, the entry is created with the
   approved device list.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CLOUD_TOKEN,
    CONF_CONFIGURATION_ID,
    CONF_DEVICES,
    CONF_HOME_ID,
    CONF_HOST_TOKEN,
    CONF_NAME,
    CONF_ROOMS,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_MEDIA_PLAYER,
    DOMAIN,
    LOGGER,
)
from .savant_client import SavantDeviceInfo, SavantHostInfo, discover_host, probe_host

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)

_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_HOST_TOKEN): str,
        vol.Optional(CONF_CLOUD_TOKEN): str,
        vol.Optional(CONF_CONFIGURATION_ID): str,
        vol.Optional(CONF_ROOMS): selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)
        ),
    }
)


def _parse_rooms(raw: Any) -> list[str]:
    if not raw:
        return []
    rooms = [r.strip() for r in raw.replace("\n", ",").split(",")]
    return list(dict.fromkeys(r for r in rooms if r))


def _devices_from_info(info: SavantDeviceInfo) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for dev in info.devices:
        devices.append(
            {
                "type": dev.device_type,
                "id": dev.stable_id,
                "name": dev.name,
                "area": dev.room,
                "room": dev.room,
                "addresses": dev.addresses,
                "state_name": dev.state_name,
                "zone": dev.zone,
                "component": dev.component,
            }
        )
    if devices:
        return devices
    # Fallback (config archive unavailable): derive from the state bus.
    for room in sorted(info.rooms):
        devices.append(
            {"type": DEVICE_TYPE_LIGHT, "id": room, "name": room, "area": room, "room": room}
        )
    for suffix in sorted(info.hvac_suffixes):
        label = "Thermostat" if suffix == "_1" else f"Thermostat {suffix.lstrip('_')}"
        devices.append({"type": DEVICE_TYPE_CLIMATE, "id": suffix, "name": label, "area": ""})
    for zone in sorted(info.zones):
        devices.append(
            {
                "type": DEVICE_TYPE_MEDIA_PLAYER,
                "id": str(zone),
                "name": f"Audio Zone {zone}",
                "area": "",
            }
        )
    return devices


def _device_label(device: dict[str, Any]) -> str:
    label = f"{device.get('type', '')} · {device['name']}"
    if device.get("room") and device["room"] != device["name"]:
        label += f" · {device['room']}"
    return label


def _devices_schema(devices: list[dict[str, Any]]) -> vol.Schema:
    # A multi-select of device names (proper labels), defaulting to all selected.  The
    # device's area is its room from the config archive (looked up, not typed).
    options = [
        {"value": device["id"], "label": _device_label(device)} for device in devices
    ]
    return vol.Schema(
        {
            vol.Required("devices", default=[d["id"] for d in devices]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            )
        }
    )


class SavantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Host discovery -> login -> device picker."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = 0
        self._name: str = ""
        self._home_id: str = ""
        self._username: str = ""
        self._password: str = ""
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT)

            info: SavantHostInfo | None = None
            if not port:
                info = await self._discover(self._host)
                if info is None or info.port <= 0:
                    errors[CONF_PORT] = "discovery_failed"
                else:
                    self._port = info.port
                    self._name = info.name
                    self._home_id = info.home_id
            else:
                self._port = int(port)
                # Best-effort enrich name/homeId even with an explicit port.
                info = await self._discover(self._host)
                if info is not None:
                    self._name = info.name
                    self._home_id = info.home_id

            if not errors:
                return self.async_show_form(
                    step_id="login",
                    data_schema=_LOGIN_SCHEMA,
                    description_placeholders={
                        "host": self._host,
                        "name": self._name or self._host,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                probe = await probe_host(
                    self._host,
                    self._port,
                    home_id=self._home_id,
                    username=self._username,
                    password=self._password,
                    timeout=20.0,
                )
            except Exception:  # noqa: BLE001 - surface a generic connect error
                LOGGER.exception("Savant probe failed")
                probe = None

            if probe is None:
                errors["base"] = "cannot_connect"
            elif not probe.authorized:
                errors["base"] = (
                    "invalid_auth" if probe.auth_response_seen else "no_auth_response"
                )
            else:
                self._devices = _devices_from_info(probe)
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    return self.async_show_form(
                        step_id="devices",
                        data_schema=_devices_schema(self._devices),
                        description_placeholders={
                            "count": str(len(self._devices)),
                            "host": self._host,
                        },
                    )

        return self.async_show_form(
            step_id="login",
            data_schema=_LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={"host": self._host, "name": self._name or self._host},
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            selected = set(user_input.get("devices", []))
            # area is the device's room, looked up from the config archive; it becomes
            # the HA area via the entity's suggested_area.
            approved = [dict(d) for d in self._devices if d["id"] in selected]

            await self.async_set_unique_id(self._host)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._name or self._host,
                data={
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_NAME: self._name,
                    CONF_HOME_ID: self._home_id,
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_DEVICES: approved,
                },
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=_devices_schema(self._devices),
            description_placeholders={"count": str(len(self._devices)), "host": self._host},
        )

    async def _discover(self, host: str) -> SavantHostInfo | None:
        try:
            return await discover_host(host, timeout=3.0)
        except Exception:  # noqa: BLE001 - discovery is best-effort
            LOGGER.exception("Savant discovery failed for %s", host)
            return None


class SavantOptionsFlow(OptionsFlow):
    """Advanced, optional settings — reached via the Configure button."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_USERNAME: user_input.get(CONF_USERNAME, ""),
                    CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
                    CONF_HOST_TOKEN: user_input.get(CONF_HOST_TOKEN, ""),
                    CONF_CLOUD_TOKEN: user_input.get(CONF_CLOUD_TOKEN, ""),
                    CONF_CONFIGURATION_ID: user_input.get(CONF_CONFIGURATION_ID, ""),
                    CONF_ROOMS: _parse_rooms(user_input.get(CONF_ROOMS)),
                },
            )
        return self.async_show_form(step_id="init", data_schema=_OPTIONS_SCHEMA)


async def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
    return SavantOptionsFlow(config_entry)
