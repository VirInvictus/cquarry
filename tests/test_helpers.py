"""Tests for pure helpers: rating display, series-gap detection, image sizing."""

import os
import sqlite3
import struct
import tempfile
import unittest

from cquarry.helpers import (
    calibre_rating_to_stars,
    db_uri_ro,
    detect_series_gaps,
    format_stars,
    get_image_size,
    get_jpeg_size,
    get_png_size,
)


class TestDbUri(unittest.TestCase):
    def test_plain_path_keeps_its_separators(self):
        self.assertEqual(
            db_uri_ro("/home/x/Calibre Library/metadata.db"),
            "file:/home/x/Calibre%20Library/metadata.db?mode=ro",
        )

    def test_uri_syntax_in_the_path_is_escaped(self):
        # '#' starts a fragment and '?' starts a query: unescaped, SQLite opens
        # a different file than the one asked for.
        self.assertEqual(
            db_uri_ro("/lib/Books #2/m.db"), "file:/lib/Books%20%232/m.db?mode=ro"
        )
        self.assertIn("%3F", db_uri_ro("/lib/what?ever/m.db"))

    def test_it_is_always_read_only(self):
        self.assertTrue(db_uri_ro("/x/m.db").endswith("?mode=ro"))

    def test_a_database_under_such_a_path_actually_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            odd = os.path.join(tmp, "Books #2")
            os.makedirs(odd)
            path = os.path.join(odd, "metadata.db")
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE books (id INTEGER)")
            con.execute("INSERT INTO books VALUES (7)")
            con.commit()
            con.close()
            ro = sqlite3.connect(db_uri_ro(path), uri=True)
            try:
                self.assertEqual(ro.execute("SELECT id FROM books").fetchone()[0], 7)
            finally:
                ro.close()


class TestRatings(unittest.TestCase):
    def test_internal_to_stars(self):
        self.assertIsNone(calibre_rating_to_stars(None))
        self.assertIsNone(calibre_rating_to_stars(0))
        self.assertEqual(calibre_rating_to_stars(10), 5.0)
        self.assertEqual(calibre_rating_to_stars(5), 2.5)

    def test_half_star_is_visually_distinct(self):
        # The 2.5 half must not render identically to 2.0 (the old bug).
        self.assertNotEqual(format_stars(2.0), format_stars(2.5))
        self.assertIn("½", format_stars(2.5))
        self.assertNotIn("½", format_stars(2.0))

    def test_none_rating(self):
        self.assertEqual(format_stars(None), "")


class TestSeriesGaps(unittest.TestCase):
    def test_no_gaps(self):
        self.assertEqual(detect_series_gaps("1.0,2.0,3.0", 3.0), [])

    def test_gap(self):
        self.assertEqual(detect_series_gaps("1.0,2.0,4.0", 4.0), [3])

    def test_novellas_ignored(self):
        # Half-index novellas do not create phantom gaps.
        self.assertEqual(detect_series_gaps("0.5,1.0,2.0,2.5,3.0", 3.0), [])

    def test_empty(self):
        self.assertEqual(detect_series_gaps("", None), [])


def _write(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".img")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _jpeg(width: int, height: int, lead: bytes = b"") -> bytes:
    """A minimal JPEG: SOI, an optional leading segment, then an SOF0 frame."""
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
    )
    return b"\xff\xd8" + lead + sof + b"\xff\xd9"


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


class TestImageSizing(unittest.TestCase):
    def test_jpeg_basic(self):
        p = _write(_jpeg(640, 400))
        try:
            self.assertEqual(get_jpeg_size(p), (640, 400))
        finally:
            os.unlink(p)

    def test_jpeg_sof_past_1kb(self):
        # A large APP1 (EXIF) block ahead of the SOF would defeat a fixed 1 KB
        # header read; the seeking scanner must still find the dimensions.
        big = b"\xff\xe1" + struct.pack(">H", 2000) + (b"\x00" * 1998)
        p = _write(_jpeg(1200, 800, lead=big))
        try:
            self.assertEqual(get_jpeg_size(p), (1200, 800))
            self.assertEqual(get_image_size(p), (1200, 800))
        finally:
            os.unlink(p)

    def test_png(self):
        p = _write(_png(300, 500))
        try:
            self.assertEqual(get_png_size(p), (300, 500))
            self.assertEqual(get_image_size(p), (300, 500))
        finally:
            os.unlink(p)

    def test_not_an_image(self):
        p = _write(b"not an image at all")
        try:
            self.assertIsNone(get_image_size(p))
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
