"""Tests for the search engine: grammar (parser), matching, and DB integration.

The grammar cases are adapted from Calibre's own search_query_parser_test.py so
the parser stays faithful to Calibre's syntax. The matching battery runs against
an in-memory provider (no DB), and a final block exercises the full stack via a
temporary SQLite fixture shaped like a Calibre metadata.db.
"""

import os
import sqlite3
import tempfile
import unittest

from cquarry.db import CalibreDB
from cquarry.search import ParseException, SearchEngine, _Parser

# --- In-memory provider for engine tests -----------------------------------

BOOKS = {
    1: {
        "title": "A Game of Thrones",
        "authors": ["George R. R. Martin"],
        "tags": ["Fic.Fantasy.Epic"],
        "series": "A Song of Ice and Fire",
        "publisher": "Bantam",
        "rating": 5,
        "formats": ["EPUB"],
        "languages": ["eng"],
        "pubdate": "1996-08-01",
        "timestamp": "2024-01-10",
        "cover": True,
        "identifiers": {"isbn": "9780553103540"},
        "comments": "A sprawling fantasy epic",
    },
    2: {
        "title": "Mistborn",
        "authors": ["Brandon Sanderson"],
        "tags": ["Fic.Fantasy"],
        "series": "Mistborn",
        "publisher": "Tor",
        "rating": 4,
        "formats": ["EPUB", "MOBI"],
        "languages": ["eng"],
        "pubdate": "2006-07-17",
        "timestamp": "2025-03-01",
        "cover": True,
        "identifiers": {"isbn": "9780765311788", "goodreads": "68428"},
        "comments": "Allomancy and heists",
    },
    3: {
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "tags": ["Fic.SciFi"],
        "series": "",
        "publisher": "Chilton",
        "rating": None,
        "formats": ["PDF"],
        "languages": ["eng"],
        "pubdate": "1965-08-01",
        "timestamp": "2023-06-01",
        "cover": False,
        "identifiers": {},
        "comments": "",
    },
    4: {
        "title": "Beär Facts",
        "authors": ["A. Author"],
        "tags": ["NonFic.Nature"],
        "series": "",
        "publisher": "P",
        "rating": 3,
        "formats": ["EPUB"],
        "languages": ["fra"],
        "pubdate": "2020-01-01",
        "timestamp": "2025-05-01",
        "cover": True,
        "identifiers": {},
        "comments": "",
    },
}
VLS = {
    "Fantasy": 'tags:"Fic.Fantasy"',
    "Epic": 'tags:"Fic.Fantasy.Epic"',
    "Loop": "vl:Loop",
}
SAVED = {
    "HF": "tags:Fic.Fantasy",
    "Nested": 'search:"HF" or author:Herbert',
    "SelfLoop": "search:SelfLoop",
}


class _FakeProvider:
    def all_ids(self):
        return set(BOOKS)

    def field(self, book_id, location):
        return BOOKS[book_id].get(location)

    def vl_expression(self, name):
        low = name.lower()
        return next(
            (v for k, v in VLS.items() if k.lower() == low),
            None,
        )

    def saved_search(self, name):
        low = name.lower()
        return next(
            (v for k, v in SAVED.items() if k.lower() == low),
            None,
        )

    def custom_locations(self):
        return {}


def _engine():
    return SearchEngine(_FakeProvider())


