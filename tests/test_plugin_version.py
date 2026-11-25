"""The Claude Code plugin describes the library release it ships with."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_version_matches_the_library():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    plugin = json.loads((ROOT / "plugins/fastapi-testing/.claude-plugin/plugin.json").read_text())
    assert plugin["version"] == project["version"], "bump plugin.json's version in the release PR"


def test_marketplace_lists_the_plugin_by_its_manifest_name():
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    plugin = json.loads((ROOT / "plugins/fastapi-testing/.claude-plugin/plugin.json").read_text())
    [entry] = marketplace["plugins"]
    assert entry["name"] == plugin["name"]
    assert (ROOT / entry["source"] / ".claude-plugin/plugin.json").is_file()
