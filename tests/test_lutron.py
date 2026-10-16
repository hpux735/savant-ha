"""Tests for conservative direct-Lutron duplicate suggestions."""

import pytest

from custom_components.savant_ha.lutron import find_duplicate_lutron_lights


def _device(id="savant-1", name="Ceiling", area="Kitchen", type="light"):
    return {"id": id, "name": name, "area": area, "type": type}


def _light(entity_id="light.kitchen_ceiling", name="Kitchen Ceiling", area="Kitchen"):
    return {"entity_id": entity_id, "names": [name], "area": area}


@pytest.mark.parametrize("name", ["Ceiling", "Kitchen Ceiling", "  KITCHEN   ceiling  "])
def test_exact_area_and_name_suggest_duplicate(name):
    device = _device(name=name)
    light = _light()
    assert find_duplicate_lutron_lights([device], [light]) == {
        "savant-1": "light.kitchen_ceiling"
    }
    assert device["name"] == name
    assert light["names"] == ["Kitchen Ceiling"]


@pytest.mark.parametrize("device", [
    _device(area="Bedroom"),
    _device(area=""),
    _device(name=""),
    _device(name="Ceiling Fan"),
    _device(name="Ceiling-Light"),
    _device(type="cover"),
    _device(type="climate"),
])
def test_nonmatching_or_nonlight_device_is_not_suggested(device):
    assert find_duplicate_lutron_lights([device], [_light()]) == {}


def test_empty_lutron_inventory_keeps_everything():
    assert find_duplicate_lutron_lights([_device()], []) == {}


def test_same_name_in_another_area_does_not_create_ambiguity():
    assert find_duplicate_lutron_lights(
        [_device(), _device(id="savant-2", area="Bedroom")], [_light()]
    ) == {"savant-1": "light.kitchen_ceiling"}


def test_multiple_lutron_entities_or_bridges_are_ambiguous():
    assert find_duplicate_lutron_lights(
        [_device()], [_light(), _light(entity_id="light.second_bridge_ceiling")]
    ) == {}


def test_multiple_savant_lights_matching_one_lutron_entity_are_ambiguous():
    assert find_duplicate_lutron_lights(
        [_device(), _device(id="savant-2", name="Kitchen Ceiling")], [_light()]
    ) == {}


def test_ambiguous_savant_candidate_also_blocks_a_shared_lutron_match():
    assert find_duplicate_lutron_lights(
        [_device(), _device(id="savant-2", name="Second")],
        [{"entity_id": "light.shared", "area": "Kitchen", "names": ["Ceiling", "Second"]},
         _light(entity_id="light.second", name="Second")],
    ) == {}


def test_area_prefix_requires_a_whole_word_boundary():
    assert find_duplicate_lutron_lights(
        [_device(name="ette Ceiling")], [_light(name="Kitchenette Ceiling")]
    ) == {}


@pytest.mark.parametrize("device, lights, reason", [
    (_device(area=""), [_light()], "missing_area=1"),
    (_device(name=""), [_light()], "missing_name=1"),
    (_device(area="Bedroom"), [_light()], "no_matching_area=1"),
    (_device(name="Accent"), [_light()], "no_matching_name=1"),
    (_device(), [_light(), _light(entity_id="light.second")], "ambiguous=1"),
    (_device(), [_light()], "suggested=1"),
])
def test_matching_summary_reports_reasons_without_identifiers(caplog, device, lights, reason):
    caplog.set_level("DEBUG", logger="custom_components.savant_ha")
    find_duplicate_lutron_lights([device], lights)
    assert "Savant Lutron matching summary savant_lights=1" in caplog.text
    assert reason in caplog.text
    for private in ("Kitchen", "Bedroom", "Ceiling", "Accent", "light.kitchen_ceiling", "savant-1"):
        assert private not in caplog.text