class TestParser(unittest.TestCase):
    """Grammar fidelity: parse() should build the right AST."""

    def setUp(self):
        self.locations = _engine().locations

    def parse(self, expr):
        return _Parser(self.locations).parse(expr)

    def test_bare_word_is_all(self):
        self.assertEqual(self.parse("Dysfunction"), ["token", "all", "Dysfunction"])

    def test_location_token(self):
        self.assertEqual(
            self.parse("title:Dysfunction"), ["token", "title", "Dysfunction"]
        )

    def test_quoted_after_location(self):
        self.assertEqual(
            self.parse('tags:"=Fic.Fantasy"'), ["token", "tags", "=Fic.Fantasy"]
        )

    def test_unknown_location_keeps_colons(self):
        # 'london' is not a known location -> whole thing is an 'all' term
        self.assertEqual(self.parse("london:thames"), ["token", "all", "london:thames"])

    def test_known_location_keeps_trailing_colons(self):
        self.assertEqual(
            self.parse("publisher:london:thames"),
            ["token", "publisher", "london:thames"],
        )

    def test_quoted_word_is_all(self):
        self.assertEqual(self.parse('"(1977)"'), ["token", "all", "(1977)"])

    def test_escaped_quote_in_value(self):
        # S\"calzi -> the value contains a literal double quote
        self.assertEqual(self.parse(r"S\"calzi"), ["token", "all", 'S"calzi'])

    def test_boolean_and_not(self):
        self.assertEqual(
            self.parse("tags:Fic AND NOT tags:Horror"),
            ["and", ["token", "tags", "Fic"], ["not", ["token", "tags", "Horror"]]],
        )

    def test_implicit_and(self):
        self.assertEqual(
            self.parse("tags:Fic tags:Fantasy"),
            ["and", ["token", "tags", "Fic"], ["token", "tags", "Fantasy"]],
        )

    def test_or(self):
        self.assertEqual(
            self.parse("tags:Fic OR tags:NonFic"),
            ["or", ["token", "tags", "Fic"], ["token", "tags", "NonFic"]],
        )

    def test_grouping(self):
        self.assertEqual(
            self.parse("NOT(tags:Fic.Romance OR tags:Fic.Contemporary)"),
            [
                "not",
                [
                    "or",
                    ["token", "tags", "Fic.Romance"],
                    ["token", "tags", "Fic.Contemporary"],
                ],
            ],
        )

    def test_missing_paren_raises(self):
        with self.assertRaises(ParseException):
            self.parse("(tags:Fic OR tags:NonFic")


class TestMatching(unittest.TestCase):
    """Matcher semantics against the in-memory provider."""

    def setUp(self):
        self.e = _engine()

    def s(self, q):
        return self.e.search(q)

    def test_hierarchical_anchored(self):
        # cquarry invariant: Fic.Fantasy matches Fic.Fantasy and Fic.Fantasy.*
        self.assertEqual(self.s("tags:Fic.Fantasy"), {1, 2})
        self.assertEqual(self.s("tags:Fic"), {1, 2, 3})

    def test_exact_tag(self):
        self.assertEqual(self.s('tags:"=Fic.Fantasy"'), {2})

    def test_boolean_semantics(self):
        self.assertEqual(self.s("tags:Fic AND NOT tags:Fic.SciFi"), {1, 2})
        self.assertEqual(self.s("tags:Fic.Fantasy OR tags:Fic.SciFi"), {1, 2, 3})
        self.assertEqual(
            self.s("(tags:Fic OR tags:NonFic) AND NOT tags:Gaming"), {1, 2, 3, 4}
        )

    def test_authors_substring_and_exact(self):
        self.assertEqual(self.s("author:Sanderson"), {2})
        self.assertEqual(self.s('authors:"George R. R. Martin"'), {1})

    def test_all_field_substring(self):
        self.assertEqual(self.s("Dune"), {3})
        self.assertEqual(self.s("Allomancy"), {2})  # matches comments

    def test_numeric_rating(self):
        self.assertEqual(self.s("rating:5"), {1})
        self.assertEqual(self.s("rating:>=4"), {1, 2})
        self.assertEqual(self.s("rating:true"), {1, 2, 4})
        self.assertEqual(self.s("rating:false"), {3})

    def test_date_relational(self):
        self.assertEqual(self.s("pubdate:1996"), {1})
        self.assertEqual(self.s("pubdate:>2000"), {2, 4})
        self.assertEqual(self.s("date:>=2025-01-01"), {2, 4})  # date == timestamp added

    def test_bool_cover(self):
        self.assertEqual(self.s("cover:false"), {3})
        self.assertEqual(self.s("cover:true"), {1, 2, 4})

    def test_formats_and_languages(self):
        self.assertEqual(self.s("formats:MOBI"), {2})
        self.assertEqual(self.s("languages:fra"), {4})

    def test_identifiers(self):
        self.assertEqual(self.s("identifiers:isbn:true"), {1, 2})
        self.assertEqual(self.s("identifiers:goodreads:true"), {2})
        self.assertEqual(self.s("identifiers:9780"), {1, 2})  # value substring
        self.assertEqual(self.s("isbn:9780765311788"), {2})
        self.assertEqual(self.s("identifiers:false"), {3, 4})

    def test_accent_insensitive(self):
        self.assertEqual(self.s("Bear"), {4})  # query 'Bear' matches 'Beär'

    def test_vl_reference_and_recursion(self):
        self.assertEqual(self.s("vl:Fantasy"), {1, 2})
        self.assertEqual(self.s("vl:Epic"), {1})
        with self.assertRaises(ParseException):
            self.s("vl:Loop")

    def test_empty_query_is_all(self):
        self.assertEqual(self.s(""), {1, 2, 3, 4})
        self.assertEqual(self.s("   "), {1, 2, 3, 4})


