"""Tests for the opt-in write module (cquarry.write).

The fixture recreates the trigger hazards that make blind writes fail against
a real Calibre library: books_insert_trg calls the title_sort() and uuid4()
SQL functions, which only exist after register_udfs().
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone

from cquarry.db import CalibreDB
from cquarry.write import WritableCalibreDB, register_udfs, title_sort


class TestTitleSort(unittest.TestCase):
    def test_articles_move_to_the_end(self):
        self.assertEqual(
            title_sort("The Three-Body Problem"), "Three-Body Problem, The"
        )
        self.assertEqual(title_sort("A Clockwork Orange"), "Clockwork Orange, A")
        self.assertEqual(title_sort("an echo of things"), "echo of things, an")

    def test_no_article(self):
        self.assertEqual(title_sort("Dune"), "Dune")
        self.assertEqual(title_sort(""), "")


def _make_simple_db(db_path: str, *, with_dirtied: bool = False) -> None:
    """The minimal books/tags/identifiers fixture plus the insert trigger.

    Shared by the simple write fixtures (was pasted per class)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE books (
            id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
            timestamp TEXT, last_modified TEXT, path TEXT
        );
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE books_tags_link (
            id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER,
            UNIQUE(book, tag)
        );
        CREATE TABLE identifiers (
            id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT,
            UNIQUE(book, type)
        );
        """
        + (
            """
        CREATE TABLE metadata_dirtied (
            id INTEGER PRIMARY KEY, book INTEGER NOT NULL,
            UNIQUE(book)
        );
        """
            if with_dirtied
            else ""
        )
        + """
        CREATE TRIGGER books_insert_trg AFTER INSERT ON books
        BEGIN
            UPDATE books SET sort = title_sort(NEW.title),
                last_modified = uuid4() WHERE id = NEW.id;
        END;
        """
    )
    conn.commit()
    conn.close()


class TestWritableCalibreDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        _make_simple_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _open_raw(self):
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)
        return conn

    def _seed_books(self, wdb, *ids):
        for i in ids:
            wdb.conn.execute(
                "INSERT INTO books (id, title) VALUES (?, ?)", (i, f"T{i}")
            )
        wdb.conn.commit()

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            WritableCalibreDB(os.path.join(self.temp_dir, "nope.db"))

    def test_triggers_work_after_udf_registration(self):
        conn = self._open_raw()
        try:
            conn.execute(
                "INSERT INTO books (id, title) VALUES (1, 'The Left Hand of Darkness')"
            )
            row = conn.execute("SELECT sort FROM books WHERE id = 1").fetchone()
            self.assertEqual(row[0], "Left Hand of Darkness, The")
        finally:
            conn.close()

    def test_update_title_bumps_sort_and_last_modified(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1)
            wdb.update_title(1, "The New Title")
        check = self._open_raw()
        try:
            title, sort, lm = check.execute(
                "SELECT title, sort, last_modified FROM books WHERE id = 1"
            ).fetchone()
            self.assertEqual((title, sort), ("The New Title", "New Title, The"))
            self.assertTrue(lm and lm != "None")
        finally:
            check.close()

    def test_add_and_remove_tag_roundtrip(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1, 2)
            self.assertTrue(wdb.add_tag(1, "Audited"))
            # Second add is a no-op.
            self.assertFalse(wdb.add_tag(1, "Audited"))
            self.assertTrue(wdb.add_tag(2, "Audited"))

        check = self._open_raw()
        links = check.execute(
            "SELECT book FROM books_tags_link ORDER BY book"
        ).fetchall()
        tag_count = check.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        check.close()
        self.assertEqual([r[0] for r in links], [1, 2])
        self.assertEqual(tag_count, 1)

        with WritableCalibreDB(self.db_path) as wdb:
            # Removing from book 1 keeps the tag (still used by book 2).
            self.assertTrue(wdb.remove_tag(1, "Audited"))
            self.assertFalse(wdb.remove_tag(1, "Audited"))
            # Removing the last link prunes the orphaned tag row.
            self.assertTrue(wdb.remove_tag(2, "Audited"))
        check = self._open_raw()
        try:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 0
            )
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM books_tags_link").fetchone()[0], 0
            )
        finally:
            check.close()

    def test_identifier_upsert_and_delete(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1)
            self.assertTrue(wdb.set_identifier(1, "isbn", "9780000000000"))
            # Same value (even different case in the type): no change.
            self.assertFalse(wdb.set_identifier(1, "ISBN", "9780000000000"))
            # Replacement respects UNIQUE(book, type).
            self.assertTrue(wdb.set_identifier(1, "isbn", "9781111111111"))
            # Deletion via None.
            self.assertTrue(wdb.set_identifier(1, "isbn", None))
            self.assertFalse(wdb.set_identifier(1, "isbn", None))
        check = self._open_raw()
        try:
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM identifiers").fetchone()[0], 0
            )
        finally:
            check.close()

    def test_set_identifiers_batch(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1)
            changed = wdb.set_identifiers(
                1, {"isbn": "9780000000000", "goodreads": "42", "amazon": None}
            )
            self.assertEqual(changed, 2)
        check = self._open_raw()
        try:
            pairs = dict(check.execute("SELECT type, val FROM identifiers").fetchall())
            self.assertEqual(pairs, {"isbn": "9780000000000", "goodreads": "42"})
        finally:
            check.close()

    def test_clean_identifier_parity(self):
        # The 1.18 pin (upstream clean_identifier, db/write.py:118-121):
        # type strips ':' and ','; value maps ',' to '|'.
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1)
            self.assertTrue(wdb.set_identifier(1, " Good:Reads, X ", "123,456"))
            # ':' and ',' are REMOVED, not mapped to space: " Good:Reads, X "
            # cleans to "goodreads x"; the value stores comma-free "123|456".
            self.assertTrue(wdb.clear_identifier(1, "goodreads x"))
            with self.assertRaises(ValueError):
                wdb.set_identifier(1, ":,", "x")  # cleans down to empty
        check = self._open_raw()
        try:
            pairs = dict(check.execute("SELECT type, val FROM identifiers").fetchall())
            # The stored value is Calibre's comma-free shape.
            self.assertEqual(pairs, {})
        finally:
            check.close()

    def test_clear_identifier(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1)
            self.assertTrue(wdb.set_identifier(1, "isbn", "9780000000000"))
            self.assertTrue(wdb.set_identifier(1, "mobi-asin", "B000123456"))
            # The type normalizes exactly like set_identifier.
            self.assertTrue(wdb.clear_identifier(1, " MOBI-ASIN "))
            # Clearing a pair that is already gone is an honest no-op.
            self.assertFalse(wdb.clear_identifier(1, "mobi-asin"))
            # Unknown books raise like every other setter.
            with self.assertRaises(ValueError):
                wdb.clear_identifier(999, "isbn")
        check = self._open_raw()
        try:
            pairs = dict(check.execute("SELECT type, val FROM identifiers").fetchall())
            self.assertEqual(pairs, {"isbn": "9780000000000"})
        finally:
            check.close()


class TestMetadataDirtied(unittest.TestCase):
    """Every mutation must queue OPF regeneration via ``metadata_dirtied``.

    Calibre regenerates a book's sidecar .opf only for ids present in that
    table (backend.py ``dirtied_books()``), so a write path that skips the
    insert leaves external edits invisible to Calibre's sync machinery.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        _make_simple_db(self.db_path, with_dirtied=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _dirtied(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT book FROM metadata_dirtied ORDER BY book"
                ).fetchall()
            ]
        finally:
            conn.close()

    def _seed_books(self, wdb, *ids):
        for i in ids:
            wdb.conn.execute(
                "INSERT INTO books (id, title) VALUES (?, ?)", (i, f"T{i}")
            )
        wdb.conn.commit()

    def test_update_title_marks_dirty(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1, 2)
            wdb.update_title(1, "Renamed")
        self.assertEqual(self._dirtied(), [1])

    def test_tag_mutations_mark_dirty_only_on_change(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 1, 2)
            self.assertTrue(wdb.add_tag(1, "Audited"))
            # No-op re-add must not queue anything new.
            self.assertFalse(wdb.add_tag(1, "Audited"))
            self.assertTrue(wdb.add_tag(2, "Audited"))
            self.assertTrue(wdb.remove_tag(1, "Audited"))
            # No-op remove of an absent link.
            self.assertFalse(wdb.remove_tag(1, "Audited"))
        self.assertEqual(self._dirtied(), [1, 2])

    def test_identifier_upserts_mark_dirty(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 3)
            wdb.set_identifiers(3, {"isbn": "9780000000000", "goodreads": None})
        self.assertEqual(self._dirtied(), [3])

    def test_clear_identifier_marks_dirty(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 3)
            wdb.set_identifier(3, "mobi-asin", "B000123456")
            wdb.clear_identifier(3, "mobi-asin")
            # An honest no-op clear queues nothing new.
            self.assertFalse(wdb.clear_identifier(3, "mobi-asin"))
        self.assertEqual(self._dirtied(), [3])

    def test_repeated_mutations_do_not_duplicate_rows(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self._seed_books(wdb, 5)
            wdb.update_title(5, "One")
            wdb.update_title(5, "Two")
            wdb.add_tag(5, "X")
        # INSERT OR IGNORE semantics: one queued entry per book.
        self.assertEqual(self._dirtied(), [5])

    def test_schema_without_table_still_writes(self):
        # Databases predating metadata_dirtied keep working: the existence
        # check degrades to a no-op instead of raising OperationalError.
        legacy = os.path.join(self.temp_dir, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT
            );
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_tags_link (
                id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER,
                UNIQUE(book, tag)
            );
            CREATE TABLE identifiers (
                id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT,
                UNIQUE(book, type)
            );
            """
        )
        conn.commit()
        conn.close()
        with WritableCalibreDB(legacy) as wdb:
            wdb.conn.execute("INSERT INTO books (id, title) VALUES (1, 'T')")
            wdb.conn.commit()
            self.assertTrue(wdb.add_tag(1, "Audited"))
            wdb.update_title(1, "Renamed")


class TestCustomColumnMetadata(unittest.TestCase):
    """set_custom_column_metadata (1.24, Phase 14): name/editable/display
    edits on an existing column, upstream backend.py:1407's shape. The
    headline use case: populating an enumeration's enum_values without
    Calibre open."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)
        conn.executescript(_ADD_BOOK_SCHEMA)
        conn.execute("INSERT INTO books (id, title) VALUES (1, 'One')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql_rows(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def test_enum_values_populate_unlock_the_write_path(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column("shelf", "Shelf", "enumeration")
            # An empty enumeration rejects every value...
            with self.assertRaises(ValueError):
                wdb.set_custom_column(1, "#shelf", "Read")
            # ...until the metadata verb populates it.
            self.assertTrue(
                wdb.set_custom_column_metadata(
                    "#shelf", display={"enum_values": ["Read", "TBR"]}
                )
            )
            self.assertTrue(wdb.set_custom_column(1, "#shelf", "Read"))
        conn = sqlite3.connect(self.db_path)
        try:
            display = conn.execute(
                "SELECT display FROM custom_columns WHERE label = 'shelf'"
            ).fetchone()[0]
            self.assertEqual(json.loads(display), {"enum_values": ["Read", "TBR"]})
            # Pattern A: the value lives in the value table, the book in
            # the link table (the shared fixture seeds columns 1-3, so the
            # created column's number comes from the call).
            value = conn.execute(
                f"SELECT c.value FROM custom_column_{num} c "
                f"JOIN books_custom_column_{num}_link l ON l.value = c.id "
                "WHERE l.book = 1"
            ).fetchone()[0]
            self.assertEqual(value, "Read")
        finally:
            conn.close()

    def test_name_and_editable_changes(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("shelf", "Shelf", "text", editable=True)
            self.assertTrue(wdb.set_custom_column_metadata("#shelf", name="Shelving"))
            self.assertTrue(wdb.set_custom_column_metadata("#shelf", editable=False))
        self.assertEqual(
            self._sql_rows(
                "SELECT name, editable FROM custom_columns WHERE label = 'shelf'"
            ),
            [("Shelving", 0)],
        )

    def test_honest_no_ops(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("shelf", "Shelf", "text")
            self.assertFalse(wdb.set_custom_column_metadata("#shelf"))
            self.assertFalse(wdb.set_custom_column_metadata("#shelf", name="Shelf"))
            self.assertFalse(wdb.set_custom_column_metadata("#shelf", display={}))
        # Exactly the one row create_custom_column wrote: a metadata no-op
        # does not touch the refresh pref.
        self.assertEqual(
            self._sql_rows(
                "SELECT val FROM preferences WHERE key = "
                "'update_all_last_mod_dates_on_start'"
            ),
            [("true",)],
        )

    def test_change_sets_the_calibre_refresh_pref(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("shelf", "Shelf", "text")
            wdb.set_custom_column_metadata("#shelf", name="Shelving")
        self.assertEqual(
            self._sql_rows(
                "SELECT val FROM preferences WHERE key = "
                "'update_all_last_mod_dates_on_start'"
            ),
            [("true",)],
        )

    def test_rejections(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("shelf", "Shelf", "text")
            with self.assertRaises(ValueError):
                wdb.set_custom_column_metadata("#nope", name="X")
            with self.assertRaises(ValueError):
                wdb.set_custom_column_metadata("#shelf", name="   ")
            with self.assertRaises(TypeError):
                wdb.set_custom_column_metadata("#shelf", display=["a"])

    def test_a_fresh_reader_sees_the_new_metadata(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("shelf", "Shelf", "enumeration")
            wdb.set_custom_column_metadata("#shelf", display={"enum_values": ["A"]})
        with CalibreDB(self.db_path) as db:
            meta = db.find_custom_column("#shelf")
            self.assertEqual(meta["display"], {"enum_values": ["A"]})


class TestMaintain(unittest.TestCase):
    """maintain() (1.24, Phase 15): vacuum/analyze/integrity_check over the
    library DB and its attached FTS sidecar."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO books (id, title) VALUES (?, ?)", [(1, "A"), (2, "B")]
        )
        conn.execute("DELETE FROM books WHERE id = 2")  # free pages to vacuum
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sidecar(self, with_table=True):
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        if with_table:
            fts.execute(
                "CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,"
                " book INTEGER NOT NULL, format TEXT NOT NULL,"
                " UNIQUE(book, format))"
            )
        fts.commit()
        fts.close()

    def test_maintain_defaults_vacuum_and_analyze_both_dbs(self):
        self._sidecar()
        with WritableCalibreDB(self.db_path) as wdb:
            out = wdb.maintain()
        self.assertTrue(out["vacuumed"])
        self.assertTrue(out["analyzed"])
        self.assertTrue(out["fts_attached"])
        self.assertIsNone(out["integrity_check"])
        self.assertIsNone(out["fts_integrity_check"])

    def test_maintain_integrity_check_reports_ok(self):
        self._sidecar()
        with WritableCalibreDB(self.db_path) as wdb:
            out = wdb.maintain(vacuum=False, analyze=False, integrity_check=True)
        self.assertFalse(out["vacuumed"])
        self.assertEqual(out["integrity_check"], ["ok"])
        self.assertEqual(out["fts_integrity_check"], ["ok"])

    def test_maintain_without_sidecar_degrades(self):
        with WritableCalibreDB(self.db_path) as wdb:
            out = wdb.maintain(integrity_check=True)
        self.assertFalse(out["fts_attached"])
        self.assertEqual(out["integrity_check"], ["ok"])
        self.assertIsNone(out["fts_integrity_check"])

    def test_maintain_include_fts_false_leaves_the_sidecar_alone(self):
        self._sidecar()
        with WritableCalibreDB(self.db_path) as wdb:
            out = wdb.maintain(integrity_check=True, include_fts=False)
        self.assertFalse(out["fts_attached"])
        self.assertIsNone(out["fts_integrity_check"])

    def test_maintain_refuses_inside_batch_and_mid_transaction(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(RuntimeError), wdb.batch():
                wdb.maintain()
            # A caller's raw uncommitted write holds the transaction too.
            wdb.conn.execute("INSERT INTO books (id, title) VALUES (3, 'C')")
            with self.assertRaises(RuntimeError):
                wdb.maintain()


class TestFtsQueueVerbs(unittest.TestCase):
    """fts_reindex_book / fts_reindex_all / fts_queue_clear (1.24, Phase
    15): pure queue SQL against the sidecar; the index rows themselves
    stay Calibre's."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,
                format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE books_pages_link (book INTEGER PRIMARY KEY,
                pages INTEGER DEFAULT 0 NOT NULL, needs_scan INTEGER NOT NULL DEFAULT 0);
            INSERT INTO books (id, title, sort) VALUES (1, 'One', 'One'), (2, 'Two', 'Two');
            INSERT INTO data (book, format, uncompressed_size, name) VALUES
                (1, 'EPUB', 10, 'One - X'), (1, 'MOBI', 10, 'One - X'),
                (2, 'EPUB', 10, 'Two - Y');
            INSERT INTO books_pages_link (book, pages, needs_scan) VALUES (1, 100, 0), (2, 50, 0);
            """
        )
        conn.commit()
        conn.close()
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.execute(
            "CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,"
            " book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,"
            " in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format))"
        )
        fts.commit()
        fts.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _fts_rows(self):
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        try:
            return sorted(fts.execute("SELECT book, format FROM dirtied_formats"))
        finally:
            fts.close()

    def test_reindex_book_queues_all_catalogued_formats(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.fts_reindex_book(1), 2)
            self.assertEqual(self._fts_rows(), [(1, "EPUB"), (1, "MOBI")])
            # Already-queued pairs do not duplicate.
            self.assertEqual(wdb.fts_reindex_book(1), 0)
            # needs_scan flips only when something was queued.
            check = sqlite3.connect(self.db_path)
            scans = list(check.execute("SELECT book, needs_scan FROM books_pages_link"))
            check.close()
            self.assertEqual(scans, [(1, 1), (2, 0)])

    def test_reindex_book_explicit_formats_and_unknown_book(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.fts_reindex_book(2, ["epub"]), 1)
            self.assertEqual(self._fts_rows(), [(2, "EPUB")])
            with self.assertRaises(ValueError):
                wdb.fts_reindex_book(99)

    def test_reindex_all_sweeps_the_catalog(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.fts_reindex_all(), 3)
            self.assertEqual(self._fts_rows(), [(1, "EPUB"), (1, "MOBI"), (2, "EPUB")])
            self.assertEqual(wdb.fts_reindex_all(), 0)

    def test_queue_clear_variants(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.fts_reindex_all()
            self.assertEqual(wdb.fts_queue_clear(1, "epub"), 1)
            self.assertEqual(self._fts_rows(), [(1, "MOBI"), (2, "EPUB")])
            self.assertEqual(wdb.fts_queue_clear(1), 1)
            self.assertEqual(self._fts_rows(), [(2, "EPUB")])
            self.assertEqual(wdb.fts_queue_clear(), 1)
            self.assertEqual(self._fts_rows(), [])
            self.assertEqual(wdb.fts_queue_clear(), 0)  # honest empty
            with self.assertRaises(ValueError):
                wdb.fts_queue_clear(fmt="EPUB")

    def test_missing_sidecar_degrades_to_zero(self):
        os.unlink(os.path.join(self.temp_dir, "full-text-search.db"))
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.fts_reindex_book(1), 0)
            self.assertEqual(wdb.fts_reindex_all(), 0)
            self.assertEqual(wdb.fts_queue_clear(), 0)

    def test_queue_writes_roll_back_with_the_batch(self):
        with (
            self.assertRaises(ValueError),
            WritableCalibreDB(self.db_path) as wdb,
            wdb.batch(),
        ):
            wdb.fts_reindex_all()
            wdb.fts_reindex_book(99)  # unknown book fails the pass
        self.assertEqual(self._fts_rows(), [])


class TestTypedPreferences(unittest.TestCase):
    """set_preference + the saved-search verbs (1.24, Phase 15): the
    calibredb saved_searches parity item, typed by key."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,"
            " author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,"
            " series_index REAL, path TEXT, has_cover INTEGER, uuid TEXT)"
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (1, 'One', 'One')")
        conn.execute(
            "CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT NOT NULL,"
            " val TEXT NOT NULL, UNIQUE(key))"
        )
        # The join family the search view hydrates over (the engine test
        # resolves a written VL through it).
        for table in (
            "authors (id INTEGER PRIMARY KEY, name TEXT)",
            "books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER)",
            "tags (id INTEGER PRIMARY KEY, name TEXT)",
            "books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER)",
            "series (id INTEGER PRIMARY KEY, name TEXT)",
            "books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER)",
            "publishers (id INTEGER PRIMARY KEY, name TEXT)",
            "books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER)",
            "ratings (id INTEGER PRIMARY KEY, rating INTEGER)",
            "books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER)",
            "languages (id INTEGER PRIMARY KEY, lang_code TEXT)",
            "books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER)",
            (
                "data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT,"
                " uncompressed_size INTEGER, name TEXT)"
            ),
        ):
            conn.execute("CREATE TABLE " + table)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _pref(self, key):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT val FROM preferences WHERE key = ?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    def test_set_preference_writes_the_five_keys(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(
                wdb.set_preference("saved_searches", {"Recent": "timestamp:>30daysago"})
            )
            self.assertTrue(
                wdb.set_preference("virtual_libraries", {"Wing": "tags:Wing"})
            )
            self.assertTrue(
                wdb.set_preference(
                    "user_categories", {"Favorites": [["Tolkien", "authors"]]}
                )
            )
            self.assertTrue(
                wdb.set_preference("grouped_search_terms", {"Ident": ["isbn", "doi"]})
            )
            self.assertTrue(wdb.set_preference("fts_enabled", True))
        self.assertEqual(
            self._pref("saved_searches"), {"Recent": "timestamp:>30daysago"}
        )
        self.assertIs(self._pref("fts_enabled"), True)

    def test_set_preference_rejects_unknown_keys_and_bad_payloads(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_preference("main_window_geometry", {"x": 1})
            with self.assertRaises(ValueError):
                wdb.set_preference("saved_searches", {"X": "   "})
            with self.assertRaises(ValueError):
                wdb.set_preference("fts_enabled", 1)
            with self.assertRaises(TypeError):
                wdb.set_preference("user_categories", {"Bad": "not-a-list"})
            with self.assertRaises(ValueError):
                wdb.set_preference("user_categories", {"Bad": [["lonely-member"]]})
        self.assertIsNone(self._pref("saved_searches"))

    def test_set_preference_honest_noop_on_equal_payload(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_preference("virtual_libraries", {"Wing": "tags:Wing"})
            self.assertFalse(
                wdb.set_preference("virtual_libraries", {"Wing": "tags:Wing"})
            )

    def test_saved_search_add_delete_rename(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.saved_search_add("Recent", " timestamp:>30daysago "))
            self.assertFalse(wdb.saved_search_add("Recent", "timestamp:>30daysago"))
            self.assertTrue(wdb.saved_search_add("Borrowed", "identifiers:loan:true"))
            self.assertTrue(wdb.saved_search_rename("recent", "New Books"))
            self.assertEqual(
                self._pref("saved_searches"),
                {
                    "New Books": "timestamp:>30daysago",
                    "Borrowed": "identifiers:loan:true",
                },
            )
            # Delete is exact-key (upstream's pop): the stored spelling
            # deletes, a case variant honestly does not.
            self.assertTrue(wdb.saved_search_delete("Borrowed"))
            self.assertFalse(wdb.saved_search_delete("Borrowed"))
            with self.assertRaises(ValueError):
                wdb.saved_search_rename("nope", "X")
            self.assertTrue(wdb.saved_search_add("Borrowed", "x"))
            with self.assertRaises(ValueError):
                wdb.saved_search_rename("New Books", "Borrowed")  # exists
            # A rename onto the stored spelling is an honest no-op; a
            # case-variant is a genuine case-change rename.
            self.assertFalse(wdb.saved_search_rename("borrowed", "Borrowed"))
            self.assertTrue(wdb.saved_search_rename("Borrowed", "borrowed"))
            self.assertIn("borrowed", self._pref("saved_searches"))

    def test_the_reader_sees_the_written_state(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.saved_search_add("Recent", "timestamp:>30daysago")
            wdb.set_preference("virtual_libraries", {"Wing": "tags:Wing"})
        with CalibreDB(self.db_path) as db:
            self.assertEqual(
                db.get_saved_searches(), {"Recent": "timestamp:>30daysago"}
            )
            self.assertEqual(db.get_virtual_libraries(), {"Wing": "tags:Wing"})

    def test_written_search_state_resolves_in_the_engine(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_preference("virtual_libraries", {"Wing": "id:1"})
        with CalibreDB(self.db_path) as db:
            self.assertEqual(db.resolve_vl("wing"), {1})


if __name__ == "__main__":
    unittest.main()


_WRITE_SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
    timestamp TEXT, pubdate TEXT, series_index REAL,
    has_cover INTEGER DEFAULT 0, uuid TEXT, path TEXT, last_modified TEXT
);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT, link TEXT DEFAULT '');
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER, UNIQUE(book, tag));
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT, link TEXT DEFAULT '');
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT, link TEXT DEFAULT '');
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT, UNIQUE(book));
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT NOT NULL, val TEXT NOT NULL, UNIQUE(key));
CREATE TABLE custom_columns (
    id INTEGER PRIMARY KEY, label TEXT UNIQUE, name TEXT, datatype TEXT,
    editable BOOL DEFAULT 1, display TEXT DEFAULT '{}',
    is_multiple BOOL DEFAULT 0, normalized BOOL DEFAULT 0
);
CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_custom_column_1_link (book INTEGER, value INTEGER, UNIQUE(book, value));
CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, book INTEGER, value BOOL);
CREATE TABLE custom_column_3 (id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_custom_column_3_link (book INTEGER, value INTEGER, UNIQUE(book, value));
CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
"""

# Phase 10 (add_book): _WRITE_SCHEMA extended with the real INSERT-path
# hazards a user_version-27 library carries — AUTOINCREMENT on books.id
# (sqlite_sequence drives dry-run's id prediction), books_insert_trg
# (title_sort()/uuid4()), the Count Pages create trigger, series_insert_trg,
# and the fkc_insert_* guards the link-table writes must satisfy.
_ADD_BOOK_SCHEMA = (
    _WRITE_SCHEMA.replace(
        "id INTEGER PRIMARY KEY, title TEXT",
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT",
        1,
    )
    + """
CREATE TABLE books_pages_link (
    book INTEGER PRIMARY KEY,
    pages INTEGER DEFAULT 0 NOT NULL,
    algorithm INTEGER DEFAULT 0 NOT NULL,
    format TEXT DEFAULT '' NOT NULL,
    format_size INTEGER DEFAULT 0 NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    needs_scan INTEGER NOT NULL DEFAULT 0 CHECK(needs_scan IN (0, 1))
);
CREATE TRIGGER books_insert_trg AFTER INSERT ON books
BEGIN
    UPDATE books SET sort = title_sort(NEW.title), uuid = uuid4() WHERE id = NEW.id;
END;
CREATE TRIGGER series_insert_trg AFTER INSERT ON series
BEGIN
    UPDATE series SET sort = title_sort(NEW.name) WHERE id = NEW.id;
END;
CREATE TRIGGER books_pages_link_create_trigger AFTER INSERT ON books FOR EACH ROW
BEGIN
    INSERT INTO books_pages_link(book) VALUES (NEW.id);
END;
CREATE TRIGGER fkc_insert_books_authors_link BEFORE INSERT ON books_authors_link
BEGIN
    SELECT CASE
        WHEN (SELECT id FROM books WHERE id = NEW.book) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: book not in books')
        WHEN (SELECT id FROM authors WHERE id = NEW.author) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: author not in authors')
    END;
END;
CREATE TRIGGER fkc_insert_books_series_link BEFORE INSERT ON books_series_link
BEGIN
    SELECT CASE
        WHEN (SELECT id FROM books WHERE id = NEW.book) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: book not in books')
        WHEN (SELECT id FROM series WHERE id = NEW.series) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: series not in series')
    END;
END;
CREATE TRIGGER fkc_insert_books_publishers_link BEFORE INSERT ON books_publishers_link
BEGIN
    SELECT CASE
        WHEN (SELECT id FROM books WHERE id = NEW.book) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: book not in books')
        WHEN (SELECT id FROM publishers WHERE id = NEW.publisher) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: publisher not in publishers')
    END;
END;
CREATE TRIGGER fkc_insert_books_languages_link BEFORE INSERT ON books_languages_link
BEGIN
    SELECT CASE
        WHEN (SELECT id FROM books WHERE id = NEW.book) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: book not in books')
        WHEN (SELECT id FROM languages WHERE id = NEW.lang_code) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: language not in languages')
    END;
END;
CREATE TRIGGER fkc_insert_books_tags_link BEFORE INSERT ON books_tags_link
BEGIN
    SELECT CASE
        WHEN (SELECT id FROM books WHERE id = NEW.book) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: book not in books')
        WHEN (SELECT id FROM tags WHERE id = NEW.tag) IS NULL
        THEN RAISE(ABORT, 'Foreign key violation: tag not in tags')
    END;
END;
"""
)


class TestNowStamp(unittest.TestCase):
    def test_now_stamp_matches_calibres_text_shape(self):
        # Calibre stores 'YYYY-MM-DD HH:MM:SS.SSSSSS+00:00' style UTC stamps.
        self.assertRegex(
            WritableCalibreDB._now(),
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}\+00:00$",
        )


class TestRollbackOnBaseException(TestMetadataDirtied):
    """The commit window: BaseException mid-write must not commit a torn edit.

    Setters used to self-heal only on ``Exception`` and ``__exit__`` committed
    unconditionally, so a ``KeyboardInterrupt`` between a setter's SQL
    statements escaped the setter's rollback and was then committed on
    context-manager exit: a link row without its ``metadata_dirtied`` row.
    """

    def _link_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM books_tags_link").fetchone()[0]
        finally:
            conn.close()

    def test_keyboardinterrupt_mid_setter_writes_nothing(self):
        # The interrupt lands after the link INSERT but inside _touch_book:
        # exactly the window the old except-Exception paths left open.
        # assertRaises stays outermost so wdb.__exit__ sees the unwind.
        with (
            self.assertRaises(KeyboardInterrupt),
            WritableCalibreDB(self.db_path) as wdb,
        ):
            self._seed_books(wdb, 1)

            def boom(book_id):
                raise KeyboardInterrupt

            wdb._mark_dirty = boom
            wdb.add_tag(1, "Audited")
        self.assertEqual(self._link_count(), 0)
        self.assertEqual(self._dirtied(), [])

    def test_keyboardinterrupt_inside_batch_rolls_back(self):
        # batch()'s finally already rolled back on any exception; pinned now
        # that the setters' own rollback paths see BaseException too.
        with (
            self.assertRaises(KeyboardInterrupt),
            WritableCalibreDB(self.db_path) as wdb,
            wdb.batch(),
        ):
            self._seed_books(wdb, 1)
            wdb.add_tag(1, "Audited")
            raise KeyboardInterrupt
        self.assertEqual(self._link_count(), 0)
        self.assertEqual(self._dirtied(), [])

    def test_exit_rolls_back_when_an_exception_is_in_flight(self):
        # An exception in the with-body (after a committed setter) must not
        # be turned into a commit, and must propagate out of __exit__.
        with (
            self.assertRaises(RuntimeError),
            WritableCalibreDB(self.db_path) as wdb,
        ):
            self._seed_books(wdb, 1)
            wdb.add_tag(1, "Committed")  # returned normally: stands
            wdb.conn.execute(
                "INSERT INTO books_tags_link (book, tag) VALUES (1, 999)"
            )  # pending, uncommitted
            raise RuntimeError("caller gave up")
        # The committed setter survives; the pending statement does not.
        self.assertEqual(self._link_count(), 1)
        self.assertEqual(self._dirtied(), [1])

    def test_exit_commit_failure_propagates(self):
        # __exit__ used to suppress sqlite3 errors, silently discarding the
        # transaction; it now propagates like batch()'s exit does.
        with (
            self.assertRaises(sqlite3.ProgrammingError),
            WritableCalibreDB(self.db_path) as wdb,
        ):
            self._seed_books(wdb, 1)
            wdb.update_title(1, "Uncommittable")
            wdb.conn.close()  # the exit commit now has nowhere to go


class TestRemoveBook(unittest.TestCase):
    """remove_book: rows go, orphans prune, queues clean. Zero tests existed
    before the 2026-09-08 sweep flagged it, despite the dynamic-table DELETEs
    and the irreversibility."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
                timestamp TEXT, pubdate TEXT, series_index REAL,
                has_cover INTEGER DEFAULT 0, uuid TEXT, path TEXT,
                last_modified TEXT
            );
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER UNIQUE);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT UNIQUE);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER);
            CREATE TABLE custom_columns (
                id INTEGER PRIMARY KEY, label TEXT UNIQUE, name TEXT, datatype TEXT,
                editable BOOL DEFAULT 1, display TEXT DEFAULT '{}',
                is_multiple BOOL DEFAULT 0, normalized BOOL DEFAULT 0
            );
            CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT '');
            CREATE TABLE books_custom_column_1_link (book INTEGER, value INTEGER, UNIQUE(book, value));
            CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, book INTEGER UNIQUE, value INTEGER);
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE books_pages_link (
                book INTEGER PRIMARY KEY,
                pages INTEGER DEFAULT 0 NOT NULL,
                needs_scan INTEGER NOT NULL DEFAULT 0
            );
            CREATE TRIGGER books_pages_link_create_trigger AFTER INSERT ON books
            BEGIN
                INSERT INTO books_pages_link(book) VALUES (NEW.id);
            END;
            -- The cascade trigger the real schema carries; remove_book cleans
            -- exactly what falls OUTSIDE this trigger.
            CREATE TRIGGER books_delete_trg AFTER DELETE ON books
            BEGIN
                DELETE FROM books_authors_link WHERE book = OLD.id;
                DELETE FROM books_tags_link WHERE book = OLD.id;
                DELETE FROM books_publishers_link WHERE book = OLD.id;
                DELETE FROM books_series_link WHERE book = OLD.id;
                DELETE FROM books_ratings_link WHERE book = OLD.id;
                DELETE FROM books_languages_link WHERE book = OLD.id;
                DELETE FROM data WHERE book = OLD.id;
            END;
            INSERT INTO books (id, title) VALUES (1, 'Doomed'), (2, 'Kept');
            INSERT INTO authors VALUES (1, 'Shared Author', 'Author, Shared'),
                                       (2, 'Lone Author', 'Author, Lone');
            INSERT INTO books_authors_link (book, author) VALUES (1, 1), (1, 2), (2, 1);
            INSERT INTO tags (name) VALUES ('Doomed Tag');
            INSERT INTO books_tags_link (book, tag) VALUES (1, 1);
            INSERT INTO publishers (name) VALUES ('Doomed Pub');
            INSERT INTO books_publishers_link (book, publisher) VALUES (1, 1);
            INSERT INTO series (name) VALUES ('Doomed Series');
            INSERT INTO books_series_link (book, series) VALUES (1, 1);
            INSERT INTO ratings (rating) VALUES (8);
            INSERT INTO books_ratings_link (book, rating) VALUES (1, 1);
            INSERT INTO languages (lang_code) VALUES ('eng');
            INSERT INTO books_languages_link (book, lang_code) VALUES (1, 1);
            INSERT INTO custom_columns VALUES (1,'aud','Audience','text',1,'{}',1,1);
            INSERT INTO custom_column_1 (value) VALUES ('Rin');
            INSERT INTO books_custom_column_1_link (book, value) VALUES (1, 1);
            INSERT INTO custom_columns VALUES (2,'flag','Flag','bool',1,'{}',0,0);
            INSERT INTO custom_column_2 (book, value) VALUES (1, 1);
            INSERT INTO metadata_dirtied (book) VALUES (1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def test_remove_book_cleans_rows_queues_and_prunes_orphans(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
        # The book row and every per-book row are gone...
        self.assertEqual(self._sql("SELECT id FROM books"), [(2,)])
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_authors_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_tags_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_publishers_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_series_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_ratings_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_languages_link WHERE book=1"), [(0,)]
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_custom_column_1_link WHERE book=1"),
            [(0,)],
        )
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM custom_column_2 WHERE book=1"), [(0,)]
        )
        # ...the dirtied queue entry did not outlive its book...
        self.assertEqual(self._sql("SELECT book FROM metadata_dirtied"), [])
        # ...lone entities pruned, shared ones survive for book 2.
        self.assertEqual(
            self._sql("SELECT name FROM authors ORDER BY id"), [("Shared Author",)]
        )
        self.assertEqual(self._sql("SELECT COUNT(*) FROM tags"), [(0,)])
        self.assertEqual(self._sql("SELECT COUNT(*) FROM publishers"), [(0,)])
        self.assertEqual(self._sql("SELECT COUNT(*) FROM series"), [(0,)])
        self.assertEqual(self._sql("SELECT COUNT(*) FROM ratings"), [(0,)])
        self.assertEqual(self._sql("SELECT COUNT(*) FROM languages"), [(0,)])
        # The Pattern-A value row REMAINS: no trigger purges it on real
        # schemas (fkc_delete_on_* guards the value table, not the links)
        # and upstream's remove leaves it too; set_custom_column's writes
        # prune such orphans instead.
        self.assertEqual(self._sql("SELECT value FROM custom_column_1"), [("Rin",)])

    def test_remove_book_clears_the_fts_queue_for_its_formats(self):
        # The six-lens-audit contract gap: remove_book cleaned
        # metadata_dirtied and annotations_dirtied but never the sidecar's
        # dirtied_formats, while the docstring and API.md claimed the queues
        # are cleaned. Calibre would have re-extracted vanished files.
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "INSERT INTO data (book, format, uncompressed_size, name) VALUES (?,?,?,?)",
            [(1, "EPUB", 1, "One"), (1, "MOBI", 1, "One"), (2, "EPUB", 1, "Two")],
        )
        conn.commit()
        conn.close()
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            """
        )
        fts.executemany(
            "INSERT INTO dirtied_formats (book, format) VALUES (?, ?)",
            [(1, "EPUB"), (1, "MOBI"), (2, "EPUB")],
        )
        fts.commit()
        fts.close()
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
        fts = sqlite3.connect(self.temp_dir + "/full-text-search.db")
        try:
            left = sorted(fts.execute("SELECT book, format FROM dirtied_formats"))
        finally:
            fts.close()
        # The doomed book's entries are gone; the kept book's survive.
        self.assertEqual(left, [(2, "EPUB")])

    def test_remove_book_without_a_sidecar_still_cleans(self):
        # The degrade rule: no sidecar file, no queue to clean, no error.
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
        self.assertEqual(self._sql("SELECT id FROM books"), [(2,)])

    def _book_dir(self, book_id, title):
        path = os.path.join(self.temp_dir, f"{title} ({book_id})")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, f"{title} - A.epub"), "w") as f:
            f.write("x")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE books SET path = ? WHERE id = ?", (f"{title} ({book_id})", book_id)
        )
        conn.commit()
        conn.close()
        return path

    def test_delete_files_permanent_removes_the_directory(self):
        book_dir = self._book_dir(1, "Doomed")
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1, delete_files="permanent")
        self.assertFalse(os.path.exists(book_dir))
        self.assertEqual(self._sql("SELECT COUNT(*) FROM books"), [(1,)])

    def test_delete_files_trash_moves_into_calthrash(self):
        # Upstream's own trash layout, library-local: the book directory
        # moves whole into .caltrash/b/<id>/ and stays recoverable by hand.
        book_dir = self._book_dir(1, "Doomed")
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1, delete_files="trash")
        self.assertFalse(os.path.exists(book_dir))
        trashed = os.path.join(self.temp_dir, ".caltrash", "b", "1")
        self.assertTrue(os.path.isfile(os.path.join(trashed, "Doomed - A.epub")))
        self.assertEqual(self._sql("SELECT COUNT(*) FROM books"), [(1,)])

    def test_delete_files_none_leaves_files_for_the_caller(self):
        book_dir = self._book_dir(1, "Doomed")
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
        self.assertTrue(os.path.isdir(book_dir))

    def test_delete_files_invalid_mode_raises(self):
        with WritableCalibreDB(self.db_path) as wdb, self.assertRaises(ValueError):
            wdb.remove_book(1, delete_files="yes")

    def test_delete_files_inside_batch_defers_until_commit(self):
        # The removal must land only after the rows COMMIT: a rolled-back
        # pass that deleted files would leave resurrected rows fileless.
        book_dir = self._book_dir(1, "Doomed")
        with (
            self.assertRaises(ValueError),
            WritableCalibreDB(self.db_path) as wdb,
            wdb.batch(),
        ):
            wdb.remove_book(1, delete_files="trash")
            wdb.remove_book(999)  # fails the pass
        self.assertTrue(os.path.isdir(book_dir))
        self.assertEqual(self._sql("SELECT COUNT(*) FROM books"), [(2,)])
        with WritableCalibreDB(self.db_path) as wdb, wdb.batch():
            wdb.remove_book(1, delete_files="trash")
        self.assertFalse(os.path.isdir(book_dir))
        self.assertTrue(
            os.path.isdir(os.path.join(self.temp_dir, ".caltrash", "b", "1"))
        )

    def test_remove_book_cleans_the_fk_cascade_tables_the_trigger_misses(self):
        # The audit's HIGH (claim-verified): books_pages_link and
        # book_storage carry ON DELETE CASCADE FKs that upstream's
        # PRAGMA foreign_keys=ON fires but this write connection does not
        # engage, and books_delete_trg covers neither. The stranded pages
        # row (its book column is the PRIMARY KEY) used to make a later
        # move_book_from_trash of the same id abort with IntegrityError.
        # The fixture's create trigger already seeded the pages row (the
        # real schema's shape: every book carries one automatically).
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_pages_link WHERE book = 1"),
            [(1,)],
        )
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
        # Book 2's own auto-seeded row legitimately remains; book 1's is gone.
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_pages_link WHERE book = 1"),
            [(0,)],
        )

    def test_remove_then_move_back_from_trash_round_trips(self):
        # The end-to-end shape the stranding broke: trash a book with a
        # pages row, restore it, and the resurrection must not hit the
        # stranded PK.
        book_dir = self._book_dir(1, "Doomed")
        # Calibre's own backup thread writes this sidecar; move_book_from_trash
        # refuses entries without one.
        with open(os.path.join(book_dir, "metadata.opf"), "w") as f:
            f.write(
                "<?xml version='1.0' encoding='utf-8'?>"
                "<package xmlns='http://www.idpf.org/2007/opf' version='2.0' unique-identifier='id'>"
                "<metadata/><guide/></package>"
            )
        # The create trigger's auto-seeded pages row is the strander.
        self.assertEqual(
            self._sql("SELECT COUNT(*) FROM books_pages_link WHERE book = 1"),
            [(1,)],
        )
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1, delete_files="trash")
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.move_book_from_trash(1)
        # Book 2 was never removed; book 1 is back (its empty OPF restores
        # the Unknown placeholder title, the documented behavior).
        self.assertEqual(self._sql("SELECT COUNT(*) FROM books"), [(2,)])
        self.assertEqual(self._sql("SELECT id FROM books ORDER BY id"), [(1,), (2,)])
        # The directory relays to the restored metadata's place: the empty
        # OPF restores title Unknown and no authors, which lays out bare
        # Unknown/ (no author nesting).
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir, "Unknown")))
        self.assertFalse(os.path.isdir(book_dir))

    def test_remove_book_is_irreversible_second_call_raises(self):
        # Irreversible: the second call sees a book that is not there.
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.remove_book(1)
            with self.assertRaises(ValueError):
                wdb.remove_book(1)

    def test_remove_book_failure_inside_batch_rolls_back(self):
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
            wdb.batch(),
        ):
            wdb.remove_book(2)  # would prune Shared Author's second link
            wdb.remove_book(999)  # unknown book fails the pass
        self.assertEqual(self._sql("SELECT id FROM books ORDER BY id"), [(1,), (2,)])
        self.assertEqual(
            self._sql("SELECT name FROM authors ORDER BY name"),
            [("Lone Author",), ("Shared Author",)],
        )


class _WriteSideFixture:
    """The phase-6 write-side fixture plumbing (schema, seeds, helpers).
    Never collected and carries no tests of its own."""

    SCHEMA = _WRITE_SCHEMA

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        conn.execute(
            "INSERT INTO books (id, title, sort, author_sort) "
            "VALUES (1, 'Old Title', 'Old Title', 'Writer, Zed A.')"
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (2, 'Other', 'Other')")
        # Existing author with a hand-tuned sort key; shared with book 2.
        conn.execute(
            "INSERT INTO authors VALUES (1, 'Zed A. Writer', 'Writer, Zed A.', '')"
        )
        conn.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, 1)", [(1,), (2,)]
        )
        # Enumeration column (Read/Reading/To Read) + bool column.
        conn.execute(
            "INSERT INTO custom_columns VALUES "
            "(1,'status','Status','enumeration',1,?,0,1)",
            ('{"enum_values": ["Read", "Reading", "To Read"]}',),
        )
        conn.execute(
            "INSERT INTO custom_columns VALUES (2,'liked','Liked','bool',1,'{}',0,0)"
        )
        # Multi-valued text column (the #audience shape CalibreQuarry's
        # set-write verbs consume).
        conn.execute(
            "INSERT INTO custom_columns VALUES (3,'audience','Audience','text',1,'{}',1,1)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _wdb(self):
        return WritableCalibreDB(self.db_path)

    def _sql2(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()


class _WriteSideTests:
    """The phase-6 expansion tests; composed into TestWriteSideExpansion
    so they run exactly once against the plain fixture."""

    def test_set_authors_relinks_and_recomputes_author_sort(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_authors(1, ["Ann Leckie", "Zed A. Writer"]))
        conn = sqlite3.connect(self.db_path)
        names = [
            r[0]
            for r in conn.execute(
                "SELECT a.name FROM books_authors_link l JOIN authors a "
                "ON a.id=l.author WHERE l.book=1 ORDER BY l.id"
            )
        ]
        asort = conn.execute("SELECT author_sort FROM books WHERE id=1").fetchone()[0]
        newsort = conn.execute(
            "SELECT sort FROM authors WHERE name='Ann Leckie'"
        ).fetchone()[0]
        others = conn.execute(
            "SELECT COUNT(*) FROM books_authors_link WHERE book=2"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(names, ["Ann Leckie", "Zed A. Writer"])
        # 1.26: new authors follow upstream's creation path -- flipped sort,
        # comma stored as the legacy pipe in the name column.
        self.assertEqual(asort, "Leckie, Ann & Writer, Zed A.")
        self.assertEqual(newsort, "Leckie, Ann")
        self.assertEqual(
            self._sql2("SELECT name FROM authors WHERE name LIKE 'Ann%'"),
            [("Ann Leckie",)],
        )
        self.assertEqual(others, 1)  # shared author survives for book 2

    def test_set_authors_noop_returns_false(self):
        with self._wdb() as wdb:
            self.assertFalse(wdb.set_authors(1, ["Zed A. Writer"]))

    def test_set_authors_empty_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_authors(1, [])

    def test_set_series_and_clear(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_series(1, "Imperial Radch", 3))
            self.assertFalse(wdb.set_series(1, "Imperial Radch", 3))
            self.assertTrue(wdb.set_series(1, "Imperial Radch", 4))
            self.assertTrue(wdb.set_series(1, None))
        conn = sqlite3.connect(self.db_path)
        idx = conn.execute("SELECT series_index FROM books WHERE id=1").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        conn.close()
        self.assertEqual(idx, 1.0)  # the no-series value, never NULL
        self.assertEqual(count, 0)  # orphaned series pruned

    def test_set_publisher_roundtrip(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_publisher(1, "Orbit"))
            self.assertFalse(wdb.set_publisher(1, "orbit"))  # NOCASE match
            self.assertTrue(wdb.set_publisher(1, None))
        self.assertEqual(
            self._sql2("SELECT COUNT(*) FROM publishers"), [(0,)]
        )  # orphan pruned

    def test_set_rating_dedups_unique_rows(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_rating(1, 4))
            self.assertTrue(wdb.set_rating(2, 4))  # same stars -> same row
            self.assertFalse(wdb.set_rating(2, 4))
            self.assertTrue(wdb.set_rating(1, None))
        self.assertEqual(self._sql2("SELECT rating FROM ratings"), [(8,)])

    def test_set_rating_out_of_range_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_rating(1, 9.5)

    def test_set_languages_canonicalizes_names(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_languages(1, ["English", "fre"]))
        codes = self._sql2(
            "SELECT l.lang_code FROM books_languages_link bl "
            "JOIN languages l ON l.id=bl.lang_code WHERE bl.book=1 "
            "ORDER BY bl.item_order"
        )
        self.assertEqual(codes, [("eng",), ("fre",)])

    def test_set_languages_writes_item_order(self):
        # Calibre orders a book's languages by item_order; the writer used
        # to leave every row at 0 and hand ordering to the tiebreaker.
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_languages(1, ["English", "fre"]))
            self.assertTrue(wdb.set_languages(1, ["fre", "English"]))  # reorder
        self.assertEqual(
            self._sql2(
                "SELECT l.lang_code, bl.item_order FROM books_languages_link bl "
                "JOIN languages l ON l.id=bl.lang_code WHERE bl.book=1 "
                "ORDER BY bl.item_order"
            ),
            [("fre", 0), ("eng", 1)],
        )

    def test_set_comments_upsert_and_clear(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_comments(1, "<p>Wise words.</p>"))
            self.assertFalse(wdb.set_comments(1, "<p>Wise words.</p>"))
            self.assertTrue(wdb.set_comments(1, "<p>Changed.</p>"))
            self.assertTrue(wdb.set_comments(1, None))
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM comments"), [(0,)])

    def test_set_custom_column_enumeration_validated(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#status", "Read"))
            self.assertFalse(wdb.set_custom_column(1, "#status", "Read"))
            with self.assertRaises(ValueError):
                wdb.set_custom_column(1, "#status", "Not In Enum")

    def test_set_custom_column_bool_tristate(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#liked", True))
            self.assertTrue(wdb.set_custom_column(1, "#liked", False))
            self.assertTrue(wdb.set_custom_column(1, "#liked", None))
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM custom_column_2"), [(0,)])

    def test_set_custom_column_not_editable_raises(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE custom_columns SET editable=0 WHERE label='status'")
        conn.commit()
        conn.close()
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_custom_column(1, "#status", "Read")

    # The hardening tests seed their own columns in-test (rather than in
    # setUp) so the TestWriteSideExpansion subclasses that re-run these
    # methods against other schemas stay valid.

    def _add_rating_column(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE custom_column_4 (
                id INTEGER PRIMARY KEY, value INTEGER UNIQUE, link TEXT DEFAULT ''
            );
            CREATE TABLE books_custom_column_4_link (
                book INTEGER, value INTEGER, UNIQUE(book, value)
            );
            INSERT INTO custom_columns VALUES
                (4,'myrat','My Rat','rating',1,'{}',0,1);
            """
        )
        conn.commit()
        conn.close()

    def _add_datetime_column(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE custom_column_5 (
                id INTEGER PRIMARY KEY, book INTEGER UNIQUE, value TIMESTAMP
            );
            INSERT INTO custom_columns VALUES
                (5,'when','When','datetime',1,'{}',0,0);
            """
        )
        conn.commit()
        conn.close()

    def test_custom_rating_column_takes_stars_and_purges_zero(self):
        # The stored scale is Calibre's internal 0-10 (4 stars = 8), shared
        # across books via UNIQUE(value); 0 stars means unrated, matching
        # Calibre's purge of 0-rating rows. Raw values used to land as-is.
        self._add_rating_column()
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#myrat", 4))
            self.assertTrue(wdb.set_custom_column(2, "#myrat", 4))  # shares row
            self.assertFalse(wdb.set_custom_column(2, "#myrat", 4))
            with self.assertRaises(ValueError):
                wdb.set_custom_column(1, "#myrat", 5.5)
            with self.assertRaises(ValueError):
                wdb.set_custom_column(1, "#myrat", -1)
        self.assertEqual(self._sql2("SELECT value FROM custom_column_4"), [(8,)])
        self.assertEqual(
            self._sql2("SELECT book FROM books_custom_column_4_link ORDER BY book"),
            [(1,), (2,)],
        )
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#myrat", 0))  # clear
            self.assertFalse(wdb.set_custom_column(1, "#myrat", 0))
        # Book 2's link (and its shared value row) survive.
        self.assertEqual(
            self._sql2("SELECT book FROM books_custom_column_4_link"), [(2,)]
        )

    def test_custom_datetime_column_normalizes_like_set_pubdate(self):
        # Datetime columns store the same ISO-in-UTC TEXT set_pubdate
        # writes; a naive datetime used to land as str() without an offset.
        self._add_datetime_column()
        with self._wdb() as wdb:
            # A naive spelling is taken as UTC, exactly like set_pubdate.
            self.assertTrue(wdb.set_custom_column(1, "#when", "2014-03-01T12:30:00"))
            self.assertTrue(
                wdb.set_custom_column(2, "#when", datetime(1991, 10, 1, tzinfo=UTC))
            )
        self.assertEqual(
            self._sql2("SELECT book, value FROM custom_column_5 ORDER BY book"),
            [
                (1, "2014-03-01 12:30:00+00:00"),
                (2, "1991-10-01 00:00:00+00:00"),
            ],
        )

    def test_custom_column_unknown_datatype_raises(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE custom_column_6 (
                id INTEGER PRIMARY KEY, book INTEGER UNIQUE, value TEXT
            );
            INSERT INTO custom_columns VALUES
                (6,'weird','Weird','weird',1,'{}',0,0);
            """
        )
        conn.commit()
        conn.close()
        # Unknown datatypes used to be silently accepted and stringified.
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_custom_column(1, "#weird", "anything")

    def test_custom_enumeration_empty_values_rejects_everything(self):
        # An enumeration whose enum_values is empty accepts nothing; the
        # validation used to be skipped entirely for that shape (upstream
        # silently drops the write instead, which is no better).
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE custom_column_6 (
                id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT ''
            );
            CREATE TABLE books_custom_column_6_link (
                book INTEGER, value INTEGER, UNIQUE(book, value)
            );
            INSERT INTO custom_columns VALUES
                (6,'gap','Gap','enumeration',1,'{}',0,1);
            """
        )
        conn.commit()
        conn.close()
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_custom_column(1, "#gap", "Read")

    def test_none_entries_are_skipped_not_stringified(self):
        # A None inside a value list used to become the literal string
        # 'None' in both the replace and the append path.
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#audience", ["Rin", None]))
            self.assertEqual(
                wdb.add_custom_column_values(1, "#audience", ["Brandon", None]), 1
            )
        self.assertEqual(
            self._sql2(
                "SELECT c.value FROM books_custom_column_3_link l "
                "JOIN custom_column_3 c ON c.id = l.value WHERE l.book = 1 "
                "ORDER BY c.id"
            ),
            [("Rin",), ("Brandon",)],
        )
        self.assertEqual(
            self._sql2("SELECT COUNT(*) FROM custom_column_3 WHERE value = 'None'"),
            [(0,)],
        )

    def test_bare_string_is_one_value_not_a_comma_split(self):
        # "Doe, John" used to round-trip as two phantom values: the write
        # side comma-split bare strings and the read side comma-joined and
        # re-split. A bare string is now exactly one stored value; lists
        # carry multiple values.
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_custom_column(1, "#audience", "Doe, John"))
        self.assertEqual(
            self._sql2("SELECT value FROM custom_column_3"), [("Doe, John",)]
        )
        self.assertEqual(
            self._sql2("SELECT COUNT(*) FROM books_custom_column_3_link"), [(1,)]
        )

    def test_add_format_negative_size_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.add_format(1, "EPUB", "oldtitle", -1)

    def test_set_rating_zero_is_unrated_not_a_zero_row(self):
        # Calibre maps 0 to unrated (no row); a 0-rating link used to land
        # and show up as a spurious Tag Browser entry.
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_rating(1, 4))
            self.assertTrue(wdb.set_rating(1, 0))  # clears
            self.assertFalse(wdb.set_rating(1, 0))  # already unrated
            self.assertFalse(wdb.set_rating(2, 0))
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM ratings"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_ratings_link"), [(0,)])

    def test_set_format_replaces_in_one_transaction(self):
        # The bindery install_format composition, promoted: remove+add of a
        # format row with no window where the book has no data row.
        with self._wdb() as wdb:
            self.assertTrue(wdb.add_format(1, "EPUB", "oldtitle", 1024))
            self.assertTrue(wdb.set_format(1, "EPUB", "newtitle", 2048))
            self.assertFalse(wdb.set_format(1, "EPUB", "newtitle", 2048))
            self.assertTrue(wdb.set_format(1, "epub", "NewTitle", 4096))
            with self.assertRaises(ValueError):
                wdb.set_format(1, "EPUB", "x", -5)
        self.assertEqual(
            self._sql2(
                "SELECT format, name, uncompressed_size FROM data WHERE book = 1"
            ),
            [("EPUB", "NewTitle", 4096)],
        )
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_add_remove_format_and_has_cover(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.add_format(1, "EPUB", "oldtitle", 2048))
            with self.assertRaises(ValueError):
                wdb.add_format(1, "epub", "dupe", 1)  # case-insensitive clash
            self.assertTrue(wdb.remove_format(1, "EPUB"))
            self.assertFalse(wdb.remove_format(1, "EPUB"))
            self.assertTrue(wdb.set_has_cover(1, True))
            self.assertFalse(wdb.set_has_cover(1, True))
            self.assertTrue(wdb.set_has_cover(1, False))


class TestWriteSideExpansion(_WriteSideFixture, _WriteSideTests, unittest.TestCase):
    """The phase-6 expansion tests, run once against the plain fixture."""


class TestBatchContext(_WriteSideFixture, unittest.TestCase):
    """batch() defers every setter's commit: one transaction per curation pass.

    The 2026-08-27 phase-3 import committed ~45 mutations as 45 separate
    transactions; a crash mid-pass left a half-curated batch. A batch makes
    the whole pass atomic while every setter keeps its signature.
    """

    def test_batch_commits_once_on_success(self):
        with self._wdb() as wdb, wdb.batch():
            wdb.add_tag(1, "Audited")
            wdb.update_title(1, "New Name")
        self.assertEqual(
            self._sql2("SELECT title FROM books WHERE id=1"), [("New Name",)]
        )
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(1,)])
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_batch_rolls_back_everything_on_failure(self):
        with self._wdb() as wdb, self.assertRaises(ValueError), wdb.batch():
            wdb.add_tag(1, "Audited")
            wdb.update_title(2, "Renamed")
            wdb.add_tag(999, "Never")  # unknown book raises mid-batch
        self.assertEqual(self._sql2("SELECT title FROM books WHERE id=2"), [("Other",)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])

    def test_nested_batches_join_one_transaction(self):
        with self._wdb() as wdb, wdb.batch():
            wdb.add_tag(1, "A")
            with wdb.batch():
                wdb.add_tag(1, "B")
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(2,)])

    def test_inner_failure_caught_by_outer_still_rolls_back(self):
        # An inner batch's exception, caught by the outer block, used to be
        # swallowed into a commit (the local ok flag only saw the outer's
        # clean yield). The poisoned flag makes the failure stick: the whole
        # pass rolls back, including writes made before and after the inner
        # segment, because that segment's partial writes are still pending.
        with self._wdb() as wdb, wdb.batch():
            wdb.add_tag(1, "Keep")
            try:
                with wdb.batch():
                    wdb.add_tag(1, "Temp")
                    raise ValueError("inner gave up")
            except ValueError:
                pass
            wdb.update_title(1, "Renamed")  # must not survive the outer exit
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(0,)])
        self.assertEqual(
            self._sql2("SELECT title FROM books WHERE id=1"), [("Old Title",)]
        )
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])

    def test_batch_recovers_after_a_rolled_back_pass(self):
        # The poisoned flag clears at the outermost exit: the NEXT batch on
        # the same handle commits normally.
        with self._wdb() as wdb:
            with wdb.batch():
                try:
                    with wdb.batch():
                        raise ValueError("poison once")
                except ValueError:
                    pass
            with wdb.batch():
                wdb.add_tag(1, "Fine")
        self.assertEqual(self._sql2("SELECT name FROM tags ORDER BY id"), [("Fine",)])

    def test_setter_outside_batch_unchanged(self):
        # The commit boundary only moves inside batch(); bare calls commit
        # per method exactly as before (update_title returns None by design).
        with self._wdb() as wdb:
            self.assertTrue(wdb.add_tag(1, "Solo"))
            wdb.update_title(1, "Solo Title")
        self.assertEqual(
            self._sql2("SELECT title FROM books WHERE id=1"), [("Solo Title",)]
        )
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(1,)])

    def test_transaction_alias_is_batch(self):
        # Pre-1.7.0 call shape (the name a 2026-08-29 phase-3 import reached
        # for). Identity at the API level; the failure twin below still
        # exercises the alias end to end.
        with self._wdb() as wdb:
            self.assertIs(type(wdb.transaction()), type(wdb.batch()))

    def test_transaction_alias_rolls_back_on_failure(self):
        with self._wdb() as wdb, self.assertRaises(ValueError), wdb.transaction():
            wdb.add_tag(1, "Audited")
            wdb.add_tag(999, "Never")  # unknown book raises mid-batch
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])


class TestBatchSetComments(_WriteSideFixture, unittest.TestCase):
    """set_comments' changed flag stays honest inside a batch, in every
    ordering.

    Pinned by the 2026-09-26 ninth-wave incident: a curation script
    reported ``set_comments`` returning False inside its batch and the
    report was filed against the setter. The actual cause was the
    caller's own loop-variable rebinding (a ``for b in SERIES_CLEAR:``
    nested inside ``for b in IDS:`` re-called ``set_comments`` on one
    already-written book 24 times); an exhaustive ordering fuzz (2,914
    scenarios) found no setter defect. These tests enforce the
    exoneration so a real regression cannot hide behind the anecdote.
    """

    def _store_comment(self, text, book_id=1):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO comments (book, text) VALUES (?, ?)", (book_id, text)
            )
            conn.commit()
        finally:
            conn.close()

    def test_in_batch_set_comments_lands_and_returns_true(self):
        with self._wdb() as wdb, wdb.batch():
            wdb.add_tag(1, "Curated")
            wdb.set_pubdate(1, "1902-01-01")
            self.assertTrue(wdb.set_comments(1, "<p>House voice.</p>"))
        self.assertEqual(
            self._sql2("SELECT text FROM comments WHERE book=1"),
            [("<p>House voice.</p>",)],
        )

    def test_series_clear_then_set_comments_in_one_batch(self):
        # The exact ordering the incident blamed: the clear runs first,
        # the comment still writes and reports changed.
        self.assertTrue(self._sql2("SELECT COUNT(*) FROM series") == [(0,)])
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_series(1, "Invented Series"))
        with self._wdb() as wdb, wdb.batch():
            self.assertTrue(wdb.set_series(1, None))
            self.assertTrue(wdb.set_comments(1, "<p>After the clear.</p>"))
        self.assertEqual(
            self._sql2("SELECT text FROM comments WHERE book=1"),
            [("<p>After the clear.</p>",)],
        )

    def test_unchanged_comment_returns_false_in_batch(self):
        self._store_comment("<p>Already stored.</p>")
        with self._wdb() as wdb, wdb.batch():
            self.assertFalse(wdb.set_comments(1, "<p>Already stored.</p>"))
        self.assertEqual(
            self._sql2("SELECT text FROM comments WHERE book=1"),
            [("<p>Already stored.</p>",)],
        )

    def test_repeat_call_in_batch_is_true_then_honest_false(self):
        # The incident's misread shape: a loop that calls the setter twice
        # on the same book sees True once, then False for the repeats --
        # that False is already-so, not a dropped write.
        with self._wdb() as wdb, wdb.batch():
            self.assertTrue(wdb.set_comments(1, "<p>Once.</p>"))
            self.assertFalse(wdb.set_comments(1, "<p>Once.</p>"))
        self.assertEqual(
            self._sql2("SELECT COUNT(*) FROM comments WHERE book=1"), [(1,)]
        )

    def test_rolled_back_batch_then_fresh_handle_rerun_lands(self):
        # A rolled-back earlier batch must not poison a later pass: the
        # rerun (fresh handle, the fresh-process shape) writes and reports
        # changed.
        self._store_comment("<p>Survives the rollback.</p>")
        with (
            self._wdb() as wdb,
            self.assertRaises(RuntimeError),
            wdb.batch(),
        ):
            self.assertTrue(wdb.set_comments(1, "<p>Lost.</p>"))
            raise RuntimeError("boom")
        self.assertEqual(
            self._sql2("SELECT text FROM comments WHERE book=1"),
            [("<p>Survives the rollback.</p>",)],
        )
        with WritableCalibreDB(self.db_path) as wdb, wdb.batch():
            self.assertTrue(wdb.set_comments(1, "<p>Lands now.</p>"))
        self.assertEqual(
            self._sql2("SELECT text FROM comments WHERE book=1"),
            [("<p>Lands now.</p>",)],
        )


class TestSetPubdate(_WriteSideFixture, unittest.TestCase):
    """set_pubdate writes Calibre's TEXT convention, never a raw integer.

    The 2026-08-27 batch wrote unix integers into the TEXT column and got
    8 'sentinel pubdate' / 'unparseable pubdate' linter errors from 4 books.
    """

    def _pubdate(self):
        return self._sql2("SELECT pubdate FROM books WHERE id=1")[0][0]

    def test_str_date_normalizes_to_utc_midnight(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_pubdate(1, "2014-03-01"))
        self.assertEqual(self._pubdate(), "2014-03-01 00:00:00+00:00")

    def test_str_datetime_and_tz_converts_to_utc(self):
        with self._wdb() as wdb:
            wdb.set_pubdate(1, "2014-03-01T12:30:00")
            self.assertEqual(self._pubdate(), "2014-03-01 12:30:00+00:00")
            wdb.set_pubdate(
                1,
                datetime(2014, 3, 1, 12, 30, tzinfo=timezone(timedelta(hours=-5))),
            )
        self.assertEqual(self._pubdate(), "2014-03-01 17:30:00+00:00")

    def test_date_object_normalizes(self):
        from datetime import date

        with self._wdb() as wdb:
            wdb.set_pubdate(1, date(1991, 10, 1))
        self.assertEqual(self._pubdate(), "1991-10-01 00:00:00+00:00")

    def test_none_writes_undefined_sentinel(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_pubdate(1, None))
        self.assertEqual(self._pubdate(), "0101-01-01 00:00:00+00:00")

    def test_same_instant_is_noop(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_pubdate(1, "1991-10-01 07:00:00+00:00"))
            # Equivalent spelling of the same instant: no rewrite, no dirty.
            self.assertFalse(wdb.set_pubdate(1, "1991-10-01 07:00:00+00:00"))
            self.assertTrue(wdb.set_pubdate(1, None))  # clears to the sentinel
            self.assertFalse(wdb.set_pubdate(1, None))  # already the sentinel
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_unparseable_string_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_pubdate(1, "sentinel pubdate")

    def test_unknown_book_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_pubdate(42, "2020-01-01")

    def test_change_touches_book_and_queues_opf(self):
        with self._wdb() as wdb:
            before = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            self.assertTrue(wdb.set_pubdate(1, "2014-03-01"))
            after = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            self.assertNotEqual(after, before)
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_pubdate_failure_inside_batch_rolls_back(self):
        with self._wdb() as wdb, self.assertRaises(ValueError), wdb.batch():
            wdb.set_pubdate(1, "2014-03-01")
            wdb.set_pubdate(2, "not a date")
        self.assertIsNone(self._pubdate())
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])


class TestSetWriteConveniences(_WriteSideFixture, unittest.TestCase):
    """Phase-11 set-write conveniences: clear_tags, add_custom_column_values,
    clear_rating. The link-table fixture carries UNIQUE(book, value), the
    shape CalibreQuarry's set-write tests will mirror."""

    def _audience(self, book=1):
        return self._sql2(
            "SELECT c.value FROM books_custom_column_3_link l "
            "JOIN custom_column_3 c ON c.id=l.value WHERE l.book=? "
            "ORDER BY c.id",
            (book,),
        )

    def test_clear_tags_returns_count_and_prunes_orphans(self):
        with self._wdb() as wdb:
            wdb.add_tag(1, "Fic")
            wdb.add_tag(1, "Audited")
            wdb.add_tag(2, "Fic")  # shared: survives book 1's clear
            self.assertEqual(wdb.clear_tags(1), 2)
            self.assertEqual(wdb.clear_tags(1), 0)  # honest no-op
        self.assertEqual(self._sql2("SELECT name FROM tags"), [("Fic",)])
        self.assertEqual(self._sql2("SELECT tag FROM books_tags_link WHERE book=1"), [])

    def test_clear_tags_touches_book_and_queues_only_on_change(self):
        with self._wdb() as wdb:
            wdb.add_tag(1, "Fic")
            before = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            self.assertEqual(wdb.clear_tags(1), 1)
            after = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            self.assertNotEqual(after, before)
            wdb.clear_tags(2)  # untagged already: nothing queued
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_clear_tags_unknown_book_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.clear_tags(999)

    def test_add_custom_column_values_appends_beside_existing(self):
        # The motivating case: #audience "Rin" already set, "Brandon" joins.
        with self._wdb() as wdb:
            wdb.set_custom_column(1, "#audience", "Rin")
            self.assertEqual(
                wdb.add_custom_column_values(1, "#audience", ["Brandon"]), 1
            )
            # Re-appending a value the book already carries is a no-op.
            self.assertEqual(
                wdb.add_custom_column_values(1, "#audience", ["Brandon"]), 0
            )
        self.assertEqual(self._audience(), [("Rin",), ("Brandon",)])

    def test_add_custom_column_values_dedupes_and_counts_honestly(self):
        with self._wdb() as wdb:
            self.assertEqual(
                wdb.add_custom_column_values(1, "#audience", ["Rin", "Rin", "Brandon"]),
                2,
            )
            # One already present, one genuinely new.
            self.assertEqual(
                wdb.add_custom_column_values(1, "#audience", ["Brandon", "New"]), 1
            )
            # A second book shares the value row instead of duplicating it:
            # the count stays at three and UNIQUE(value) holds.
            self.assertEqual(wdb.add_custom_column_values(2, "#audience", ["Rin"]), 1)
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM custom_column_3"), [(3,)])
        self.assertEqual(self._audience(2), [("Rin",)])

    def test_add_custom_column_values_rejects_replace_only_surfaces(self):
        with self._wdb() as wdb:
            # Single-valued (enumeration) and direct-storage (bool) columns
            # have a value to replace, not a set to append to.
            with self.assertRaises(ValueError):
                wdb.add_custom_column_values(1, "#status", ["Read"])
            with self.assertRaises(ValueError):
                wdb.add_custom_column_values(1, "#liked", ["Yes"])
            # A bare string is ambiguous for an append (wrong type, not a
            # bad value).
            with self.assertRaises(TypeError):
                wdb.add_custom_column_values(1, "#audience", "Brandon")
            with self.assertRaises(ValueError):
                wdb.add_custom_column_values(1, "#nope", ["x"])
            with self.assertRaises(ValueError):
                wdb.add_custom_column_values(999, "#audience", ["x"])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM custom_column_3"), [(0,)])

    def test_add_custom_column_values_noop_touches_nothing(self):
        with self._wdb() as wdb:
            wdb.add_custom_column_values(1, "#audience", ["Rin"])
            before = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            wdb.add_custom_column_values(1, "#audience", ["Rin"])
            after = self._sql2("SELECT last_modified FROM books WHERE id=1")[0][0]
            self.assertEqual(after, before)
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_clear_rating_alias_deletes_link_and_prunes(self):
        with self._wdb() as wdb:
            wdb.set_rating(1, 4)
            wdb.set_rating(2, 4)  # shares the ratings row (UNIQUE(rating))
            self.assertTrue(wdb.clear_rating(1))
            self.assertFalse(wdb.clear_rating(1))  # already unrated
            self.assertEqual(self._sql2("SELECT rating FROM ratings"), [(8,)])
            self.assertTrue(wdb.clear_rating(2))
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM ratings"), [(0,)])

    def test_new_setters_inside_batch_rollback_atomically(self):
        with self._wdb() as wdb, self.assertRaises(ValueError), wdb.batch():
            wdb.add_tag(1, "Temp")
            wdb.add_custom_column_values(1, "#audience", ["Rin"])
            wdb.clear_rating(1)
            wdb.clear_tags(999)  # unknown book raises mid-batch
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM custom_column_3"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])


