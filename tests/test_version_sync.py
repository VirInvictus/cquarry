"""The version-sync guard: every carrier of the version must agree.

Eight carriers of the version live in this repo (VERSION, pyproject.toml,
src/cquarry/__init__.py, src/cquarry/config.py, spec.md, API.md, the
patchnotes head, and the annotated v tag); the 2026-09-08 sweep noted
nothing guarded them and carriers had drifted before (1.12.0 shipped
with config.py and API.md stale). The 2026-09-15 blitz added the
patchnotes head (a release forgetting its entry used to pass the suite)
and __init__.py to the guard.

2026-09-16: this file was plain pytest-style functions, which
``unittest discover`` (how CI runs the suite) silently collected ZERO
of -- so v1.23.1 shipped with four stale carriers while CI stayed
green. The guard is now a TestCase like the rest of the suite, so the
discover run that gates every push actually executes it."""

import re
import unittest
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


class TestVersionSync(unittest.TestCase):
    def test_version_carriers_agree(self):
        self.assertEqual(_pyproject_version(), VERSION, "pyproject.toml")
        for name in ("src/cquarry/config.py", "src/cquarry/__init__.py"):
            self.assertIn(f'VERSION = "{VERSION}"', _read_text(name), name)
        for name in ("spec.md", "API.md"):
            self.assertIn(f"**Version:** {VERSION}", _read_text(name), name)
        self.assertEqual(_read_text("VERSION").strip(), VERSION, "VERSION")

    def test_patchnotes_head_is_the_current_release(self):
        first = _read_text("patchnotes.md").splitlines()[0]
        self.assertTrue(
            first.startswith(f"## v{VERSION} ("),
            f"patchnotes head {first!r} is not the current release's entry",
        )


if __name__ == "__main__":
    unittest.main()