# --- Full-stack integration over a temporary Calibre-shaped DB --------------

_SCHEMA = """
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
CREATE TABLE data (id INTEGER PRIMARY KEY, book INT, format TEXT, name TEXT, uncompressed_size INT DEFAULT 0);
CREATE TABLE identifiers (book INT, type TEXT, val TEXT);
CREATE TABLE comments (book INT, text TEXT);
CREATE TABLE preferences (id INTEGER PRIMARY KEY, key TEXT, val TEXT);
CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT, is_multiple BOOL);
-- A normalized single-valued enumeration: value table + link table, like Calibre.
CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT, link TEXT DEFAULT '');
CREATE TABLE books_custom_column_1_link (book INT, value INT);
"""


def _build_fixture(path):
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    cur = con.cursor()
    # Two authors, three books, a series with a gap and a half-index novella.
    cur.executemany(
        "INSERT INTO authors (id,name,sort) VALUES (?,?,?)",
        [(1, "Ann Leckie", "Leckie, Ann"), (2, "Frank Herbert", "Herbert, Frank")],
    )
    cur.executemany(
        "INSERT INTO books (id,title,sort,author_sort,timestamp,pubdate,has_cover,last_modified,series_index,path,uuid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "Ancillary Justice",
                "Ancillary Justice",
                "Leckie, Ann",
                "2024-01-01",
                "2013-10-01",
                1,
                "2024-01-01",
                1.0,
                "Ann Leckie/Ancillary Justice (1)",
                "u1",
            ),
            (
                2,
                "Ancillary Sword",
                "Ancillary Sword",
                "Leckie, Ann",
                "2024-02-01",
                "2014-10-01",
                1,
                "2024-02-01",
                2.0,
                "Ann Leckie/Ancillary Sword (2)",
                "u2",
            ),
            (
                3,
                "Dune",
                "Dune",
                "Herbert, Frank",
                "2024-03-01",
                "1965-08-01",
                0,
                "2024-03-01",
                1.0,
                "Frank Herbert/Dune (3)",
                "u3",
            ),
        ],
    )
    cur.executemany(
        "INSERT INTO books_authors_link (book,author) VALUES (?,?)",
        [(1, 1), (2, 1), (3, 2)],
    )
    cur.executemany(
        "INSERT INTO tags (id,name) VALUES (?,?)",
        [(1, "Fic.SciFi"), (2, "Fic.SciFi.Space"), (3, "Award.Hugo")],
    )
    cur.executemany(
        "INSERT INTO books_tags_link (book,tag) VALUES (?,?)",
        [(1, 2), (1, 3), (2, 2), (3, 1)],
    )
    cur.execute("INSERT INTO series (id,name) VALUES (1,'Imperial Radch')")
    cur.executemany(
        "INSERT INTO books_series_link (book,series) VALUES (?,?)", [(1, 1), (2, 1)]
    )
    cur.executemany("INSERT INTO ratings (id,rating) VALUES (?,?)", [(1, 8), (2, 10)])
    cur.executemany(
        "INSERT INTO books_ratings_link (book,rating) VALUES (?,?)", [(1, 1), (3, 2)]
    )
    cur.execute("INSERT INTO publishers (id,name) VALUES (1,'Orbit')")
    cur.execute("INSERT INTO books_publishers_link (book,publisher) VALUES (1,1)")
    cur.execute("INSERT INTO languages (id,lang_code) VALUES (1,'eng')")
    cur.executemany(
        "INSERT INTO books_languages_link (book,lang_code) VALUES (?,?)",
        [(1, 1), (2, 1), (3, 1)],
    )
    cur.executemany(
        "INSERT INTO data (book,format,name,uncompressed_size) VALUES (?,?,?,?)",
        [(1, "EPUB", "x", 2097152), (3, "PDF", "y", 5242880)],
    )
    cur.execute(
        "INSERT INTO identifiers (book,type,val) VALUES (1,'isbn','9781841499789')"
    )
    cur.execute(
        "INSERT INTO comments (book,text) VALUES (1,'A space opera about identity')"
    )
    cur.execute(
        "INSERT INTO preferences (key,val) VALUES ('virtual_libraries', ?)",
        ('{"SciFi": "tags:\\"Fic.SciFi\\"", "Hugo": "tags:Award.Hugo"}',),
    )
    cur.execute(
        "INSERT INTO preferences (key,val) VALUES ('saved_searches', ?)",
        (
            '{"Award Winners": "tags:Award.Hugo", "Chain": "search:\\"Award Winners\\""}',
        ),
    )
    cur.execute(
        "INSERT INTO preferences (key,val) VALUES ('virt_libs_hidden', ?)",
        ('["SciFi"]',),
    )
    cur.execute(
        "INSERT INTO preferences (key,val) VALUES ('virt_libs_order', ?)",
        ('{"Hugo": 0, "SciFi": 1}',),
    )
    # Normalized enumeration custom column "Status" (#status): book 1 = Read.
    cur.execute(
        "INSERT INTO custom_columns (id,label,name,datatype,is_multiple) "
        "VALUES (1,'status','Status','enumeration',0)"
    )
    cur.executemany(
        "INSERT INTO custom_column_1 (id,value) VALUES (?,?)",
        [(1, "Read"), (2, "To Read")],
    )
    cur.execute("INSERT INTO books_custom_column_1_link (book,value) VALUES (1,1)")
    # Direct int custom column labelled pages (#pages): book 3 = 412 pages.
    cur.execute(
        "INSERT INTO custom_columns (id,label,name,datatype,is_multiple) "
        "VALUES (2,'pages','Pages','int',0)"
    )
    cur.execute(
        "CREATE TABLE custom_column_2 (id INTEGER PRIMARY KEY, book INT,"
        " value INT, link TEXT DEFAULT '');"
    )
    cur.execute("INSERT INTO custom_column_2 (book,value) VALUES (3, 412)")
    con.commit()
    con.close()


