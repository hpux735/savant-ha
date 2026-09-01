"""Tests for archive-derived ``service/request`` payload construction (control.py).

These capture the reverse-engineered, live-verified control surface: DimmerSet/SwitchOn
lighting payloads, archive-derived HVAC scope and thermostat addresses, shade command
addresses, and audio-zone state-key prefixes.
"""

from __future__ import annotations

from custom_components.savant_ha import control
from custom_components.savant_ha.const import (
    HVAC_STATE_PREFIX,
    SVC_AV_SAVANTMUSIC,
    SVC_ENV_HVAC,
)


def _light(**overrides):
    device = {
        "id": "1",
        "type": "light",
        "name": "Kitchen Overhead",
        "room": "Kitchen",
        "addresses": "002,1,(null),(null),(null),(null)",
        "state_name": "Savant.Lighting.CurrentDimmerLevel_1_002",
        "control": {
            "entity_type": "Dimmer",
            "dimmer_command": "DimmerSet",
            "fade_time": 2,
            "delay_time": 0,
            "technology": "Custom 1",
        },
    }
    device.update(overrides)
    return device


# --------------------------------------------------------------- lighting


def test_light_address_args_pads_to_six():
    assert control.light_address_args("00C,1") == {
        "Address1": "00C",
        "Address2": "1",
        "Address3": "(null)",
        "Address4": "(null)",
        "Address5": "(null)",
        "Address6": "(null)",
    }


def test_light_address_args_empty_address_is_null():
    assert control.light_address_args("") == {
        "Address1": "(null)",
        "Address2": "(null)",
        "Address3": "(null)",
        "Address4": "(null)",
        "Address5": "(null)",
        "Address6": "(null)",
    }


def test_dimmer_args_uses_archive_fade_delay_and_curve():
    args = control.dimmer_args(_light(), 100)
    assert args["DimmerLevel"] == 100
    assert args["Address1"] == "002"
    assert args["Address2"] == "1"
    assert args["Address6"] == "(null)"
    assert args["FadeTime"] == 2
    assert args["DelayTime"] == 0
    assert args["Curve"] == "Custom 1"


def test_dimmer_args_uses_technology_for_curve():
    device = _light(control={"entity_type": "DMX", "technology": "Infinite Color"})
    assert control.dimmer_args(device, 50)["Curve"] == "Infinite Color"


def test_color_dimmer_args_uses_nested_rgbw_color_without_flat_defaults():
    device = _light(
        state_name="Savant.Lighting.CurrentColor_6_006",
        control={"entity_type": "DMX", "technology": "Infinite Color"},
    )
    args = control.dimmer_args(device, 50, (255, 64, 32, 16))
    assert args["bleColor"] == {
        "red": 255,
        "green": 64,
        "blue": 32,
        "white": 16,
        "kelvin": 0,
    }
    assert "bleColorRed" not in args


def test_is_color_light_uses_color_state_names():
    assert control.is_color_light(
        _light(state_name="Savant.Lighting.CurrentColor_6_006")
    )
    assert control.is_color_light(
        _light(state_name="Savant.Lighting.CurrentBleColor_2_004")
    )
    assert not control.is_color_light(_light())


def test_color_dimmer_brightness_only_omits_color_to_preserve_host_state():
    device = _light(
        state_name="Savant.Lighting.CurrentColor_6_006",
        control={"entity_type": "DMX", "technology": "Infinite Color"},
    )
    args = control.dimmer_args(device, 50)
    assert "bleColor" not in args
    assert "bleColorRed" not in args


def test_color_dimmer_can_omit_color_when_no_state_is_known():
    device = _light(
        state_name="Savant.Lighting.CurrentColor_6_006",
        control={"entity_type": "DMX", "technology": "Infinite Color"},
    )
    args = control.dimmer_args(device, 100, use_last_dimmer_value=True)
    assert args["useLastDimmerValue"] is True
    assert "bleColor" not in args


def test_dimmer_args_includes_flat_ble_color_keys():
    # The archive's DimmerSet definition requires these flat keys even for dimmers.
    args = control.dimmer_args(_light(), 100)
    for key in (
        "bleColorRed",
        "bleColorGreen",
        "bleColorBlue",
        "bleColorWhite",
        "kelvin",
    ):
        assert key in args
        assert args[key] == 0


def test_dimmer_args_defaults_fade_time_when_absent():
    device = _light(control={"entity_type": "Dimmer"})
    args = control.dimmer_args(device, 100)
    assert args["FadeTime"] == "0.5"
    assert args["DelayTime"] == "0"


def test_dimmer_command_defaults_and_archive_override():
    assert control.dimmer_command(_light()) == "DimmerSet"
    device = _light(control={"entity_type": "Dimmer", "dimmer_command": "DimUp"})
    assert control.dimmer_command(device) == "DimUp"


def test_is_switch_distinguishes_switch_from_dimmer():
    assert control.is_switch(_light()) is False
    assert control.is_switch(_light(control={"entity_type": "Switch"})) is True
    assert control.is_switch(_light(control={})) is False


def test_state_name_component_and_logical():
    assert control.state_name_component("Savant.Lighting.CurrentDimmerLevel_1_002") == "Savant"
    assert control.state_name_logical("Savant.Lighting.CurrentDimmerLevel_1_002") == "Lighting"
    assert control.state_name_component("") == ""
    assert control.state_name_logical("") == ""


# ----------------------------------------------------------------- climate


def test_climate_identity_derives_component_logical_and_suffix():
    prefix, suffix, component, logical = control.climate_identity(
        "CLIW220.HVAC_controller.ThermostatCurrentTemperature_1", "_1"
    )
    assert prefix == "CLIW220.HVAC_controller."
    assert suffix == "_1"
    assert component == "CLIW220"
    assert logical == "HVAC_controller"


