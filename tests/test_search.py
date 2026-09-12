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

    def test_super_quotes(self):
        # The 1.18 pin: """...""" super-quotes shield quotes/parens/escapes
        # from the lexer (upstream's documented escape hatch, gui.rst:445).
        # The span (quotes included) is hex-shielded into one bare word, so
        # `location:"""..."""` lexes and parses as one token+query.
        self.assertEqual(
            self.parse('title:"""a "b" (c)"""'), ["token", "title", 'a "b" (c)']
        )
        # Escapes and parens survive verbatim inside the span (the escape
        # replacement cycle runs after the docstring pass, on the rest).
        self.assertEqual(
            self.parse('comments:"""((escapes \\" stay)"""'),
            ["token", "comments", '((escapes \\" stay)'],
        )
        # A super-quote among ordinary tokens.
        self.assertEqual(
            self.parse('title:"""Weird (Title) Here""" or tags:Foo'),
            ["or", ["token", "title", "Weird (Title) Here"], ["token", "tags", "Foo"]],
        )
        # Upstream's span regex is the "at least one character" idiom
        # (..*?), so a one-character span IS super-quoted; only an empty
        # span falls through to the plain lexer.
        self.assertEqual(self.parse('title:"""x"""'), ["token", "title", "x"])

    def test_super_quotes_engine_match(self):
        books = {
            1: {
                "title": 'Heavy "Metal" (Deluxe)',
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": False,
            },
            2: {
                "title": "Plain Title",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": False,
            },
        }

        class _QProvider:
            def all_ids(self):
                return set(books)

            def field(self, book_id, location):
                return books[book_id].get(location)

            def vl_expression(self, name):
                return None

            def saved_search(self, name):
                return None

            def custom_locations(self):
                return {}

        engine = SearchEngine(_QProvider())
        self.assertEqual(engine.search('title:"""Heavy "Metal" (Deluxe)"""'), {1})
        self.assertEqual(engine.search('title:"""(Deluxe)"""'), {1})
        self.assertEqual(engine.search('title:"""a "b" (c)"""'), set())
        # The pre-1.18 pain point: the same text without super-quotes
        # mis-tokenizes (an absorbed prefix, bare words, an unbalanced
        # trailing quote), ending in a parse error instead of a query.
        self.assertRaises(ParseException, engine.search, 'title:"a "b"')

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

    def test_identifiers_presence_gate_is_case_insensitive(self):
        # The 1.18 pin: the presence gate folded the value but the selection
        # compared the raw one, so uppercase TRUE/FALSE silently inverted
        # (returning the complement of the right answer).
        self.assertEqual(self.s("identifiers:isbn:TRUE"), {1, 2})
        self.assertEqual(self.s("identifiers:isbn:False"), {3, 4})
        self.assertEqual(self.s("identifiers:GOODREADS:True"), {2})

    def test_accent_insensitive(self):
        self.assertEqual(self.s("Bear"), {4})  # query 'Bear' matches 'Beär'

    def test_regex_matchkind_matches_and_raises(self):
        # The `~` match kind: stdlib re, case-insensitive; a malformed
        # pattern surfaces as a ParseException, never a raw re.error.
        self.assertEqual(self.s("title:~^A Game"), {1})
        self.assertEqual(self.s("title:~^mist"), {2})
        self.assertEqual(self.s("authors:~r\\."), {1})  # George R. R. Martin
        with self.assertRaises(ParseException):
            self.s("title:~([unclosed")

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

    def test_search_hierarchy(self):
        # The fixture sanity check rides along with the first real assert.
        self.assertEqual(len(self.db.get_all_books()), 3)
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

    def test_resolve_vl_and_saved_search_strip_quotes_and_padding(self):
        # A quoted or padded KNOWN name used to raise bare StopIteration /
        # ValueError because the guard and the lookup normalized differently.
        self.assertEqual(self.db.resolve_vl('"SciFi"'), {1, 2, 3})
        self.assertEqual(self.db.resolve_vl("  Hugo  "), {1})
        self.assertEqual(self.db.resolve_saved_search('"Award Winners"'), {1})
        self.assertEqual(self.db.resolve_saved_search("  award winners  "), {1})
        # Unknown names (quoted or not) still raise ValueError.
        with self.assertRaises(ValueError):
            self.db.resolve_vl('"Nope"')
        with self.assertRaises(ValueError):
            self.db.resolve_saved_search('"Nope"')

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

    def test_bare_hash_is_literal_text_not_a_count(self):
        # '#abc' carries no relop: upstream text-searches the literal string
        # instead of raising an invalid-count error (a 2026-09-09 parity fix;
        # this used to raise ParseException).
        self.assertEqual(self.s("formats:#abc"), set())
        self.assertEqual(self.s("formats:#>1"), {2})  # real counts still work


