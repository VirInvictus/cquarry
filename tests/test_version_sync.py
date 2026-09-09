"""The version-sync guard: every carrier of the version must agree.

Six copies of the version live in this repo (VERSION, pyproject.toml,
src/cquarry/__init__.py, src/cquarry/config.py, spec.md, API.md); the
2026-09-08 sweep noted nothing guarded them and carriers had drifted
before (1.12.0 shipped with config.py and API.md stale)."""

import re
from pathlib import Path

from cquarry import VERSION

_REPO = Path(__file__).resolve().parent.parent


def _read_text(name: str) -> str:
    return (_REPO / name).read_text(encoding="utf-8")


def _pyproject_version() -> str:
    raw = _read_text("pyproject.toml")
    match = re.search(r'^version = "([^"]+)"$', raw, re.MULTILINE)
    assert match, "pyproject.toml has no version line"
    return match.group(1)


def test_version_carriers_agree():
    assert _pyproject_version() == VERSION
    assert f'VERSION = "{VERSION}"' in _read_text("src/cquarry/config.py")
    assert f"**Version:** {VERSION}" in _read_text("spec.md")
    assert f"**Version:** {VERSION}" in _read_text("API.md")
    assert _read_text("VERSION").strip() == VERSION
