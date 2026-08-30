import json
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


class TestCommentsAccess(TestCalibreDB):
    """Rows deliberately omit comment text (it can be huge).

    get_book(include_comments=True) and get_comments() are the two
    sanctioned reads; anything rendering must pass strip_html() first.
    """

    def _seed_comment(self):
        self.conn.execute("INSERT INTO comments (book, text) VALUES (1, '<p>Hi</p>')")
        self.conn.commit()

    def test_get_book_omits_comments_by_default(self):
        self._seed_comment()
        db = CalibreDB(self.db_path)
        self.assertNotIn("comments", db.get_book(1))
        db.close()

    def test_get_book_include_comments(self):
        self._seed_comment()
        db = CalibreDB(self.db_path)
        book = db.get_book(1, include_comments=True)
        self.assertEqual(book["comments"], "<p>Hi</p>")
        db.close()

    def test_get_comments_bulk_and_single(self):
        self._seed_comment()
        db = CalibreDB(self.db_path)
        self.assertEqual(db.get_comments(), {1: "<p>Hi</p>"})
        self.assertEqual(db.get_comments(1), {1: "<p>Hi</p>"})
        self.assertEqual(db.get_comments(2), {})
        db.close()

    def test_get_comments_absent_table_degrades(self):
        self.conn.execute("DROP TABLE comments")
        self.conn.commit()
        db = CalibreDB(self.db_path)
        self.assertEqual(db.get_comments(), {})
        self.assertEqual(db.get_comments(1), {})
        db.close()


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


