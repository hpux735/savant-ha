"""Repository-level quality and privacy checks."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_prompt_ledger_contains_no_private_ipv4_addresses():
    prompts = (ROOT / "savant_ha_prompts.csv").read_text()
    private_ip = re.compile(
        r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
    )
    assert private_ip.search(prompts) is None
