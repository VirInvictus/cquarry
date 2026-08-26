"""Calibre-parity search expression engine (stdlib only).

This module ports Calibre's search grammar (``search_query_parser.py``) and the
field-matching semantics of ``calibre/db/search.py`` as closely as the standard
library allows. It powers both ``--search`` and virtual-library (Wing)
resolution.

Coverage:
  - Full grammar: quotes, ``\\`` / ``\\"`` / ``\\(`` / ``\\)`` escapes, parentheses,
    ``or`` / ``and`` / ``not`` and implicit AND, ``location:query`` tokens.
  - Candidate-set boolean evaluation (matches Calibre's and/or/not semantics).
  - Match kinds: contains (default), ``=`` exact, ``~`` regex, ``^`` accent.
  - Exact-match modifiers on every text field: leading ``.`` (subtree) and
    ``..`` (component) matching under ``=``, e.g. ``author:=..Cj. Cherryh``.
  - Multi-valued count operator: ``tags:#>3``, ``authors:#=2``, and
    ``identifiers:#<5`` compare the number of values in a multi-valued field.
  - Field locations: title, title_sort, authors/author, author_sort, series,
    publisher, series_sort, tags/tag (hierarchical), rating, formats/format,
    languages/language (canonicalized: ``languages:English`` matches ``eng``),
    pubdate, timestamp/date, last_modified, size (bytes with k/m/g suffixes),
    pages, identifiers/identifier/isbn, comments/comment, annotations (the
    book's concatenated annotation text; presence via ``true``/``false``),
    cover, id, uuid, ``#custom`` columns, ``all``, ``vl:`` and ``search:``.
  - Grouped search terms: ``GroupName:query`` expands to the union over the
    group's member locations (provider-supplied, from Calibre's
    ``grouped_search_terms`` preference); ``GroupName:false`` inverts. Real
    field names always win over same-named groups.
  - Saved-search interpolation: ``search:"My Saved Search"`` resolves through
    the provider (Calibre stores them in the ``preferences`` table), including
    nested references with cycle detection.
  - Numeric relational (``= > < >= <= !=`` and ``true``/``false``), date
    relational (incl. ``today``/``yesterday``/``thismonth``/``N daysago``),
    boolean columns, tristate boolean keywords (``checked``, ``blank``, ...).

Deliberate, documented deviations from Calibre (dependency-bound):
  - ``~`` regex uses the stdlib ``re`` engine, not Calibre's third-party
    ``regex`` module (no ``VERSION1``/``\\X``; otherwise compatible).
  - Accent/contains folding uses ``unicodedata`` (NFKD) rather than ICU
    collation, so punctuation-insensitivity is not reproduced.
  - GPM templates (``@...:``) are tokenized for parse parity but not evaluated.
  - GUI-state locations that only exist inside Calibre's own UI (``marked``,
    ``ondevice``, ``in_tag_browser``) are not implemented.
  - ``tags:`` uses cquarry's anchored hierarchical match (``Foo`` matches ``Foo``
    and ``Foo.*``) rather than Calibre's raw substring default. This is a
    long-standing cquarry invariant; see the project's CLAUDE.md.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Protocol

# --- Match kinds ---
CONTAINS = 0
EQUALS = 1
REGEXP = 2
ACCENT = 3

# --- Field datatypes ---
DT_TEXT = "text"  # single-valued substring text (title, publisher, comments, ...)
DT_TEXT_MULTI = (
    "text_multi"  # multi-valued substring text (authors, formats, languages)
)
DT_HIER = "hier"  # hierarchical multi-valued (tags) — anchored prefix match
DT_RATING = "rating"
DT_INT = "int"
DT_FLOAT = "float"
DT_DATE = "date"
DT_BOOL = "bool"
DT_IDENTIFIERS = "identifiers"
DT_ALL = "all"
DT_VL = "vl"

# Canonical location -> datatype, for the built-in Calibre fields.
_BUILTIN_DATATYPES: dict[str, str] = {
    "title": DT_TEXT,
    "title_sort": DT_TEXT,
    "author_sort": DT_TEXT,
    "series": DT_TEXT,
    "series_sort": DT_TEXT,
    "publisher": DT_TEXT,
    "comments": DT_TEXT,
    "uuid": DT_TEXT,
    "authors": DT_TEXT_MULTI,
    "formats": DT_TEXT_MULTI,
    "languages": DT_TEXT_MULTI,
    "tags": DT_HIER,
    "rating": DT_RATING,
    "series_index": DT_FLOAT,
    "size": DT_FLOAT,  # total bytes across all formats; k/m/g suffixes honored
    "pages": DT_INT,  # sourced from a '#pages' custom column when present
    "id": DT_INT,
    "pubdate": DT_DATE,
    "timestamp": DT_DATE,
    "last_modified": DT_DATE,
    "identifiers": DT_IDENTIFIERS,
    "cover": DT_BOOL,
    "annotations": DT_TEXT,  # concatenated annotation searchable_text (1.4)
    "all": DT_ALL,
    "vl": DT_VL,
}

# Alias -> canonical location.
_ALIASES: dict[str, str] = {
    "author": "authors",
    "tag": "tags",
    "format": "formats",
    "language": "languages",
    "lang": "languages",
    "comment": "comments",
    "date": "timestamp",
    "identifier": "identifiers",
    "ids": "identifiers",
    "isbn": "identifiers",  # shorthand; `original` still routes the isbn special-case
}

# Locations searched when the location is 'all' (un-prefixed terms).
_ALL_FIELDS = (
    "title",
    "authors",
    "author_sort",
    "series",
    "publisher",
    "tags",
    "comments",
)

# English language names -> ISO 639-2 codes, mirroring Calibre's
# lang_map so `languages:English` matches books stored with `eng`.
_LANG_MAP: dict[str, str] = {
    "afrikaans": "afr",
    "albanian": "sqi",
    "arabic": "ara",
    "armenian": "hye",
    "basque": "eus",
    "belarusian": "bel",
    "bengali": "ben",
    "bosnian": "bos",
    "bulgarian": "bul",
    "catalan": "cat",
    "chinese": "zho",
    "croatian": "hrv",
    "czech": "ces",
    "danish": "dan",
    "dutch": "nld",
    "english": "eng",
    "esperanto": "epo",
    "estonian": "est",
    "filipino": "fil",
    "finnish": "fin",
    "french": "fra",
    "galician": "glg",
    "georgian": "kat",
    "german": "deu",
    "greek": "ell",
    "hebrew": "heb",
    "hindi": "hin",
    "hungarian": "hun",
    "icelandic": "isl",
    "indonesian": "ind",
    "irish": "gle",
    "italian": "ita",
    "japanese": "jpn",
    "korean": "kor",
    "latin": "lat",
    "latvian": "lav",
    "lithuanian": "lit",
    "macedonian": "mkd",
    "malay": "msa",
    "norwegian": "nor",
    "persian": "fas",
    "polish": "pol",
    "portuguese": "por",
    "romanian": "ron",
    "russian": "rus",
    "serbian": "srp",
    "slovak": "slk",
    "slovenian": "slv",
    "spanish": "spa",
    "swahili": "swa",
    "swedish": "swe",
    "thai": "tha",
    "turkish": "tur",
    "ukrainian": "ukr",
    "urdu": "urd",
    "vietnamese": "vie",
    "welsh": "cym",
    "yiddish": "yid",
}


class ParseException(Exception):
    """Raised for malformed search expressions."""


class MetadataProvider(Protocol):
    """What the engine needs from a data source (CalibreDB implements this)."""

    def all_ids(self) -> set[int]: ...

    def field(self, book_id: int, location: str) -> Any:
        """Return a book's value for a *canonical* location.

        Contract by datatype:
          text / text_multi / hier : List[str]
          rating / int / float     : number or None
          date                     : raw date string or None
          bool                     : bool
          identifiers              : Dict[str, str]
        """
        ...

    def vl_expression(self, name: str) -> str | None:
        """Return a virtual library's search expression, or None if unknown."""
        ...

    def saved_search(self, name: str) -> str | None:
        """Return a saved search's expression, or None if unknown."""
        ...

    def custom_locations(self) -> dict[str, str]:
        """Return {location_token: datatype} for custom columns (e.g. '#read')."""
        ...


