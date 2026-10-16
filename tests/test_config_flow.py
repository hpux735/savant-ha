"""Exercise config-flow decisions with lightweight HA registry/UI stand-ins."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def flow_module(monkeypatch):
    """Load the real flow and voluptuous schemas without installing HA Core."""
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_show_menu(self, **kwargs):
            return {"type": "menu", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

    class Selector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            return value

    modules = {}
    for name in (
        "homeassistant", "homeassistant.config_entries", "homeassistant.const",
        "homeassistant.core", "homeassistant.data_entry_flow", "homeassistant.helpers",
        "homeassistant.helpers.area_registry", "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_registry", "homeassistant.helpers.selector",
    ):
        modules[name] = module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
    for name, module in modules.items():
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(modules[parent], attr, module)
    entries = modules["homeassistant.config_entries"]
    entries.ConfigFlow = ConfigFlow
    entries.ConfigEntry = SimpleNamespace
    entries.OptionsFlow = ConfigFlow
    const = modules["homeassistant.const"]
    const.CONF_HOST = "host"
    const.CONF_USERNAME = "username"
    const.CONF_PASSWORD = "password"
    modules["homeassistant.core"].callback = lambda fn: fn
    modules["homeassistant.data_entry_flow"].FlowResult = dict
    selector = modules["homeassistant.helpers.selector"]
    selector.TextSelector = selector.SelectSelector = Selector
    selector.TextSelectorConfig = selector.SelectSelectorConfig = lambda **kwargs: kwargs
    selector.TextSelectorType = SimpleNamespace(PASSWORD="password")
    for name in ("area", "device", "entity"):
        modules[f"homeassistant.helpers.{name}_registry"].async_get = Mock()

    name = "custom_components.savant_ha.config_flow"
    path = Path(__file__).parents[1] / "custom_components/savant_ha/config_flow.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def flow(flow_module):
    flow = flow_module.SavantConfigFlow()
    entry = SimpleNamespace(entry_id="lutron-entry", disabled_by=None)
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_entries=Mock(side_effect=lambda domain: [entry] if domain == "lutron_caseta" else []),
        async_update_entry=Mock(),
    ))
    entity = SimpleNamespace(
        entity_id="light.kitchen_ceiling", platform="lutron_caseta",
        device_id="device-1", area_id=None, disabled_by=None,
        name=None, original_name="Kitchen Ceiling",
    )
    device = SimpleNamespace(
        area_id="kitchen", disabled_by=None, name="Kitchen Ceiling", name_by_user=None,
    )
    flow_module.er.async_entries_for_config_entry = Mock(return_value=[entity])
    flow_module.dr.async_get.return_value.async_get.return_value = device
    flow_module.ar.async_get.return_value.async_get_area.side_effect = lambda id: {
        "kitchen": SimpleNamespace(name="Kitchen"), "bedroom": SimpleNamespace(name="Bedroom"),
    }.get(id)
    flow._devices = [{"type": "light", "id": "savant-1", "name": "Ceiling", "area": "Kitchen"}]
    return flow


def test_registry_match_uses_caseta_light_name_and_device_area(flow):
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}


@pytest.mark.parametrize("field, value", [
    ("disabled_by", "user"), ("platform", "other"), ("entity_id", "cover.ceiling"),
    ("area_id", "bedroom"), ("area_id", "unknown"),
])
def test_nonmatching_registry_entities_are_not_used(flow_module, flow, field, value):
    entity = flow_module.er.async_entries_for_config_entry.return_value[0]
    setattr(entity, field, value)
    assert flow._find_lutron_duplicates() == {}


def test_disabled_lutron_entry_is_not_used(flow):
    flow.hass.config_entries.async_entries("lutron_caseta")[0].disabled_by = "user"
    assert flow._find_lutron_duplicates() == {}


def test_disabled_device_is_not_used(flow_module, flow):
    flow_module.dr.async_get.return_value.async_get.return_value.disabled_by = "user"
    assert flow._find_lutron_duplicates() == {}


def test_missing_area_is_not_used(flow_module, flow):
    flow_module.dr.async_get.return_value.async_get.return_value.area_id = None
    assert flow._find_lutron_duplicates() == {}


def test_entity_area_override_takes_precedence_over_device_area(flow_module, flow):
    flow_module.dr.async_get.return_value.async_get.return_value.area_id = "bedroom"
    flow_module.er.async_entries_for_config_entry.return_value[0].area_id = "kitchen"
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}


def test_device_name_is_used_when_entity_has_no_name(flow_module, flow):
    flow_module.er.async_entries_for_config_entry.return_value[0].original_name = None
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}


def test_entity_rename_does_not_discard_original_name(flow_module, flow):
    flow_module.er.async_entries_for_config_entry.return_value[0].name = "Renamed Light"
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}


@pytest.mark.parametrize("field", ["name", "name_by_user"])
def test_device_names_are_candidates_even_when_entity_has_a_name(flow_module, flow, field):
    flow._devices[0]["name"] = "Accent"
    device = flow_module.dr.async_get.return_value.async_get.return_value
    setattr(device, field, "Kitchen Accent")
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}


def test_registry_aliases_still_require_unique_matches(flow_module, flow):
    entity = flow_module.er.async_entries_for_config_entry.return_value[0]
    flow_module.er.async_entries_for_config_entry.return_value.append(SimpleNamespace(
        **{**vars(entity), "entity_id": "light.second", "name": "Kitchen Ceiling"}
    ))
    assert flow._find_lutron_duplicates() == {}


def test_lutron_scan_logs_missing_integration(flow, caplog):
    caplog.set_level("DEBUG", logger="custom_components.savant_ha")
    flow.hass.config_entries.async_entries.return_value = []
    flow.hass.config_entries.async_entries.side_effect = None
    assert flow._find_lutron_duplicates() == {}
    assert "registry scan entries=0 enabled_entries=0" in caplog.text
    assert "registry_lights=0 eligible_lights=0" in caplog.text


@pytest.mark.parametrize("reason", ["missing_area", "missing_name", "disabled_entities", "disabled_devices"])
def test_lutron_scan_logs_exclusion_counts_without_names(flow_module, flow, caplog, reason):
    caplog.set_level("DEBUG", logger="custom_components.savant_ha")
    entity = flow_module.er.async_entries_for_config_entry.return_value[0]
    device = flow_module.dr.async_get.return_value.async_get.return_value
    if reason == "missing_area":
        device.area_id = None
    elif reason == "missing_name":
        entity.name = entity.original_name = device.name_by_user = device.name = None
    elif reason == "disabled_entities":
        entity.disabled_by = "user"
    else:
        device.disabled_by = "user"
    assert flow._find_lutron_duplicates() == {}
    assert f"{reason}=1" in caplog.text
    assert "registry_lights=1 eligible_lights=0" in caplog.text
    summaries = "\n".join(record.message for record in caplog.records if "registry scan" in record.message)
    assert "Kitchen" not in summaries
    assert "light.kitchen_ceiling" not in caplog.text


def test_registry_names_are_logged_only_at_debug(flow_module, flow, caplog):
    caplog.set_level("INFO", logger="custom_components.savant_ha")
    flow._find_lutron_duplicates()
    assert "registry candidate" not in caplog.text
    caplog.set_level("DEBUG", logger="custom_components.savant_ha")
    assert flow._find_lutron_duplicates() == {"savant-1": "light.kitchen_ceiling"}
    assert "registry_lights=1 eligible_lights=1" in caplog.text
    assert "registry candidate domain=light names=['Kitchen Ceiling', 'Kitchen Ceiling'] area='Kitchen'" in caplog.text
    assert "light.kitchen_ceiling" not in caplog.text


@pytest.fixture
def cover_flow(flow_module, flow):
    flow._devices[0]["type"] = "cover"
    flow_module.er.async_entries_for_config_entry.return_value[0].entity_id = "cover.kitchen_ceiling"
    return flow


def test_registry_collects_cover_candidates(flow_module, cover_flow, caplog):
    caplog.set_level("DEBUG", logger="custom_components.savant_ha")
    assert cover_flow._find_lutron_duplicates() == {"savant-1": "cover.kitchen_ceiling"}
    assert "registry_covers=1 eligible_covers=1" in caplog.text
    assert "registry candidate domain=cover" in caplog.text


def test_cover_picker_is_unchecked_but_allows_override(cover_flow):
    cover_flow._lutron_duplicates = cover_flow._find_lutron_duplicates()
    result = asyncio.run(cover_flow.async_step_devices())
    assert result["data_schema"]({}) == {"devices": []}
    selector = next(iter(result["data_schema"].schema.values()))
    assert "Possible Lutron duplicate: cover.kitchen_ceiling" in selector.config["options"][0]["label"]
    result = asyncio.run(cover_flow.async_step_devices({"devices": ["savant-1"]}))
    assert result["data"]["devices"] == cover_flow._devices


def test_reconfigure_preserves_previously_imported_cover(flow_module, cover_flow, monkeypatch):
    entry = SimpleNamespace(data={"devices": list(cover_flow._devices)})
    cover_flow._get_reconfigure_entry = lambda: entry
    cover_flow._discover_configured_entry = AsyncMock(return_value=SimpleNamespace(
        host="example.invalid", port=12345, home_id="",
    ))
    monkeypatch.setattr(flow_module, "_devices_from_info", lambda probe: cover_flow._devices)
    monkeypatch.setattr(flow_module, "probe_host", AsyncMock(return_value=SimpleNamespace(
        authorized=True, devices=[object()],
    )))
    result = asyncio.run(cover_flow.async_step_reconfigure({"username": "test", "password": "test"}))
    assert result["data_schema"]({}) == {"devices": ["savant-1"]}
    cover_flow.hass.config_entries.async_update_entry.assert_not_called()


def test_new_import_suggests_unchecked_duplicate_and_allows_override(flow_module, flow, monkeypatch):
    devices = flow._devices + [{"type": "light", "id": "savant-2", "name": "Walls", "area": "Kitchen"}]
    monkeypatch.setattr(flow_module, "_devices_from_info", lambda probe: devices)
    monkeypatch.setattr(flow_module, "probe_host", AsyncMock(return_value=SimpleNamespace(
        authorized=True, devices=[object()],
    )))
    result = asyncio.run(flow.async_step_login({"username": "test", "password": "test"}))
    assert result["step_id"] == "devices"
    assert result["data_schema"]({}) == {"devices": ["savant-2"]}
    selector = next(iter(result["data_schema"].schema.values()))
    assert len(selector.config["options"]) == 2
    assert "Possible Lutron duplicate: light.kitchen_ceiling" in selector.config["options"][0]["label"]
    result = asyncio.run(flow.async_step_devices({"devices": ["savant-1", "savant-2"]}))
    assert result["data"]["devices"] == devices
    assert "lutron_duplicates" not in result["data"]


def test_reconfigure_keeps_existing_savant_duplicate_selected(flow_module, flow, monkeypatch):
    entry = SimpleNamespace(data={"devices": list(flow._devices)})
    flow._get_reconfigure_entry = lambda: entry
    flow._discover_configured_entry = AsyncMock(return_value=SimpleNamespace(
        host="example.invalid", port=12345, home_id="",
    ))
    devices = flow._devices + [{"type": "light", "id": "savant-2", "name": "Walls", "area": "Kitchen"}]
    entity = flow_module.er.async_entries_for_config_entry.return_value[0]
    flow_module.er.async_entries_for_config_entry.return_value.append(SimpleNamespace(
        **{**vars(entity), "entity_id": "light.kitchen_walls", "original_name": "Kitchen Walls", "device_id": "device-2"}
    ))
    ceiling = flow_module.dr.async_get.return_value.async_get.return_value
    walls = SimpleNamespace(**{**vars(ceiling), "name": "Kitchen Walls"})
    flow_module.dr.async_get.return_value.async_get.side_effect = lambda id: {
        "device-1": ceiling, "device-2": walls,
    }.get(id)
    monkeypatch.setattr(flow_module, "_devices_from_info", lambda probe: devices)
    monkeypatch.setattr(flow_module, "probe_host", AsyncMock(return_value=SimpleNamespace(
        authorized=True, devices=[object()],
    )))
    result = asyncio.run(flow.async_step_reconfigure({"username": "test", "password": "test"}))
    assert result["step_id"] == "reconfigure_devices"
    assert result["data_schema"]({}) == {"devices": ["savant-1"]}
    assert flow._lutron_duplicates == {
        "savant-1": "light.kitchen_ceiling", "savant-2": "light.kitchen_walls",
    }
    flow.hass.config_entries.async_update_entry.assert_not_called()
    result = asyncio.run(flow.async_step_reconfigure_devices({"devices": ["savant-1"]}))
    assert result["reason"] == "reconfigure_successful"
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]["devices"] == entry.data["devices"]


def test_coolmaster_continue_retains_lutron_suggestions(flow):
    flow._lutron_duplicates = flow._find_lutron_duplicates()
    result = asyncio.run(flow.async_step_continue_without_coolmaster())
    assert result["data_schema"]({}) == {"devices": []}
