"""Test scaffolding.

The integration's ``__init__.py`` imports ``homeassistant``, which is not installed in
the lightweight test venv.  The pure protocol modules (``const`` and ``savant_client``)
have no HA dependency, so we load them directly and pre-register a stub parent package —
this lets test modules import them normally without triggering the HA import.
"""

import importlib.util
import os
import sys
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

_PKG = os.path.join(_REPO_ROOT, "custom_components", "savant_ha")

for _parent in ("custom_components", "custom_components.savant_ha"):
    if _parent not in sys.modules:
        sys.modules[_parent] = types.ModuleType(_parent)


def _load(name: str, relpath: str) -> None:
    path = os.path.join(_PKG, relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name, _rel in (
    ("custom_components.savant_ha.const", "const.py"),
    ("custom_components.savant_ha.uiconfig", "uiconfig.py"),
    ("custom_components.savant_ha.savant_client", "savant_client.py"),
):
    if _name not in sys.modules:
        _load(_name, _rel)
