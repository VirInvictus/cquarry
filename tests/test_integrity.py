"""Tests for the Phase 9 integrity predicates (cquarry.integrity).

Pure functions over the cached rows; the fixture below is the same synthetic
library style test_db.py uses, extended with cover files on disk (minimal
PNGs, which get_image_size can sniff) and a gapped series.
"""

import os
import sqlite3
import tempfile
import unittest

from cquarry.db import CalibreDB
from cquarry.integrity import (
    find_authorless,
    find_coverless,
    find_deprecated_formats,
    find_duplicate_books,
    find_formatless,
    find_low_res_covers,
    find_missing_cover_files,
    find_series_gaps,
    find_unrated,
    find_untagged,
)


def _png(width: int, height: int) -> bytes:
    """A minimal PNG good enough for get_png_size (sig + IHDR)."""
    import struct

    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", 0)
    )


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "metadata.db")
        self.conn = sqlite3.connect(self.db_path)
        c = self.conn
        c.execute(
            "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT,"
            " author_sort TEXT, timestamp TEXT, pubdate TEXT, last_modified TEXT,"
            " series_index REAL, path TEXT, has_cover INTEGER)"
        )
        # 1: fully clean, shares title+author with 4 and 5 (duplicate group),
        #    gapped series member (index 1 of 1..3).
        # 2: untagged, unrated, authorless (Unknown), MOBI-only (deprecated),
        #    coverless.
        # 3: formatless, has_cover set but no cover file on disk, unrated.
        # 4: low-res cover (100x100), duplicate member (index 3 of 1..3).
        # 5: duplicate member with no series.
        rows = [
            (1, "Shared Title", "Alice", "2024-01-15", "p1", 1),
            (2, "Other", "Unknown", "2024-02-01", "p2", 0),
            (3, "Third", "Bob", "2024-03-05", "p3", 1),
            (4, "shared title", "Alice", "2024-04-01", "p4", 1),
            (5, "Shared Title", "Alice", "2024-05-01", "p5", 1),
        ]
        for i, (bid, title, author, ts, path, cover) in enumerate(rows):
            c.execute(
                "INSERT INTO books (id, title, sort, author_sort, timestamp,"
                " pubdate, last_modified, series_index, path, has_cover)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (bid, title, title, author, ts, ts, ts, float(i + 1), path, cover),
            )
        c.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute(
            "INSERT INTO authors (id, name) VALUES (1, 'Alice'), (2, 'Unknown'),"
            " (3, 'Bob')"
        )
        c.execute(
            "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, author INTEGER)"
        )
        c.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
            [(1, 1), (2, 2), (3, 3), (4, 1), (5, 1)],
        )
        c.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO tags (id, name) VALUES (1, 'Fic')")
        c.execute(
            "CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, tag INTEGER)"
        )
        c.executemany(
            "INSERT INTO books_tags_link (book, tag) VALUES (?, 1)", [(1,), (3,), (4,)]
        )
        c.execute("CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER)")
        c.execute(
            "CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, rating INTEGER)"
        )
        # Alice's two books: 8 and 6 → stars 4.0 and 3.0.
        c.executemany(
            "INSERT INTO ratings (id, rating) VALUES (?, ?)", [(1, 8), (2, 6)]
        )
        c.executemany(
            "INSERT INTO books_ratings_link (book, rating) VALUES (?, ?)",
            [(1, 1), (4, 2)],
        )
        c.execute(
            "CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,"
            " format TEXT, uncompressed_size INTEGER, name TEXT)"
        )
        c.executemany(
            "INSERT INTO data (book, format, uncompressed_size, name) VALUES (?, ?, 1, ?)",
            [(1, "EPUB", "One"), (2, "MOBI", "Two"), (4, "EPUB", "Four")],
        )
        c.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT)")
        c.execute("INSERT INTO series (id, name) VALUES (1, 'Gap Series')")
        c.execute(
            "CREATE TABLE books_series_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, series INTEGER)"
        )
        c.executemany(
            "INSERT INTO books_series_link (book, series, id) VALUES (?, 1, ?)",
            [(1, 1), (4, 2)],
        )
        c.execute(
            "CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT, sort TEXT)"
        )
        c.execute(
            "CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, publisher INTEGER)"
        )
        c.execute("CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT)")
        c.execute(
            "CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, lang_code INTEGER)"
        )
        # Series index lives on the books row: book 1 idx 1, book 4 idx 3
        # (gap at 2). Books 2/3/5 keep their default float indices above.
        c.execute("UPDATE books SET series_index = 1 WHERE id = 1")
        c.execute("UPDATE books SET series_index = 3 WHERE id = 4")
        c.execute("UPDATE books SET series_index = 1.5 WHERE id = 5")
        for path, blob in [("p1", _png(600, 800)), ("p4", _png(100, 100))]:
            os.makedirs(os.path.join(self.temp_dir, path), exist_ok=True)
            with open(os.path.join(self.temp_dir, path, "cover.jpg"), "wb") as f:
                f.write(blob)
        self.conn.commit()
        self.db = CalibreDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.conn.close()
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_find_untagged(self):
        # Book 5 carries no tags either; the duplicate group shares a title,
        # not a taxonomy.
        self.assertEqual(find_untagged(self.db), [2, 5])

    def test_find_unrated(self):
        # Book 3 has no ratings row; book 2 has none either ( unrated=0/None).
        self.assertEqual(find_unrated(self.db), [2, 3, 5])

    def test_find_authorless(self):
        self.assertEqual(find_authorless(self.db), [2])

    def test_find_formatless(self):
        self.assertEqual(find_formatless(self.db), [3, 5])

    def test_find_coverless(self):
        # Catalogued flag only: book 3's flag is set, so it is not here.
        self.assertEqual(find_coverless(self.db), [2])

    def test_find_missing_cover_files(self):
        # Book 3: flag set, file absent. Books 1/4: files exist. Book 5:
        # flag set but no path guard issue (path p5, file absent → flagged).
        self.assertEqual(find_missing_cover_files(self.db), [3, 5])

    def test_find_deprecated_formats(self):
        self.assertEqual(find_deprecated_formats(self.db, {"MOBI", "LIT", "LRF"}), [2])
        # EPUB-only books are deprecated-only when the caller says so.
        self.assertEqual(find_deprecated_formats(self.db, {"EPUB"}), [1, 4])
        # Case-insensitive on the caller's set.
        self.assertEqual(find_deprecated_formats(self.db, {"mobi"}), [2])

    def test_find_low_res_covers(self):
        self.assertEqual(find_low_res_covers(self.db), {4: (100, 100)})

    def test_find_duplicate_books(self):
        self.assertEqual(
            find_duplicate_books(self.db), {("shared title", "alice"): [1, 4, 5]}
        )

    def test_find_series_gaps(self):
        self.assertEqual(find_series_gaps(self.db), {"Gap Series": [2]})


if __name__ == "__main__":
    unittest.main()
