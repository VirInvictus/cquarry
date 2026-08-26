import os
import shutil
import sqlite3
import tempfile
import unittest

from cquarry.db import CalibreDB


class TestCalibreDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT, series_index REAL, path TEXT, has_cover INTEGER)"
        )
        self.conn.execute(
            "INSERT INTO books (id, title, sort, author_sort, timestamp, pubdate, last_modified, series_index, path, has_cover) VALUES (1, 'Book 1', 'Book 1', 'Author', '2020-01-01', '2020-01-01', '2020-01-01', 1.0, 'path1', 1)"
        )
        self.conn.execute(
            "INSERT INTO books (id, title, sort, author_sort, timestamp, pubdate, last_modified, series_index, path, has_cover) VALUES (2, 'Book 2', 'Book 2', 'Author', '2020-02-01', '2020-02-01', '2020-02-01', 2.0, 'path2', 0)"
        )
        self.conn.execute(
            "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT)"
        )
        self.conn.execute(
            "INSERT INTO authors (id, name, sort, link) VALUES (1, 'Author', 'Author', '')"
        )
        self.conn.execute(
            "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER)"
        )
        self.conn.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, 1)", [(1,), (2,)]
        )
        self.conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.execute(
            "CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INTEGER, rating INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INTEGER, lang_code INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, uncompressed_size INTEGER, name TEXT)"
        )
        # A real format file on disk so get_format_path can verify it.
        os.makedirs(os.path.join(self.temp_dir, "path1"))
        with open(os.path.join(self.temp_dir, "path1", "BookOne.epub"), "wb") as f:
            f.write(b"EPUB")
        self.conn.execute(
            "INSERT INTO data (book, format, uncompressed_size, name)"
            " VALUES (1, 'EPUB', 2048, 'BookOne')"
        )
        # Book 2 has a catalogued PDF whose file is absent from disk.
        self.conn.execute(
            "INSERT INTO data (book, format, uncompressed_size, name)"
            " VALUES (2, 'PDF', 4096, 'BookTwo')"
        )
        self.conn.execute(
            "CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER, text TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir)

    def test_get_all_books(self):
        db = CalibreDB(self.db_path)
        books = db.get_all_books()
        self.assertEqual(len(books), 2)
        self.assertEqual(books[0]["title"], "Book 1")
        self.assertEqual(books[0]["authors"], ["Author"])
        db.close()


class TestSingleEntityAndPaths(TestCalibreDB):
    """Phase 1 APIs: get_book / search_books / get_format_path."""

    def test_get_book_single_record(self):
        db = CalibreDB(self.db_path)
        book = db.get_book(1)
        self.assertIsNotNone(book)
        self.assertEqual(book["title"], "Book 1")
        self.assertEqual(book["authors"], ["Author"])
        self.assertEqual(book["formats"], ["EPUB"])
        self.assertIsNone(db.get_book(999))
        db.close()

    def test_search_books_hydrates_matches_only(self):
        db = CalibreDB(self.db_path)
        hits = db.search_books("authors:Author and id:<2")
        self.assertEqual([b["id"] for b in hits], [1])
        self.assertEqual(db.search_books("title:Nothing"), [])
        db.close()

    def test_get_format_path_resolves_and_verifies(self):
        db = CalibreDB(self.db_path)
        path = db.get_format_path(1, "EPUB")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(os.path.join("path1", "BookOne.epub")))
        db.close()

    def test_get_format_path_unverified_allows_missing_files(self):
        db = CalibreDB(self.db_path)
        path = db.get_format_path(2, "PDF", verify=False)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(path.endswith(".pdf"))
        with self.assertRaises(ValueError):
            db.get_format_path(2, "EPUB")  # no EPUB row for book 2
        with self.assertRaises(ValueError):
            db.get_format_path(999, "EPUB")
        db.close()

    def test_preferences_defaults_without_rows(self):
        db = CalibreDB(self.db_path)
        self.assertEqual(db.get_saved_searches(), {})
        self.assertEqual(db.get_vl_ui_state(), {"hidden": [], "order": {}})
        db.close()


