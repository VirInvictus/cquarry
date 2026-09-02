"""Tests for the opt-in write module (cquarry.write).

The fixture recreates the trigger hazards that make blind writes fail against
a real Calibre library: books_insert_trg calls the title_sort() and uuid4()
SQL functions, which only exist after register_udfs().
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

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


class TestWritableCalibreDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
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
            CREATE TRIGGER books_insert_trg AFTER INSERT ON books
            BEGIN
                UPDATE books SET sort = title_sort(NEW.title),
                    last_modified = uuid4() WHERE id = NEW.id;
            END;
            """
        )
        conn.commit()
        conn.close()

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
        conn = sqlite3.connect(self.db_path)
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
            CREATE TABLE metadata_dirtied (
                id INTEGER PRIMARY KEY, book INTEGER NOT NULL,
                UNIQUE(book)
            );
            """
        )
        conn.commit()
        conn.close()

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
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER);
CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, text TEXT, UNIQUE(book));
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT, UNIQUE(book, type));
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
CREATE TABLE custom_columns (
    id INTEGER PRIMARY KEY, label TEXT UNIQUE, name TEXT, datatype TEXT,
    editable BOOL DEFAULT 1, display TEXT DEFAULT '{}',
    is_multiple BOOL DEFAULT 0, normalized BOOL DEFAULT 0
);
CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT UNIQUE, link TEXT DEFAULT '');
CREATE TABLE books_custom_column_1_link (book INTEGER, value INTEGER);
CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, book INTEGER, value BOOL);
CREATE TABLE metadata_dirtied (id INTEGER PRIMARY KEY, book INTEGER NOT NULL, UNIQUE(book));
"""


class TestWriteSideExpansion(unittest.TestCase):
    """Phase-6 write-side expansion: entity setters, set_comments,
    set_custom_column, format management and remove_book."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_WRITE_SCHEMA)
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
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def _wdb(self):
        return WritableCalibreDB(self.db_path)

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
        self.assertEqual(asort, "Ann Leckie & Writer, Zed A.")  # new author sort=name
        self.assertEqual(newsort, "Ann Leckie")  # new rows default sort=name
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
        self.assertIsNone(idx)
        self.assertEqual(count, 0)  # orphaned series pruned

    def _sql2(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

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
            "JOIN languages l ON l.id=bl.lang_code WHERE bl.book=1"
        )
        self.assertEqual(codes, [("eng",), ("fre",)])

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


class TestBatchContext(TestWriteSideExpansion):
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

    def test_transaction_alias_commits_once_on_success(self):
        # Pre-1.7.0 call shape (the name a 2026-08-29 phase-3 import reached
        # for); must behave exactly like batch().
        with self._wdb() as wdb, wdb.transaction():
            wdb.add_tag(1, "Audited")
            wdb.update_title(1, "New Name")
        self.assertEqual(
            self._sql2("SELECT title FROM books WHERE id=1"), [("New Name",)]
        )
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(1,)])
        self.assertEqual(self._sql2("SELECT book FROM metadata_dirtied"), [(1,)])

    def test_transaction_alias_rolls_back_on_failure(self):
        with self._wdb() as wdb, self.assertRaises(ValueError), wdb.transaction():
            wdb.add_tag(1, "Audited")
            wdb.add_tag(999, "Never")  # unknown book raises mid-batch
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM books_tags_link"), [(0,)])
        self.assertEqual(self._sql2("SELECT COUNT(*) FROM metadata_dirtied"), [(0,)])


class TestSetPubdate(TestWriteSideExpansion):
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