class TestRecursionGuard(unittest.TestCase):
    """Grammar-valid adversarial queries escape as ParseException, never as
    a raw RecursionError (upstream converts RuntimeError at its parse site
    and its VL site)."""

    def setUp(self):
        self.e = _engine()

    def test_deep_nesting_raises_parseexception(self):
        with self.assertRaises(ParseException):
            self.e.search("(" * 1000 + "tags:Fic" + ")" * 1000)

    def test_long_or_chain_raises_parseexception(self):
        with self.assertRaises(ParseException):
            self.e.search(" OR ".join(["tags:Fic"] * 5000))

    def test_deep_vl_chain_raises_parseexception(self):
        class ChainProvider(_FakeProvider):
            def vl_expression(self, name):
                low = name.lower()
                for i in range(400):
                    if low == f"chain{i}".lower():
                        return f"vl:chain{i + 1}"
                return next((v for k, v in VLS.items() if k.lower() == low), None)

        with self.assertRaises(ParseException):
            SearchEngine(ChainProvider()).search("vl:chain0")

    def test_deep_saved_search_chain_raises_parseexception(self):
        class SSChainProvider(_FakeProvider):
            def saved_search(self, name):
                low = name.lower()
                for i in range(400):
                    if low == f"link{i}":
                        return f"search:link{i + 1}"
                return next((v for k, v in SAVED.items() if k.lower() == low), None)

        with self.assertRaises(ParseException):
            SearchEngine(SSChainProvider()).search("search:link0")

    def test_normal_queries_still_work(self):
        self.assertEqual(self.e.search("tags:Fic.Fantasy"), {1, 2})
        self.assertEqual(self.e.search("vl:Fantasy"), {1, 2})