class TestPhase2Extractors(unittest.TestCase):
    """Annotations / reading positions / plugin data / conversion profiles."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
            INSERT INTO books VALUES (1, 'Annotated');
            CREATE TABLE annotations (
                id INTEGER PRIMARY KEY, book INTEGER, format TEXT,
                user_type TEXT, user TEXT, timestamp REAL,
                annot_id TEXT, annot_type TEXT, annot_data TEXT
            );
            INSERT INTO annotations VALUES
                (1, 1, 'EPUB', 'local', 'reader', 1750000000,
                 'a1', 'highlight', '{"text": "wise words"}');
            CREATE TABLE last_read_positions (
                id INTEGER PRIMARY KEY,
                book INTEGER NOT NULL,
                format TEXT NOT NULL COLLATE NOCASE,
                user TEXT NOT NULL,
                device TEXT NOT NULL,
                cfi TEXT NOT NULL,
                epoch REAL NOT NULL,
                pos_frac REAL NOT NULL DEFAULT 0,
                UNIQUE(user, device, book, format)
            );
            INSERT INTO last_read_positions (book, format, user, device, cfi, epoch, pos_frac)
            VALUES
                (1, 'EPUB', 'reader', 'kobo', 'epubcfi(/6/4)', 1750000000, 0.42),
                (1, 'EPUB', 'reader', 'phone', 'epubcfi(/6/9)', 1750000100, 0.90);
            CREATE TABLE books_plugin_data (book INTEGER, name TEXT, val TEXT);
            INSERT INTO books_plugin_data VALUES
                (1, 'wordcount', '98000'),
                (1, 'goodreads_id', '12345');
            CREATE TABLE conversion_options (book INTEGER, format TEXT, data BLOB);
            INSERT INTO conversion_options VALUES (1, 'EPUB', X'000102');
            CREATE TABLE metadata_dirtied (
                id INTEGER PRIMARY KEY, book INTEGER NOT NULL,
                UNIQUE(book)
            );
            INSERT INTO metadata_dirtied (book) VALUES (7), (3);
            """
        )
        conn.commit()
        conn.close()
        self.db = CalibreDB(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_annotations_decode_json_payload(self):
        notes = self.db.get_annotations(1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["annot_type"], "highlight")
        self.assertEqual(notes[0]["annot_data"], {"text": "wise words"})

    def test_reading_positions(self):
        # Real schema columns only: format/user/device/cfi/epoch/pos_frac.
        rows = self.db.get_last_read_positions(1)
        by_device = {r["device"]: r for r in rows}
        self.assertAlmostEqual(by_device["kobo"]["pos_frac"], 0.42)
        # Most-recent device is whatever has the highest epoch.
        latest = max(rows, key=lambda r: r["epoch"])
        self.assertEqual(latest["device"], "phone")
        self.assertAlmostEqual(latest["pos_frac"], 0.90)
        self.assertEqual(self.db.get_last_read_positions(999), [])

    def test_plugin_data_filters_by_name(self):
        counts = self.db.get_plugin_data(name="wordcount")
        self.assertEqual(counts, [{"book": 1, "name": "wordcount", "val": "98000"}])
        self.assertEqual(len(self.db.get_plugin_data()), 2)

    def test_conversion_profiles_surface_raw_blob(self):
        profs = self.db.get_conversion_profiles()
        self.assertEqual(profs[0]["format"], "EPUB")
        self.assertEqual(profs[0]["data_size"], 3)

    def test_dirtied_books_sorted_and_unique(self):
        # Seeded 7, 3, 7 — INSERT OR IGNORE in real libraries keeps it unique,
        # but the reader must not care: sorted ids, duplicates collapsed.
        self.assertEqual(self.db.get_dirtied_books(), [3, 7])

    def test_dirtied_books_missing_table_returns_empty(self):
        bare = os.path.join(self.temp_dir, "bare.db")
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()
        db = CalibreDB(bare)
        try:
            self.assertEqual(db.get_dirtied_books(), [])
        finally:
            db.close()


_SCHEMA_V27 = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
    timestamp TEXT, pubdate TEXT, has_cover INT, last_modified TEXT,
    series_index REAL DEFAULT 1.0, path TEXT, uuid TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INT, author INT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INT, tag INT);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INT, series INT);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INT);
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INT, rating INT);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INT, publisher INT);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INT, lang_code INT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INT, format TEXT, name TEXT, uncompressed_size INT);
CREATE TABLE identifiers (book INT, type TEXT, val TEXT);
CREATE TABLE comments (book INT, text TEXT);
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple BOOL);
CREATE TABLE books_pages_link (
    book INTEGER NOT NULL, pages INTEGER, algorithm TEXT, format TEXT,
    format_size INTEGER, timestamp TEXT, needs_scan BOOLEAN DEFAULT 0 NOT NULL);