def test_climate_identity_handles_non_default_suffix():
    prefix, suffix, component, logical = control.climate_identity(
        "New Thermostat.HVAC_controller.ThermostatCurrentTemperature_7", "_7"
    )
    assert suffix == "_7"
    assert component == "New Thermostat"


def test_climate_identity_falls_back_to_default_prefix():
    prefix, suffix, component, logical = control.climate_identity("", "_3")
    assert prefix == HVAC_STATE_PREFIX
    assert suffix == "_3"
    assert component == "HVAC Controller"
    assert logical == "HVAC_controller"


def test_climate_scope_is_archive_derived_with_empty_zone():
    assert control.climate_scope("CLIW220", "HVAC_controller") == {
        "component": "CLIW220",
        "service_type": SVC_ENV_HVAC,
        "zone": "",
        "logical_component": "HVAC_controller",
        "variant_id": "1",
    }


def test_thermostat_args_single_point_uses_null_second_address():
    assert control.thermostat_args("1,", "_1") == {
        "ThermostatAddress": "1",
        "ThermostatAddress2": "(null)",
    }


def test_thermostat_args_falls_back_to_suffix_when_no_addresses():
    assert control.thermostat_args("", "_7") == {
        "ThermostatAddress": "7",
        "ThermostatAddress2": "(null)",
    }


def test_suffix_address_strips_underscore():
    assert control.suffix_address("_1") == "1"
    assert control.suffix_address("_12") == "12"


# ------------------------------------------------------------------- shade


def test_shade_address_args_uses_five_addresses():
    assert control.shade_address_args("c2aac4873a450684,,,,") == {
        "Address1": "c2aac4873a450684",
        "Address2": "(null)",
        "Address3": "(null)",
        "Address4": "(null)",
        "Address5": "(null)",
    }


def test_shade_component_logical_from_state_name():
    assert control.shade_component_logical(
        "Bond Bridge.Lighting_controller.ShadeLevel_c2aac4873a450684"
    ) == ("Bond Bridge", "Lighting_controller")


# -------------------------------------------------------------- media player


def test_audio_zone_logical_component_from_zone_or_name():
    assert control.audio_zone_logical_component({"zone": "Audio Zone 3"}) == "Audio Zone 3"
    assert control.audio_zone_logical_component({"name": "Audio Zone 2"}) == "Audio Zone 2"
    assert control.audio_zone_logical_component({"name": "Other"}) is None


def test_zone_state_prefix_includes_service_type():
    assert (
        control.zone_state_prefix("Music", "Audio Zone 1")
        == f"Music.Audio Zone 1.{SVC_AV_SAVANTMUSIC}."
    )


# ------------------------------------------------------------ light state parsing


def test_parse_dimmer_level_on_and_off():
    assert control.parse_light_state(
        "Savant.Lighting.CurrentDimmerLevel_1_002", 100
    ) == (True, 255)
    assert control.parse_light_state(
        "Savant.Lighting.CurrentDimmerLevel_1_002", 50
    ) == (True, 128)
    assert control.parse_light_state(
        "Savant.Lighting.CurrentDimmerLevel_1_002", 0
    ) == (False, 0)


def test_parse_switch_state_uses_dimmer_level():
    # Switches also report CurrentDimmerLevel (0 = off, 100 = on); their *control* uses
    # SwitchOn/SwitchOff, but their *state* parses identically to a dimmer.
    assert control.parse_light_state(
        "Savant.Lighting.CurrentDimmerLevel_1_010", 100
    ) == (True, 255)
    assert control.parse_light_state(
        "Savant.Lighting.CurrentDimmerLevel_1_010", 0
    ) == (False, 0)


def test_parse_color_string_on_full_white():
    value = "083,079,245,000,096,096|6000,096,096|Custom 1"
    assert control.parse_light_state("Savant.Lighting.CurrentColor_6_006", value) == (
        True,
        245,
    )


def test_parse_color_string_off():
    value = "000,000,000,000,000,000|6000,000,000|Custom 1"
    assert control.parse_light_state("Savant.Lighting.CurrentColor_6_006", value) == (
        False,
        0,
    )


def test_parse_color_string_retained_rgbw_with_zero_level_is_off():
    # The host retains RGBW channels after a power-off; the embedded level is the
    # authoritative on/off value when it is present.
    value = "083,079,245,016,000,000|6000,000,000|Custom 1"
    assert control.parse_light_state("Savant.Lighting.CurrentColor_6_006", value) == (
        False,
        0,
    )


def test_parse_color_string_without_level_uses_max_channel():
    value = "000,000,128,000|6000,000,000|Infinite Color"
    assert control.parse_light_state("Savant.Lighting.CurrentColor_6_006", value) == (
        True,
        128,
    )


def test_parse_color_string_extracts_rgbw_channels():
    assert control.parse_light_color(
        "Savant.Lighting.CurrentColor_6_006",
        "083,079,245,016,096,096|6000,096,096|Custom 1",
    ) == (83, 79, 245, 16)


def test_parse_light_state_unrecognized_returns_none():
    assert control.parse_light_state("Savant.Lighting.CurrentColor_6_006", 123) == (
        None,
        None,
    )
    assert control.parse_light_state("Savant.Lighting.CurrentLEDState_1_002", "on") == (
        None,
        None,
    )


def test_coerce_number_accepts_strings_and_numbers():
    assert control.coerce_number("72") == 72.0
    assert control.coerce_number(72) == 72.0
    assert control.coerce_number("72.5") == 72.5
    assert control.coerce_number("not-a-number") is None
    assert control.coerce_number(True) is None