class TestAddBook(_WriteSideFixture, unittest.TestCase):
    """Phase 10: the creation path, against the schema that genuinely
    differs (AUTOINCREMENT plus the real INSERT-path hazards:
    books_insert_trg calling title_sort()/uuid4(), the Count Pages create
    trigger, the fkc_insert_* guards). Since the deflation this class
    carries ONLY its own tests: the expansion suite runs once, in
    TestWriteSideExpansion. setUp seeds ids 1 and 2, so with
    AUTOINCREMENT the next id (real or predicted) is 3.
    """

    SCHEMA = _ADD_BOOK_SCHEMA

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        # The seeded inserts themselves fire books_insert_trg.
        register_udfs(conn)
        conn.executescript(self.SCHEMA)
        conn.execute(
            "INSERT INTO books (id, title, sort, author_sort) "
            "VALUES (1, 'Old Title', 'Old Title', 'Writer, Zed A.')"
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (2, 'Other', 'Other')")
        # Existing author with a hand-tuned sort key; shared with book 2.
        conn.execute(
            "INSERT INTO authors VALUES (1, 'Zed A. Writer', 'Writer, Zed A.', '')"
        )
        conn.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, 1)", [(1,), (2,)]
        )
        # The shared expansion tests exercise the custom-column writers
        # against this trigger schema too, so it seeds the same columns.
        conn.execute(
            "INSERT INTO custom_columns VALUES "
            "(1,'status','Status','enumeration',1,?,0,1)",
            ('{"enum_values": ["Read", "Reading", "To Read"]}',),
        )
        conn.execute(
            "INSERT INTO custom_columns VALUES (2,'liked','Liked','bool',1,'{}',0,0)"
        )
        conn.execute(
            "INSERT INTO custom_columns VALUES (3,'audience','Audience','text',1,'{}',1,1)"
        )
        conn.commit()
        conn.close()

    def _epub(self, payload: bytes = b"EPUBPAYLOAD") -> str:
        path = os.path.join(self.temp_dir, "seed.epub")
        with open(path, "wb") as f:
            f.write(payload)
        return path

    def _count(self, sql: str) -> int:
        return self._sql2(sql)[0][0]

    def test_add_book_minimal_row_links_and_path(self):
        with self._wdb() as wdb:
            book_id = wdb.add_book("The Fifth Head of Data", ["Ann Leckie"])
        self.assertEqual(book_id, 3)
        row = self._sql2(
            "SELECT title, sort, uuid, author_sort, path, has_cover FROM books "
            "WHERE id = 3"
        )[0]
        title, sort, uuid, author_sort, path, has_cover = row
        self.assertEqual(title, "The Fifth Head of Data")
        self.assertEqual(sort, "Fifth Head of Data, The")  # books_insert_trg
        self.assertEqual(len(uuid), 36)  # books_insert_trg uuid4()
        # 1.26: new author, flipped sort (upstream's creation path); the
        # path takes the first stored author name verbatim.
        self.assertEqual(author_sort, "Leckie, Ann")
        self.assertEqual(path, "Ann Leckie/The Fifth Head of Data (3)")
        self.assertEqual(has_cover, 0)
        # Author link + the Count Pages create-trigger row.
        self.assertEqual(
            self._sql2(
                "SELECT a.name FROM books_authors_link l JOIN authors a "
                "ON a.id = l.author WHERE l.book = 3"
            ),
            [("Ann Leckie",)],
        )
        self.assertEqual(
            self._sql2("SELECT book FROM books_pages_link WHERE book = 3"), [(3,)]
        )
        # Queued for OPF regeneration like every other write.
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(3,)])

    def test_add_book_formats_placed_atomically_with_truthful_rows(self):
        source = self._epub(b"EXACT-PAYLOAD")
        with self._wdb() as wdb:
            book_id = wdb.add_book(
                "The Fifth Head of Data", ["Ann Leckie"], formats=[source]
            )
        name, size = self._sql2(
            "SELECT name, uncompressed_size FROM data WHERE book = ? AND format = 'EPUB'",
            (book_id,),
        )[0]
        self.assertEqual(name, "The Fifth Head of Data - Ann Leckie")
        self.assertEqual(size, len(b"EXACT-PAYLOAD"))
        placed = os.path.join(
            self.temp_dir, "Ann Leckie", "The Fifth Head of Data (3)", f"{name}.epub"
        )
        with open(placed, "rb") as f:
            self.assertEqual(f.read(), b"EXACT-PAYLOAD")
        # Copy only: the source survives untouched.
        with open(source, "rb") as f:
            self.assertEqual(f.read(), b"EXACT-PAYLOAD")

    def test_add_book_seeds_formats_queue_extraction_and_pages_scan(self):
        # The six-lens-audit gap: add_book inserted data rows directly, so
        # seeded formats never entered dirtied_formats or needs_scan;
        # upstream's own add path queues every data INSERT for extraction.
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            """
        )
        fts.commit()
        fts.close()
        source = self._epub(b"INDEXME")
        with self._wdb() as wdb:
            book_id = wdb.add_book(
                "The Fifth Head of Data", ["Ann Leckie"], formats=[source]
            )
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        try:
            self.assertEqual(
                {r[0] for r in fts.execute("SELECT format FROM dirtied_formats")},
                {"EPUB"},
            )
        finally:
            fts.close()
        self.assertEqual(
            self._sql2(
                "SELECT needs_scan FROM books_pages_link WHERE book = ?", (book_id,)
            ),
            [(1,)],
        )

    def test_failed_add_book_leaves_no_queue_entry(self):
        # The queue write joins the add's batch: a rolled-back add leaves
        # nothing behind for Calibre to chew on.
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            """
        )
        fts.commit()
        fts.close()
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(RuntimeError), wdb.batch():
            wdb.add_book(
                "The Fifth Head of Data",
                ["Ann Leckie"],
                formats=[self._epub(b"ROLLBACK")],
            )
            raise RuntimeError("boom")
        wdb.close()
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        try:
            self.assertEqual(
                fts.execute("SELECT COUNT(*) FROM dirtied_formats").fetchone()[0], 0
            )
        finally:
            fts.close()

    def test_add_book_cover_places_and_flags(self):
        for sig, expected in (
            (b"\xff\xd8\xff\xe0jpegbody", "cover.jpg"),
            (b"\x89PNG\r\n\x1a\nrest", "cover.png"),
        ):
            with self.subTest(expected=expected):
                with self._wdb() as wdb:
                    book_id = wdb.add_book(
                        "The Fifth Head of Data", ["Ann Leckie"], cover=sig
                    )
                rel_path = self._sql2(
                    "SELECT path FROM books WHERE id = ?", (book_id,)
                )[0][0]
                self.assertTrue(
                    os.path.isfile(os.path.join(self.temp_dir, rel_path, expected))
                )
                self.assertEqual(
                    self._sql2("SELECT has_cover FROM books WHERE id = ?", (book_id,)),
                    [(1,)],
                )

    def test_add_book_cover_sniff_or_raise_writes_nothing(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.add_book(
                "The Fifth Head of Data",
                ["Ann Leckie"],
                cover=b"definitely not an image",
            )
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 2)
        self.assertFalse(os.path.isdir(os.path.join(self.temp_dir, "Ann Leckie")))

    def test_add_book_empty_title_and_no_authors_is_legal(self):
        with self._wdb() as wdb:
            book_id = wdb.add_book("   ", [])
        self.assertEqual(book_id, 3)
        title, author_sort, path = self._sql2(
            "SELECT title, author_sort, path FROM books WHERE id = 3"
        )[0]
        self.assertEqual(title, "Unknown")  # Calibre's own default
        self.assertEqual(author_sort, "")
        self.assertEqual(path, "Unknown/Unknown (3)")
        # No author links: exactly what find_authorless expects.
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM books_authors_link WHERE book = 3"),
            0,
        )

    def test_add_book_full_seed(self):
        with self._wdb() as wdb:
            wdb.add_book(
                "The Fifth Head of Data",
                ["Ann Leckie"],
                formats=[self._epub()],
                identifiers={"ISBN": " 9780000000000 ", "GoodReads": "42"},
                language="English",
                pubdate="2001-02-03",
                publisher="Orbit",
            )
        row = self._sql2("SELECT pubdate, has_cover FROM books WHERE id = 3")[0]
        self.assertEqual(row[0], "2001-02-03 00:00:00+00:00")
        self.assertEqual(row[1], 0)  # no cover seeded, no cover catalogued
        self.assertEqual(
            self._sql2(
                "SELECT type, val FROM identifiers WHERE book = 3 ORDER BY type"
            ),
            [("goodreads", "42"), ("isbn", "9780000000000")],
        )
        self.assertEqual(
            self._sql2(
                "SELECT l.lang_code FROM books_languages_link bl JOIN languages l "
                "ON l.id = bl.lang_code WHERE bl.book = 3"
            ),
            [("eng",)],
        )
        self.assertEqual(
            self._sql2(
                "SELECT p.name FROM books_publishers_link pl JOIN publishers p "
                "ON p.id = pl.publisher WHERE pl.book = 3"
            ),
            [("Orbit",)],
        )
        self.assertEqual(
            self._sql2("SELECT format FROM data WHERE book = 3"), [("EPUB",)]
        )

    def test_add_book_duplicate_format_seed_raises(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.add_book("T", ["A"], formats=[self._epub(), self._epub(b"other")])

    def test_add_book_refuses_byte_identical_reimport(self):
        # The 'never imported twice' invariant (CQ Phase 18 carve-out): a
        # file the library already catalogues cannot be added again, under
        # any title, before anything is written.
        source = self._epub(b"SAME-BYTES")
        with self._wdb() as wdb:
            wdb.add_book("The Fifth Head of Data", ["Ann Leckie"], formats=[source])
            with self.assertRaises(ValueError):
                wdb.add_book("Second Title", ["Other Author"], formats=[source])
            with self.assertRaises(ValueError):
                # Dry runs hit the same guard: refusal is a validation.
                wdb.add_book(
                    "Second Title", ["Other Author"], formats=[source], dry_run=True
                )
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 3)

    def test_add_book_same_size_different_bytes_is_allowed(self):
        # The guard compares content, not just the size pre-filter.
        with self._wdb() as wdb:
            wdb.add_book("A Book", ["Ann Leckie"], formats=[self._epub(b"0" * 32)])
            wdb.add_book("Another", ["Ann Leckie"], formats=[self._epub(b"1" * 32)])
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 4)
        self.assertEqual(self._count("SELECT COUNT(*) FROM data"), 2)

    def test_add_book_ascii_fold_is_the_documented_deviation(self):
        with self._wdb() as wdb:
            book_id = wdb.add_book("Ünicode Tëst", ["Ann Leckie"])
        path = self._sql2("SELECT path FROM books WHERE id = ?", (book_id,))[0][0]
        self.assertEqual(path, "Ann Leckie/_nicode T_st (3)")

    def test_add_book_existing_author_reuses_sort_nocase(self):
        with self._wdb() as wdb:
            wdb.add_book("The Fifth Head of Data", ["zed a. writer"])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM authors"), [(1,)])
        self.assertEqual(
            self._sql2("SELECT author_sort FROM books WHERE id = 3"),
            [("Writer, Zed A.",)],
        )

    def test_add_book_dry_run_writes_nothing(self):
        source = self._epub()
        with self._wdb() as wdb:
            plan = wdb.add_book(
                "The Fifth Head of Data",
                ["Ann Leckie"],
                formats=[source],
                cover=b"\xff\xd8\xff\xe0jpegbody",
                dry_run=True,
            )
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["predicted_id"], 3)
        self.assertEqual(
            plan["books_row"]["path"], "Ann Leckie/The Fifth Head of Data (3)"
        )
        self.assertEqual(plan["books_row"]["sort"], "Fifth Head of Data, The")
        self.assertEqual(
            plan["authors"], [{"name": "Ann Leckie", "sort": "Ann Leckie", "new": True}]
        )
        self.assertEqual(
            plan["formats"],
            [
                {
                    "format": "EPUB",
                    "filename": "The Fifth Head of Data - Ann Leckie.epub",
                    "size": len(b"EPUBPAYLOAD"),
                    "source": source,
                }
            ],
        )
        self.assertEqual(plan["cover"], "cover.jpg")
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM authors"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM metadata_dirtied"), 0)
        self.assertFalse(os.path.isdir(os.path.join(self.temp_dir, "Ann Leckie")))

    def test_add_book_dry_run_prediction_tracks_adds(self):
        with self._wdb() as wdb:
            first = wdb.add_book("First", ["A"])
            plan = wdb.add_book("Second", ["A"], dry_run=True)
            second = wdb.add_book("Second", ["A"])
        self.assertEqual(plan["predicted_id"], first + 1)
        self.assertEqual(second, first + 1)

    def test_add_book_failure_removes_rows_and_directory(self):
        # A directory squatting on the format-file path makes the atomic
        # replace fail AFTER the row was inserted: the batch must undo the
        # SQL and the tracked rmtree must take the whole directory (and the
        # squatter) with it.
        book_dir = os.path.join(
            self.temp_dir, "Ann Leckie", "The Fifth Head of Data (3)"
        )
        squatter = os.path.join(book_dir, "The Fifth Head of Data - Ann Leckie.epub")
        os.makedirs(squatter)
        with open(os.path.join(squatter, "sentinel.txt"), "w") as f:
            f.write("planted")
        with self._wdb() as wdb, self.assertRaises(OSError):
            wdb.add_book(
                "The Fifth Head of Data", ["Ann Leckie"], formats=[self._epub()]
            )
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM authors"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM data"), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM metadata_dirtied"), 0)
        self.assertFalse(os.path.exists(book_dir))

    def test_add_book_nested_failure_rolls_back_together(self):
        # add_book joined to a caller's batch: its OWN failure must remove
        # its files (inner compensation) while the outer batch undoes the
        # SQL. An outer failure AFTER a successful add is the documented
        # orphan window instead (the risks note in the roadmap).
        book_dir = os.path.join(self.temp_dir, "Ann Leckie", "Doomed (3)")
        squatter = os.path.join(book_dir, "Doomed - Ann Leckie.epub")
        os.makedirs(squatter)
        with self._wdb() as wdb, self.assertRaises(OSError), wdb.batch():
            wdb.add_book("Doomed", ["Ann Leckie"], formats=[self._epub()])
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM authors"), 1)
        self.assertEqual(self._count("SELECT COUNT(*) FROM metadata_dirtied"), 0)
        self.assertFalse(os.path.isdir(book_dir))

    def test_batch_failure_after_add_removes_earlier_directories(self):
        # Two books in one shared batch; the second one fails on its format
        # placement. The SQL rollback used to strand the FIRST book's
        # directory on disk as an orphan that looks like a real book while
        # no row points at it; the outermost failed exit now removes every
        # directory created inside the pass.
        kept_dir = os.path.join(self.temp_dir, "Ann Leckie", "Kept (3)")
        doomed_dir = os.path.join(self.temp_dir, "Ann Leckie", "Doomed (4)")
        os.makedirs(os.path.join(doomed_dir, "Doomed - Ann Leckie.epub"))
        with self._wdb() as wdb, self.assertRaises(OSError), wdb.batch():
            wdb.add_book("Kept", ["Ann Leckie"])
            wdb.add_book("Doomed", ["Ann Leckie"], formats=[self._epub()])
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 2)
        self.assertEqual(self._count("SELECT COUNT(*) FROM metadata_dirtied"), 0)
        self.assertFalse(os.path.isdir(kept_dir))
        self.assertFalse(os.path.isdir(doomed_dir))

    def test_failed_batch_never_touches_committed_books(self):
        # Only directories created INSIDE the failed batch compensate; a
        # book added (and committed) before the batch stays untouched.
        with self._wdb() as wdb:
            kept = wdb.add_book("Kept", ["Ann Leckie"])
            with self.assertRaises(ValueError), wdb.batch():
                wdb.add_tag(999, "Never")  # unknown book fails the pass
        kept_dir = os.path.join(self.temp_dir, "Ann Leckie", f"Kept ({kept})")
        self.assertTrue(os.path.isdir(kept_dir))
        self.assertEqual(self._count("SELECT COUNT(*) FROM books"), 3)
        # The kept add's OPF queue entry survives the later failed batch.
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(3,)])