CREATE TABLE library_id (id INTEGER PRIMARY KEY CHECK (id = 1), uuid TEXT NOT NULL);
"""


def _make_library(tmp, *, with_native_pages=True, with_custom_pages=False):
    con = sqlite3.connect(os.path.join(tmp, "metadata.db"))
    con.executescript(_SCHEMA_V27)
    con.executemany(
        "INSERT INTO books (id,title,sort,path,has_cover) VALUES (?,?,?,?,?)",
        [
            (1, "Thick Book", "Thick Book", "Author A/Thick Book (1)", 1),
            (2, "Png Cover", "Png Cover", "Author A/Png Cover (2)", 1),
            (3, "Bare Book", "Bare Book", "Author A/Bare Book (3)", 0),
        ],
    )
    con.execute("INSERT INTO authors VALUES (1, 'Author A', 'A, Author')")
    con.executemany(
        "INSERT INTO books_authors_link (book, author) VALUES (?, 1)",
        [(1,), (2,), (3,)],
    )
    con.executemany(
        "INSERT INTO data (book, format, name, uncompressed_size) VALUES (?,?,?,?)",
        [(1, "EPUB", "thick", 2048), (2, "EPUB", "pngcov", 4096)],
    )
    if with_native_pages:
        con.executemany(
            "INSERT INTO books_pages_link (book, pages, algorithm) VALUES (?,?, 'demo')",
            [(1, 512)],
        )
    if with_custom_pages:
        con.execute(
            "INSERT INTO custom_columns (label,name,datatype,is_multiple) "
            "VALUES ('pages','Pages','int',0)"
        )
        cid = con.execute(
            "SELECT id FROM custom_columns WHERE label='pages'"
        ).fetchone()[0]
        con.execute(
            f"CREATE TABLE custom_column_{cid} "
            "(id INTEGER PRIMARY KEY, book INT, value INT, link TEXT DEFAULT '')"
        )
        con.execute(f"INSERT INTO custom_column_{cid} (book, value) VALUES (2, 333)")
    con.commit()
    con.close()
    return os.path.join(tmp, "metadata.db")


class TestNativePages(unittest.TestCase):
    """Calibre manages page counts natively in books_pages_link; the custom
    column labelled 'pages' remains the fallback for older schemas."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_native_pages_in_rows_and_search(self):
        db = CalibreDB(_make_library(self.temp_dir))
        try:
            rows = {b["id"]: b for b in db.get_all_books()}
            self.assertEqual(rows[1]["pages"], 512)
            self.assertIsNone(rows[2]["pages"])
            self.assertEqual(db.search("pages:>400"), {1})
            self.assertEqual(db.field(1, "pages"), 512)
            self.assertEqual(db.get_book(1)["pages"], 512)
        finally:
            db.close()

    def test_custom_column_fallback_when_native_absent(self):
        subdir = os.path.join(self.temp_dir, "fallback")
        os.makedirs(subdir)
        db = CalibreDB(
            _make_library(subdir, with_native_pages=False, with_custom_pages=True)
        )
        try:
            rows = {b["id"]: b for b in db.get_all_books()}
            self.assertIsNone(rows[1]["pages"])  # no native row
            self.assertEqual(rows[2]["pages"], 333)  # from #pages column
            self.assertEqual(db.search("pages:>300"), {2})
        finally:
            db.close()

    def test_native_wins_over_custom_column(self):
        subdir = os.path.join(self.temp_dir, "both")
        os.makedirs(subdir)
        db = CalibreDB(
            _make_library(subdir, with_native_pages=True, with_custom_pages=True)
        )
        try:
            rows = {b["id"]: b for b in db.get_all_books()}
            self.assertEqual(rows[1]["pages"], 512)  # native, not the column
        finally:
            db.close()


class TestLibraryUuidAndFormats(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = _make_library(self.temp_dir)
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO library_id (uuid) VALUES ('lib-uuid-1234')")
        con.commit()
        con.close()
        # Real files so verify= paths resolve.
        d1 = os.path.join(self.temp_dir, "Author A", "Thick Book (1)")
        os.makedirs(d1)
        with open(os.path.join(d1, "cover.jpg"), "wb") as f:
            f.write(b"jpeg")
        d2 = os.path.join(self.temp_dir, "Author A", "Png Cover (2)")
        os.makedirs(d2)
        with open(os.path.join(d2, "cover.png"), "wb") as f:
            f.write(b"png")
        os.makedirs(os.path.join(self.temp_dir, "Author A", "Bare Book (3)"))
        self._extra = os.path.join(self.temp_dir, "extra.db")
        con = sqlite3.connect(self._extra)
        con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_library_uuid_roundtrip(self):
        db = CalibreDB(self.db_path)
        try:
            self.assertEqual(db.get_library_uuid(), "lib-uuid-1234")
        finally:
            db.close()

    def test_library_uuid_missing_everything_is_none(self):
        db = CalibreDB(self._extra)
        try:
            self.assertIsNone(db.get_library_uuid())
        finally:
            db.close()

    def test_get_formats_shape(self):
        db = CalibreDB(self.db_path)
        try:
            fmts = db.get_formats(1)
            self.assertIn("EPUB", fmts)
            entry = fmts["EPUB"]
            self.assertEqual(entry["size_bytes"], 2048)
            self.assertEqual(entry["name"], "thick")
            self.assertTrue(
                entry["path"].endswith(
                    os.path.join("Author A", "Thick Book (1)", "thick.epub")
                )
            )
            self.assertEqual(db.get_formats(999), {})
        finally:
            db.close()

    def test_get_cover_path_variants(self):
        db = CalibreDB(self.db_path)
        try:
            c1 = db.get_cover_path(1)
            self.assertTrue(c1 and c1.endswith("cover.jpg"))
            c2 = db.get_cover_path(2)  # png fallback
            self.assertTrue(c2 and c2.endswith("cover.png"))
            self.assertIsNone(db.get_cover_path(3))  # neither file exists
            unverified = db.get_cover_path(3, verify=False)
            self.assertTrue(unverified.endswith("cover.jpg"))
            with self.assertRaises(ValueError):
                db.get_cover_path(999)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
