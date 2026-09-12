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

    def test_remove_entity_everywhere_series_nulls_indices(self):
        self._exec("INSERT INTO series (id, name) VALUES (1, 'Dune Saga')")
        self._exec("UPDATE books SET series_index = 3 WHERE id = 1")
        self._link(1, "series", "series", 1)
        self._link(2, "series", "series", 1)
        with self._wdb() as wdb:
            n = wdb.remove_entity_everywhere("series", "Dune Saga")
        self.assertEqual(n, 2)
        self.assertEqual(
            self._sql2("SELECT series_index FROM books WHERE id IN (1, 2)"),
            [(None,), (None,)],
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