class TestPathRelaying(_WriteSideFixture, unittest.TestCase):
    """C.1: update_title/set_authors re-lay the on-disk layout (dir rename,
    format-file renames to the new stem, emptied-parent removal), with the
    fs half deferred to the outermost commit inside a batch."""

    def _seed_layout(self, book_id, path, files):
        """Create the on-disk book directory with files and matching rows."""
        book_dir = os.path.join(self.temp_dir, *path.split("/"))
        os.makedirs(book_dir, exist_ok=True)
        rows = []
        for stem_ext, payload in files:
            with open(os.path.join(book_dir, stem_ext), "wb") as f:
                f.write(payload)
            stem, ext = stem_ext.rsplit(".", 1)
            rows.append((book_id, ext.upper(), len(payload), stem))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE books SET path = ? WHERE id = ?", (path, book_id))
            conn.executemany(
                "INSERT INTO data (book, format, uncompressed_size, name) "
                "VALUES (?,?,?,?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _dir(self, rel):
        return os.path.join(self.temp_dir, *rel.split("/"))

    def test_update_title_moves_dir_and_format_files(self):
        self._seed_layout(
            1,
            "Zed A. Writer/Old Title (1)",
            [("Old Title - Zed A. Writer.epub", b"EPUB")],
        )
        # A non-format satellite file rides the directory rename.
        with open(self._dir("Zed A. Writer/Old Title (1)/cover.jpg"), "wb") as f:
            f.write(b"COVER")
        with self._wdb() as wdb:
            wdb.update_title(1, "New Title")
        self.assertEqual(
            self._sql2("SELECT path FROM books WHERE id = 1")[0][0],
            "Zed A. Writer/New Title (1)",
        )
        self.assertEqual(
            self._sql2("SELECT name FROM data WHERE book = 1")[0][0],
            "New Title - Zed A. Writer",
        )
        self.assertFalse(os.path.exists(self._dir("Zed A. Writer/Old Title (1)")))
        new_dir = self._dir("Zed A. Writer/New Title (1)")
        self.assertTrue(os.path.isdir(new_dir))
        with open(os.path.join(new_dir, "New Title - Zed A. Writer.epub"), "rb") as f:
            self.assertEqual(f.read(), b"EPUB")
        self.assertTrue(os.path.exists(os.path.join(new_dir, "cover.jpg")))

    def test_set_authors_moves_dir_and_renames_stem(self):
        self._seed_layout(
            1,
            "Zed A. Writer/Old Title (1)",
            [("Old Title - Zed A. Writer.epub", b"EPUB")],
        )
        with self._wdb() as wdb:
            wdb.set_authors(1, ["Ann Leckie"])
        self.assertEqual(
            self._sql2("SELECT path FROM books WHERE id = 1")[0][0],
            "Ann Leckie/Old Title (1)",
        )
        self.assertEqual(
            self._sql2("SELECT name FROM data WHERE book = 1")[0][0],
            "Old Title - Ann Leckie",
        )
        self.assertTrue(
            os.path.isfile(
                self._dir("Ann Leckie/Old Title (1)/Old Title - Ann Leckie.epub")
            )
        )
        # The emptied old author directory is removed.
        self.assertFalse(os.path.exists(self._dir("Zed A. Writer")))

    def test_batch_defers_the_fs_half_until_the_commit(self):
        self._seed_layout(
            1,
            "Zed A. Writer/Old Title (1)",
            [("Old Title - Zed A. Writer.epub", b"EPUB")],
        )
        with WritableCalibreDB(self.db_path) as wdb:
            with wdb.batch():
                wdb.update_title(1, "New Title")
                # In-transaction rows already point at the new layout...
                row = wdb.conn.execute("SELECT path FROM books WHERE id = 1").fetchone()
                self.assertEqual(row["path"], "Zed A. Writer/New Title (1)")
                # ...but the files have not moved yet.
                self.assertTrue(
                    os.path.exists(self._dir("Zed A. Writer/Old Title (1)"))
                )
            # After the commit, the filesystem caught up.
            self.assertTrue(os.path.exists(self._dir("Zed A. Writer/New Title (1)")))
        self.assertEqual(
            self._sql2("SELECT path FROM books WHERE id = 1")[0][0],
            "Zed A. Writer/New Title (1)",
        )

    def test_failed_batch_drops_the_relay(self):
        self._seed_layout(
            1,
            "Zed A. Writer/Old Title (1)",
            [("Old Title - Zed A. Writer.epub", b"EPUB")],
        )
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(RuntimeError), wdb.batch():
            wdb.update_title(1, "New Title")
            raise RuntimeError("boom")
        wdb.close()
        # Rows rolled back AND the layout untouched: no half-applied state.
        self.assertEqual(
            self._sql2("SELECT path FROM books WHERE id = 1")[0][0],
            "Zed A. Writer/Old Title (1)",
        )
        self.assertTrue(os.path.exists(self._dir("Zed A. Writer/Old Title (1)")))
        self.assertFalse(os.path.exists(self._dir("Zed A. Writer/New Title (1)")))

    def test_failed_commit_leaves_rows_and_files_in_agreement(self):
        # The six-lens-audit catch: bare (non-batched) update_title/
        # set_authors applied the fs half BEFORE the commit, so a failed
        # commit diverged rows from files (remove_book's ordering was the
        # correct one). The re-lay queues now and lands only after COMMIT.
        class CommitFails:
            def __init__(self, conn):
                self._conn = conn

            def commit(self):
                raise sqlite3.OperationalError("database or disk is full")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        for setter, new_rel in (
            ("update_title", "Zed A. Writer/New Title (1)"),
            ("set_authors", "Ann Leckie/Old Title (1)"),
        ):
            with self.subTest(setter=setter):
                self._seed_layout(
                    1,
                    "Zed A. Writer/Old Title (1)",
                    [("Old Title - Zed A. Writer.epub", b"EPUB")],
                )
                wdb = WritableCalibreDB(self.db_path)
                wdb.conn = CommitFails(wdb.conn)
                with self.assertRaises(sqlite3.OperationalError):
                    if setter == "update_title":
                        wdb.update_title(1, "New Title")
                    else:
                        wdb.set_authors(1, ["Ann Leckie"])
                wdb.close()
                # The rollback reverted every row...
                self.assertEqual(
                    self._sql2("SELECT path FROM books WHERE id = 1")[0][0],
                    "Zed A. Writer/Old Title (1)",
                )
                self.assertEqual(
                    self._sql2("SELECT title FROM books WHERE id = 1")[0][0],
                    "Old Title",
                )
                # ...and the fs half never ran, so files agree with rows.
                self.assertTrue(
                    os.path.exists(self._dir("Zed A. Writer/Old Title (1)"))
                )
                self.assertFalse(os.path.exists(self._dir(new_rel)))
                self.assertEqual(wdb._pending_relayouts, [])

    def test_no_path_row_gets_the_db_only_correction(self):
        # Book 2 has no path and no directory (legacy rows): the setter
        # writes the computed path without touching the filesystem.
        with self._wdb() as wdb:
            wdb.update_title(2, "Other Title")
        self.assertEqual(
            self._sql2("SELECT path FROM books WHERE id = 2")[0][0],
            "Zed A. Writer/Other Title (2)",
        )
        self.assertFalse(os.path.exists(self._dir("Zed A. Writer/Other Title (2)")))

    def test_stale_target_directory_is_replaced(self):
        self._seed_layout(
            1,
            "Zed A. Writer/Old Title (1)",
            [("Old Title - Zed A. Writer.epub", b"EPUB")],
        )
        # A leftover from a crashed prior attempt at the target name.
        stale = self._dir("Zed A. Writer/New Title (1)")
        os.makedirs(stale)
        with open(os.path.join(stale, "junk.txt"), "w") as f:
            f.write("stale")
        with self._wdb() as wdb:
            wdb.update_title(1, "New Title")
        self.assertTrue(
            os.path.isfile(
                self._dir("Zed A. Writer/New Title (1)/New Title - Zed A. Writer.epub")
            )
        )
        self.assertFalse(
            os.path.exists(self._dir("Zed A. Writer/New Title (1)/junk.txt"))
        )

    def test_failed_batch_drops_pending_removals_too(self):
        # The 1.18 regression pin: a failed batch must clear the deferred
        # removal queue, or a LATER successful batch would trash the files
        # of rows the rollback resurrected.
        self._seed_layout(
            2,
            "Zed A. Writer/Other (2)",
            [("Other - Zed A. Writer.epub", b"EPUB")],
        )
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(RuntimeError), wdb.batch():
            wdb.remove_book(2, delete_files="trash")
            raise RuntimeError("boom")
        self.assertEqual(wdb._pending_removals, [])  # dropped with the rollback
        wdb.close()
        # A later successful batch must NOT flush the stale removal.
        with self._wdb() as wdb2, wdb2.batch():
            wdb2.add_tag(1, "Safe")
        self.assertTrue(os.path.exists(self._dir("Zed A. Writer/Other (2)")))
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books WHERE id = 2")[0][0], 1)


class TestBatchFlushFailure(unittest.TestCase):
    """The six-lens-audit corruption: a flush OSError AFTER the commit used
    to skip the batch-state reset (the resets ran after the flushes), so the
    failed pass's directories stayed registered and a LATER failed exit
    rmtreed directories of already-committed books."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
                timestamp TEXT, pubdate TEXT, series_index REAL,
                has_cover INTEGER DEFAULT 0, uuid TEXT, path TEXT,
                last_modified TEXT
            );
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER UNIQUE);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT UNIQUE);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER);
            CREATE TABLE custom_columns (
                id INTEGER PRIMARY KEY, label TEXT UNIQUE, name TEXT, datatype TEXT,
                editable BOOL DEFAULT 1, display TEXT DEFAULT '{}',
                is_multiple BOOL DEFAULT 0, normalized BOOL DEFAULT 0
            );
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,
                format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TRIGGER books_delete_trg AFTER DELETE ON books
            BEGIN
                DELETE FROM books_authors_link WHERE book = OLD.id;
                DELETE FROM books_tags_link WHERE book = OLD.id;
                DELETE FROM books_publishers_link WHERE book = OLD.id;
                DELETE FROM books_series_link WHERE book = OLD.id;
                DELETE FROM books_ratings_link WHERE book = OLD.id;
                DELETE FROM books_languages_link WHERE book = OLD.id;
                DELETE FROM data WHERE book = OLD.id;
            END;
            INSERT INTO books (id, title) VALUES (1, 'Doomed'), (2, 'Kept');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def _book_dir(self, book_id, title):
        """Give a book a real directory and matching path row."""
        path = os.path.join(self.temp_dir, f"{title} ({book_id})")
        os.makedirs(path)
        with open(os.path.join(path, "book.epub"), "w") as f:
            f.write("x")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE books SET path = ? WHERE id = ?", (f"{title} ({book_id})", book_id)
        )
        conn.commit()
        conn.close()
        return path

    def _new_book_dir(self, book_id):
        row = self._sql(f"SELECT path FROM books WHERE id = {book_id}")[0][0]
        return os.path.join(self.temp_dir, *row.split("/"))

    def test_flush_failure_resets_batch_state_before_propagating(self):
        # add_book registers its directory for the batch's compensation;
        # remove_book queues its file removal; the flush after the COMMIT
        # fails. The state must already be reset when the caller sees the
        # error, or a later failed exit would rmtree the committed book.
        wdb = WritableCalibreDB(self.db_path)
        self._book_dir(1, "Doomed")

        def disk_full(book_id, book_dir, mode):
            raise OSError("disk full")

        wdb._remove_book_dir = disk_full
        with self.assertRaises(OSError), wdb.batch():
            new_id = wdb.add_book("Added Later", [])
            wdb.remove_book(1, delete_files="permanent")
        # The COMMIT landed before the failing flush: the new row is real
        # and the doomed book's rows are gone (book 2 survives).
        self.assertEqual(
            self._sql("SELECT id FROM books"),
            [(2,), (new_id,)],
        )
        # Reset BEFORE propagating: nothing left registered for a later
        # failed exit to rmtree.
        self.assertEqual(wdb._batch_dirs, [])
        self.assertFalse(wdb._batch_poisoned)
        new_dir = self._new_book_dir(new_id)
        self.assertTrue(os.path.isdir(new_dir))
        # The doomed book's removal is still pending (the flush failed
        # before performing any of it), not silently lost.
        self.assertEqual(len(wdb._pending_removals), 1)
        # A later FAILED batch must leave the committed book's files alone
        # (and drops the stale queue with its rollback, the 1.18 rule).
        del wdb._remove_book_dir  # reveal the class's real method again
        with self.assertRaises(RuntimeError), wdb.batch():
            raise RuntimeError("boom")
        self.assertTrue(os.path.isdir(new_dir))
        self.assertEqual(wdb._pending_removals, [])
        wdb.close()

    def test_a_failed_flush_retries_cleanly(self):
        # The committed removals stay pending after a failed flush; a retry
        # must skip the directories the first pass already removed (trash
        # mode used to raise on the missing source) and finish the rest.
        wdb = WritableCalibreDB(self.db_path)
        dir1 = self._book_dir(1, "Doomed")
        dir2 = self._book_dir(2, "Kept")
        real = wdb._remove_book_dir
        calls = {"n": 0}

        def flaky(book_id, book_dir, mode):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("transient")
            real(book_id, book_dir, mode)

        wdb._remove_book_dir = flaky
        with self.assertRaises(OSError), wdb.batch():
            wdb.remove_book(1, delete_files="trash")
            wdb.remove_book(2, delete_files="trash")
        self.assertEqual(calls["n"], 2)
        del wdb._remove_book_dir  # reveal the class's real method again
        wdb._flush_pending_removals()  # the retry: must not raise
        self.assertEqual(wdb._pending_removals, [])
        self.assertFalse(os.path.exists(dir1))
        self.assertFalse(os.path.exists(dir2))
        self.assertEqual(
            sorted(e["book_id"] for e in wdb.list_trash() if e["category"] == "book"),
            [1, 2],
        )
        wdb.close()

    def test_remove_book_dir_is_idempotent(self):
        wdb = WritableCalibreDB(self.db_path)
        try:
            # A directory that is already gone: no move, no rmtree, no
            # trash entry.
            wdb._remove_book_dir(
                9, os.path.join(self.temp_dir, "Unknown", "Gone (9)"), "trash"
            )
            self.assertEqual(wdb.list_trash(), [])
        finally:
            wdb.close()


class TestFtsDirtying(unittest.TestCase):
    """C.4: format writes keep Calibre's FTS index and page counts honest
    (dirtied_formats queue + books_pages_link.needs_scan)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,
                format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE books_pages_link (book INTEGER PRIMARY KEY,
                pages INTEGER DEFAULT 0 NOT NULL, needs_scan INTEGER NOT NULL DEFAULT 0);
            """
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (1, 'One', 'One')")
        conn.execute(
            "INSERT INTO data (book, format, uncompressed_size, name) "
            "VALUES (1, 'EPUB', 10, 'One - X')"
        )
        conn.execute(
            "INSERT INTO books_pages_link (book, pages, needs_scan) VALUES (1, 100, 0)"
        )
        conn.commit()
        conn.close()
        # The sidecar (plain tables; the FTS5 machinery is irrelevant here).
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            CREATE TABLE books_text (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, timestamp REAL NOT NULL,
                format TEXT NOT NULL COLLATE NOCASE, format_hash TEXT NOT NULL,
                format_size INTEGER DEFAULT 0, searchable_text TEXT DEFAULT '',
                text_size INTEGER DEFAULT 0, text_hash TEXT DEFAULT '',
                err_msg TEXT DEFAULT '', UNIQUE(book, format));
            INSERT INTO books_text (book, timestamp, format, format_hash,
                searchable_text) VALUES (1, 1700000000.0, 'EPUB', 'h1', 'old text');
            """
        )
        fts.commit()
        fts.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _fts(self):
        conn = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        conn.row_factory = sqlite3.Row
        return conn

    def _pages(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT needs_scan FROM books_pages_link WHERE book = 1"
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def test_set_format_queues_reextraction_and_pages_scan(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_format(1, "epub", "One - X", 99))
        fts = self._fts()
        row = fts.execute("SELECT book, format FROM dirtied_formats").fetchone()
        fts.close()
        self.assertEqual((row["book"], row["format"]), (1, "EPUB"))
        self.assertEqual(self._pages(), 1)

    def test_add_format_queues_too(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.add_format(1, "MOBI", "One - X", 5))
        fts = self._fts()
        fmts = {r["format"] for r in fts.execute("SELECT format FROM dirtied_formats")}
        fts.close()
        self.assertEqual(fmts, {"MOBI"})

    def test_remove_format_clears_the_queue_but_not_books_text(self):
        # Pre-seed a queue entry as if extraction were pending.
        fts = self._fts()
        fts.execute("INSERT INTO dirtied_formats (book, format) VALUES (1, 'EPUB')")
        fts.commit()
        fts.close()
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.remove_format(1, "EPUB"))
        fts = self._fts()
        self.assertEqual(
            fts.execute("SELECT COUNT(*) FROM dirtied_formats").fetchone()[0], 0
        )
        # The tokenizer trap: the stale text row is Calibre's own to clean.
        self.assertEqual(
            fts.execute("SELECT COUNT(*) FROM books_text").fetchone()[0], 1
        )
        fts.close()

    def test_identical_set_format_queues_nothing(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertFalse(wdb.set_format(1, "EPUB", "One - X", 10))
        fts = self._fts()
        self.assertEqual(
            fts.execute("SELECT COUNT(*) FROM dirtied_formats").fetchone()[0], 0
        )
        fts.close()
        self.assertEqual(self._pages(), 0)

    def test_failed_batch_rolls_the_sidecar_back(self):
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(RuntimeError), wdb.batch():
            wdb.set_format(1, "EPUB", "Repaired - X", 123)
            raise RuntimeError("boom")
        wdb.close()
        fts = self._fts()
        self.assertEqual(
            fts.execute("SELECT COUNT(*) FROM dirtied_formats").fetchone()[0], 0
        )
        fts.close()
        self.assertEqual(self._pages(), 0)

    def test_no_sidecar_degrades_to_noop(self):
        os.remove(os.path.join(self.temp_dir, "full-text-search.db"))
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_format(1, "EPUB", "One - X", 42))
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM data WHERE book = 1").fetchone()[0],
                1,
            )
        finally:
            conn.close()