# ============================================================================
# Grammar (ported from Calibre's search_query_parser.Parser)
# ============================================================================


class _Parser:
    OPCODE = 1
    WORD = 2
    QUOTED_WORD = 3
    EOF = 4
    REPLACEMENTS = tuple(("\\" + x, chr(i + 1)) for i, x in enumerate('\\"()'))

    # Token grammar, tried in order at each position (replaces the old
    # re.Scanner: same semantics, documented API only).
    #   @...:word        GPM template reference (tokenized, not evaluated)
    #   "..."            quoted word (trailing quote must not be escaped)
    #   [^"()\s]+        bare word
    #   [()]             grouping opcode
    _TOKEN_RE = re.compile(
        r'(@.+?:[^")\s]+|".*?(?:(?<!\\)")|[^"()\s]+|[()])', re.DOTALL
    )

    def __init__(self, locations: set[str]):
        self.locations = locations
        self.tokens: list[tuple[int, str]] = []
        self.current = 0

    def _tokenize(self, expr: str) -> list[tuple[int, str]]:
        for k, v in self.REPLACEMENTS:
            expr = expr.replace(k, v)
        tokens: list[tuple[int, str]] = []
        pos = 0
        for m in self._TOKEN_RE.finditer(expr):
            gap = expr[pos : m.start()]
            if gap.strip():
                raise ParseException(f"Could not parse near: {gap!r}")
            t = m.group(0)
            if t in ("(", ")"):
                tokens.append((self.OPCODE, t))
            elif t.startswith('"'):
                if len(t) < 2 or not t.endswith('"'):
                    raise ParseException(f"Unterminated quoted string near: {t!r}")
                tokens.append((self.QUOTED_WORD, t[1:-1]))
            else:
                tokens.append((self.WORD, t))
            pos = m.end()
        tail = expr[pos:]
        if tail.strip():
            raise ParseException(f"Could not parse near: {tail!r}")

        def unescape(x: str) -> str:
            for k, v in self.REPLACEMENTS:
                x = x.replace(v, k[1:])
            return x

        return [(tt, unescape(tv)) for tt, tv in tokens]

    def parse(self, expr: str):
        self.tokens = self._tokenize(expr)
        self.current = 0
        tree = self._or_expr()
        if not self._is_eof():
            raise ParseException("Extra characters at end of search")
        return tree

    # -- token helpers --
    def _is_eof(self) -> bool:
        return self.current >= len(self.tokens)

    def _ttype(self) -> int:
        return self.EOF if self._is_eof() else self.tokens[self.current][0]

    def _token(self, advance: bool = False) -> str | None:
        if self._is_eof():
            return None
        res = self.tokens[self.current][1]
        if advance:
            self.current += 1
        return res

    def _lc(self) -> str | None:
        t = self._token()
        return t.lower() if t is not None else None

    def _advance(self) -> None:
        self.current += 1

    # -- grammar --
    def _or_expr(self):
        lhs = self._and_expr()
        if self._lc() == "or":
            self._advance()
            return ["or", lhs, self._or_expr()]
        return lhs

    def _and_expr(self):
        lhs = self._not_expr()
        if self._lc() == "and":
            self._advance()
            return ["and", lhs, self._and_expr()]
        # implicit AND
        if (
            self._ttype() in (self.WORD, self.QUOTED_WORD) or self._token() == "("
        ) and self._lc() != "or":
            return ["and", lhs, self._and_expr()]
        return lhs

    def _not_expr(self):
        if self._lc() == "not":
            self._advance()
            return ["not", self._not_expr()]
        return self._location_expr()

    def _location_expr(self):
        if self._ttype() == self.OPCODE and self._token() == "(":
            self._advance()
            res = self._or_expr()
            if self._ttype() != self.OPCODE or self._token(advance=True) != ")":
                raise ParseException("missing )")
            return res
        if self._ttype() not in (self.WORD, self.QUOTED_WORD):
            raise ParseException("Invalid syntax. Expected a lookup name or a word")
        return self._base_token()

    def _base_token(self):
        if self._ttype() == self.QUOTED_WORD:
            return ["token", "all", self._token(advance=True)]

        words = (self._token(advance=True) or "").split(":")
        if len(words) > 1 and words[0].lower() in self.locations:
            loc = words[0].lower()
            words = words[1:]
            suffix = ":".join(words)
            if self._ttype() == self.QUOTED_WORD and (
                not suffix or suffix.endswith("=") or suffix.endswith(":")
            ):
                suffix += self._token(advance=True)
            return ["token", loc, suffix]
        return ["token", "all", ":".join(words)]


