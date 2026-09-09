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
from datetime import UTC, datetime, timedelta, timezone

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
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
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
        self.assertEqual(author_sort, "Ann Leckie")  # new author, sort = name
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
