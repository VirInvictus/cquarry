"""Tests for the Phase 9 analytics derivations (cquarry.analytics).

The fixture mirrors test_integrity.py's synthetic library (timestamps,
ratings, tags) plus a preferences table carrying two virtual libraries so
vl_overlap can compose real resolutions.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from cquarry.analytics import (
    addition_timeline,
    author_stats,
    rating_distribution,
    vl_overlap,
)
from cquarry.db import CalibreDB


class TestAnalytics(unittest.TestCase):
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
        rows = [
            (1, "Alpha", "Alice", "2024-01-15", 1),
            (2, "Beta", "Bob", "2024-01-20", 0),
            (3, "Gamma", "Alice", "2024-03-05", 0),
            (4, "Delta", None, None, 0),  # no author, no timestamp
        ]
        for bid, title, author, ts, cover in rows:
            c.execute(
                "INSERT INTO books (id, title, sort, author_sort, timestamp,"
                " pubdate, last_modified, series_index, path, has_cover)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?)",
                (
                    bid,
                    title,
                    title,
                    author or "Unknown",
                    ts,
                    ts or "",
                    ts or "",
                    f"p{bid}",
                    cover,
                ),
            )
        c.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO authors (id, name) VALUES (1, 'Alice'), (2, 'Bob')")
        c.execute(
            "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, author INTEGER)"
        )
        c.executemany(
            "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
            [(1, 1), (2, 2), (3, 1)],
        )
        c.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO tags (id, name) VALUES (1, 'Fic')")
        c.execute(
            "CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, tag INTEGER)"
        )
        c.executemany(
            "INSERT INTO books_tags_link (book, tag) VALUES (?, 1)", [(1,), (3,)]
        )
        c.execute("CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER)")
        c.execute(
            "CREATE TABLE books_ratings_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, rating INTEGER)"
        )
        c.executemany(
            "INSERT INTO ratings (id, rating) VALUES (?, ?)", [(1, 8), (2, 6)]
        )
        c.executemany(
            "INSERT INTO books_ratings_link (book, rating) VALUES (?, ?)",
            [(1, 1), (2, 2)],
        )
        c.execute(
            "CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER,"
            " format TEXT, uncompressed_size INTEGER, name TEXT)"
        )
        c.executemany(
            "INSERT INTO data (book, format, uncompressed_size, name) VALUES (?, ?, 1, ?)",
            [(1, "EPUB", "One"), (3, "MOBI", "Three")],
        )
        c.execute(
            "CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT)"
        )
        c.execute(
            "INSERT INTO preferences (key, val) VALUES ('virtual_libraries', ?)",
            (json.dumps({"Wing A": "tags:Fic", "Wing B": "rating:>3"}),),
        )
        # The search engine's identifier path queries this directly.
        c.execute("CREATE TABLE identifiers (book INT, type TEXT, val TEXT)")
        # Tables _BOOK_SELECT joins and the enrichment probes expect; empty
        # here, present so get_all_books() works on the synthetic schema.
        c.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT, sort TEXT)")
        c.execute(
            "CREATE TABLE books_series_link (id INTEGER PRIMARY KEY,"
            " book INTEGER, series INTEGER)"
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
        c.execute(
            "CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER, text TEXT)"
        )
        self.conn.commit()
        self.db = CalibreDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.conn.close()
        shutil.rmtree(self.temp_dir)

    def test_addition_timeline_month(self):
        self.assertEqual(addition_timeline(self.db), {"2024-01": 2, "2024-03": 1})

    def test_addition_timeline_year_skips_untimestamped(self):
        self.assertEqual(addition_timeline(self.db, granularity="year"), {"2024": 3})

    def test_addition_timeline_bad_granularity_raises(self):
        with self.assertRaises(ValueError):
            addition_timeline(self.db, granularity="week")

    def test_author_stats_count_desc_then_name(self):
        stats = author_stats(self.db)
        # Alice: 2 books, rated 8 → 4.0 stars only (book 3 unrated), EPUB+MOBI.
        # Bob: 1 book, 6 → 3.0, no formats. The authorless book 4 is skipped.
        self.assertEqual(
            stats,
            [
                {
                    "author": "Alice",
                    "book_count": 2,
                    "avg_rating": 4.0,
                    "rated_count": 1,
                    "formats": ["EPUB", "MOBI"],
                },
                {
                    "author": "Bob",
                    "book_count": 1,
                    "avg_rating": 3.0,
                    "rated_count": 1,
                    "formats": [],
                },
            ],
        )

    def test_authorless_books_are_skipped(self):
        # Book 4 has no author link, so it lands in no author's rollup;
        # Alice(2) + Bob(1) account for every authorable book.
        self.assertEqual(sum(s["book_count"] for s in author_stats(self.db)), 3)

    def test_rating_distribution_ascending_unrated_last(self):
        dist = rating_distribution(self.db)
        self.assertEqual(dist, {3.0: 1, 4.0: 1, "unrated": 2})
        self.assertEqual(list(dist)[-1], "unrated")

    def test_vl_overlap_multi_wing_only(self):
        # Wing A = tags:Fic → books 1, 3. Wing B = rating:>3 → books 1, 2
        # (8 and 6 on the 0-10 scale). Only book 1 is in both.
        self.assertEqual(vl_overlap(self.db), {("Wing A", "Wing B"): [1]})

    def test_vl_overlap_names_restrict_and_unknown_raises(self):
        self.assertEqual(vl_overlap(self.db, ["Wing A"]), {})
        with self.assertRaises(ValueError):
            vl_overlap(self.db, ["No Such Wing"])


if __name__ == "__main__":
    unittest.main()