# ============================================================================
# Matching helpers
# ============================================================================


def _fold(s: str) -> str:
    """Case- and accent-fold a string (approximates Calibre's primary match)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def _matchkind(query: str) -> tuple[int, str]:
    kind = CONTAINS
    if len(query) > 1:
        if query.startswith("\\"):
            query = query[1:]
        elif query.startswith("="):
            kind = EQUALS
            query = query[1:]
        elif query.startswith("~"):
            kind = REGEXP
            query = query[1:]
        elif query.startswith("^"):
            kind = ACCENT
            query = query[1:]
    return kind, query


def _match_text(query: str, values: list[str], kind: int) -> bool:
    """Match a (single- or multi-valued) plain text field.

    Under ``=`` exact match, Calibre's leading-dot modifiers apply to every
    text field: ``.foo`` matches the subtree rooted at ``foo`` and ``..foo``
    matches a single dot-delimited *component* exactly.
    """
    if kind == REGEXP:
        try:
            pat = re.compile(query, re.IGNORECASE | re.UNICODE)
        except re.error as e:
            raise ParseException(f"Invalid regular expression {query!r}: {e}") from e
        return any(pat.search(v) is not None for v in values)

    q = _fold(query)
    for v in values:
        fv = _fold(v)
        if kind == EQUALS:
            if q.startswith(".."):
                sq = q[2:]
                if fv == q or sq in [c for c in fv.split(".") if c]:
                    return True
            elif q.startswith("."):
                qq = q[1:]
                if fv.startswith(qq) and (
                    len(fv) == len(qq) or fv[len(qq) : len(qq) + 1] == "."
                ):
                    return True
            elif fv == q:
                return True
        else:  # CONTAINS / ACCENT both fold accents here
            if q in fv:
                return True
    return False


def _match_hier(query: str, values: list[str], kind: int) -> bool:
    """Match a hierarchical tag field.

    Default (contains): anchored — ``Foo`` matches ``Foo`` and ``Foo.*``.
    ``=`` exact: strict equality, with Calibre's leading-``.`` / ``..`` rules.
    ``~`` regex / ``^`` accent: applied to the tag name directly.
    """
    if kind == REGEXP:
        try:
            pat = re.compile(query, re.IGNORECASE | re.UNICODE)
        except re.error as e:
            raise ParseException(f"Invalid regular expression {query!r}: {e}") from e
        return any(pat.search(v) is not None for v in values)

    q = _fold(query)
    for v in values:
        fv = _fold(v)
        if kind == EQUALS:
            if q.startswith(".."):
                sq = q[2:]
                if fv == q or sq in [c for c in fv.split(".") if c]:
                    return True
            elif q.startswith("."):
                qq = q[1:]
                if fv.startswith(qq) and (
                    len(fv) == len(qq) or fv[len(qq) : len(qq) + 1] == "."
                ):
                    return True
            elif fv == q:
                return True
        elif kind == ACCENT:
            if q in fv:
                return True
        else:  # CONTAINS -> anchored hierarchical (cquarry invariant)
            if fv == q or fv.startswith(q + "."):
                return True
    return False


_NUM_RELOPS = (
    ("!=", lambda a, b: a != b),
    (">=", lambda a, b: a >= b),
    ("<=", lambda a, b: a <= b),
    ("=", lambda a, b: a == b),
    (">", lambda a, b: a > b),
    ("<", lambda a, b: a < b),
)
_SIZE_MULT = {"k": 1024.0, "m": 1024.0**2, "g": 1024.0**3}


def _num_predicate(query: str, datatype: str):
    """Return a value predicate for a numeric query.

    ``true``/``false`` test value presence/absence; otherwise a relational
    comparison against the parsed number.
    """
    if query in _BOOL_TRUE:
        return lambda v: v is not None and (datatype != DT_RATING or v > 0)
    if query in _BOOL_FALSE:
        return lambda v: v is None or (datatype == DT_RATING and not v)

    op = _NUM_RELOPS[3][1]  # '='
    for k, f in _NUM_RELOPS:
        if query.startswith(k):
            op = f
            query = query[len(k) :]
            break

    mult = 1.0
    if len(query) > 1 and query[-1].lower() in _SIZE_MULT:
        mult = _SIZE_MULT[query[-1].lower()]
        query = query[:-1]

    cast = float if datatype in (DT_FLOAT, DT_RATING) else int
    try:
        q = cast(query) * mult
    except (ValueError, TypeError) as e:
        raise ParseException(f"Non-numeric value in query: {query!r}") from e
    return lambda v: v is not None and op(v, q)


_COUNT_RE = re.compile(r"^#(!=|>=|<=|=|>|<)?(\d+)$")


def _count_predicate(query: str):
    """Parse a multi-valued count query like ``#>3`` into a len() predicate."""
    m = _COUNT_RE.match(query.strip())
    if not m:
        raise ParseException(
            f"Invalid count operator in query: {query!r} (expected #<relop><n>)"
        )
    opname, count = m.group(1) or "=", int(m.group(2))
    op = dict(_NUM_RELOPS)[opname]
    return lambda n: op(n, count)


