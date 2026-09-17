"""Coverage fills for the discovery and formatting helpers (the wave-2
test-only lane): find_db's four-step resolution chain including the
EOFError/KeyboardInterrupt translation, the config save/get/set trio,
color(), normalize_author_display(), author_sort_key(), and a direct
canonical_language() test."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from cquarry import config, helpers
from cquarry.search import canonical_language


class ConfigPathTrioTests(unittest.TestCase):
    """save_config / get_db_path / set_db_path against a temp config file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = config.CONFIG_FILE
        config.CONFIG_FILE = os.path.join(self._tmp.name, "sub", "config.json")

    def tearDown(self):
        config.CONFIG_FILE = self._saved
        self._tmp.cleanup()

    def test_get_db_path_is_none_without_config(self):
        self.assertIsNone(config.get_db_path())

    def test_set_db_path_persists_absolute_expanded(self):
        config.set_db_path("rel/lib/metadata.db")
        saved = config.get_db_path()
        self.assertTrue(os.path.isabs(saved))
        self.assertEqual(saved, os.path.abspath("rel/lib/metadata.db"))
        # the write created the config's parent directories
        self.assertTrue(os.path.exists(config.CONFIG_FILE))

    def test_save_config_round_trips_the_dict(self):
        config.save_config({"db_path": "/x/metadata.db", "other": 1})
        with open(config.CONFIG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw, {"db_path": "/x/metadata.db", "other": 1})
        self.assertEqual(config.load_config()["other"], 1)

    def test_set_db_path_preserves_other_config_keys(self):
        config.save_config({"other": 1})
        config.set_db_path("/x/metadata.db")
        self.assertEqual(config.load_config()["other"], 1)


class FindDbChainTests(unittest.TestCase):
    """The four-step resolution order, and the aborted-prompt translation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_config_file = config.CONFIG_FILE
        self._saved_defaults = helpers.DEFAULT_DB_PATHS
        config.CONFIG_FILE = os.path.join(self._tmp.name, "config.json")
        helpers.DEFAULT_DB_PATHS = ()

    def tearDown(self):
        config.CONFIG_FILE = self._saved_config_file
        helpers.DEFAULT_DB_PATHS = self._saved_defaults
        self._tmp.cleanup()

    def _db(self) -> str:
        p = os.path.join(self._tmp.name, "metadata.db")
        open(p, "wb").close()
        return p

    def test_explicit_path_wins(self):
        p = self._db()
        self.assertEqual(helpers.find_db(p), os.path.abspath(p))

    def test_explicit_missing_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, "nope.db"):
            helpers.find_db(os.path.join(self._tmp.name, "nope.db"))

    def test_saved_config_path_is_second(self):
        p = self._db()
        config.save_config({"db_path": p})
        with mock.patch.object(
            helpers, "_resolve_path", side_effect=AssertionError("step 1 ran")
        ):
            self.assertEqual(helpers.find_db(), p)

    def test_default_path_is_third_and_persists(self):
        p = self._db()
        helpers.DEFAULT_DB_PATHS = (p,)
        with mock.patch.object(helpers, "get_db_path", return_value=None):
            found = helpers.find_db()
        self.assertEqual(found, os.path.abspath(p))
        # the discovery saved itself for the next run
        self.assertEqual(config.get_db_path(), os.path.abspath(p))

    def test_non_tty_falls_through_to_not_found(self):
        with (
            mock.patch.object(helpers, "get_db_path", return_value=None),
            mock.patch.object(sys, "stdin") as stdin,
        ):
            stdin.isatty.return_value = False
            with self.assertRaises(FileNotFoundError):
                helpers.find_db()

    def test_interactive_prompt_resolves_and_saves(self):
        p = self._db()
        with (
            mock.patch.object(helpers, "get_db_path", return_value=None),
            mock.patch.object(sys, "stdin") as stdin,
            mock.patch("builtins.input", return_value=p),
        ):
            stdin.isatty.return_value = True
            found = helpers.find_db()
        self.assertEqual(found, os.path.abspath(p))
        self.assertEqual(config.get_db_path(), os.path.abspath(p))

    def test_aborted_prompt_translates_to_not_found(self):
        # The translation at the prompt: an aborted interactive prompt
        # means "no database found" (exit 1 with the actionable message),
        # and neither the EOFError nor the KeyboardInterrupt traceback
        # belongs in that story.
        for exc in (EOFError, KeyboardInterrupt):
            with self.subTest(exc=exc):
                with (
                    mock.patch.object(helpers, "get_db_path", return_value=None),
                    mock.patch.object(sys, "stdin") as stdin,
                    mock.patch("builtins.input", side_effect=exc),
                ):
                    stdin.isatty.return_value = True
                    with self.assertRaisesRegex(FileNotFoundError, "Specify with --db"):
                        helpers.find_db()


class ColorTests(unittest.TestCase):
    def test_tty_gets_ansi_wrapping(self):
        with mock.patch.object(sys, "stdout") as out:
            out.isatty.return_value = True
            self.assertEqual(helpers.color("x", "31"), "\033[31mx\033[0m")

    def test_piped_output_stays_plain(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(helpers.color("x", "31"), "x")


class AuthorDisplayTests(unittest.TestCase):
    def test_none_is_unknown(self):
        self.assertEqual(helpers.normalize_author_display(None), "Unknown Author")

    def test_string_splits_on_commas(self):
        self.assertEqual(
            helpers.normalize_author_display("A, B"), "A & B"
        )

    def test_native_list_is_joined(self):
        self.assertEqual(
            helpers.normalize_author_display(["A", "B"]), "A & B"
        )

    def test_primary_only_takes_the_first(self):
        self.assertEqual(
            helpers.normalize_author_display(["A", "B"], primary_only=True), "A"
        )

    def test_empty_string_is_unknown(self):
        self.assertEqual(helpers.normalize_author_display("  "), "Unknown Author")


class AuthorSortKeyTests(unittest.TestCase):
    def test_key_is_lowercased(self):
        self.assertEqual(helpers.author_sort_key("Herbert, Frank"), "herbert, frank")

    def test_none_is_empty(self):
        self.assertEqual(helpers.author_sort_key(None), "")

    def test_primary_only_stops_at_the_ampersand(self):
        self.assertEqual(
            helpers.author_sort_key("Herbert, Frank & Second, Author", True),
            "herbert, frank",
        )


class CanonicalLanguageTests(unittest.TestCase):
    def test_full_name_maps(self):
        self.assertEqual(canonical_language("English"), "eng")

    def test_two_letter_code_maps(self):
        self.assertEqual(canonical_language("en"), "eng")

    def test_exact_prefixed_form_maps(self):
        # the "=" prefix is the search engine's exact-match spelling; the
        # map keys are full names, so "=german" (not "=ger") canonicalizes
        self.assertEqual(canonical_language("=german"), "deu")

    def test_unknown_passes_through(self):
        self.assertEqual(canonical_language("Klingon"), "Klingon")

    def test_empty_stays_empty(self):
        self.assertEqual(canonical_language("  "), "  ")


if __name__ == "__main__":
    unittest.main()