class TestSearchParityFixes(unittest.TestCase):
    """The 2026-09-08 parity-gap batch, each behavior checked against the
    upstream search_query_parser.py / db/search.py."""

    def setUp(self):
        self.e = _engine()

    def s(self, q):
        return self.e.search(q)

    def test_empty_location_query_matches_nothing(self):
        # `title:` used to match presence (all books) while upstream and
        # cquarry's own `tags:` returned nothing; upstream returns the empty
        # set for an empty query after ANY location.
        self.assertEqual(self.s("title:"), set())
        self.assertEqual(self.s("tags:"), set())
        self.assertEqual(self.s("vl:"), set())
        self.assertEqual(self.s("pubdate:"), set())  # was the dateless set
        # The top-level empty query still returns everything.
        self.assertEqual(self.s(""), {1, 2, 3, 4})

    def test_invalid_boolean_query_raises(self):
        # Upstream's BooleanSearch raises instead of silently matching
        # nothing.
        with self.assertRaises(ParseException):
            self.s("cover:maybe")
        self.assertEqual(self.s("cover:true"), {1, 2, 4})

    def test_two_letter_language_codes_canonicalize(self):
        self.assertEqual(self.s("languages:fr"), {4})
        self.assertEqual(self.s("languages:ja"), set())  # no jpn books
        self.assertEqual(self.s("languages:jpn"), set())  # nor any codes

    def test_component_exact_match_strips_components(self):
        # The component list upstream matches against is STRIPPED; cquarry
        # compared raw split parts, so "..Cherryh" missed "C.J. Cherryh"-shaped
        # values whose later components carry a leading space.
        self.assertEqual(self.s('tags:"=..Fantasy"'), {1, 2})
        self.assertEqual(self.s('tags:"=..Epic"'), {1})
        self.assertEqual(self.s('authors:"=..Author"'), {4})  # 'A. Author'
        self.assertEqual(self.s('authors:"=..Sanderson"'), set())

    def test_all_sweep_covers_formats_languages_and_numeric(self):
        # Upstream's bare-term sweep includes formats, languages (with
        # canonicalization) and exact-equality probes on numeric fields;
        # dates match nothing in the sweep.
        self.assertEqual(self.s("EPUB"), {1, 2, 4})
        self.assertEqual(self.s("french"), {4})  # languages canonicalized
        self.assertEqual(self.s("Sanderson"), {2})  # authors via text
        self.assertEqual(self.s("1965"), set())  # dates are not swept
        self.assertEqual(self.s("5"), {1})  # book 1 rated 5 stars
        # "3" matches book 4 via the rating probe (the fake provider has no
        # id/size fields; the real view probes series_index, rating, pages
        # and size the same way -- id is excluded from the sweep, upstream
        # parity since 1.18).
        self.assertEqual(self.s("3"), {4})

    def test_all_sweep_bare_true_false_is_the_presence_test(self):
        # The 1.18 pin: with contains semantics, a bare true/false term is
        # upstream's presence branch across the sweep (DS:849-855), not a
        # substring match. Every fixture book has a title, so true matches
        # everything and false matches nothing.
        self.assertEqual(self.s("true"), {1, 2, 3, 4})
        self.assertEqual(self.s("false"), set())

    def test_all_sweep_presence_counts_identifiers_and_cover(self):
        # Isolated sweep: books with no text at all -- identifiers and the
        # cover flag are the only presence signals, and identifier KEYS are
        # never text-matched by a bare term (the old spec claim, corrected
        # in 1.18 to match upstream DS:808-818).
        books = {
            1: {
                "title": "",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {"isbn": "x"},
                "cover": False,
            },
            2: {
                "title": "",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": True,
            },
            3: {
                "title": "",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": False,
            },
        }

        class _BareProvider:
            def all_ids(self):
                return set(books)

            def field(self, book_id, location):
                return books[book_id].get(location)

            def vl_expression(self, name):
                return None

            def saved_search(self, name):
                return None

            def custom_locations(self):
                return {}

        engine = SearchEngine(_BareProvider())
        self.assertEqual(engine.search("true"), {1, 2})
        self.assertEqual(engine.search("false"), {3})
        # Identifier keys do not text-sweep: a bare term matching an
        # identifier key or value matches nothing here.
        self.assertEqual(engine.search("isbn"), set())

    def test_search_with_exact_prefix_resolves_saved_search(self):
        # Upstream removeprefix's the '=' before the saved-search lookup;
        # cquarry used to report a KNOWN search as unknown.
        self.assertEqual(self.s("search:=HF"), {1, 2})
        self.assertEqual(self.s('search:="HF"'), {1, 2})

    def test_identifier_presence_uses_exact_true_false_only(self):
        # yes/no/checked are literal values in identifier search, not
        # presence tests (upstream's keypair set is exactly true/false).
        self.assertEqual(self.s("isbn:true"), {1, 2})
        self.assertEqual(self.s("isbn:yes"), set())
        self.assertEqual(self.s("identifiers:goodreads:true"), {2})

    def test_whitespace_only_value_is_not_present(self):
        class WSProvider(_FakeProvider):
            def all_ids(self):
                return {1, 5}

            def field(self, book_id, location):
                if book_id == 5 and location == "title":
                    return "   "
                return BOOKS[book_id].get(location)

        e = SearchEngine(WSProvider())
        self.assertEqual(e.search("title:true"), {1})
        self.assertEqual(e.search("title:false"), {5})

    def test_rating_scale_single_convention(self):
        # The builtin rating is star-scaled in the engine; custom rating
        # columns join it (covered end-to-end in test_db, pinned here via
        # the builtin to keep the fake provider honest).
        self.assertEqual(self.s("rating:5"), {1})


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
        # 1.18 parity pin: numerics take exactly true/false as presence
        # words (upstream NumericSearch, DS:245-290); the tristate
        # vocabulary is bool-only and raises on a numeric field, like
        # upstream's "Non-numeric value" error.
        self.assertEqual(self.s("rating:true"), {1, 2, 4})
        self.assertEqual(self.s("rating:false"), {3})
        self.assertRaises(ParseException, self.s, "rating:checked")
        self.assertRaises(ParseException, self.s, "rating:blank")


class TestTemplateLocation(unittest.TestCase):
    """template: is a clear parse error, never a silent empty match."""

    def setUp(self):
        self.s = lambda q: _engine().search(q)

    def test_template_raises_parse_exception(self):
        self.assertRaises(ParseException, self.s, "template:foo")
        self.assertRaises(ParseException, self.s, "template:'#authors#@#:t:xy'")
        # The error says why.
        with self.assertRaises(ParseException) as cm:
            self.s("template:foo")
        self.assertIn("template", str(cm.exception).lower())