class TestEntityRename(_WriteSideFixture, unittest.TestCase):
    """C.2: rename_entity / remove_entity_everywhere (1.19), against the
    trigger-hazard schema (the insert/update triggers call title_sort and
    uuid4, so setUp must register the UDFs before seeding)."""

    SCHEMA = _ADD_BOOK_SCHEMA

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)
        conn.executescript(self.SCHEMA)
        conn.execute(
            "INSERT INTO books (id, title, sort, author_sort) "
            "VALUES (1, 'Old Title', 'Old Title', 'Writer, Zed A.')"
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (2, 'Other', 'Other')")
        conn.execute(
            "INSERT INTO authors VALUES (1, 'Zed A. Writer', 'Writer, Zed A.', '')"
        )
        conn.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, 1)", [(1,), (2,)]
        )
        conn.commit()
        conn.close()

    def _exec(self, sql, params=()):
        # Seed helper: commit + trigger UDFs (series_insert_trg calls
        # title_sort), unlike the read-only _sql2.
        conn = sqlite3.connect(self.db_path)
        try:
            register_udfs(conn)
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _link(self, book, table, col, item_id):
        self._exec(
            f"INSERT INTO books_{table}_link (book, {col}) VALUES (?, ?)",
            (book, item_id),
        )

    def _dirtied(self):
        return sorted(r[0] for r in self._sql2("SELECT book FROM metadata_dirtied"))

    def test_rename_author_in_place_recomputes_and_relays(self):
        self._exec(
            "INSERT INTO authors (id, name, sort) VALUES (2, 'Ann Leckie', 'Leckie, Ann')"
        )
        self._exec("INSERT INTO books_authors_link (book, author) VALUES (1, 2)")
        self._exec("UPDATE books SET path = 'Zed A. Writer/Old Title (1)' WHERE id = 1")
        book_dir = os.path.join(self.temp_dir, "Zed A. Writer", "Old Title (1)")
        os.makedirs(book_dir)
        with open(os.path.join(book_dir, "cover.jpg"), "wb") as f:
            f.write(b"C")
        with self._wdb() as wdb:
            n = wdb.rename_entity("authors", "Zed A. Writer", "Zed A. Writer Junior")
        self.assertEqual(n, 2)  # both seeded books carry the author
        self.assertEqual(
            self._sql2("SELECT name, sort FROM authors WHERE id = 1"),
            [("Zed A. Writer Junior", "Writer, Zed A.")],
        )
        # author_sort recomputed from the surviving per-author sort keys.
        self.assertEqual(
            self._sql2("SELECT author_sort FROM books WHERE id = 1")[0][0],
            "Writer, Zed A. & Leckie, Ann",
        )
        self.assertEqual(self._dirtied(), [1, 2])
        # The on-disk layout re-laid to the new author name.
        self.assertFalse(os.path.exists(book_dir))
        self.assertTrue(
            os.path.isdir(
                os.path.join(self.temp_dir, "Zed A. Writer Junior", "Old Title (1)")
            )
        )

    def test_rename_author_case_merge_drops_duplicate_links(self):
        self._exec(
            "INSERT INTO authors (id, name, sort) VALUES (2, 'zed a. writer', '')"
        )
        self._link(2, "authors", "author", 2)  # book 2 carries BOTH spellings
        with self._wdb() as wdb:
            n = wdb.rename_entity("authors", "zed a. writer", "Zed A. Writer")
        self.assertEqual(n, 1)  # book 2 changed: its duplicate-spelling
        # link (the one that would collide with the survivor's UNIQUE) was
        # dropped, leaving the book linked to the single surviving row.
        self.assertEqual(
            self._sql2("SELECT COUNT(*) FROM authors WHERE id = 2")[0][0], 0
        )
        self.assertEqual(
            self._sql2("SELECT author FROM books_authors_link WHERE book = 2"),
            [(1,)],
        )

    def test_rename_series_merge_renumbers_incoming_books(self):
        self._exec(
            "INSERT INTO series (id, name) VALUES (1, 'Dune Saga'), (2, 'dune saga')"
        )
        self._exec("UPDATE books SET series_index = 1 WHERE id = 1")
        self._exec("UPDATE books SET series_index = 5 WHERE id = 2")
        self._link(1, "series", "series", 1)
        self._link(2, "series", "series", 2)
        with self._wdb() as wdb:
            n = wdb.rename_entity("series", "dune saga", "Dune Saga")
        self.assertEqual(n, 1)  # book 2 moved onto the survivor
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM series")[0][0], 1)
        # Incoming book renumbers to max + 1 over the survivor's books.
        self.assertEqual(
            self._sql2("SELECT series_index FROM books WHERE id = 2")[0][0], 2.0
        )
        self.assertEqual(self._dirtied(), [2])

    def test_remove_entity_everywhere_author_relays_and_recomputes(self):
        self._exec("UPDATE books SET path = 'Zed A. Writer/Old Title (1)' WHERE id = 1")
        book_dir = os.path.join(self.temp_dir, "Zed A. Writer", "Old Title (1)")
        os.makedirs(book_dir)
        with self._wdb() as wdb:
            n = wdb.remove_entity_everywhere("authors", "Zed A. Writer")
        self.assertEqual(n, 2)
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM authors")[0][0], 0)
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_authors_link")[0][0], 0)
        self.assertEqual(
            self._sql2("SELECT author_sort FROM books WHERE id = 1")[0][0], ""
        )
        self.assertEqual(self._dirtied(), [1, 2])
        # Authorless path component falls back to Unknown.
        self.assertTrue(
            os.path.isdir(os.path.join(self.temp_dir, "Unknown", "Old Title (1)"))
        )

    def test_remove_entity_everywhere_series_resets_indices(self):
        self._exec("INSERT INTO series (id, name) VALUES (1, 'Dune Saga')")
        self._exec("UPDATE books SET series_index = 3 WHERE id = 1")
        self._link(1, "series", "series", 1)
        self._link(2, "series", "series", 1)
        with self._wdb() as wdb:
            n = wdb.remove_entity_everywhere("series", "Dune Saga")
        self.assertEqual(n, 2)
        self.assertEqual(
            self._sql2("SELECT series_index FROM books WHERE id IN (1, 2)"),
            [(1.0,), (1.0,)],
        )
        self.assertEqual(self._dirtied(), [1, 2])

    def test_honest_noops_and_validation(self):
        with self._wdb() as wdb:
            self.assertEqual(
                wdb.rename_entity("authors", "Zed A. Writer", "Zed A. Writer"), 0
            )
            self.assertEqual(wdb.remove_entity_everywhere("tags", "Nobody"), 0)
            with self.assertRaises(ValueError):
                wdb.rename_entity("ratings", "a", "b")  # not a name table
            with self.assertRaises(ValueError):
                wdb.rename_entity("authors", "Missing", "Whatever")
            with self.assertRaises(ValueError):
                wdb.remove_entity_everywhere("authors", "  ")


