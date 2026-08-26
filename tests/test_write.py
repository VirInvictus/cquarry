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
