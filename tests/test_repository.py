"""Repository-level quality and privacy checks."""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_config_flow_forms_and_menus_have_step_handlers():
    """HA validates the returned step, including menus, before displaying it."""
    tree = ast.parse((ROOT / "custom_components/savant_ha/config_flow.py").read_text())
    for flow in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        handlers = {
            node.name.removeprefix("async_step_")
            for node in flow.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name.startswith("async_step_")
        }
        for node in ast.walk(flow):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"async_show_form", "async_show_menu"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "step_id":
                    assert isinstance(keyword.value, ast.Constant)
                    assert keyword.value.value in handlers, (
                        f"{flow.name} has no handler for step {keyword.value.value}"
                    )
                elif keyword.arg == "menu_options":
                    assert isinstance(keyword.value, (ast.List, ast.Dict))
                    options = keyword.value.elts if isinstance(keyword.value, ast.List) else keyword.value.keys
                    for option in options:
                        assert isinstance(option, ast.Constant)
                        assert option.value in handlers, (
                            f"{flow.name} has no handler for menu option {option.value}"
                        )


def test_lutron_choice_has_matching_english_ui_text():
    integration = ROOT / "custom_components/savant_ha"
    strings = json.loads((integration / "strings.json").read_text())
    english = json.loads((integration / "translations/en.json").read_text())
    assert strings == english
    step = strings["config"]["step"]["lutron"]
    assert set(step["menu_options"]) == {"exclude_lutron", "include_lutron"}
    assert "{count}" in step["description"]


def test_prompt_ledger_contains_no_private_ipv4_addresses():
    prompts = (ROOT / "savant_ha_prompts.csv").read_text()
    private_ip = re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    )
    assert private_ip.search(prompts) is None
