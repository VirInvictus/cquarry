"""Tests for cquarry.config (defaults, load/save, degradation)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from cquarry import config


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        patcher = mock.patch.object(
            config, "CONFIG_FILE", os.path.join(self.temp_dir, "config.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_missing_config_is_empty(self):
        self.assertEqual(config.load_config(), {})

    def test_valid_config_round_trips(self):
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"db_path": "/tmp/x/metadata.db"}, f)
        self.assertEqual(config.load_config(), {"db_path": "/tmp/x/metadata.db"})

    def test_broken_config_degrades_with_a_note(self):
        # The six-lens-audit LOW: a broken file used to fail silently into
        # the "no config" path; now the failure is narrowed (OSError and
        # JSONDecodeError only) and a note lands on stderr.
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(config.load_config(), {})
        self.assertIn("NOTE: ignoring unreadable config", err.getvalue())

    def test_unreadable_config_degrades_with_a_note(self):
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(config.CONFIG_FILE, 0o000)
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(config.load_config(), {})
            self.assertIn("NOTE: ignoring unreadable config", err.getvalue())
        finally:
            os.chmod(config.CONFIG_FILE, 0o600)


class TestVersionCarrier(unittest.TestCase):
    def test_config_version_matches_package(self):
        from cquarry import VERSION

        self.assertEqual(config.VERSION, VERSION)


if __name__ == "__main__":
    unittest.main()