class TestCoverManagement(_WriteSideFixture, unittest.TestCase):
    """C.3: set_cover / remove_cover (1.19)."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE books SET path = ? WHERE id = 1",
                ("Zed A. Writer/Old Title (1)",),
            )
            conn.commit()
        finally:
            conn.close()
        self.book_dir = os.path.join(self.temp_dir, "Zed A. Writer", "Old Title (1)")
        os.makedirs(self.book_dir)

    def test_set_cover_jpeg_sniffs_and_places(self):
        # A real minimal JPEG header: sniff_image_format must see JPEG.
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_cover(1, jpeg))
        self.assertEqual(
            self._sql2("SELECT has_cover FROM books WHERE id = 1")[0][0], 1
        )
        self.assertTrue(os.path.isfile(os.path.join(self.book_dir, "cover.jpg")))
        with open(os.path.join(self.book_dir, "cover.jpg"), "rb") as f:
            self.assertEqual(f.read(), jpeg)

    def test_set_cover_png_replaces_stale_jpeg(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
        with open(os.path.join(self.book_dir, "cover.jpg"), "wb") as f:
            f.write(b"STALE")
        with self._wdb() as wdb:
            wdb.set_cover(1, png)
        self.assertFalse(os.path.exists(os.path.join(self.book_dir, "cover.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.book_dir, "cover.png")))

    def test_set_cover_rejects_unparseable_data(self):
        with self._wdb() as wdb, self.assertRaises(ValueError):
            wdb.set_cover(1, b"not an image")
        self.assertEqual(
            self._sql2("SELECT has_cover FROM books WHERE id = 1")[0][0], 0
        )

    def test_failed_batch_leaves_no_cover_file(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(RuntimeError), wdb.batch():
            wdb.set_cover(1, jpeg)
            raise RuntimeError("boom")
        wdb.close()
        self.assertEqual(
            self._sql2("SELECT has_cover FROM books WHERE id = 1")[0][0], 0
        )
        self.assertFalse(os.path.exists(os.path.join(self.book_dir, "cover.jpg")))

    def test_remove_cover_clears_flag_and_files(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
        with self._wdb() as wdb:
            wdb.set_cover(1, jpeg)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE books SET has_cover = 0 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        # A stray file with the flag already off still gets swept.
        with self._wdb() as wdb:
            changed = wdb.remove_cover(1)
        self.assertFalse(changed)
        self.assertFalse(os.path.exists(os.path.join(self.book_dir, "cover.jpg")))

        with self._wdb() as wdb:
            wdb.set_cover(1, jpeg)
            self.assertTrue(wdb.remove_cover(1))
        self.assertEqual(
            self._sql2("SELECT has_cover FROM books WHERE id = 1")[0][0], 0
        )
        self.assertFalse(os.path.exists(os.path.join(self.book_dir, "cover.jpg")))
        # The removal queued OPF resync for the book.
        self.assertEqual(
            self._sql2("SELECT book FROM metadata_dirtied WHERE book = 1")[0][0], 1
        )


class TestPassthroughSetters(_WriteSideFixture, unittest.TestCase):
    """C.6: set_author_sort / set_title_sort / set_timestamp (1.19)."""

    def test_author_sort_override_sticks_until_recomputed(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_author_sort(1, "Writer, Zed A. B."))
            self.assertFalse(wdb.set_author_sort(1, "Writer, Zed A. B."))
        self.assertEqual(
            self._sql2("SELECT author_sort FROM books WHERE id = 1")[0][0],
            "Writer, Zed A. B.",
        )
        # set_authors recomputes OVER the override -- documented interplay.
        with self._wdb() as wdb:
            wdb.set_authors(1, ["Zed A. Writer"])
        self.assertEqual(
            self._sql2("SELECT author_sort FROM books WHERE id = 1")[0][0],
            "Writer, Zed A.",
        )

    def test_title_sort_override_does_not_recompute(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_title_sort(1, "Custom Sort, A"))
        self.assertEqual(
            self._sql2("SELECT sort FROM books WHERE id = 1")[0][0],
            "Custom Sort, A",
        )

    def test_empty_sort_values_raise(self):
        with self._wdb() as wdb:
            with self.assertRaises(ValueError):
                wdb.set_author_sort(1, "   ")
            with self.assertRaises(ValueError):
                wdb.set_title_sort(1, "")

    def test_timestamp_normalizes_like_pubdate(self):
        with self._wdb() as wdb:
            self.assertTrue(wdb.set_timestamp(1, "2024-03-05"))
            self.assertFalse(wdb.set_timestamp(1, "2024-03-05T00:00:00+00:00"))
            self.assertTrue(wdb.set_timestamp(1, None))
        value = self._sql2("SELECT timestamp FROM books WHERE id = 1")[0][0]
        self.assertTrue(value.startswith("0101-01-01"))
        with self._wdb() as wdb:
            wdb.set_timestamp(1, None)
        self.assertEqual(
            self._sql2("SELECT book FROM metadata_dirtied WHERE book = 1")[0][0], 1
        )


class TestOriginalFormat(unittest.TestCase):
    """C.8: save_original_format / restore_original_format (1.19)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,
                format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE books_pages_link (book INTEGER PRIMARY KEY,
                pages INTEGER DEFAULT 0 NOT NULL, needs_scan INTEGER NOT NULL DEFAULT 0);
            """
        )
        conn.execute("INSERT INTO books (id, title, sort) VALUES (1, 'One', 'One')")
        conn.execute("INSERT INTO books_pages_link (book, pages) VALUES (1, 10)")
        conn.commit()
        conn.close()
        self.book_dir = os.path.join(self.temp_dir, "Zed A. Writer", "One (1)")
        os.makedirs(self.book_dir)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE books SET path = ? WHERE id = 1",
                ("Zed A. Writer/One (1)",),
            )
            conn.executemany(
                "INSERT INTO data (book, format, uncompressed_size, name) VALUES (?,?,?,?)",
                [(1, "EPUB", 11, "One - Zed A. Writer")],
            )
            conn.commit()
        finally:
            conn.close()
        self.write_file("One - Zed A. Writer.epub", b"ORIGINAL-BYTES")
        # The sidecar, so the FTS queue assertions are real.
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            CREATE TABLE books_text (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL, err_msg TEXT DEFAULT '');
            """
        )
        fts.commit()
        fts.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def write_file(self, name, payload):
        with open(os.path.join(self.book_dir, name), "wb") as f:
            f.write(payload)

    def read_file(self, name):
        with open(os.path.join(self.book_dir, name), "rb") as f:
            return f.read()

    def fts_fmts(self):
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        try:
            return {r[0] for r in fts.execute("SELECT format FROM dirtied_formats")}
        finally:
            fts.close()

    def test_save_copies_bytes_to_an_original_row_and_file(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.save_original_format(1, "epub"))
        self.assertEqual(
            self._data(),
            [
                ("EPUB", 11, "One - Zed A. Writer"),
                ("ORIGINAL_EPUB", 14, "One - Zed A. Writer"),
            ],
        )
        self.assertEqual(self.read_file("One - Zed A. Writer.epub"), b"ORIGINAL-BYTES")
        self.assertEqual(
            self.read_file("One - Zed A. Writer.original_epub"), b"ORIGINAL-BYTES"
        )
        # Mirrors upstream's trigger-on-any-insert: the original is queued
        # for extraction like any added format.
        self.assertIn("ORIGINAL_EPUB", self.fts_fmts())

    def test_save_misses_are_false_and_originals_are_rejected(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertFalse(wdb.save_original_format(1, "MOBI"))
            with self.assertRaises(ValueError):
                wdb.save_original_format(1, "ORIGINAL_EPUB")
            with self.assertRaises(ValueError):
                wdb.save_original_format(1, " ")

    def test_restore_swaps_bytes_back_and_removes_the_original(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.save_original_format(1, "EPUB"))
        # The repair swaps the EPUB file and re-catalogues it.
        self.write_file("One - Zed A. Writer.epub", b"REPAIRED-BYTES")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE data SET uncompressed_size = 14 WHERE book = 1 AND format = 'EPUB'"
            )
            conn.commit()
        finally:
            conn.close()
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.restore_original_format(1, "ORIGINAL_EPUB"))
        # The original bytes are back under EPUB.
        self.assertEqual(self.read_file("One - Zed A. Writer.epub"), b"ORIGINAL-BYTES")
        self.assertEqual(self._data(), [("EPUB", 14, "One - Zed A. Writer")])
        self.assertFalse(
            os.path.exists(
                os.path.join(self.book_dir, "One - Zed A. Writer.original_epub")
            )
        )
        # The restored format is queued for FTS re-extraction; the
        # removed original's queue entry is gone.
        self.assertIn("EPUB", self.fts_fmts())
        self.assertNotIn("ORIGINAL_EPUB", self.fts_fmts())

    def test_restore_without_an_original_is_false(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertFalse(wdb.restore_original_format(1, "ORIGINAL_EPUB"))
            with self.assertRaises(ValueError):
                wdb.restore_original_format(1, "EPUB")

    def test_save_first_on_a_fresh_connection_keeps_later_verbs_queueing(self):
        # The six-lens-audit HIGH: save_original_format opened its batch
        # without the sidecar attach, leaving the nested add_format/set_format
        # to attach INSIDE the transaction; on SQLite builds that forbid that
        # (pre-3.21.0) the failure was caught and _fts_state=False was cached
        # for the whole connection, silently killing queueing for every later
        # format verb. The save must attach up front so the connection keeps
        # queueing.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.save_original_format(1, "EPUB"))
            self.assertTrue(wdb.set_format(1, "EPUB", "One - Zed A. Writer", 99))
        self.assertEqual({"ORIGINAL_EPUB", "EPUB"}, self.fts_fmts())

    def test_save_queues_even_when_attach_inside_a_transaction_raises(self):
        # The audit's poison trigger, simulated: an SQLite build with the
        # pre-3.21.0 rule (in-transaction ATTACH raises). The queue must
        # still fill, because the attach already happened before the batch.
        class NoAttachInTransaction:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith("ATTACH") and (
                    self._conn.in_transaction
                ):
                    raise sqlite3.OperationalError(
                        "cannot ATTACH database within transaction"
                    )
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with WritableCalibreDB(self.db_path) as wdb:
            wdb.conn = NoAttachInTransaction(wdb.conn)
            self.assertTrue(wdb.save_original_format(1, "EPUB"))
        self.assertIn("ORIGINAL_EPUB", self.fts_fmts())

    def _data(self):
        return sorted(
            self._sql_rows("SELECT format, uncompressed_size, name FROM data")
        )

    def _sql_rows(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()


class TestTrashLifecycle(unittest.TestCase):
    """C.5: list_trash / empty_trash / expire_trash (1.20)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()
        self.trash = os.path.join(self.temp_dir, ".caltrash")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _entry(self, category, book_id, files=("x.epub",), age_seconds=0.0):
        path = os.path.join(self.trash, category, str(book_id))
        os.makedirs(path, exist_ok=True)
        for name in files:
            with open(os.path.join(path, name), "wb") as f:
                f.write(b"D")
        old = time.time() - age_seconds
        os.utime(path, (old, old))

    def _wdb(self):
        return WritableCalibreDB(self.db_path)

    def test_empty_trash_removes_everything_and_recreates(self):
        self._entry("b", 3)
        self._entry("b", 4, files=("a.epub", "cover.jpg"))
        self._entry("f", 2, files=("EPUB",))
        with self._wdb() as wdb:
            n = wdb.empty_trash()
        self.assertEqual(n, 3)
        self.assertEqual(wdb.list_trash(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.trash, "b")))
        self.assertTrue(os.path.isdir(os.path.join(self.trash, "f")))

    def test_list_trash_inventories_both_categories(self):
        self._entry("b", 7, files=("One - X.epub", "cover.jpg"))
        self._entry("f", 3, files=("EPUB", "MOBI"))
        with self._wdb() as wdb:
            entries = wdb.list_trash()
        by = {(e["category"], e["book_id"]): e for e in entries}
        self.assertEqual(by[("book", 7)]["files"], ["One - X.epub", "cover.jpg"])
        self.assertEqual(by[("format", 3)]["files"], ["EPUB", "MOBI"])

    def test_expire_trash_honors_age(self):
        self._entry("b", 1, age_seconds=40 * 86400)  # older than 14 days
        self._entry("b", 2, age_seconds=1 * 86400)  # fresh enough
        with self._wdb() as wdb:
            n = wdb.expire_trash()  # upstream's 14-day default
        self.assertEqual(n, 1)
        self.assertEqual([e["book_id"] for e in wdb.list_trash()], [2])

    def test_expire_trash_timedelta_and_zero_meaning(self):
        self._entry("b", 1, age_seconds=3600)
        with self._wdb() as wdb:
            n = wdb.expire_trash(timedelta(days=30))
        self.assertEqual(n, 0)  # an hour old is far under 30 days
        n = wdb.expire_trash(0)  # <= 0 expires everything, like upstream
        self.assertEqual(n, 1)

    def test_expire_trash_counts_only_actual_removals(self):
        # The six-lens-audit LOW: an entry whose removal fails (here: a
        # read-only entry directory, unwritable for the runner user) used
        # to be counted as removed.
        self._entry("b", 1, age_seconds=40 * 86400)
        self._entry("b", 2, age_seconds=40 * 86400)
        stuck = os.path.join(self.trash, "b", "2")
        os.chmod(stuck, 0o500)  # no write bit: rmtree cannot unlink inside
        try:
            with self._wdb() as wdb:
                self.assertEqual(wdb.expire_trash(), 1)
                self.assertEqual([e["book_id"] for e in wdb.list_trash()], [2])
        finally:
            os.chmod(stuck, 0o700)  # let tearDown's rmtree work

    def test_missing_trash_is_an_honest_zero(self):
        with self._wdb() as wdb:
            self.assertEqual(wdb.empty_trash(), 0)
            self.assertEqual(wdb.expire_trash(), 0)
            self.assertEqual(wdb.list_trash(), [])


class TestCustomColumnSchema(unittest.TestCase):
    """C.7: create_custom_column / delete_custom_column (1.20).

    The acceptance is the full round trip: create, write through
    set_custom_column, read through a fresh CalibreDB (load/field/search),
    then flag-delete and confirm the faithful not-yet-purged state."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)
        conn.executescript(_ADD_BOOK_SCHEMA)
        # The real schema's custom_columns carries mark_for_delete (the
        # delete verb records it); the shared fixture omits it.
        conn.execute(
            "ALTER TABLE custom_columns ADD COLUMN mark_for_delete INTEGER DEFAULT 0"
        )
        conn.execute("INSERT INTO books (id, title) VALUES (1, 'One')")
        conn.execute("INSERT INTO books (id, title) VALUES (2, 'Two')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql_rows(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def test_create_text_multiple_round_trip(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column(
                "audience", "Audience", "text", is_multiple=True
            )
            self.assertGreaterEqual(num, 1)
            wdb.set_custom_column(1, "#audience", ["Fantasy", "Young Adult"])
        # Pattern A storage exists: value table + link table + triggers.
        self.assertEqual(
            self._sql_rows(f"SELECT value FROM custom_column_{num}"),
            [("Fantasy",), ("Young Adult",)],
        )
        self.assertGreaterEqual(
            self._sql_rows(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                f" AND name LIKE 'fkc_%books_custom_column_{num}_link'"
            )[0][0],
            2,
        )
        # A fresh reader sees the column and its values end to end.
        with CalibreDB(self.db_path) as db:
            self.assertEqual(
                db.load_custom_column("#audience"), {1: ["Fantasy", "Young Adult"]}
            )
            self.assertEqual(db.search("#audience:Fantasy"), {1})

    def test_create_series_column_carries_the_extra_index(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column("saga", "Saga", "series")
            wdb.set_custom_column(1, "#saga", "The Saga")
            wdb.conn.execute(
                f"UPDATE books_custom_column_{num}_link SET extra = 2.5 WHERE book = 1"
            )
        with CalibreDB(self.db_path) as db:
            # A.3's derived index location works on created columns too.
            self.assertEqual(db.field(1, "#saga_index"), 2.5)

    def test_create_bool_direct_storage(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column("flagged", "Flagged", "bool")
            self.assertTrue(wdb.set_custom_column(1, "#flagged", True))
            # Direct storage upserts: the second write flips in place.
            self.assertTrue(wdb.set_custom_column(1, "#flagged", False))
        self.assertEqual(
            self._sql_rows(f"SELECT book, value FROM custom_column_{num}"),
            [(1, 0)],
        )

    def test_create_validation(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.create_custom_column("2bad", "X", "text")
            with self.assertRaises(ValueError):
                wdb.create_custom_column("Bad Upper", "X", "text")
            with self.assertRaises(ValueError):
                wdb.create_custom_column("ok", "X", "notatype")
            wdb.create_custom_column("ok", "X", "text")
            with self.assertRaises(ValueError):
                wdb.create_custom_column("ok", "Y", "text")  # duplicate label

    def test_composite_creates_storage_like_upstream_but_writes_raise(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column(
                "calc",
                "Calc",
                "composite",
                display={"composite_template": "{title}"},
            )
            self.assertGreaterEqual(num, 1)
            self.assertEqual(
                self._sql_rows(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    f" AND name='custom_column_{num}'"
                )[0][0],
                1,
            )
            with self.assertRaises(ValueError):
                wdb.set_custom_column(1, "#calc", "x")  # no storage writes

    def test_delete_flags_but_never_drops(self):
        with WritableCalibreDB(self.db_path) as wdb:
            num = wdb.create_custom_column("gone", "Gone", "text")
            self.assertTrue(wdb.delete_custom_column("#gone"))
            with self.assertRaises(ValueError):
                wdb.delete_custom_column("#nope")
        self.assertEqual(
            self._sql_rows("SELECT mark_for_delete FROM custom_columns")[0][0], 1
        )
        # Faithful to the reader: the column still works until Calibre
        # performs the physical purge at its next startup.
        tables = self._sql_rows(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
            f" AND name IN ('custom_column_{num}',"
            f" 'books_custom_column_{num}_link')"
        )[0][0]
        self.assertEqual(tables, 2)

    def test_delete_on_schema_without_the_flag_raises(self):
        # A schema predating mark_for_delete cannot record the deletion.
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.create_custom_column("gone", "Gone", "text")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE cc_old AS SELECT id, label, name, datatype,"
                " is_multiple FROM custom_columns"
            )
            conn.execute("DROP TABLE custom_columns")
            conn.execute("ALTER TABLE cc_old RENAME TO custom_columns")
            conn.commit()
        finally:
            conn.close()
        wdb = WritableCalibreDB(self.db_path)
        with self.assertRaises(ValueError):
            wdb.delete_custom_column("#gone")
        wdb.close()


class TestSetFormatSizeCast(unittest.TestCase):
    """set_format stores sizes as integers, mirroring add_format (L2.2):
    a float size used to land a REAL in uncompressed_size."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,
                format TEXT, uncompressed_size INTEGER, name TEXT);
            """
        )
        conn.execute("INSERT INTO books (id, title) VALUES (1, 'One')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_float_size_stores_an_integer_row(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.add_format(1, "EPUB", "stem", 1234))
            self.assertTrue(wdb.set_format(1, "EPUB", "stem2", 2048.0))
            row = wdb.conn.execute(
                "SELECT uncompressed_size, typeof(uncompressed_size) FROM data"
            ).fetchone()
        self.assertEqual(row[0], 2048)
        self.assertEqual(row[1], "integer")


class TestCustomColumnBoolVocabulary(unittest.TestCase):
    """The writer accepts the engine's exact tristate vocabulary (L2.3):
    `_blank` reads as false in search but used to raise on write."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT,
                name TEXT, datatype TEXT, is_multiple INTEGER,
                editable INTEGER, display TEXT);
            CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY,
                book INTEGER, value INTEGER, UNIQUE(book));
            """
        )
        conn.execute("INSERT INTO books (id, title) VALUES (1, 'One')")
        conn.execute(
            "INSERT INTO custom_columns VALUES (2, 'flag', 'Flag', 'bool', 0, 1, '{}')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_every_engine_word_round_trips(self):
        from cquarry.search import BOOL_FALSE_WORDS, BOOL_TRUE_WORDS

        with WritableCalibreDB(self.db_path) as wdb:

            def stored():
                return wdb.conn.execute(
                    "SELECT value FROM custom_column_2 WHERE book = 1"
                ).fetchone()[0]

            for word in sorted(BOOL_TRUE_WORDS):
                # Flip first: every word must land as a real write, not an
                # equal-value honest no-op (the writer returns False then).
                wdb.set_custom_column(1, "flag", "false")
                self.assertTrue(wdb.set_custom_column(1, "flag", word), word)
                self.assertEqual(stored(), 1, word)
            for word in sorted(BOOL_FALSE_WORDS):
                wdb.set_custom_column(1, "flag", "true")
                self.assertTrue(wdb.set_custom_column(1, "flag", word), word)
                self.assertEqual(stored(), 0, word)

    def test_unknown_word_still_raises(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertRaises(ValueError, wdb.set_custom_column, 1, "flag", "maybe")


class TestCustomColumnMetaOldSchema(unittest.TestCase):
    """_custom_column_meta raises the house ValueError on schemas predating
    the editable/display columns (L2.6), not a raw OperationalError."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT,
                name TEXT, datatype TEXT, is_multiple INTEGER);
            INSERT INTO custom_columns VALUES (2, 'flag', 'Flag', 'bool', 0);
            """
        )
        conn.execute("INSERT INTO books (id, title) VALUES (1, 'One')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_write_raises_house_valueerror(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError) as cm:
                wdb.set_custom_column(1, "flag", "true")
            self.assertIn("predates", str(cm.exception))


class TestTrashNeverMaterializes(unittest.TestCase):
    """L2 polish: empty/expire on a library with no trash must not
    materialize a .caltrash tree."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()
        self.trash = os.path.join(self.temp_dir, ".caltrash")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_empty_operations_never_materialize_trash(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.empty_trash(), 0)
            self.assertEqual(wdb.expire_trash(), 0)
        self.assertFalse(os.path.isdir(self.trash))


class TestUuid4FreshPerCall(unittest.TestCase):
    """uuid4 is registered non-deterministic on purpose (L2.4): a
    deterministic SQL function may return a reused value within one
    statement, and a UUID must be fresh on every trigger call."""

    def test_two_inserts_get_distinct_values(self):
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "metadata.db")
        _make_simple_db(db_path)
        try:
            with WritableCalibreDB(db_path) as wdb:
                wdb.conn.execute("INSERT INTO books (title) VALUES ('A')")
                wdb.conn.execute("INSERT INTO books (title) VALUES ('B')")
                wdb.conn.commit()
                vals = [
                    r[0] for r in wdb.conn.execute("SELECT last_modified FROM books")
                ]
            self.assertEqual(len(set(vals)), 2)
        finally:
            shutil.rmtree(temp_dir)


class TestSetSeriesIndex(unittest.TestCase):
    """set_series_index: the bare index correction (1.23, the approved
    write API). Updates books.series_index in place; the link row stays."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT, series_index REAL, path TEXT, has_cover INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY,
                book INTEGER, series INTEGER, UNIQUE(book));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY,
                book INTEGER, rating INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY,
                book INTEGER, publisher INTEGER);
            INSERT INTO books (id, title, series_index) VALUES (1, 'One', 3.0);
            INSERT INTO books (id, title) VALUES (2, 'No Series');
            INSERT INTO series (id, name) VALUES (1, 'Wing');
            INSERT INTO books_series_link (book, series) VALUES (1, 1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_updates_the_index_in_place(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_series_index(1, 7.5))
            self.assertEqual(
                wdb.conn.execute(
                    "SELECT series_index FROM books WHERE id = 1"
                ).fetchone()[0],
                7.5,
            )
            # The link row survives: no delete-and-reinsert.
            self.assertIsNotNone(
                wdb.conn.execute(
                    "SELECT 1 FROM books_series_link WHERE book = 1"
                ).fetchone()
            )
            self.assertEqual(
                [r[0] for r in wdb.conn.execute("SELECT book FROM metadata_dirtied")],
                [1],
            )

    def test_equal_value_is_an_honest_noop(self):
        with WritableCalibreDB(self.db_path) as wdb:
            stamp = wdb.conn.execute(
                "SELECT last_modified FROM books WHERE id = 1"
            ).fetchone()[0]
            self.assertFalse(wdb.set_series_index(1, 3.0))
            self.assertEqual(
                wdb.conn.execute(
                    "SELECT last_modified FROM books WHERE id = 1"
                ).fetchone()[0],
                stamp,
            )
            self.assertEqual(
                wdb.conn.execute("SELECT COUNT(*) FROM metadata_dirtied").fetchone()[0],
                0,
            )

    def test_requires_a_series(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError) as cm:
                wdb.set_series_index(2, 1.0)
            self.assertIn("no series", str(cm.exception))

    def test_none_raises(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertRaises(ValueError, wdb.set_series_index, 1, None)

    def test_composes_inside_a_batch(self):
        with (
            self.assertRaises(RuntimeError),
            WritableCalibreDB(self.db_path) as wdb,
            wdb.batch(),
        ):
            wdb.set_series_index(1, 9.0)
            raise RuntimeError("roll it back")
        with sqlite3.connect(self.db_path) as check:
            self.assertEqual(
                check.execute("SELECT series_index FROM books WHERE id = 1").fetchone()[
                    0
                ],
                3.0,
            )


class TestSeriesClearAgainstNotNullSchema(unittest.TestCase):
    """set_series(book, None) / remove_entity_everywhere("series", ...)
    against the REAL library schema: books.series_index is declared
    ``REAL NOT NULL DEFAULT 1.0`` there, so writing NULL raised
    IntegrityError (the 2026-09-16 math/classics phase 3 field find).
    Calibre's no-series state is index 1.0 with no link row."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT, series_index REAL NOT NULL DEFAULT 1.0, path TEXT, has_cover INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY,
                book INTEGER, series INTEGER, UNIQUE(book));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY,
                book INTEGER, rating INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY,
                book INTEGER, publisher INTEGER);
            INSERT INTO books (id, title, series_index) VALUES (1, 'One', 4.0);
            INSERT INTO books (id, title, series_index) VALUES (2, 'Two', 2.0);
            INSERT INTO series (id, name) VALUES (1, 'Wing');
            INSERT INTO books_series_link (book, series) VALUES (1, 1), (2, 1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_set_series_none_survives_not_null_index(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_series(1, None))
            self.assertEqual(
                wdb.conn.execute(
                    "SELECT series_index FROM books WHERE id = 1"
                ).fetchone()[0],
                1.0,
            )
            self.assertIsNone(
                wdb.conn.execute(
                    "SELECT 1 FROM books_series_link WHERE book = 1"
                ).fetchone()
            )
            self.assertEqual(
                [r[0] for r in wdb.conn.execute("SELECT book FROM metadata_dirtied")],
                [1],
            )

    def test_remove_series_everywhere_survives_not_null_index(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(wdb.remove_entity_everywhere("series", "Wing"), 2)
            self.assertEqual(
                [
                    r[0]
                    for r in wdb.conn.execute(
                        "SELECT series_index FROM books ORDER BY id"
                    )
                ],
                [1.0, 1.0],
            )
            self.assertEqual(
                wdb.conn.execute("SELECT COUNT(*) FROM series").fetchone()[0], 0
            )

    def test_clear_composes_inside_a_batch(self):
        with WritableCalibreDB(self.db_path) as wdb, wdb.batch():
            wdb.set_series(1, None)
            wdb.set_series(2, None)
        with sqlite3.connect(self.db_path) as check:
            self.assertEqual(
                [
                    r[0]
                    for r in check.execute("SELECT series_index FROM books ORDER BY id")
                ],
                [1.0, 1.0],
            )
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM books_series_link").fetchone()[0],
                0,
            )


class TestRestoreFromTrash(unittest.TestCase):
    """The Phase 14 restore-from-trash verbs (1.24): a trashed book or
    format is not a dead end. The book half rebuilds the row from the
    sidecar metadata.opf Calibre keeps in every book directory."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)  # the seed INSERT fires books_insert_trg
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
                timestamp TEXT, pubdate TEXT, series_index REAL,
                has_cover INTEGER DEFAULT 0, uuid TEXT, path TEXT,
                last_modified TEXT
            );
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER UNIQUE);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT UNIQUE);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE custom_columns (
                id INTEGER PRIMARY KEY, label TEXT UNIQUE, name TEXT, datatype TEXT,
                editable BOOL DEFAULT 1, display TEXT DEFAULT '{}',
                is_multiple BOOL DEFAULT 0, normalized BOOL DEFAULT 0
            );
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
            CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT, UNIQUE(book));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
            CREATE TRIGGER books_insert_trg AFTER INSERT ON books
            BEGIN
                UPDATE books SET sort = title_sort(NEW.title),
                    uuid = uuid4() WHERE id = NEW.id;
            END;
            CREATE TRIGGER books_delete_trg AFTER DELETE ON books
            BEGIN
                DELETE FROM books_authors_link WHERE book = OLD.id;
                DELETE FROM books_tags_link WHERE book = OLD.id;
                DELETE FROM books_publishers_link WHERE book = OLD.id;
                DELETE FROM books_series_link WHERE book = OLD.id;
                DELETE FROM books_ratings_link WHERE book = OLD.id;
                DELETE FROM books_languages_link WHERE book = OLD.id;
                DELETE FROM data WHERE book = OLD.id;
            END;
            INSERT INTO books (id, title, path) VALUES (1, 'Keeper', 'Keeper, A (1)');
            INSERT INTO authors VALUES (1, 'Alive Author', 'Author, Alive');
            INSERT INTO books_authors_link (book, author) VALUES (1, 1);
            """
        )
        conn.commit()
        conn.close()
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        fts.executescript(
            """
            CREATE TABLE dirtied_formats (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                in_progress INTEGER NOT NULL DEFAULT FALSE, UNIQUE(book, format));
            """
        )
        fts.commit()
        fts.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def _write_opf(self, book_dir, title="The Doomed Title"):
        # A Calibre-shaped sidecar OPF (namespaced, like the real backups).
        opf = f"""<?xml version='1.0' encoding='utf-8'?>
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>{title}</dc:title>
        <dc:creator opf:role="aut" opf:file-as="Author, Doomed">Doomed Author</dc:creator>
        <dc:subject>Doomed Tag</dc:subject>
        <dc:identifier opf:scheme="uuid">doomed-uuid-123</dc:identifier>
        <dc:identifier opf:scheme="ISBN">9780123456789</dc:identifier>
        <dc:description>&lt;p&gt;Doomed comments.&lt;/p&gt;</dc:description>
        <dc:publisher>Doomed Pub</dc:publisher>
        <dc:language>eng</dc:language>
        <dc:date>2020-05-01T00:00:00+00:00</dc:date>
        <meta name="calibre:title_sort" content="Doomed Title, The"/>
        <meta name="calibre:author_sort" content="Author, Doomed"/>
        <meta name="calibre:timestamp" content="2021-06-01T12:00:00+00:00"/>
        <meta name="calibre:series" content="Doomed Series"/>
        <meta name="calibre:series_index" content="2.0"/>
        <meta name="calibre:rating" content="8"/>
    </metadata>
    <guide/>
</package>
"""
        with open(os.path.join(book_dir, "metadata.opf"), "w") as f:
            f.write(opf)

    def _seed_book_dir(self, book_id, title="The Doomed Title", *, opf=True):
        rel = f"Doomed Author/{title} ({book_id})"
        book_dir = os.path.join(self.temp_dir, *rel.split("/"))
        os.makedirs(book_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE books SET path = ?, uuid = ? WHERE id = ?",
            (rel, "doomed-uuid-123", book_id),
        )
        conn.commit()
        conn.close()
        with open(os.path.join(book_dir, f"{title} - Doomed Author.epub"), "w") as f:
            f.write("xxxxx")
        with open(os.path.join(book_dir, "cover.jpg"), "w") as f:
            f.write("cover")
        if opf:
            self._write_opf(book_dir, title)
        return rel, book_dir

    def test_move_book_round_trip(self):
        rel, book_dir = self._seed_book_dir(3)
        with WritableCalibreDB(self.db_path) as wdb:
            # A live row with the satellites, like a real doomed book.
            wdb.conn.execute(
                "INSERT INTO books (id, title, series_index, author_sort, path, "
                "pubdate, timestamp) VALUES (3, 'The Doomed Title', 2.0, "
                "'Author, Doomed', ?, '2020-05-01 00:00:00+00:00', "
                "'2021-06-01 12:00:00+00:00')",
                (rel,),
            )
            wdb.conn.commit()  # the raw INSERT opened an implicit transaction
            wdb.set_authors(3, ["Doomed Author"])
            wdb.add_tag(3, "Doomed Tag")
            wdb.set_publisher(3, "Doomed Pub")
            wdb.set_series(3, "Doomed Series", 2.0)
            wdb.set_rating(3, 4.0)
            wdb.set_languages(3, ["eng"])
            wdb.set_identifier(3, "isbn", "9780123456789")
            wdb.set_comments(3, "<p>Doomed comments.</p>")
            wdb.add_format(3, "EPUB", "The Doomed Title - Doomed Author", 5)
            wdb.remove_book(3, delete_files="trash")
        # The trash half: rows gone, directory parked in .caltrash/b/3.
        self.assertEqual(self._sql("SELECT id FROM books"), [(1,)])
        self.assertFalse(os.path.isdir(book_dir))
        trashed = os.path.join(self.temp_dir, ".caltrash", "b", "3")
        self.assertTrue(os.path.isfile(os.path.join(trashed, "metadata.opf")))
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.move_book_from_trash(3)
        # The row is back with the OPF's values, original id and uuid.
        row = self._sql(
            "SELECT title, sort, author_sort, uuid, path, series_index, "
            "has_cover, pubdate, timestamp FROM books WHERE id = 3"
        )[0]
        self.assertEqual(row[0], "The Doomed Title")
        self.assertEqual(row[1], "Doomed Title, The")
        self.assertEqual(row[2], "Author, Doomed")
        self.assertEqual(row[3], "doomed-uuid-123")
        self.assertEqual(row[4], rel)
        self.assertEqual(row[5], 2.0)
        self.assertEqual(row[6], 1)
        self.assertEqual(row[7], "2020-05-01 00:00:00+00:00")
        self.assertEqual(row[8], "2021-06-01 12:00:00+00:00")
        # Links rebuilt; the fresh author row defaults sort to the display
        # name while the book's author_sort keeps the OPF's file-as.
        self.assertEqual(
            self._sql("SELECT name FROM authors WHERE id > 1"), [("Doomed Author",)]
        )
        self.assertEqual(self._sql("SELECT name FROM tags"), [("Doomed Tag",)])
        self.assertEqual(self._sql("SELECT name FROM publishers"), [("Doomed Pub",)])
        self.assertEqual(self._sql("SELECT name FROM series"), [("Doomed Series",)])
        self.assertEqual(self._sql("SELECT rating FROM ratings"), [(8,)])
        self.assertEqual(self._sql("SELECT lang_code FROM languages"), [("eng",)])
        self.assertEqual(
            self._sql("SELECT type, val FROM identifiers WHERE book = 3"),
            [("isbn", "9780123456789")],
        )
        self.assertEqual(
            self._sql("SELECT text FROM comments WHERE book = 3"),
            [("<p>Doomed comments.</p>",)],
        )
        # The format row and the files came home; the trash entry is gone.
        self.assertEqual(
            self._sql(
                "SELECT format, uncompressed_size, name FROM data WHERE book = 3"
            ),
            [("EPUB", 5, "The Doomed Title - Doomed Author")],
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(book_dir, "The Doomed Title - Doomed Author.epub")
            )
        )
        self.assertTrue(os.path.isfile(os.path.join(book_dir, "cover.jpg")))
        self.assertFalse(os.path.exists(trashed))
        # OPF resync queued (Calibre regenerates the sidecar) and the
        # restored format queued for FTS re-extraction.
        self.assertEqual(self._sql("SELECT book FROM metadata_dirtied"), [(3,)])
        fts = sqlite3.connect(os.path.join(self.temp_dir, "full-text-search.db"))
        try:
            queued = sorted(fts.execute("SELECT book, format FROM dirtied_formats"))
        finally:
            fts.close()
        self.assertIn((3, "EPUB"), queued)

    def test_move_book_refuses_a_live_id_and_keeps_the_entry(self):
        _rel, book_dir = self._seed_book_dir(3)
        trashed = os.path.join(self.temp_dir, ".caltrash", "b", "3")
        os.makedirs(os.path.dirname(trashed), exist_ok=True)
        shutil.move(book_dir, trashed)
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
        ):
            wdb.move_book_from_trash(1)
        self.assertTrue(os.path.isdir(trashed))

    def test_move_book_missing_entry_and_missing_opf_raise(self):
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
        ):
            wdb.move_book_from_trash(9)  # no entry at all
        # An entry without the sidecar OPF cannot rebuild a row.
        entry = os.path.join(self.temp_dir, ".caltrash", "b", "9")
        os.makedirs(entry)
        with open(os.path.join(entry, "x.epub"), "w") as f:
            f.write("x")
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
        ):
            wdb.move_book_from_trash(9)
        self.assertEqual(self._sql("SELECT COUNT(*) FROM books WHERE id = 9"), [(0,)])

    def test_move_book_inside_failed_batch_rolls_back(self):
        _rel, book_dir = self._seed_book_dir(3)
        trashed = os.path.join(self.temp_dir, ".caltrash", "b", "3")
        os.makedirs(os.path.dirname(trashed), exist_ok=True)
        shutil.move(book_dir, trashed)
        with (
            self.assertRaises(ValueError),
            WritableCalibreDB(self.db_path) as wdb,
            wdb.batch(),
        ):
            wdb.move_book_from_trash(3)
            wdb.remove_book(999)  # fails the pass
        self.assertEqual(self._sql("SELECT id FROM books"), [(1,)])
        self.assertTrue(os.path.isdir(trashed))

    def test_move_format_from_trash_round_trip(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO data (book, format, uncompressed_size, name) "
            "VALUES (1, 'MOBI', 3, 'Keeper - Alive Author')"
        )
        conn.commit()
        conn.close()
        entry = os.path.join(self.temp_dir, ".caltrash", "f", "1")
        os.makedirs(entry)
        with open(os.path.join(entry, "epub"), "w") as f:
            f.write("xxxxxxxxx")  # 9 bytes
        with open(os.path.join(entry, "metadata.json"), "w") as f:
            f.write("{}")
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.move_format_from_trash(1, "epub"))
        # The row uses the book's surviving stem; the file sits beside the
        # MOBI; the entry (only metadata.json left) is cleaned up.
        self.assertEqual(
            self._sql(
                "SELECT format, uncompressed_size, name FROM data "
                "WHERE book = 1 AND format = 'EPUB'"
            ),
            [("EPUB", 9, "Keeper - Alive Author")],
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(
                    self.temp_dir, "Keeper, A (1)", "Keeper - Alive Author.epub"
                )
            )
        )
        self.assertFalse(os.path.exists(entry))

    def test_move_format_from_trash_missing_targets_raise(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.move_format_from_trash(99, "epub")  # unknown book
            with self.assertRaises(ValueError):
                wdb.move_format_from_trash(1, "epub")  # no trash entry

    def test_copy_verbs_leave_the_database_alone(self):
        _rel, book_dir = self._seed_book_dir(3)
        book_entry = os.path.join(self.temp_dir, ".caltrash", "b", "3")
        os.makedirs(os.path.dirname(book_entry), exist_ok=True)
        shutil.move(book_dir, book_entry)
        entry = os.path.join(self.temp_dir, ".caltrash", "f", "3")
        os.makedirs(entry)
        with open(os.path.join(entry, "epub"), "w") as f:
            f.write("epubbytes")
        with WritableCalibreDB(self.db_path) as wdb:
            dest_fmt = wdb.copy_format_from_trash(
                3, "EPUB", os.path.join(self.temp_dir, "rescued.epub")
            )
            dest_book = wdb.copy_book_from_trash(
                3, os.path.join(self.temp_dir, "rescued-book")
            )
        self.assertTrue(os.path.isfile(dest_fmt))
        self.assertTrue(os.path.isfile(os.path.join(dest_book, "metadata.opf")))
        self.assertEqual(self._sql("SELECT COUNT(*) FROM data"), [(0,)])
        # Both entries are still in the trash.
        self.assertTrue(os.path.isdir(book_entry))
        self.assertTrue(os.path.isfile(os.path.join(entry, "epub")))

    def test_delete_trash_entry(self):
        entry = os.path.join(self.temp_dir, ".caltrash", "b", "3")
        os.makedirs(entry)
        with open(os.path.join(entry, "x.epub"), "w") as f:
            f.write("x")
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.delete_trash_entry(3, "b"))
            self.assertFalse(wdb.delete_trash_entry(3, "b"))  # already gone
            with self.assertRaises(ValueError):
                wdb.delete_trash_entry(3, "z")  # invalid category
        self.assertFalse(os.path.exists(entry))

    def test_trash_verbs_do_not_materialize_a_trash_tree(self):
        # The 1.20.1 LOW rule holds for the restore family too: a library
        # that never trashed gains no .caltrash directories.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertFalse(wdb.delete_trash_entry(1, "b"))
            with self.assertRaises(ValueError):
                wdb.copy_book_from_trash(1, os.path.join(self.temp_dir, "x"))
        self.assertFalse(os.path.exists(os.path.join(self.temp_dir, ".caltrash")))


class TestAuthorSortNameAndLinkMap(_WriteSideFixture, unittest.TestCase):
    """set_author_sort_name and set_link_map: the link/sort column writers
    (1.25, Phase 17 item 14)."""

    def test_author_sort_name_recomputes_affected_books(self):
        with self._wdb() as wdb:
            affected = wdb.set_author_sort_name("Zed A. Writer", "Writer, Zed")
        self.assertEqual(affected, 2)  # books 1 and 2 share the author
        self.assertEqual(
            self._sql2("SELECT sort FROM authors WHERE id=1"), [("Writer, Zed",)]
        )
        for book_id in (1, 2):
            self.assertEqual(
                self._sql2("SELECT author_sort FROM books WHERE id=?", (book_id,)),
                [("Writer, Zed",)],
            )

    def test_author_sort_name_resolves_nocase(self):
        with self._wdb() as wdb:
            self.assertEqual(wdb.set_author_sort_name("zed a. writer", "W, Z.A."), 2)

    def test_author_sort_name_honest_noop(self):
        # The docstring promised this from day one; the 1.25 body never
        # compared (it touched and queued every book of the author). The
        # 1.26 body honors it -- and the fixture's stored sort IS
        # 'Writer, Zed A.', so the very first call is the no-op.
        with self._wdb() as wdb:
            self.assertEqual(
                wdb.set_author_sort_name("Zed A. Writer", "Writer, Zed A."), 0
            )
            self.assertEqual(
                self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)]
            )
            self.assertEqual(
                wdb.set_author_sort_name("Zed A. Writer", "Writer, Zed"), 2
            )
            self.assertEqual(
                wdb.set_author_sort_name("Zed A. Writer", "Writer, Zed"), 0
            )

    def test_author_sort_name_rejects_empty_and_unknown(self):
        with self._wdb() as wdb:
            with self.assertRaises(ValueError):
                wdb.set_author_sort_name("Zed A. Writer", "")
            with self.assertRaises(ValueError):
                wdb.set_author_sort_name("Ghost Author", "G, A")

    def test_author_sort_name_queues_opf_resync(self):
        with self._wdb() as wdb:
            wdb.set_author_sort_name("Zed A. Writer", "Writer, Zed")
        self.assertEqual(
            self._sql2("SELECT book FROM metadata_dirtied ORDER BY book"),
            [(1,), (2,)],
        )

    def test_set_link_map_author(self):
        with self._wdb() as wdb:
            affected = wdb.set_link_map(
                "authors", {"Zed A. Writer": "https://example.com/zed"}
            )
        self.assertEqual(affected, 2)
        self.assertEqual(
            self._sql2("SELECT link FROM authors WHERE id=1"),
            [("https://example.com/zed",)],
        )

    def test_set_link_map_clears_with_none_and_skips_existing(self):
        with self._wdb() as wdb:
            wdb.set_link_map("authors", {"Zed A. Writer": "https://a"})
            wdb.set_link_map("authors", {"Zed A. Writer": None})
            self.assertEqual(
                self._sql2("SELECT link FROM authors WHERE id=1"), [(None,)]
            )
            wdb.set_link_map(
                "authors",
                {"Zed A. Writer": "https://b"},
                only_set_if_no_existing_link=True,
            )
            self.assertEqual(
                self._sql2("SELECT link FROM authors WHERE id=1"), [("https://b",)]
            )
            wdb.set_link_map(
                "authors",
                {"Zed A. Writer": "https://c"},
                only_set_if_no_existing_link=True,
            )
            self.assertEqual(
                self._sql2("SELECT link FROM authors WHERE id=1"), [("https://b",)]
            )

    def test_set_link_map_all_entity_kinds_and_honest_noop(self):
        with self._wdb() as wdb:
            wdb.add_tag(1, "Space")
            self.assertEqual(wdb.set_link_map("tags", {"Space": "https://t"}), 1)
            # Same link again: 0 books affected.
            self.assertEqual(wdb.set_link_map("tags", {"Space": "https://t"}), 0)

    def test_set_link_map_unknown_value_and_kind_raise(self):
        with self._wdb() as wdb:
            with self.assertRaises(ValueError):
                wdb.set_link_map("authors", {"Ghost Author": "https://x"})
            with self.assertRaises(ValueError):
                wdb.set_link_map("languages", {"eng": "https://x"})

    def test_set_link_map_custom_column_values(self):
        # custom_column_3 (#audience) exists in the fixture schema with its
        # link column; seed one value linked to book 1.
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO custom_column_3 (id, value) VALUES (1, 'YA')")
        conn.execute(
            "INSERT INTO books_custom_column_3_link (book, value) VALUES (1, 1)"
        )
        conn.commit()
        conn.close()
        with self._wdb() as wdb:
            affected = wdb.set_link_map("#audience", {"YA": "https://gr/ya"})
        self.assertEqual(affected, 1)
        self.assertEqual(
            self._sql2("SELECT link FROM custom_column_3 WHERE id=1"),
            [("https://gr/ya",)],
        )


class TestSetPages(unittest.TestCase):
    """set_pages: the page-count row writer (1.25, Phase 17 item 15)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                timestamp TEXT, last_modified TEXT, path TEXT);
            CREATE TABLE books_pages_link (
                book INTEGER PRIMARY KEY,
                pages INTEGER DEFAULT 0 NOT NULL,
                algorithm INTEGER DEFAULT 0 NOT NULL,
                format TEXT DEFAULT '' NOT NULL COLLATE NOCASE,
                format_size INTEGER DEFAULT 0 NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                needs_scan INTEGER NOT NULL DEFAULT 0 CHECK(needs_scan IN (0, 1))
            );
            CREATE TABLE metadata_dirtied (
                id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book)
            );
            INSERT INTO books (id, title) VALUES (1, 'One');
            INSERT INTO books_pages_link (book, pages, algorithm, format, format_size, needs_scan)
                VALUES (1, 100, 4, 'EPUB', 1234, 1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def test_replace_row_and_clear_needs_scan(self):
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(
                wdb.set_pages(1, 321, algorithm=4, format="epub", format_size=2000)
            )
        self.assertEqual(
            self._sql(
                "SELECT pages, algorithm, format, format_size, needs_scan FROM books_pages_link WHERE book=1"
            ),
            [(321, 4, "EPUB", 2000, 0)],
        )
        self.assertEqual(self._sql("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_fresh_row_for_a_book_without_one(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO books (id, title) VALUES (2, 'Two')")
        conn.commit()
        conn.close()
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_pages(2, 55))
        self.assertEqual(
            self._sql("SELECT pages, needs_scan FROM books_pages_link WHERE book=2"),
            [(55, 0)],
        )

    def test_identical_row_with_clean_flag_is_honest_noop(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE books_pages_link SET needs_scan = 0 WHERE book = 1")
        conn.commit()
        conn.close()
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertFalse(
                wdb.set_pages(1, 100, algorithm=4, format="EPUB", format_size=1234)
            )

    def test_pending_scan_always_rewrites(self):
        # needs_scan=1 means the stored count is stale; an equal value still
        # lands so the flag clears.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(
                wdb.set_pages(1, 100, algorithm=4, format="EPUB", format_size=1234)
            )
        self.assertEqual(
            self._sql("SELECT needs_scan FROM books_pages_link WHERE book=1"), [(0,)]
        )

    def test_negative_pages_and_unknown_book_raise(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_pages(1, -1)
            with self.assertRaises(ValueError):
                wdb.set_pages(999, 10)

    def test_schema_predating_the_table_raises(self):
        path2 = os.path.join(self.temp_dir, "old.db")
        conn = sqlite3.connect(path2)
        conn.executescript(
            "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,"
            " timestamp TEXT, last_modified TEXT, path TEXT);"
            "INSERT INTO books (id, title) VALUES (1, 'Old');"
        )
        conn.commit()
        conn.close()
        with WritableCalibreDB(path2) as wdb, self.assertRaises(ValueError):
            wdb.set_pages(1, 10)


class TestDataFiles(_WriteSideFixture, unittest.TestCase):
    """The data/ directory verbs: reads and the pure-filesystem writes
    (1.25, Phase 17 item 16)."""

    def _book_dir(self, book_id=1):
        path = os.path.join(self.temp_dir, f"Book ({book_id})")
        os.makedirs(path, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE books SET path = ? WHERE id = ?", (f"Book ({book_id})", book_id)
        )
        conn.commit()
        conn.close()
        return path

    def test_list_and_get_roundtrip(self):
        book_dir = self._book_dir()
        data = os.path.join(book_dir, "data", "sub")
        os.makedirs(data)
        with open(os.path.join(data, "notes.txt"), "wb") as f:
            f.write(b"hello data")
        with CalibreDB(self.db_path) as db:
            files = db.list_data_files(1)
            self.assertEqual([e["relpath"] for e in files], ["sub/notes.txt"])
            self.assertEqual(files[0]["size"], 10)
            self.assertEqual(db.get_data_file(1, "sub/notes.txt"), b"hello data")
            self.assertIsNone(db.get_data_file(1, "missing.txt"))
            self.assertEqual(db.list_data_files(2), [])  # no dir yet

    def test_unknown_book_and_empty_path(self):
        with CalibreDB(self.db_path) as db:
            with self.assertRaises(ValueError):
                db.list_data_files(999)
            self.assertEqual(db.list_data_files(1), [])

    def test_traversal_is_refused_on_both_sides(self):
        book_dir = self._book_dir()
        with open(os.path.join(book_dir, "secret.txt"), "w") as f:
            f.write("top secret")
        with CalibreDB(self.db_path) as db:
            with self.assertRaises(ValueError):
                db.get_data_file(1, "../secret.txt")
            with self.assertRaises(ValueError):
                db.get_data_file(1, "/etc/passwd")
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
        ):
            wdb.add_data_file(1, "../escape.txt", b"x")

    def test_add_bytes_and_source_path(self):
        self._book_dir()
        src = os.path.join(self.temp_dir, "src.bin")
        with open(src, "wb") as f:
            f.write(b"from disk")
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertEqual(
                wdb.add_data_file(1, "docs/readme.txt", b"inline"), "docs/readme.txt"
            )
            self.assertEqual(wdb.add_data_file(1, "copy.bin", src), "copy.bin")
        with CalibreDB(self.db_path) as db:
            self.assertEqual(db.get_data_file(1, "docs/readme.txt"), b"inline")
            self.assertEqual(db.get_data_file(1, "copy.bin"), b"from disk")

    def test_add_conflict_semantics(self):
        self._book_dir()
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.add_data_file(1, "file.txt", b"v1")
            self.assertIsNone(wdb.add_data_file(1, "file.txt", b"v2"))
            self.assertEqual(
                wdb.add_data_file(1, "file.txt", b"v2", replace=True), "file.txt"
            )
            conflict = wdb.add_data_file(1, "file.txt", b"v3", auto_rename=True)
            self.assertEqual(conflict, "merge conflict/file.txt")
        with CalibreDB(self.db_path) as db:
            self.assertEqual(db.get_data_file(1, "file.txt"), b"v2")
            self.assertEqual(db.get_data_file(1, "merge conflict/file.txt"), b"v3")

    def test_rename_and_remove(self):
        self._book_dir()
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.add_data_file(1, "old.txt", b"data")
            self.assertTrue(wdb.rename_data_file(1, "old.txt", "new/deep.txt"))
            self.assertFalse(wdb.rename_data_file(1, "old.txt", "again.txt"))
            out = wdb.remove_data_files(1, ["new/deep.txt", "ghost.txt"])
            self.assertIsNone(out["new/deep.txt"])
            self.assertIsInstance(out["ghost.txt"], FileNotFoundError)

    def test_writes_touch_no_rows(self):
        self._book_dir()
        before = self._sql2("SELECT id, title FROM books")
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.add_data_file(1, "x.txt", b"x")
            wdb.remove_data_files(1, ["x.txt"])
        self.assertEqual(self._sql2("SELECT id, title FROM books"), before)
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])