def _canonical_languages(query: str) -> str:
    """Canonicalize English language names to their ISO 639-2 codes.

    ``languages:English`` must match books stored with ``eng``, mirroring
    Calibre's internal lang_map. Unknown tokens pass through untouched so raw
    codes and free text keep working.
    """
    if ":" in query or not query:
        return query  # identifiers-style keypair queries are not language names

    def canon(token: str) -> str:
        low = token.strip().lower()
        if not low:
            return token
        mapped = _LANG_MAP.get(low)
        if mapped is None and low.startswith("="):
            mapped = _LANG_MAP.get(low[1:])
        return mapped if mapped is not None else token

    return ",".join(canon(t) for t in query.split(","))


def canonical_language(name: str) -> str:
    """Canonicalize one language name to its ISO 639-2 code.

    Public wrapper over the search engine's lang map (``English`` ->
    ``eng``); unknown tokens pass through untouched. Used by the write path
    (``set_languages``) so reads and writes agree on codes.
    """
    low = name.strip().lower()
    if not low:
        return name
    mapped = _LANG_MAP.get(low)
    if mapped is None and low.startswith("="):
        mapped = _LANG_MAP.get(low[1:])
    return mapped if mapped is not None else name.strip()


class _DateQuery:
    """Parsed date query: a comparison operator plus a target with precision."""

    _RELOPS = ("!=", ">=", "<=", "=", ">", "<")

    def __init__(self, query: str):
        op = "="
        for k in self._RELOPS:
            if query.startswith(k):
                op = k
                query = query[len(k) :]
                break
        self.op = op
        self.target, self.field_count = self._parse_target(query.strip())

    @staticmethod
    def _parse_target(q: str) -> tuple[date, int]:
        today = date.today()
        ql = q.lower()
        if ql in ("today", "_today"):
            return today, 3
        if ql in ("yesterday", "_yesterday"):
            return today - timedelta(days=1), 3
        if ql in ("thismonth", "_thismonth"):
            return today.replace(day=1), 2
        m = re.match(r"^(\d+)\s*(?:days?ago|_daysago)$", ql)
        if m:
            return today - timedelta(days=int(m.group(1))), 3
        # Calibre also accepts slashes as date separators: 2024/06/15.
        parts = q.replace("/", "-").split("-")
        try:
            if len(parts) == 1:
                return date(int(parts[0]), 1, 1), 1
            if len(parts) == 2:
                return date(int(parts[0]), int(parts[1]), 1), 2
            return date(int(parts[0]), int(parts[1]), int(parts[2])), 3
        except (ValueError, IndexError) as e:
            raise ParseException(f"Invalid date in query: {q!r}") from e

    def matches(self, value: date | None) -> bool:
        if value is None:
            return False
        op, q, fc = self.op, self.target, self.field_count
        if op == "=":
            return self._eq(value, q, fc)
        if op == "!=":
            return not self._eq(value, q, fc)
        if op == ">":
            return self._gt(value, q, fc)
        if op == "<":
            return self._lt(value, q, fc)
        if op == ">=":
            return self._gt(value, q, fc) or self._eq(value, q, fc)
        if op == "<=":
            return self._lt(value, q, fc) or self._eq(value, q, fc)
        return False

    @staticmethod
    def _eq(v: date, q: date, fc: int) -> bool:
        if v.year != q.year:
            return False
        if fc == 1:
            return True
        if v.month != q.month:
            return False
        if fc == 2:
            return True
        return v.day == q.day

    @staticmethod
    def _gt(v: date, q: date, fc: int) -> bool:
        if v.year > q.year:
            return True
        if fc > 1 and v.year == q.year:
            if v.month > q.month:
                return True
            return fc == 3 and v.month == q.month and v.day > q.day
        return False

    @staticmethod
    def _lt(v: date, q: date, fc: int) -> bool:
        if v.year < q.year:
            return True
        if fc > 1 and v.year == q.year:
            if v.month < q.month:
                return True
            return fc == 3 and v.month == q.month and v.day < q.day
        return False


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    s = str(raw).strip()
    # Calibre's undefined-date sentinel
    if s.startswith("0101-01-01") or s.startswith("0100-01-01"):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


