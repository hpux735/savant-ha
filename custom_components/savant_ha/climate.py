"""Climate platform: one entity per Savant HVAC controller (from the config archive).

Key/verb knowledge is reconstructed — see ``PROTOCOL.md`` §5.1 and §6.  The unit index
(``_1``) maps to ``ThermostatAddress`` (``"1"``); each ``HVACEntities`` row becomes one
thermostat.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_TYPE_CLIMATE,
    DOMAIN,
    HVAC_STATE_PREFIX,
    SVC_ENV_HVAC,
    VERB_FAN_MODE_AUTO,
    VERB_FAN_MODE_CYCLE,
    VERB_FAN_MODE_ON,
    VERB_HVAC_MODE_AUTO,
    VERB_HVAC_MODE_COOL,
    VERB_HVAC_MODE_HEAT,
    VERB_HVAC_MODE_OFF,
    VERB_SET_COOL_POINT,
    VERB_SET_HEAT_POINT,
)
from .entity import SavantEntity
from .hub import SavantHub

# State-key suffixes per unit (PROTOCOL.md §5.1).  The suffix is the unit index.
_TEMP_ATTR = "ThermostatCurrentTemperature"
_HEAT_POINT_ATTR = "ThermostatCurrentHeatPoint"
_COOL_POINT_ATTR = "ThermostatCurrentCoolPoint"
_SET_POINT_ATTR = "ThermostatCurrentSetPoint"
_HUMIDITY_ATTR = "ThermostatCurrentHumidity"
_REMOTE_TEMP_ATTR = "ThermostatCurrentRemoteTemperature"
_TARGET_TEMP_LOW = "target_temp_low"
_TARGET_TEMP_HIGH = "target_temp_high"

_FAN_MODE_AUTO = "auto"
_FAN_MODE_CYCLE = "cycle"
_FAN_MODE_ON = "on"

_MODE_FLAGS = {
    HVACMode.OFF: "IsCurrentHVACModeOff",
    HVACMode.COOL: "IsCurrentHVACModeCool",
    HVACMode.HEAT: "IsCurrentHVACModeHeat",
    HVACMode.AUTO: "IsCurrentHVACModeAuto",
}

_MODE_VERBS = {
    HVACMode.OFF: VERB_HVAC_MODE_OFF,
    HVACMode.COOL: VERB_HVAC_MODE_COOL,
    HVACMode.HEAT: VERB_HVAC_MODE_HEAT,
    HVACMode.AUTO: VERB_HVAC_MODE_AUTO,
}

_FAN_MODE_VERBS = {
    _FAN_MODE_AUTO: VERB_FAN_MODE_AUTO,
    _FAN_MODE_CYCLE: VERB_FAN_MODE_CYCLE,
    _FAN_MODE_ON: VERB_FAN_MODE_ON,
}


def _suffix_address(suffix: str) -> str:
    # ``_1`` -> ``"1"`` (PROTOCOL.md §6: ThermostatAddress is a string index).
    return suffix.lstrip("_")


class SavantClimate(SavantEntity, ClimateEntity):
    """A single HVAC controller (unit index)."""

    # ASSUMPTION: temperature units are Fahrenheit — matches the observed
    # ``SchedulerSettings`` record (TemperatureScale:"Fahrenheit"); not otherwise
    # confirmed on the wire (PROTOCOL.md §5.4).
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO]
    _attr_fan_modes = [_FAN_MODE_AUTO, _FAN_MODE_CYCLE, _FAN_MODE_ON]
    _attr_min_temp = 50
    _attr_max_temp = 90
    _attr_target_temperature_step = 1.0

    def __init__(self, hub: SavantHub, device: dict[str, str], suffix: str) -> None:
        super().__init__(
            hub,
            device_key=f"climate:{device['id']}",
            device_name=device["name"],
            area=device.get("area", ""),
        )
        state_name = device.get("state_name", "")
        marker = _TEMP_ATTR
        if marker in state_name:
            self._state_prefix, self._suffix = state_name.split(marker, 1)
        else:
            self._state_prefix = HVAC_STATE_PREFIX
            self._suffix = suffix
        parts = self._state_prefix.rstrip(".").split(".")
        self._component = parts[0] if len(parts) >= 2 else "HVAC Controller"
        self._logical_component = parts[1] if len(parts) >= 2 else "HVAC_controller"
        addresses = str(device.get("addresses") or "").split(",")
        self._thermostat_address = addresses[0].strip() or _suffix_address(self._suffix)
        self._thermostat_address_2 = (
            addresses[1].strip() if len(addresses) > 1 and addresses[1].strip() else "(null)"
        )
        self._attr_unique_id = f"{hub.uid}_climate_{device['id']}"

    # ------------------------------------------------------------ state keys

    def _key(self, attr: str) -> str:
        return f"{self._state_prefix}{attr}{self._suffix}"

    def _scope(self) -> dict[str, str]:
        return {
            "component": self._component,
            "service_type": SVC_ENV_HVAC,
            "zone": "",
            "logical_component": self._logical_component,
            "variant_id": "1",
        }

    def _thermostat_args(self) -> dict[str, Any]:
        return {
            "ThermostatAddress": self._thermostat_address,
            "ThermostatAddress2": self._thermostat_address_2,
        }

    def _num(self, attr: str) -> float | None:
        value = self._state(self._key(attr))
        return float(value) if isinstance(value, (int, float)) else None

    def _bool(self, attr: str) -> bool:
        return bool(self._state(self._key(attr)))

    # ------------------------------------------------------------ ClimateEntity

    @property
    def current_temperature(self) -> float | None:
        return self._num(_TEMP_ATTR)

    @property
    def current_humidity(self) -> float | None:
        return self._num(_HUMIDITY_ATTR)

    @property
    def extra_state_attributes(self) -> dict[str, float]:
        remote_temperature = self._num(_REMOTE_TEMP_ATTR)
        return {"remote_temperature": remote_temperature} if remote_temperature is not None else {}

    @property
    def hvac_mode(self) -> HVACMode:
        for mode, flag in _MODE_FLAGS.items():
            if self._bool(flag):
                return mode
        # Fall back to the (unobserved-enum) mode string if flags are absent.
        return HVACMode.OFF

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.AUTO:
            return None
        if self.hvac_mode == HVACMode.COOL:
            return self._num(_COOL_POINT_ATTR)
        if self.hvac_mode == HVACMode.HEAT:
            return self._num(_HEAT_POINT_ATTR)
        return self._num(_SET_POINT_ATTR)

    @property
    def target_temperature_low(self) -> float | None:
        return self._num(_HEAT_POINT_ATTR)

    @property
    def target_temperature_high(self) -> float | None:
        return self._num(_COOL_POINT_ATTR)

    @property
    def fan_mode(self) -> str | None:
        value = self._state(self._key("ThermostatFanMode"))
        if isinstance(value, str) and value.lower() in _FAN_MODE_VERBS:
            return value.lower()
        for mode, flag in (
            (_FAN_MODE_AUTO, "IsThermostatCurrentFanModeAuto"),
            (_FAN_MODE_ON, "IsThermostatCurrentFanModeOn"),
        ):
            if self._bool(flag):
                return mode
        return None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        verb = _MODE_VERBS.get(hvac_mode)
        if verb is None:
            return
        await self._service_request(
            verb,
            request_args=self._thermostat_args(),
            **self._scope(),
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        verb = _FAN_MODE_VERBS.get(fan_mode.lower())
        if verb is not None:
            await self._service_request(
                verb,
                request_args=self._thermostat_args(),
                **self._scope(),
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        low = kwargs.get(_TARGET_TEMP_LOW)
        high = kwargs.get(_TARGET_TEMP_HIGH)
        if low is not None or high is not None:
            if low is not None:
                args = self._thermostat_args()
                args["HeatPointTemperature"] = low
                await self._service_request(
                    VERB_SET_HEAT_POINT,
                    request_args=args,
                    **self._scope(),
                )
            if high is not None:
                args = self._thermostat_args()
                args["CoolPointTemperature"] = high
                await self._service_request(
                    VERB_SET_COOL_POINT,
                    request_args=args,
                    **self._scope(),
                )
            return
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        args = self._thermostat_args()
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            args["CoolPointTemperature"] = temperature
            await self._service_request(
                VERB_SET_COOL_POINT,
                request_args=args,
                **self._scope(),
            )
        else:
            args["HeatPointTemperature"] = temperature
            await self._service_request(
                VERB_SET_HEAT_POINT,
                request_args=args,
                **self._scope(),
            )


def _discovered_suffixes(hub: SavantHub) -> set[str]:
    suffixes: set[str] = set()
    marker = f"{HVAC_STATE_PREFIX}{_TEMP_ATTR}"
    for key in hub.states:
        if key.startswith(marker):
            suffixes.add(key[len(marker):])
    return suffixes


def _build_entities(hub: SavantHub) -> list[SavantClimate]:
    if hub.devices is not None:
        climate_devices = [
            d for d in hub.devices if d.get("type") == DEVICE_TYPE_CLIMATE
        ]
        # ASSUMPTION: the Nth thermostat maps to state suffix "_<N>" (ThermostatAddress
        # "<N>"); the exact per-entity unit index is not in the documented schema.
        return [
            SavantClimate(hub, device, f"_{index + 1}")
            for index, device in enumerate(climate_devices)
        ]
    return [
        SavantClimate(hub, {"id": suffix, "name": "Thermostat"}, suffix)
        for suffix in sorted(_discovered_suffixes(hub))
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: SavantHub = hass.data[DOMAIN][entry.entry_id]

    def _add() -> None:
        entities = [e for e in _build_entities(hub) if not hub.is_created(e.unique_id)]
        if entities:
            hub.mark_created([e.unique_id for e in entities])
            async_add_entities(entities)

    _add()
    hub.add_platform_callback(_add)
