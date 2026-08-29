"""Config flow for the Savant integration.

The primary flow is a single step: enter the host address (and, optionally, an explicit
control port).  The control port and ``homeId`` are auto-discovered over UDP
(PROTOCOL.md §1.1) when no port is given.

Advanced settings (credentials and a list of room names) are deliberately *not* asked
during setup — a new user has no idea what "cloud token" or "host token" mean.  They are
instead exposed later via the integration's "Configure" button (options flow) for
power users, and default to sane empty values.
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
    CONF_HOME_ID,
    CONF_HOST_TOKEN,
    CONF_NAME,
    CONF_ROOMS,
    DOMAIN,
    LOGGER,
)
from .savant_client import SavantHostInfo, discover_host

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


class SavantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a host-only setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT)
            name = ""
            home_id = ""

            info = await self._discover(host)
            if port:
                # Explicit port: discovery is still consulted (best-effort) to enrich
                # the entry with the host name / homeId.
                if info is not None:
                    name = info.name
                    home_id = info.home_id
            elif info is not None and info.port > 0:
                port = info.port
                name = info.name
                home_id = info.home_id
            else:
                errors[CONF_PORT] = "discovery_failed"

            if not errors:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name or host,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: int(port),
                        CONF_NAME: name,
                        CONF_HOME_ID: home_id,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
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
