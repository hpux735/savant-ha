"""Config flow for the Savant integration.

Two steps: (1) host address + optional explicit port, (2) optional advanced credentials
and room names.  When no port is supplied, the flow attempts UDP discovery (PROTOCOL.md
§1.1) to resolve the control port and ``homeId`` before proceeding.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CLOUD_TOKEN,
    CONF_CONFIGURATION_ID,
    CONF_HOME_ID,
    CONF_HOST_TOKEN,
    CONF_NAME,
    CONF_ROOMS,
    DOMAIN,
    LOGGER,
)
from .savant_client import discover_host

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

_ADVANCED_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_CLOUD_TOKEN): str,
        vol.Optional(CONF_CONFIGURATION_ID): str,
        vol.Optional(CONF_HOST_TOKEN): str,
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


class SavantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the two-step config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int | None = None
        self._discovered: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = user_input.get(CONF_PORT)

            if not self._port:
                try:
                    info = await discover_host(self._host, timeout=3.0)
                except Exception:  # noqa: BLE001 - discovery is best-effort
                    LOGGER.exception("Savant discovery failed")
                    info = None
                if info is None or info.port == 0:
                    errors[CONF_PORT] = "discovery_failed"
                else:
                    self._discovered = {
                        CONF_HOST: info.host,
                        CONF_PORT: info.port,
                        CONF_NAME: info.name,
                        CONF_HOME_ID: info.home_id,
                    }
            if not errors:
                return self.async_show_form(
                    step_id="advanced",
                    data_schema=_ADVANCED_SCHEMA,
                    description_placeholders={
                        "host": self._host,
                        "port": str(self._port or self._discovered.get(CONF_PORT, "?")),
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="advanced", data_schema=_ADVANCED_SCHEMA)

        data: dict[str, Any] = {
            CONF_HOST: self._discovered.get(CONF_HOST, self._host),
            CONF_PORT: self._discovered.get(CONF_PORT, self._port),
            CONF_NAME: self._discovered.get(CONF_NAME, ""),
            CONF_HOME_ID: self._discovered.get(CONF_HOME_ID, ""),
            CONF_USERNAME: user_input.get(CONF_USERNAME, ""),
            CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
            CONF_CLOUD_TOKEN: user_input.get(CONF_CLOUD_TOKEN, ""),
            CONF_CONFIGURATION_ID: user_input.get(CONF_CONFIGURATION_ID, ""),
            CONF_HOST_TOKEN: user_input.get(CONF_HOST_TOKEN, ""),
            CONF_ROOMS: _parse_rooms(user_input.get(CONF_ROOMS)),
        }
        await self.async_set_unique_id(f"{data[CONF_HOST]}:{data[CONF_PORT]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=data.get(CONF_NAME) or data[CONF_HOST], data=data
        )
