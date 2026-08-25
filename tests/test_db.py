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
            "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT, link TEXT)"
        )
        self.conn.execute(
            "INSERT INTO authors (id, name, sort, link) VALUES (1, 'Author', 'Author', '')"
        )
        self.conn.execute(
            "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER)"
        )
        self.conn.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
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
        self.conn.execute(
            "CREATE TABLE comments (id INTEGER PRIMARY KEY, book INTEGER, text TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE virtual_libraries (id INTEGER PRIMARY KEY, name TEXT, search_expression TEXT)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir)

    def test_get_all_books(self):
        db = CalibreDB(self.db_path)
        books = db.get_all_books()
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["title"], "Book 1")
        self.assertEqual(books[0]["authors"], ["Author"])


if __name__ == "__main__":
    unittest.main()