class TestHonestyParity(unittest.TestCase):
    """The 1.18 spec §5 honesty pass: each formerly lenient behavior now
    matches upstream exactly (or raises where upstream raises)."""

    def setUp(self):
        self.s = lambda q: _engine().search(q)

    def test_date_presence_is_exact_true_false(self):
        books = {
            1: {
                "pubdate": "2020-01-01",
                "title": "Dated",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": False,
            },
            2: {
                "pubdate": None,
                "title": "Undated",
                "authors": [],
                "tags": [],
                "comments": "",
                "identifiers": {},
                "cover": False,
            },
        }

        class _DProvider:
            def all_ids(self):
                return set(books)

            def field(self, book_id, location):
                return books[book_id].get(location)

            def vl_expression(self, name):
                return None

            def saved_search(self, name):
                return None

            def custom_locations(self):
                return {}

        engine = SearchEngine(_DProvider())
        self.assertEqual(engine.search("pubdate:true"), {1})
        self.assertEqual(engine.search("pubdate:false"), {2})

    def test_date_vocabulary_no_longer_over_accepts(self):
        # Upstream raises "Date conversion error" for these; cquarry used to
        # match the tristate words against dateless books and strip ~/= as
        # match kinds.
        self.assertRaises(ParseException, self.s, "pubdate:blank")
        self.assertRaises(ParseException, self.s, "pubdate:empty")
        self.assertRaises(ParseException, self.s, "pubdate:~2020")
        self.assertRaises(ParseException, self.s, "pubdate:notadate")

    def test_text_presence_words_are_exact_true_false(self):
        # 'yes' is ordinary substring text now, not a presence test
        # (upstream DS:849-855 knows only true/false). No fixture title
        # contains 'yes'.
        self.assertEqual(self.s("title:yes"), set())
        # 'true' itself is still the presence test on a direct location
        # (upstream's branch runs there too): every fixture book has a title.
        self.assertEqual(self.s("title:true"), {1, 2, 3, 4})
        self.assertEqual(self.s("authors:true"), {1, 2, 3, 4})  # presence works


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


class TestEmptyLocationQuery(unittest.TestCase):
    """A location with an empty query (e.g. 'rating:') — upstream's
    NumericSearch returns an empty set instead of raising on the parse."""

    def s(self, q):
        return SearchEngine(_FakeProvider()).search(q)

    def test_numeric_empty_query_matches_nothing(self):
        self.assertEqual(self.s("rating:"), set())
        self.assertEqual(self.s("size:"), set())
        self.assertEqual(self.s("pages:"), set())
        self.assertEqual(self.s("series_index:"), set())
        self.assertEqual(self.s("id:"), set())

    def test_whitespace_query_matches_nothing(self):
        self.assertEqual(self.s("rating:  "), set())

    def test_prefixed_numeric_query_still_works(self):
        self.assertEqual(self.s("rating:>3"), {1, 2})
        self.assertEqual(self.s("rating:4"), {2})


class TestUserCategorySearch(unittest.TestCase):
    """Calibre's '@Name' user-category location (get_user_category_matches)."""

    def provider(self):
        class _UserCatProvider(_FakeProvider):
            def grouped_search_terms(self):
                return {"Fav": ["tags"]}

            def user_categories(self):
                return {
                    "Favorites": [
                        ["Fic.Fantasy.Epic", "tags", 0],
                        ["George R. R. Martin", "authors", 0],
                    ],
                    "Favorites.Sub": [["Fic.Fantasy", "tags", 0]],
                    "publisher": [["Bantam", "publisher", 0]],
                    "Fav": [["Tor", "publisher", 0]],
                }

        return _UserCatProvider()

    def s(self, q):
        return SearchEngine(self.provider()).search(q)

    def test_true_matches_members_across_locations(self):
        # Tag member (book 1) OR author member (book 1).
        self.assertEqual(self.s("@Favorites:true"), {1})

    def test_case_insensitive_name_and_query(self):
        self.assertEqual(self.s("@favorites:TRUE"), {1})

    def test_false_inverts(self):
        self.assertEqual(self.s("@Favorites:false"), {2, 3, 4})

    def test_leading_dot_includes_subcategories(self):
        self.assertEqual(self.s("@Favorites:true"), {1})
        self.assertEqual(self.s("@Favorites:.true"), {1, 2})

    def test_other_query_text_is_ignored_like_upstream(self):
        self.assertEqual(self.s("@Favorites:fantasy"), {1})

    def test_short_query_matches_nothing(self):
        self.assertEqual(self.s("@Favorites:a"), set())

    def test_unknown_category_matches_nothing(self):
        # Routed as a location (not an all: text search), so empty — parity
        # with Calibre, where unknown user categories match nothing.
        self.assertEqual(self.s("@Nope:true"), set())

    def test_bare_field_beats_same_named_category(self):
        # 'publisher' is both a real field and a user category: the bare form
        # searches the field; only the @-form reaches the category.
        self.assertEqual(self.s("publisher:Tor"), {2})
        self.assertEqual(self.s("@publisher:true"), {1})

    def test_group_beats_same_named_category(self):
        # Group 'Fav' (-> tags) wins over user category 'Fav' (-> publisher).
        self.assertEqual(self.s("@Fav:Fic.SciFi"), {3})
        self.assertEqual(self.s("Fav:Fic.SciFi"), {3})

    def test_provider_without_hook_degrades_to_nothing(self):
        # No user_categories() on the provider: '@Name' queries match nothing
        # instead of crashing (getattr fallback, mirroring groups).
        self.assertEqual(SearchEngine(_FakeProvider()).search("@Favorites:true"), set())


if __name__ == "__main__":
    unittest.main()
