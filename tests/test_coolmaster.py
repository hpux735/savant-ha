"""Tests for direct CoolMasterNet coexistence filtering."""

from custom_components.savant_ha.coolmaster import (
    exclude_duplicate_coolmaster_devices,
    is_coolmaster_climate,
)


def _coolmaster(name: str) -> dict[str, str]:
    return {
        "type": "climate",
        "name": name,
        "state_name": "CoolMaster.HVAC.ThermostatCurrentTemperature_1_001",
    }


def test_is_coolmaster_climate_requires_the_observed_two_address_shape():
    assert is_coolmaster_climate(_coolmaster("Office"))
    assert not is_coolmaster_climate(
        {
            "type": "climate",
            "name": "Office",
            "state_name": "HVAC.HVAC_controller.ThermostatCurrentTemperature_1",
        }
    )


def test_exclude_duplicate_coolmaster_devices_keeps_nonmatching_devices():
    office = _coolmaster("Office")
    bedroom = _coolmaster("Bedroom")
    thermostat = {
        "type": "climate",
        "name": "Main Thermostat",
        "state_name": "HVAC.HVAC_controller.ThermostatCurrentTemperature_1",
    }
    light = {"type": "light", "name": "Office", "state_name": "Lighting.Level_1"}

    devices = exclude_duplicate_coolmaster_devices(
        [office, bedroom, thermostat, light], {" office "}
    )

    assert devices == [bedroom, thermostat, light]