_BOOL_TRUE = {
    "true",
    "yes",
    "checked",
    "_true",
    "_yes",
    "_checked",
}
_BOOL_FALSE = {
    "false",
    "no",
    "unchecked",
    "blank",
    "empty",
    "_false",
    "_no",
    "_unchecked",
    "_blank",
    "_empty",
}


# ============================================================================
# Engine
# ============================================================================


class SearchEngine:
    """Evaluate Calibre search expressions against a MetadataProvider."""

    def __init__(self, provider: MetadataProvider):
        self.provider = provider
        self._custom = provider.custom_locations()
        # Grouped search terms (Calibre's preferences key of the same name):
        # group name -> member locations. Optional provider hook — standalone
        # providers simply don't define it and get no groups.
        gst = getattr(provider, "grouped_search_terms", None)
        raw = gst() if callable(gst) else {}
        self._grouped: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for name, members in raw.items():
                if isinstance(members, list) and members:
                    self._grouped[str(name).lower()] = [
                        self._canonical(str(m)) for m in members
                    ]
        self.locations = (
            set(_BUILTIN_DATATYPES)
            | set(_ALIASES)
            | {"isbn", "search"}
            | set(self._custom)
            | set(self._grouped)
        )

    def search(self, expr: str) -> set[int]:
        expr = (expr or "").strip()
        all_ids = self.provider.all_ids()
        if not expr:
            return set(all_ids)
        tree = _Parser(self.locations).parse(expr)
        return self._evaluate(tree, set(all_ids), set())

    # -- boolean evaluation with candidate-set semantics --
    def _evaluate(self, node, candidates: set[int], seen: set[str]) -> set[int]:
        op = node[0]
        if op == "and":
            left = self._evaluate(node[1], candidates, seen)
            return left & self._evaluate(node[2], left, seen)
        if op == "or":
            left = self._evaluate(node[1], candidates, seen)
            return left | self._evaluate(node[2], candidates - left, seen)
        if op == "not":
            return candidates - self._evaluate(node[1], candidates, seen)
        # token
        return self._get_matches(node[1], node[2], candidates, seen)

    # -- per-location matching --
    def _canonical(self, location: str) -> str:
        location = location.lower().strip()
        return _ALIASES.get(location, location)

    def _datatype(self, location: str) -> str | None:
        if location in _BUILTIN_DATATYPES:
            return _BUILTIN_DATATYPES[location]
        if location in self._custom:
            return self._custom[location]
        return None

    def _get_matches(
        self,
        location: str,
        query: str,
        candidates: set[int],
        seen: set[str],
        allow_groups: bool = True,
    ) -> set[int]:
        if not candidates or query is None:
            return set()

        original = location.lower().strip()
        location = self._canonical(location)

        if location == "vl":
            return self._match_vl(query, candidates, seen)

        if location == "search":
            return self._match_saved_search(query, candidates, seen)

        if location == "all":
            return self._match_all(query, candidates)

        datatype = self._datatype(location)
        if datatype is None:
            # Grouped search term (Calibre parity): "GroupName:query" expands
            # to the union of its member locations; "GroupName:false" matches
            # books where NO member matches. Real fields always win over a
            # same-named group. The '@Name' spelling is accepted too, though
            # the tokenizer reserves leading-'@' tokens for GPM templates, so
            # users write the bare form. Nesting a group inside a group is a
            # ParseException, mirroring upstream's recursion guard.
            group_key = original.removeprefix("@")
            members = self._grouped.get(group_key)
            if members is None:
                return set()  # unsupported location -> no matches
            if not allow_groups:
                raise ParseException(f"Recursive query group detected: {group_key}")
            invert = query.strip().lower() == "false"
            member_query = "true" if invert else query
            matches: set[int] = set()
            for member in members:
                found = self._get_matches(
                    member,
                    member_query,
                    candidates - matches,
                    seen,
                    allow_groups=False,
                )
                matches |= found
            if invert:
                return set(self.provider.all_ids()) - matches
            return matches

        # Multi-valued count operator: tags:#>3, authors:#=2, identifiers:#<5.
        if query.startswith("#") and datatype in (
            DT_TEXT_MULTI,
            DT_HIER,
            DT_IDENTIFIERS,
        ):
            pred = _count_predicate(query)
            out = set()
            for b in candidates:
                if datatype == DT_IDENTIFIERS:
                    n = len(self.provider.field(b, "identifiers") or {})
                else:
                    n = len(self._values(b, location))
                if pred(n):
                    out.add(b)
            return out

        if datatype == DT_IDENTIFIERS:
            return self._match_identifiers(original, query, candidates)
        if datatype == DT_BOOL:
            return self._match_bool(location, query, candidates)
        if datatype in (DT_RATING, DT_INT, DT_FLOAT):
            return self._match_numeric(location, datatype, query, candidates)
        if datatype == DT_DATE:
            return self._match_date(location, query, candidates)
        if location == "languages":
            query = _canonical_languages(query)
        # text-like
        return self._match_textlike(location, datatype, query, candidates)

    def _values(self, book_id: int, location: str) -> list[str]:
        v = self.provider.field(book_id, location)
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if x is not None and str(x) != ""]
        s = str(v)
        return [s] if s else []

    def _match_textlike(self, location, datatype, query, candidates) -> set[int]:
        kind, q = _matchkind(query)
        # the bare true/false presence test (Calibre's contains special case)
        if kind == CONTAINS and q.lower() in (_BOOL_TRUE | _BOOL_FALSE):
            want = q.lower() in _BOOL_TRUE
            return {b for b in candidates if bool(self._values(b, location)) == want}
        matcher = _match_hier if datatype == DT_HIER else _match_text
        return {b for b in candidates if matcher(q, self._values(b, location), kind)}

    def _match_numeric(self, location, datatype, query, candidates) -> set[int]:
        pred = _num_predicate(query.lower().strip(), datatype)
        out = set()
        for b in candidates:
            val = self.provider.field(b, location)
            if val is not None:
                try:
                    val = float(val)
                except ValueError, TypeError:
                    continue
            if pred(val):
                out.add(b)
        return out

    def _match_date(self, location, query, candidates) -> set[int]:
        q = query.lower().strip()
        _kind, q = _matchkind(q)
        if q in _BOOL_FALSE or q == "":
            return {
                b
                for b in candidates
                if _parse_date(self.provider.field(b, location)) is None
            }
        if q in _BOOL_TRUE:
            return {
                b
                for b in candidates
                if _parse_date(self.provider.field(b, location)) is not None
            }
        dq = _DateQuery(q)
        return {
            b
            for b in candidates
            if dq.matches(_parse_date(self.provider.field(b, location)))
        }

    def _match_bool(self, location, query, candidates) -> set[int]:
        q = query.lower().strip()
        if q in _BOOL_TRUE:
            return {b for b in candidates if bool(self.provider.field(b, location))}
        if q in _BOOL_FALSE:
            return {b for b in candidates if not bool(self.provider.field(b, location))}
        return set()

    def _match_identifiers(self, original, query, candidates) -> set[int]:
        # Mirrors Calibre's keypair_search. `isbn:X` is shorthand for an exact
        # `identifiers:=isbn:X` lookup. A bare `identifiers:foo` (no colon)
        # matches identifier *values*; use `identifiers:type:true` for presence.
        if original == "isbn":
            query = "=isbn:" + query
        if ":" in query:
            keyq_raw, _, valq_raw = query.partition(":")
            keyq_kind, keyq = _matchkind(keyq_raw.strip())
            valq_kind, valq = _matchkind(valq_raw.strip())
        else:
            keyq, keyq_kind = "", CONTAINS
            valq_kind, valq = _matchkind(query.strip())

        if valq in (_BOOL_TRUE | _BOOL_FALSE):
            found = set()
            for b in candidates:
                ids = self.provider.field(b, "identifiers") or {}
                if keyq:
                    if any(_match_text(keyq, [k], keyq_kind) for k in ids):
                        found.add(b)
                elif ids:
                    found.add(b)
            return found if valq in _BOOL_TRUE else (candidates - found)

        out = set()
        for b in candidates:
            ids = self.provider.field(b, "identifiers") or {}
            for k, v in ids.items():
                if keyq and not _match_text(keyq, [k], keyq_kind):
                    continue
                if valq and not _match_text(valq, [v], valq_kind):
                    continue
                out.add(b)
                break
        return out

    def _match_all(self, query, candidates) -> set[int]:
        kind, q = _matchkind(query)
        fields = list(_ALL_FIELDS)
        # Calibre's bare-term search also sweeps user text columns; numeric,
        # date, bool and identifier columns are excluded from 'all'.
        fields += [
            loc
            for loc, dt in self._custom.items()
            if dt in (DT_TEXT, DT_TEXT_MULTI, DT_HIER)
        ]
        out = set()
        for b in candidates:
            for loc in fields:
                vals = self._values(b, loc)
                # 'all' treats every field as plain substring text
                if _match_text(q, vals, kind):
                    out.add(b)
                    break
        return out

    def _match_vl(self, name: str, candidates: set[int], seen: set[str]) -> set[int]:
        key = "vl:" + name.lower()
        if key in seen:
            raise ParseException(f"Recursive virtual library reference: {name!r}")
        expr = self.provider.vl_expression(name)
        if expr is None:
            raise ParseException(f"Unknown virtual library: {name!r}")
        tree = _Parser(self.locations).parse(expr.strip())
        return candidates & self._evaluate(tree, self.provider.all_ids(), seen | {key})

    def _match_saved_search(
        self, name: str, candidates: set[int], seen: set[str]
    ) -> set[int]:
        name = name.strip().strip('"')
        key = "ss:" + name.lower()
        if key in seen:
            raise ParseException(f"Recursive saved search reference: {name!r}")
        expr = self.provider.saved_search(name)
        if expr is None:
            raise ParseException(f"Unknown saved search: {name!r}")
        tree = _Parser(self.locations).parse(expr.strip())
        return candidates & self._evaluate(tree, self.provider.all_ids(), seen | {key})