class TestCopyBookFromLibrary(unittest.TestCase):
    """copy_book_from_library: the blessed cross-library copy (1.25,
    Phase 17 item 17), modeled on upstream copy_one_book."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.src_path = os.path.join(self.temp_dir, "src", "metadata.db")
        self.dest_path = os.path.join(self.temp_dir, "dest", "metadata.db")
        os.makedirs(os.path.dirname(self.src_path))
        os.makedirs(os.path.dirname(self.dest_path))
        # The destination starts as an empty library (same DDL, no rows,
        # plus the insert trigger add_book's creation path relies on).
        dest_conn = sqlite3.connect(self.dest_path)
        dest_conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,
                series_index REAL, path TEXT, has_cover INTEGER);
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT DEFAULT '');
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
            CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT, UNIQUE(book));
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
            CREATE TRIGGER books_insert_trg AFTER INSERT ON books
            BEGIN
                UPDATE books SET sort = title_sort(NEW.title),
                    last_modified = uuid4() WHERE id = NEW.id;
            END;
            """
        )
        dest_conn.commit()
        dest_conn.close()
        # Source: a fully-dressed book with real format and cover files.
        conn = sqlite3.connect(self.src_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,
                series_index REAL, path TEXT, has_cover INTEGER);
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT DEFAULT '');
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
            CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT, UNIQUE(book));
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
            INSERT INTO books (id, title, sort, author_sort, timestamp, pubdate,
                series_index, path, has_cover) VALUES
                (1, 'Ancillary Justice', 'Ancillary Justice', 'Leckie, Ann & Writer, Zed',
                 '2020-05-01 00:00:00+00:00', '2013-10-01 00:00:00+00:00', 2.0,
                 'Leckie, Ann/Ancillary Justice (1)', 1);
            INSERT INTO authors VALUES (1, 'Ann Leckie', 'Leckie, Ann', ''), (2, 'Zed A. Writer', 'Writer, Zed', '');
            INSERT INTO books_authors_link (book, author) VALUES (1, 1), (1, 2);
            INSERT INTO tags VALUES (1, 'SciFi'), (2, 'Award');
            INSERT INTO books_tags_link (book, tag) VALUES (1, 1), (1, 2);
            INSERT INTO series (id, name, sort) VALUES (1, 'Imperial Radch', 'Imperial Radch');
            INSERT INTO books_series_link (book, series) VALUES (1, 1);
            INSERT INTO publishers (id, name, sort) VALUES (1, 'Orbit', 'Orbit');
            INSERT INTO books_publishers_link (book, publisher) VALUES (1, 1);
            INSERT INTO ratings VALUES (1, 8);
            INSERT INTO books_ratings_link (book, rating) VALUES (1, 1);
            INSERT INTO languages VALUES (1, 'eng');
            INSERT INTO books_languages_link (book, lang_code, item_order) VALUES (1, 1, 0);
            INSERT INTO comments (book, text) VALUES (1, '<p>A mind-ship novel</p>');
            INSERT INTO data (book, format, uncompressed_size, name) VALUES (1, 'EPUB', 11, 'Ancillary Justice - Leckie, Ann');
            INSERT INTO identifiers (book, type, val) VALUES (1, 'isbn', '9781590178884');
            """
        )
        conn.commit()
        conn.close()
        book_dir = os.path.join(
            self.temp_dir, "src", "Leckie, Ann", "Ancillary Justice (1)"
        )
        os.makedirs(book_dir)
        with open(
            os.path.join(book_dir, "Ancillary Justice - Leckie, Ann.epub"), "wb"
        ) as f:
            f.write(b"EPUB-BYTES!")
        with open(os.path.join(book_dir, "cover.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0jpegdata")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _dest_counts(self):
        conn = sqlite3.connect(self.dest_path)
        try:
            return (
                conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
            )
        finally:
            conn.close()

    def test_copy_carries_every_core_field(self):
        with CalibreDB(self.src_path) as src, WritableCalibreDB(self.dest_path) as dest:
            new_id = dest.copy_book_from_library(src, 1)
        with CalibreDB(self.dest_path) as check:
            row = check.get_book(new_id, include_comments=True)
            self.assertEqual(row["title"], "Ancillary Justice")
            self.assertEqual(row["title_sort"], "Ancillary Justice")
            self.assertEqual(row["authors"], ["Ann Leckie", "Zed A. Writer"])
            self.assertEqual(row["author_sorts"], ["Leckie, Ann", "Writer, Zed"])
            self.assertEqual(row["author_sort"], "Leckie, Ann & Writer, Zed")
            # Tag ORDER is link-id order per library, not part of the copy
            # contract; the set is what carries over.
            self.assertEqual(sorted(row["tags"]), ["Award", "SciFi"])
            self.assertEqual(row["series"], "Imperial Radch")
            self.assertEqual(row["series_index"], 2.0)
            self.assertEqual(row["publisher"], "Orbit")
            self.assertEqual(row["rating"], 8)
            self.assertEqual(row["languages"], ["eng"])
            self.assertEqual(row["identifiers"], {"isbn": "9781590178884"})
            self.assertEqual(row["comments"], "<p>A mind-ship novel</p>")
            self.assertTrue(row["pubdate"].startswith("2013-10-01"))
            self.assertTrue(row["timestamp"].startswith("2020-05-01"))
            self.assertTrue(row["has_cover"])
            fmts = check.get_formats(new_id)
            self.assertIn("EPUB", fmts)
            self.assertEqual(fmts["EPUB"]["size_bytes"], 11)
        dest_dir = os.path.dirname(self.dest_path)
        copied = os.path.join(dest_dir, row["path"], "cover.jpg")
        with open(copied, "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8\xff\xe0jpegdata")

    def test_missing_format_file_rolls_everything_back(self):
        conn = sqlite3.connect(self.src_path)
        conn.execute("UPDATE data SET name = 'vanished'")
        conn.commit()
        conn.close()
        with (
            CalibreDB(self.src_path) as src,
            WritableCalibreDB(self.dest_path) as dest,
            self.assertRaises(FileNotFoundError),
        ):
            dest.copy_book_from_library(src, 1)
        self.assertEqual(self._dest_counts(), (0, 0))

    def test_unknown_source_book_raises(self):
        with (
            CalibreDB(self.src_path) as src,
            WritableCalibreDB(self.dest_path) as dest,
            self.assertRaises(ValueError),
        ):
            dest.copy_book_from_library(src, 999)

    def test_preserve_timestamp_false_leaves_now(self):
        with CalibreDB(self.src_path) as src, WritableCalibreDB(self.dest_path) as dest:
            new_id = dest.copy_book_from_library(src, 1, preserve_timestamp=False)
        with CalibreDB(self.dest_path) as check:
            row = check.get_book(new_id)
            self.assertFalse(row["timestamp"].startswith("2020-05-01"))


class TestBlobWriters(unittest.TestCase):
    """set_plugin_data / set_conversion_options / set_book_storage: the
    opaque-blob passthrough writers (1.25, Phase 17 item 18), plus the copy
    primitive's new conversion-option carriage."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,
                series_index REAL, path TEXT, has_cover INTEGER);
            INSERT INTO books (id, title) VALUES (1, 'One'), (2, 'Two');
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT DEFAULT '');
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
            CREATE TABLE books_plugin_data (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, name TEXT NOT NULL, val TEXT NOT NULL,
                UNIQUE(book, name));
            CREATE TABLE conversion_options (id INTEGER PRIMARY KEY,
                format TEXT NOT NULL COLLATE NOCASE, book INTEGER,
                data BLOB NOT NULL, UNIQUE(format, book));
            CREATE TABLE book_storage (id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL, format TEXT NOT NULL COLLATE NOCASE,
                user_type TEXT NOT NULL, user TEXT NOT NULL,
                timestamp REAL NOT NULL, data TEXT NOT NULL DEFAULT '{}',
                UNIQUE(book, format, user_type, user));
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    def test_plugin_data_str_verbatim_json_serialized_and_delete(self):
        # 1.26: every payload json.dumps's like upstream's writer -- a
        # verbatim plain string was unreadable to upstream's json.loads
        # reader (it needs quoted JSON strings).
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_plugin_data(1, "wordcount", "5123"))
            self.assertTrue(wdb.set_plugin_data(1, "metrics", {"a": 1}))
            self.assertEqual(
                self._sql("SELECT val FROM books_plugin_data WHERE name='wordcount'"),
                [('"5123"',)],
            )
            self.assertEqual(
                json.loads(
                    self._sql("SELECT val FROM books_plugin_data WHERE name='metrics'")[
                        0
                    ][0]
                ),
                {"a": 1},
            )
            self.assertTrue(wdb.set_plugin_data(1, "wordcount", None))
            self.assertFalse(wdb.set_plugin_data(1, "wordcount", None))
            self.assertEqual(
                self._sql("SELECT COUNT(*) FROM books_plugin_data"), [(1,)]
            )

    def test_plugin_data_name_and_schema_guards(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_plugin_data(1, "  ", "x")
            with self.assertRaises(ValueError):
                wdb.set_plugin_data(999, "name", "x")

    def test_conversion_options_passthrough_and_delete(self):
        # 1.26: fresh payloads store inside upstream's protocol-2 BINSTRING
        # frame, so Calibre's unpickling reader can decode them.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_conversion_options(1, b"recipe-bytes"))
            self.assertTrue(wdb.set_conversion_options(1, "text payload", fmt="mobi"))
            rows = self._sql(
                "SELECT format, data FROM conversion_options ORDER BY format"
            )
            self.assertEqual(rows[0][0], "MOBI")  # NOCASE lookup, stored spelling ours
            frame = rows[1][1]
            self.assertTrue(frame.startswith(b"\x80\x02T"))
            self.assertEqual(int.from_bytes(frame[3:7], "little"), 12)
            self.assertEqual(frame[7:-1], b"recipe-bytes")  # 3 magic + 4 length
            self.assertEqual(frame[-1:], b".")
            self.assertTrue(wdb.set_conversion_options(1, None, fmt="mobi"))
            self.assertFalse(wdb.set_conversion_options(1, None, fmt="mobi"))
            self.assertEqual(
                self._sql("SELECT COUNT(*) FROM conversion_options"), [(1,)]
            )

    def test_book_storage_stores_the_bare_map_and_clears(self):
        # Upstream's column contract (the 1.25 shape bug, fixed): the data
        # column is ONLY the str->str payload map, JSON-encoded; the
        # timestamp lives in its own REAL column. The 1.25 wrapped
        # {"timestamp", "data"} into the column and Calibre's reader
        # silently dropped every such row.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_book_storage(1, "epub", {"position": "0.42"}))
            row = self._sql(
                "SELECT format, user_type, user, timestamp, data FROM book_storage"
            )[0]
            self.assertEqual(row[0], "EPUB")
            self.assertEqual((row[1], row[2]), ("local", "viewer"))
            self.assertIsInstance(row[3], float)
            self.assertEqual(json.loads(row[4]), {"position": "0.42"})
            self.assertTrue(wdb.set_book_storage(1, "EPUB", None))
            self.assertFalse(wdb.set_book_storage(1, "EPUB", None))

    def test_book_storage_non_string_values_raise(self):
        # Upstream validate_book_storage: keys AND values must be str. The
        # 1.25 test used a float value, which upstream's reader rejects.
        # Both cases raise ValueError: upstream's InvalidBookStorage (a
        # ValueError subclass) does not distinguish them.
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_book_storage(1, "EPUB", {"position": 0.42})
            with self.assertRaises(ValueError):
                wdb.set_book_storage(1, "EPUB", {1: "x"})

    def test_book_storage_newer_entry_wins(self):
        # Upstream's guard: an older entry never overwrites a newer one.
        with WritableCalibreDB(self.db_path) as wdb:
            self.assertTrue(wdb.set_book_storage(1, "EPUB", {"v": "new"}))
            # Force an older timestamp into the row, then replay an old write.
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE book_storage SET timestamp = ?", (time.time() + 3600,))
            conn.commit()
            conn.close()
            self.assertFalse(wdb.set_book_storage(1, "EPUB", {"v": "old"}))
            row = self._sql("SELECT data FROM book_storage")[0]
            self.assertEqual(json.loads(row[0]), {"v": "new"})

    def test_book_storage_non_dict_raises(self):
        with (
            WritableCalibreDB(self.db_path) as wdb,
            self.assertRaises(ValueError),
        ):
            wdb.set_book_storage(1, "EPUB", ["not", "a", "dict"])

    def test_missing_tables_raise(self):
        path2 = os.path.join(self.temp_dir, "old.db")
        conn = sqlite3.connect(path2)
        conn.executescript(
            "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,"
            " timestamp TEXT, last_modified TEXT, path TEXT);"
            "INSERT INTO books (id, title) VALUES (1, 'Old');"
        )
        conn.commit()
        conn.close()
        with WritableCalibreDB(path2) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_plugin_data(1, "n", "v")
            with self.assertRaises(ValueError):
                wdb.set_conversion_options(1, b"x")
            with self.assertRaises(ValueError):
                wdb.set_book_storage(1, "EPUB", {})

    def test_copy_carries_conversion_options(self):
        src_path = os.path.join(self.temp_dir, "src.db")
        conn = sqlite3.connect(src_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,
                series_index REAL, path TEXT, has_cover INTEGER);
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT DEFAULT '');
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
            CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
            CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
            CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
            CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
            CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER);
            CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
            CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER, item_order INTEGER);
            CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
            CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
            CREATE TABLE conversion_options (id INTEGER PRIMARY KEY,
                format TEXT NOT NULL COLLATE NOCASE, book INTEGER,
                data BLOB NOT NULL, UNIQUE(format, book));
            INSERT INTO books (id, title, sort, path, has_cover) VALUES (1, 'One', 'One', '', 0);
            """
        )
        conn.execute(
            "INSERT INTO conversion_options (book, format, data) VALUES (?, ?, ?)",
            (1, "PIPE", b"recipe"),
        )
        conn.commit()
        conn.close()
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_plugin_data(1, "wordcount", "10")
        with CalibreDB(src_path) as src, WritableCalibreDB(self.db_path) as dest:
            new_id = dest.copy_book_from_library(src, 1)
        # The destination stores the framed shape (upstream's column
        # contract): the source's framed blob is unwrapped and re-framed.
        dest_blob = self._sql(
            "SELECT data FROM conversion_options WHERE book=?", (new_id,)
        )[0][0]
        self.assertTrue(dest_blob.startswith(b"\x80\x02T"))
        self.assertEqual(dest_blob[7:-1], b"recipe")
        # The source book's plugin data does NOT follow (upstream's copy doesn't).
        self.assertEqual(self._sql("SELECT book FROM books_plugin_data"), [(1,)])


class TestCustomSeriesIndex(unittest.TestCase):
    """The custom-series index writer + the fresh-assignment seed
    (1.26, Phase 17's D2)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,
                author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,
                series_index REAL, path TEXT, has_cover INTEGER);
            INSERT INTO books (id, title) VALUES (1, 'One'), (2, 'Two');
            CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT UNIQUE,
                name TEXT, datatype TEXT, editable BOOL DEFAULT 1,
                display TEXT DEFAULT '{}', is_multiple BOOL DEFAULT 0,
                normalized BOOL DEFAULT 0);
            INSERT INTO custom_columns VALUES (1,'sag','Saga','series',1,'{}',0,1);
            CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT '');
            CREATE TABLE books_custom_column_1_link (book INTEGER, value INTEGER, extra REAL, UNIQUE(book, value));
            CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def test_fresh_assignment_seeds_extra_10(self):
        # Upstream seeds a fresh series assignment's extra at 1.0; the 1.25
        # writer left it NULL, making #sag_index permanently unreadable.
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_custom_column(1, "#sag", "Saga")
        extra = self._sql("SELECT extra FROM books_custom_column_1_link")[0][0]
        self.assertEqual(extra, 1.0)

    def test_set_custom_series_index_updates_and_noops(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_custom_column(1, "#sag", "Saga")
            self.assertTrue(wdb.set_custom_series_index(1, "#sag", 3.5))
            self.assertFalse(wdb.set_custom_series_index(1, "#sag", 3.5))
            self.assertEqual(
                self._sql("SELECT extra FROM books_custom_column_1_link"),
                [(3.5,)],
            )
            # None writes the schema's no-index value 1.0.
            self.assertTrue(wdb.set_custom_series_index(1, "#sag", None))
            self.assertEqual(
                self._sql("SELECT extra FROM books_custom_column_1_link"),
                [(1.0,)],
            )

    def test_unassigned_book_and_non_series_raise(self):
        with WritableCalibreDB(self.db_path) as wdb:
            with self.assertRaises(ValueError):
                wdb.set_custom_series_index(2, "#sag", 2.0)
            with self.assertRaises(ValueError):
                wdb.set_custom_series_index(1, "title", 2.0)


class TestAuthorCreationParity(unittest.TestCase):
    """New author rows follow upstream's creation path (1.26, D5): the
    name stores comma-as-pipe and the sort is author_to_author_sort's
    flip -- the pre-1.26 verbatim defaults rested on the wrong claim
    that upstream's flip runs only on GUI edits."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        _make_simple_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        register_udfs(conn)  # the insert trigger calls title_sort/uuid4
        conn.executescript(
            """
            ALTER TABLE books ADD COLUMN author_sort TEXT;
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort TEXT);
            CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
            INSERT INTO books (id, title, sort) VALUES (1, 'Target', 'Target');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _sql(self, query):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query).fetchall()]
        finally:
            conn.close()

    def test_new_comma_author_stores_piped_name_and_flipped_sort(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_authors(1, ["Leckie, Ann"])
        self.assertEqual(
            self._sql("SELECT name, sort FROM authors"),
            [("Leckie| Ann", "Leckie, Ann")],
        )
        # The book-level author_sort joins the stored sort keys.
        self.assertEqual(
            self._sql("SELECT author_sort FROM books WHERE id = 1"),
            [("Leckie, Ann",)],
        )

    def test_existing_rows_keep_their_sorts(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO authors VALUES (1, 'Hand Tuned', 'Custom, Sort')")
        conn.commit()
        conn.close()
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_authors(1, ["Hand Tuned"])
        self.assertEqual(
            self._sql("SELECT name, sort FROM authors"),
            [("Hand Tuned", "Custom, Sort")],
        )

    def test_plain_names_are_unchanged(self):
        with WritableCalibreDB(self.db_path) as wdb:
            wdb.set_authors(1, ["Ann Leckie"])
        self.assertEqual(
            self._sql("SELECT name, sort FROM authors"),
            [("Ann Leckie", "Leckie, Ann")],
        )