class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db", prefix="cq_test_")
        os.close(fd)
        _build_fixture(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def setUp(self):
        self.db = CalibreDB(self.path)

    def tearDown(self):
        self.db.close()

    def test_get_all_books(self):
        self.assertEqual(len(self.db.get_all_books()), 3)

    def test_search_hierarchy(self):
        self.assertEqual(self.db.search("tags:Fic.SciFi"), {1, 2, 3})
        self.assertEqual(self.db.search('tags:"=Fic.SciFi"'), {3})

    def test_search_author_rating_identifier(self):
        self.assertEqual(self.db.search("author:Leckie"), {1, 2})
        self.assertEqual(self.db.search("rating:5"), {3})  # internal 10 -> 5 stars
        self.assertEqual(self.db.search("rating:4"), {1})  # internal 8 -> 4 stars
        self.assertEqual(self.db.search("isbn:9781841499789"), {1})
        self.assertEqual(self.db.search("opera"), {1})  # comments via 'all'

    def test_resolve_vl(self):
        self.assertEqual(self.db.resolve_vl("SciFi"), {1, 2, 3})
        self.assertEqual(self.db.resolve_vl("Hugo"), {1})
        with self.assertRaises(ValueError):
            self.db.resolve_vl("Nope")

    def test_get_all_series_gap_and_python_aggregation(self):
        series = {s["name"]: s for s in self.db.get_all_series()}
        radch = series["Imperial Radch"]
        self.assertEqual(radch["book_count"], 2)
        self.assertEqual(radch["max_index"], 2.0)
        self.assertEqual(radch["indices"], "1.0,2.0")

    def test_normalized_custom_column(self):
        # Regression: a normalized single-valued enumeration is stored via a
        # link table, not directly. It must load and be searchable by #label.
        self.assertEqual(self.db.load_custom_column("Status"), {1: "Read"})
        self.assertEqual(self.db.search("#status:=Read"), {1})
        self.assertEqual(self.db.search("#status:Read"), {1})  # contains too

    def test_new_builtin_locations(self):
        # title_sort / series_sort / size / pages resolve end-to-end.
        self.assertEqual(self.db.search("title_sort:Ancillary"), {1, 2})
        self.assertEqual(self.db.search('series_sort:"Imperial Radch [1]"'), {1})
        self.assertEqual(self.db.search("size:>3000000"), {3})  # 5 MB PDF
        self.assertEqual(self.db.search("pages:>400"), {3})  # via #pages column
        self.assertEqual(self.db.search("pages:100"), set())

    def test_size_and_pages_in_books_rows(self):
        books = {b["id"]: b for b in self.db.get_all_books()}
        self.assertEqual(books[1]["size"], 2097152)
        self.assertIsNone(books[2]["size"])

    def test_resolve_saved_search(self):
        self.assertEqual(self.db.resolve_saved_search("Award Winners"), {1})
        self.assertEqual(self.db.resolve_saved_search("award winners"), {1})
        with self.assertRaises(ValueError):
            self.db.resolve_saved_search("Nope")

    def test_search_interpolates_saved_searches(self):
        self.assertEqual(self.db.search('search:"Award Winners"'), {1})
        # Chain references another saved search.
        self.assertEqual(self.db.search("search:Chain"), {1})
        with self.assertRaises(ParseException):
            self.db.search("search:Missing")

    def test_strict_unknown_vl_in_query(self):
        with self.assertRaises(ParseException):
            self.db.search("vl:DoesNotExist")

    def test_resolve_vl_case_insensitive(self):
        self.assertEqual(self.db.resolve_vl("scifi"), {1, 2, 3})
        self.assertEqual(self.db.vl_expression("SCIFI"), 'tags:"Fic.SciFi"')

    def test_get_saved_searches(self):
        sss = self.db.get_saved_searches()
        self.assertIn("Award Winners", sss)
        self.assertEqual(sss["Chain"], 'search:"Award Winners"')

    def test_get_vl_ui_state(self):
        state = self.db.get_vl_ui_state()
        self.assertEqual(state["hidden"], ["SciFi"])
        self.assertEqual(state["order"], {"Hugo": 0, "SciFi": 1})


# --- Phase 4 parity features -------------------------------------------------


class TestLanguageCanonicalization(unittest.TestCase):
    """languages:English canonicalizes to the stored ISO code eng."""

    def setUp(self):
        self.engine = _engine()

    def s(self, q):
        return self.engine.search(q)

    def test_english_matches_eng(self):
        self.assertEqual(self.s("languages:English"), {1, 2, 3})

    def test_french(self):
        self.assertEqual(self.s("language:French"), {4})

    def test_case_insensitive_and_alias(self):
        self.assertEqual(self.s("lang:eNgLiSh"), {1, 2, 3})

    def test_unknown_token_passes_through(self):
        # Raw codes and unknown names still behave as substring text.
        self.assertEqual(self.s("languages:eng"), {1, 2, 3})
        self.assertEqual(self.s("languages:zzz"), set())


class TestCountOperator(unittest.TestCase):
    """#<relop><count> compares the number of values in multi-valued fields."""

    def setUp(self):
        self.engine = _engine()

    def s(self, q):
        return self.engine.search(q)

    def test_formats_greater_than(self):
        # book 2 has EPUB+MOBI; everyone else has exactly one format.
        self.assertEqual(self.s("formats:#>1"), {2})

    def test_identifiers_count(self):
        self.assertEqual(self.s("identifiers:#>=1"), {1, 2})
        self.assertEqual(self.s("identifiers:#=0"), {3, 4})

    def test_invalid_count_raises(self):
        with self.assertRaises(ParseException):
            self.s("formats:#abc")


class TestSlashDateSeparators(unittest.TestCase):
    def setUp(self):
        self.s = lambda q: _engine().search(q)

    def test_exact_day_with_slashes(self):
        self.assertEqual(self.s("pubdate:=1965/08/01"), {3})

    def test_month_precision_with_slashes(self):
        self.assertEqual(self.s("pubdate:2006/07"), {2})

    def test_relational_with_slashes(self):
        self.assertEqual(self.s("pubdate:>1996/07/01"), {1, 2, 4})


class TestBooleanKeywords(unittest.TestCase):
    def setUp(self):
        self.s = lambda q: _engine().search(q)

    def test_checked_and_unchecked(self):
        self.assertEqual(self.s("cover:checked"), {1, 2, 4})
        self.assertEqual(self.s("cover:unchecked"), {3})

    def test_blank_and_empty_mean_false(self):
        self.assertEqual(self.s("cover:blank"), {3})
        self.assertEqual(self.s("cover:empty"), {3})

    def test_underscore_variants(self):
        self.assertEqual(self.s("cover:_checked"), {1, 2, 4})
        self.assertEqual(self.s("cover:_blank"), {3})

    def test_tristate_rating_keywords(self):
        # rating uses the numeric path; checked/blank must work there too.
        self.assertEqual(self.s("rating:checked"), {1, 2, 4})
        self.assertEqual(self.s("rating:blank"), {3})


class TestComponentMatchingTextFields(unittest.TestCase):
    """Calibre's leading-dot modifiers work on plain text fields under =."""

    def provider(self):
        books = {
            1: {
                "title": "War of the Worlds",
                "publisher": "Tor.Com",
                "authors": ["C.J. Cherryh"],
                "tags": [],
                "formats": [],
                "languages": [],
            },
            2: {
                "title": "Tor.Com Originals",
                "publisher": "Other",
                "authors": ["Someone Else"],
                "tags": [],
                "formats": [],
                "languages": [],
            },
        }

        class _P:
            def all_ids(self):
                return set(books)

            def field(self, bid, loc):
                return books.get(bid, {}).get(loc)

            def vl_expression(self, name):
                return None

            def saved_search(self, name):
                return None

            def custom_locations(self):
                return {}

        return _P()

    def s(self, q):
        return SearchEngine(self.provider()).search(q)

    def test_subtree_dot_on_publisher(self):
        # .tor.com matches values starting with that subtree...
        self.assertEqual(self.s("publisher:=.Tor.Com"), {1})

    def test_component_match_on_author_dots(self):
        # ..<comp> matches a single dot-delimited component exactly; the
        # components of "Tor.Com" are "tor" and "com".
        self.assertEqual(self.s("publisher:=..Com"), {1})
        self.assertEqual(self.s("publisher:=..Org"), set())

    def test_plain_exact_still_works(self):
        self.assertEqual(self.s("publisher:=Other"), {2})


class TestSavedSearchInterpolation(unittest.TestCase):
    """search:"name" resolves through the provider with cycle detection."""

    def setUp(self):
        self.s = lambda q: _engine().search(q)

    def test_simple_resolution(self):
        self.assertEqual(self.s('search:"HF"'), {1, 2})

    def test_case_insensitive(self):
        self.assertEqual(self.s('search:"hf"'), {1, 2})

    def test_nested_resolution(self):
        # Nested -> HF plus author Herbert (book 3).
        self.assertEqual(self.s("search:Nested"), {1, 2, 3})

    def test_recursion_detected(self):
        with self.assertRaises(ParseException):
            self.s("search:SelfLoop")

    def test_unknown_saved_search_raises(self):
        with self.assertRaises(ParseException):
            self.s('search:"Nope"')

    def test_saved_search_composes_with_other_terms(self):
        self.assertEqual(self.s('search:"HF" and rating:5'), {1})


if __name__ == "__main__":
    unittest.main()