_SCHEMA_V28 = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
    timestamp TEXT, pubdate TEXT, has_cover INT, last_modified TEXT,
    series_index REAL DEFAULT 1.0, path TEXT, uuid TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INT, author INT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, link TEXT);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INT, tag INT);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INT, series INT);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INT, publisher INT);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT, link TEXT);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INT, lang_code INT);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INT);
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INT, rating INT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INT, format TEXT, name TEXT, uncompressed_size INT);
CREATE TABLE identifiers (book INT, type TEXT, val TEXT);
CREATE TABLE comments (book INT, text TEXT);
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY, book INT, format TEXT, user_type TEXT, user TEXT,
    timestamp TEXT, annot_id TEXT, annot_type TEXT, annot_data TEXT,
    searchable_text TEXT
);
CREATE TABLE custom_columns (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT,
    editable BOOL, display TEXT, is_multiple BOOL DEFAULT 0,
    normalized BOOL DEFAULT 0
);
CREATE TABLE custom_column_1 (
    id INTEGER PRIMARY KEY, value TEXT, link TEXT DEFAULT ''
);
CREATE TABLE books_custom_column_1_link (book INT, value INT);
"""


class TestReadSideV14(unittest.TestCase):
    """Author/entity secondary columns, custom-column display config, the
    generic preferences accessor, grouped-search expansion and the
    ``annotations:`` location."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        con = sqlite3.connect(os.path.join(self.temp_dir, "metadata.db"))
        con.executescript(_SCHEMA_V28)
        con.executemany(
            "INSERT INTO books (id,title,sort,path) VALUES (?,?,?,?)",
            [
                (1, "Ancillary Justice", "Ancillary Justice", "a/aj (1)"),
                (2, "Dune", "Dune", "b/dune (2)"),
            ],
        )
        # Author with true sort key and an author-page URL; second author bare.
        con.executemany(
            "INSERT INTO authors VALUES (?,?,?,?)",
            [
                (1, "Ann Leckie", "Leckie, Ann", "https://example.com/leckie"),
                (2, "Frank Herbert", "", ""),
            ],
        )
        con.executemany(
            "INSERT INTO books_authors_link (book,author) VALUES (?,?)",
            [(1, 1), (2, 2)],
        )
        con.execute("INSERT INTO tags (name, link) VALUES ('Fic.SciFi', '')")
        con.executemany(
            "INSERT INTO books_tags_link (book,tag) VALUES (?,1)", [(1,), (2,)]
        )
        con.execute(
            "INSERT INTO publishers (name,sort,link) VALUES ('Orbit','','https://orbit')"
        )
        con.execute("INSERT INTO books_publishers_link (book,publisher) VALUES (1,1)")
        # Enumeration column with display config (enum values + colors).
        display_json = (
            '{"enum_values": ["Read", "Reading"], "enum_colors": {"Read": "#00ff00"}}'
        )
        con.execute(
            "INSERT INTO custom_columns VALUES "
            "(1,'status','Status','enumeration',1,?,0,1)",
            (display_json,),
        )
        con.execute("INSERT INTO custom_column_1 (value) VALUES ('Read')")
        con.execute("INSERT INTO books_custom_column_1_link VALUES (1,1)")
        # Annotations feed the annotations: location.
        con.execute(
            "INSERT INTO annotations (book, format, searchable_text, annot_data) "
            "VALUES (1, 'EPUB', 'the night dye was still wet on her hands', '{}')"
        )
        # Preferences exercising the typed accessor.
        prefs = {
            "grouped_search_terms": {"People": ["authors", "series"]},
            "user_categories": {"Favorites": [{"name": "Fic.SciFi", "label": ":tags"}]},
            "tag_browser_category_order": ["authors", "tags"],
            "tag_browser_hidden_categories": ["languages"],
            "field_metadata": {"#status": {"datatype": "enumeration", "colnum": 1}},
            "plain_note": "just a string",
        }
        for key, val in prefs.items():
            con.execute(
                "INSERT INTO preferences (key,val) VALUES (?,?)",
                (key, json.dumps(val)),
            )
        con.commit()
        con.close()
        self.db = CalibreDB(os.path.join(self.temp_dir, "metadata.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_author_sort_and_link_parallel_arrays(self):
        b = self.db.get_book(1)
        self.assertEqual(b["authors"], ["Ann Leckie"])
        self.assertEqual(b["author_sorts"], ["Leckie, Ann"])
        self.assertEqual(b["author_links"], ["https://example.com/leckie"])
        row = {x["id"]: x for x in self.db.get_all_books()}[2]
        self.assertEqual(row["author_sorts"], [""])  # author has no sort set

    def test_get_entities_shapes(self):
        authors = self.db.get_entities("authors")
        leckie = next(a for a in authors if a["name"] == "Ann Leckie")
        self.assertEqual(leckie["count"], 1)
        self.assertEqual(leckie["sort"], "Leckie, Ann")
        self.assertEqual(leckie["link"], "https://example.com/leckie")
        tags = self.db.get_entities("tags")
        self.assertEqual(tags[0]["name"], "Fic.SciFi")
        self.assertEqual(tags[0]["count"], 2)
        pubs = self.db.get_entities("publishers")
        self.assertEqual(pubs[0]["link"], "https://orbit")

    def test_get_entities_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            self.db.get_entities("nope")

    def test_custom_column_display_config(self):
        cols = self.db.get_custom_columns()
        status = cols["Status"]
        self.assertTrue(status["editable"])
        self.assertTrue(status["normalized"])
        self.assertEqual(status["display"]["enum_values"], ["Read", "Reading"])
        self.assertEqual(status["display"]["enum_colors"], {"Read": "#00ff00"})

    def test_preferences_accessor(self):
        self.assertEqual(self.db.get_preference("plain_note"), "just a string")
        self.assertEqual(
            self.db.get_preference("user_categories"),
            {"Favorites": [{"name": "Fic.SciFi", "label": ":tags"}]},
        )
        self.assertIsNone(self.db.get_preference("missing"))
        self.assertEqual(self.db.get_preference("missing", 7), 7)

    def test_typed_preference_helpers(self):
        fm = self.db.get_field_metadata()
        self.assertEqual(fm["#status"]["colnum"], 1)
        self.assertEqual(
            self.db.get_grouped_search_terms(), {"People": ["authors", "series"]}
        )
        self.assertIn("Favorites", self.db.get_user_categories())
        state = self.db.get_tag_browser_state()
        self.assertEqual(state["order"], ["authors", "tags"])
        self.assertEqual(state["hidden"], ["languages"])

    def test_grouped_search_expansion(self):
        # People covers authors AND series: Leckie matches via authors,
        # Herbert via authors; "dune" is a title and matches neither member.
        self.assertEqual(self.db.search("People:leckie"), {1})
        self.assertEqual(self.db.search("people:herbert"), {2})
        self.assertEqual(self.db.search("People:dune"), set())
        # Union semantics: a term matching either member finds both books.
        both = self.db.search("(People:leckie or People:herbert)")
        self.assertEqual(both, {1, 2})

    def test_grouped_search_false_inverts(self):
        self.assertEqual(self.db.search("People:false"), set())
        self.assertEqual(self.db.search("not People:false"), {1, 2})

    def test_annotations_location_search(self):
        self.assertEqual(self.db.search('annotations:"dye was still wet"'), {1})
        self.assertEqual(self.db.search("annotations:wet"), {1})
        self.assertEqual(self.db.search("annotations:true"), {1})
        self.assertNotIn(1, self.db.search("annotations:false"))

    def test_bare_terms_do_not_scan_annotations(self):
        # Calibre's all-location excludes annotation text; 'wet' lives only
        # in an annotation, so a bare term must not match book 1 through it.
        self.assertEqual(self.db.search("wet"), set())


_SCHEMA_V16 = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, author_sort TEXT,
    timestamp TEXT, pubdate TEXT, has_cover INT, last_modified TEXT,
    series_index REAL DEFAULT 1.0, path TEXT, uuid TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INT, author INT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, link TEXT);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INT, tag INT);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INT, series INT);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INT, publisher INT);
CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT, link TEXT);
CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY, book INT, lang_code INT,
    item_order INT NOT NULL DEFAULT 0);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INT, link TEXT DEFAULT '');
CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY, book INT, rating INT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INT, format TEXT, name TEXT,
    uncompressed_size INT);
CREATE TABLE identifiers (book INT, type TEXT, val TEXT);
CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
    datatype TEXT, editable BOOL, display TEXT, is_multiple BOOL DEFAULT 0,
    normalized BOOL DEFAULT 0);
CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, value TEXT, link TEXT DEFAULT '');
CREATE TABLE books_custom_column_2_link (book INT, value INT);
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
CREATE TABLE feeds (id INTEGER PRIMARY KEY, title TEXT, script TEXT);
CREATE TABLE annotations_dirtied (id INTEGER PRIMARY KEY, book INT, UNIQUE(book));
CREATE VIEW tag_browser_tags AS SELECT
    id, name,
    (SELECT COUNT(id) FROM books_tags_link WHERE tag=tags.id) count,
    (SELECT AVG(ratings.rating) FROM books_tags_link AS tl,
            books_ratings_link AS bl, ratings
     WHERE tl.tag=tags.id AND bl.book=tl.book AND ratings.id = bl.rating
       AND ratings.rating <> 0) avg_rating,
    name AS sort
 FROM tags;
CREATE VIEW tag_browser_filtered_tags AS SELECT
    id, name,
    (SELECT COUNT(id) FROM books_tags_link WHERE tag=tags.id
       AND books_list_filter(book)) count,
    0.0 AS avg_rating,
    name AS sort
 FROM tags;
CREATE VIEW tag_browser_custom_column_2 AS SELECT
    id, value,
    (SELECT COUNT(id) FROM books_custom_column_2_link
      WHERE value=custom_column_2.id) count,
    0.0 AS avg_rating,
    value AS sort
 FROM custom_column_2;
CREATE VIEW tag_browser_series AS SELECT
    id, name,
    (SELECT COUNT(id) FROM books_series_link WHERE series=series.id) count,
    0.0 AS avg_rating,
    (title_sort(name)) AS sort
 FROM series;
CREATE VIEW tag_browser_ratings AS SELECT
    id, rating,
    (SELECT COUNT(id) FROM books_ratings_link WHERE rating=ratings.id) count,
    0.0 AS avg_rating,
    rating AS sort
 FROM ratings;
"""


class TestReadSideV16(unittest.TestCase):
    """Completeness mining: feeds, annotations_dirtied, tag_browser views,
    the ratings entity kind, row-shape parity, and language item_order."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        path = os.path.join(self.temp_dir, "metadata.db")
        con = sqlite3.connect(path)
        con.executescript(_SCHEMA_V16)
        con.executemany(
            "INSERT INTO books (id,title,sort,path,uuid) VALUES (?,?,?,?,?)",
            [
                (1, "Alpha", "Alpha", "a/alpha (1)", "uuid-1"),
                (2, "Beta", "Beta", "b/beta (2)", None),
            ],
        )
        con.execute("INSERT INTO authors (name) VALUES ('A. Author')")
        con.executemany(
            "INSERT INTO books_authors_link (book,author) VALUES (?,1)", [(1,), (2,)]
        )
        con.executemany(
            "INSERT INTO tags (id,name) VALUES (?,?)", [(1, "Fic"), (2, "Solo")]
        )
        con.executemany(
            "INSERT INTO books_tags_link (book,tag) VALUES (?,1)", [(1,), (2,)]
        )
        # Two languages where link-id order deliberately contradicts item_order.
        con.executemany(
            "INSERT INTO languages (id,lang_code) VALUES (?,?)",
            [(1, "eng"), (2, "fra")],
        )
        con.executemany(
            "INSERT INTO books_languages_link (id,book,lang_code,item_order) VALUES (?,?,?,?)",
            [(1, 1, 2, 1), (2, 1, 1, 0)],
        )
        # Ratings: rating 4 shared by both books, rating 2 on book 2 only.
        con.executemany(
            "INSERT INTO ratings (id,rating,link) VALUES (?,?,?)",
            [(1, 4, ""), (2, 2, "https://x")],
        )
        con.executemany(
            "INSERT INTO books_ratings_link (book,rating) VALUES (?,?)",
            [(1, 1), (2, 1), (2, 2)],
        )
        con.execute(
            "INSERT INTO data (book,format,name,uncompressed_size) VALUES (1,'EPUB','alpha',1024)"
        )
        con.execute("INSERT INTO identifiers (book,type,val) VALUES (1,'isbn','123')")
        con.execute(
            "INSERT INTO custom_columns VALUES (2,'status','Status','enumeration',1,'{}',0,1)"
        )
        con.execute("INSERT INTO custom_column_2 (value) VALUES ('Read')")
        con.execute("INSERT INTO books_custom_column_2_link VALUES (1,1)")
        con.execute("INSERT INTO series (id,name) VALUES (1,'The Culture')")
        con.execute("INSERT INTO books_series_link (book,series) VALUES (2,1)")
        con.executemany(
            "INSERT INTO feeds (title,script) VALUES (?,?)",
            [("Zed Feed", "z"), ("Abc Feed", "a")],
        )
        con.executemany(
            "INSERT INTO annotations_dirtied (book) VALUES (?)", [(2,), (1,)]
        )
        con.commit()
        con.close()
        self.db = CalibreDB(path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def _old_schema_db(self):
        path = os.path.join(self.temp_dir, "old.db")
        con = sqlite3.connect(path)
        con.executescript(_SCHEMA_V28)
        con.commit()
        con.close()
        return CalibreDB(path)

    def test_get_feeds_sorted_nocase(self):
        feeds = self.db.get_feeds()
        self.assertEqual([f["title"] for f in feeds], ["Abc Feed", "Zed Feed"])
        self.assertEqual(feeds[0]["script"], "a")

    def test_get_annotations_dirtied_books(self):
        self.assertEqual(self.db.get_annotations_dirtied_books(), [1, 2])

    def test_get_tag_browser_counts(self):
        counts = self.db.get_tag_browser_counts()
        # The filtered_* view (books_list_filter) must be skipped; native and
        # custom views read, custom rekeyed to the #label location. The
        # series view's title_sort() dependency is satisfied locally and the
        # ratings view's rating-column spelling is handled.
        self.assertEqual(set(counts), {"tags", "#status", "series", "ratings"})
        fic = next(r for r in counts["tags"] if r["name"] == "Fic")
        self.assertEqual(fic["count"], 2)
        # AVG over the joined rows: book 1 rated 4, book 2 rated 4 AND 2.
        self.assertEqual(fic["avg_rating"], 10 / 3)
        solo = next(r for r in counts["tags"] if r["name"] == "Solo")
        self.assertEqual(solo["count"], 0)
        self.assertIsNone(solo["avg_rating"])
        self.assertEqual(
            counts["#status"],
            [{"id": 1, "name": "Read", "count": 1, "avg_rating": 0.0, "sort": "Read"}],
        )
        self.assertEqual(
            counts["series"],
            [
                {
                    "id": 1,
                    "name": "The Culture",
                    "count": 1,
                    "avg_rating": 0.0,
                    "sort": "Culture, The",
                }
            ],
        )
        self.assertEqual(
            [(r["name"], r["count"]) for r in counts["ratings"]], [("2", 1), ("4", 2)]
        )

    def test_get_tag_browser_counts_degrades_without_views(self):
        db = self._old_schema_db()
        try:
            self.assertEqual(db.get_tag_browser_counts(), {})
        finally:
            db.close()

    def test_get_feeds_and_dirtied_degrade_without_tables(self):
        db = self._old_schema_db()
        try:
            self.assertEqual(db.get_feeds(), [])
            self.assertEqual(db.get_annotations_dirtied_books(), [])
        finally:
            db.close()

    def test_ratings_entity_kind(self):
        ratings = self.db.get_entities("ratings")
        self.assertEqual(
            [(r["name"], r["count"]) for r in ratings],
            [("2", 1), ("4", 2)],  # numeric order, half-star int as text
        )
        self.assertEqual(ratings[0]["link"], "https://x")
        with self.assertRaises(ValueError):
            self.db.get_entities("nope")

    def test_row_shape_parity_between_get_book_and_get_all_books(self):
        row = {b["id"]: b for b in self.db.get_all_books()}[1]
        book = self.db.get_book(1)
        self.assertEqual(sorted(row.keys()), sorted(book.keys()))
        for key in ("size", "uuid", "identifiers", "pages", "languages"):
            self.assertEqual(row[key], book[key], key)
        self.assertEqual(row["uuid"], "uuid-1")
        self.assertIsNone(self.db.get_book(2)["uuid"] or None)
        self.assertEqual(book["identifiers"], {"isbn": "123"})
        self.assertEqual(row["size"], 1024)

    def test_languages_ordered_by_item_order(self):
        # Link ids say fra(1), eng(2); item_order says eng first.
        self.assertEqual(self.db.get_book(1)["languages"], ["eng", "fra"])
        row = {b["id"]: b for b in self.db.get_all_books()}[1]
        self.assertEqual(row["languages"], ["eng", "fra"])

    def test_languages_old_schema_falls_back_to_link_order(self):
        db = self._old_schema_db()
        try:
            con = sqlite3.connect(db.db_path)
            con.execute("INSERT INTO books (id,title) VALUES (1,'Old Book')")
            con.executemany(
                "INSERT INTO languages (id,lang_code) VALUES (?,?)",
                [(1, "eng"), (2, "fra")],
            )
            con.executemany(
                "INSERT INTO books_languages_link (id,book,lang_code) VALUES (?,?,?)",
                [(1, 1, 2), (2, 1, 1)],  # fra first by link id
            )
            con.commit()
            con.close()
            self.assertEqual(db.get_book(1)["languages"], ["fra", "eng"])
        finally:
            db.close()


class TestDossierAndPathIndex(unittest.TestCase):
    """Phase 9: get_book_dossier (the composed deep fetch) and the
    format-path index (every catalogued format path → book id)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        con = sqlite3.connect(os.path.join(self.temp_dir, "metadata.db"))
        con.executescript(_SCHEMA_V27)
        # Extractor tables the dossier composes; empty ones degrade to [].
        con.executescript(
            """
            CREATE TABLE annotations (id INTEGER PRIMARY KEY, book INT,
                format TEXT, user_type TEXT, user TEXT, timestamp TEXT,
                annot_id TEXT, annot_type TEXT, annot_data TEXT,
                searchable_text TEXT);
            CREATE TABLE last_read_positions (id INTEGER PRIMARY KEY, book INT,
                format TEXT, user TEXT, device TEXT, cfi TEXT, epoch INT,
                pos_frac REAL);
            CREATE TABLE books_plugin_data (book INT, name TEXT, val TEXT);
            CREATE TABLE conversion_options (book INT, format TEXT, data BLOB);
            """
        )
        con.executemany(
            "INSERT INTO books (id,title,sort,path,has_cover) VALUES (?,?,?,?,?)",
            [(1, "Dossier Book", "Dossier Book", "Auth/Dossier Book (1)", 1)],
        )
        con.execute("INSERT INTO authors VALUES (1, 'Auth', 'Auth, A')")
        con.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
        con.execute(
            "INSERT INTO data (book, format, name, uncompressed_size)"
            " VALUES (1, 'EPUB', 'dossier', 2048)"
        )
        con.execute(
            "INSERT INTO comments (book, text) VALUES (1, '<p>Deep &amp; rich.</p>')"
        )
        con.execute(
            "INSERT INTO annotations (book, format, searchable_text, annot_data)"
            " VALUES (1, 'EPUB', 'a highlight', '{}')"
        )
        con.execute(
            "INSERT INTO last_read_positions (book, format, user, device, cfi,"
            " epoch, pos_frac) VALUES (1, 'EPUB', 'brandon', 'kobo', 'cfi/2', 7, 0.5)"
        )
        con.execute(
            "INSERT INTO books_plugin_data (book, name, val) VALUES (1, 'wordcount', '99000')"
        )
        con.execute(
            "INSERT INTO conversion_options (book, format, data) VALUES (1, 'EPUB', X'00')"
        )
        # A Pattern-B int custom column.
        con.execute(
            "INSERT INTO custom_columns (id, label, name, datatype, is_multiple)"
            " VALUES (1, 'pages_orig', 'Original Pages', 'int', 0)"
        )
        con.execute("CREATE TABLE custom_column_1 (book INT, value INT)")
        con.execute("INSERT INTO custom_column_1 (book, value) VALUES (1, 611)")
        # A cover file on disk so the dossier's cover_path resolves.
        os.makedirs(os.path.join(self.temp_dir, "Auth", "Dossier Book (1)"))
        with open(
            os.path.join(self.temp_dir, "Auth", "Dossier Book (1)", "cover.jpg"), "wb"
        ) as f:
            f.write(b"\xff\xd8fake")
        con.commit()
        con.close()
        self.db = CalibreDB(os.path.join(self.temp_dir, "metadata.db"))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_dossier_composes_everything(self):
        d = self.db.get_book_dossier(1)
        self.assertIsNotNone(d)
        self.assertEqual(d["book"]["title"], "Dossier Book")
        self.assertEqual(
            d["cover_path"],
            os.path.join(self.temp_dir, "Auth", "Dossier Book (1)", "cover.jpg"),
        )
        self.assertEqual(d["formats"]["EPUB"]["name"], "dossier")
        self.assertEqual(
            d["custom_columns"],
            {
                "#pages_orig": {
                    "name": "Original Pages",
                    "datatype": "int",
                    "value": 611,
                }
            },
        )
        self.assertEqual(len(d["annotations"]), 1)
        self.assertEqual(d["reading_positions"][0]["device"], "kobo")
        self.assertEqual(d["plugin_data"][0]["name"], "wordcount")
        self.assertEqual(len(d["conversion_overrides"]), 1)
        self.assertNotIn("comments", d)

    def test_dossier_comments_flag(self):
        d = self.db.get_book_dossier(1, include_comments=True)
        self.assertEqual(d["comments"]["html"], "<p>Deep &amp; rich.</p>")
        self.assertEqual(d["comments"]["plain"], "Deep & rich.")

    def test_dossier_unknown_book_is_none(self):
        self.assertIsNone(self.db.get_book_dossier(999))

    def test_format_path_index_and_lookup(self):
        idx = self.db.format_path_index()
        expected = os.path.join(
            self.temp_dir, "Auth", "Dossier Book (1)", "dossier.epub"
        )
        self.assertEqual(idx[os.path.normcase(os.path.normpath(expected))], 1)
        # Reverse lookup survives redundant separators and dot segments.
        noisy = os.path.join(
            self.temp_dir, "Auth", ".", "Dossier Book (1)", "", "dossier.epub"
        )
        self.assertEqual(self.db.find_book_by_path(noisy), 1)
        # A relative spelling resolves against the process cwd only via
        # abspath — a nonexistent absolute path just misses.
        self.assertIsNone(self.db.find_book_by_path("/nowhere/thick.epub"))


if __name__ == "__main__":
    unittest.main()
