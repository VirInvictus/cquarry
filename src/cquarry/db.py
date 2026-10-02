"""The read-only Calibre database layer.

:class:`CalibreDB` is the primary public interface: it opens Calibre's
``metadata.db`` strictly read-only (``?mode=ro``, with a lock-escape snapshot
copy when Calibre holds the lock), hydrates book rows over a 6-JOIN cache,
and implements the :class:`cquarry.search.MetadataProvider` protocol so the
search engine evaluates against it directly. This module owns the read-only
contract: it never writes to the database, and the only sanctioned mutation
path lives in the separate, opt-in :mod:`cquarry.write` module (never
imported from here).

Everything the module surfaces rides the lazy caches initialized in
:meth:`CalibreDB._init_caches` (``refresh()`` clears them all); reads degrade
to empty results rather than errors on schemas that predate a table.
"""

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Self

from cquarry.helpers import (
    calibre_rating_to_stars,
    db_uri_ro,
    isbn_normalize,
    strip_html,
    title_sort,
)
from cquarry.search import (
    DT_BOOL,
    DT_DATE,
    DT_FLOAT,
    DT_INT,
    DT_RATING,
    DT_TEXT,
    DT_TEXT_MULTI,
    ParseException,
    SearchEngine,
    _fold,
)

# Sentinel distinguishing "cache not populated" from a cached None result.
_UNSET = object()


def _snapshot_copy(src_path: str, tmp: str, leash: float = 10.0) -> None:
    """Snapshot a live database into ``tmp`` through sqlite3's backup API.

    The backup API takes one consistent page image (WAL content folded
    in), where the earlier hand-rolled main+``-wal``+``-shm`` copy2 could
    tear when Calibre checkpointed mid-copy -- the same defect shape Wave
    14 flagged in CalibreQuarry's own backup. Python's backup retries a
    busy source forever, so the copy runs on a leash: a writer holding the
    lock past ``leash`` seconds trips the fallback to the raw file trio,
    trading the tear risk back for that pathological case rather than
    hanging the reader.
    """
    deadline = time.monotonic() + leash

    def _leash(_status: int, _remaining: int, _total: int) -> None:
        # Invoked per backup step, busy steps included; raising is how a
        # caller aborts Python's internal busy-retry loop.
        if time.monotonic() > deadline:
            raise TimeoutError("snapshot backup leash tripped")

    src = sqlite3.connect(db_uri_ro(src_path), uri=True)
    dst = sqlite3.connect(tmp)
    consistent = False
    try:
        try:
            src.backup(dst, progress=_leash)
            consistent = True
        except (TimeoutError, sqlite3.Error):
            pass  # leashed or failed: the fallback below takes over
    finally:
        dst.close()
        src.close()
    if not consistent:
        shutil.copy2(src_path, tmp)
        for suffix in ("-wal", "-shm"):
            side = src_path + suffix
            if os.path.exists(side):
                shutil.copy2(side, tmp + suffix)


_DUPLICATE_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _duplicate_title_key(title: str) -> str:
    """Folded title key for duplicate screening: subtitle scrubbed (the
    segment before a colon), leading article dropped, whitespace collapsed."""
    text = _fold(title or "")
    text = text.split(":", 1)[0].strip()
    text = _DUPLICATE_ARTICLE_RE.sub("", text).strip()
    return re.sub(r"\s+", " ", text)


def _canonical_pref_name(name: str, names) -> str | None:
    """Match a preference name (VL or saved search) to its stored spelling.

    Case-, padding-, and surrounding-quote-insensitive: the same
    normalization :meth:`vl_expression` and :meth:`saved_search` apply, so
    ``" My VL "`` and ``'"My VL"'`` resolve to a KNOWN ``My VL`` instead of
    raising as unknown after the guard already blessed them.
    """
    low = name.lower().strip().strip('"')
    for n in names:
        if n.lower().strip().strip('"') == low:
            return n
    return None


# The hydrated book-row contract (spec §3.1): get_all_books() and get_book()
# MUST select the identical column set so row shapes stay identical. They
# drifted once — get_book() silently lacked `size` — hence the shared
# constant; append new fields here, never to one call site.
_BOOK_SELECT = """
    SELECT
        b.id, b.title, b.sort as title_sort, b.author_sort,
        b.timestamp, b.pubdate, b.has_cover, b.last_modified,
        b.series_index, b.path,
        s.name as series,
        r.rating,
        p.name as publisher
    FROM books b
    LEFT JOIN books_series_link bsl ON bsl.book = b.id
    LEFT JOIN series s ON s.id = bsl.series
    LEFT JOIN books_ratings_link brl ON brl.book = b.id
    LEFT JOIN ratings r ON r.id = brl.rating
    LEFT JOIN books_publishers_link bpl ON bpl.book = b.id
    LEFT JOIN publishers p ON p.id = bpl.publisher
"""


class CalibreDB:
    """Read-only interface to Calibre's metadata.db.

    If the database is locked by Calibre, automatically copies it to a
    temporary file and reads from the copy instead.

    Also implements the search.MetadataProvider interface so the search
    engine can resolve expressions against this library.
    """

    #: Seconds a held write lock may delay the lock-escape snapshot before
    #: it degrades to the raw file copy (see :func:`_snapshot_copy`). Class
    #: attribute so embedders and tests can tune it.
    SNAPSHOT_LEASH = 10.0

    def __init__(self, db_path: str):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        self._tmp_path: str | None = None
        self._init_caches()

        self.conn = self._open(db_path)
        self.conn.row_factory = sqlite3.Row

    def _init_caches(self) -> None:
        """(Re)initialize every lazy cache; __init__ and refresh() share it."""
        self._vl_cache: dict[str, str] | None = None
        self._vl_for_books_cache: dict[int, tuple[str, ...]] | None = None
        self._books_cache: list[dict[str, Any]] | None = None
        self._all_ids_cache: set[int] | None = None
        self._all_formats_cache: dict[int, list[str]] | None = None

        # Search-engine state (lazily built).
        self._search_engine: SearchEngine | None = None
        self._search_view: dict[int, dict[str, Any]] | None = None
        self._custom_loc_cache: dict[str, str] | None = None
        self._custom_label_cache: dict[str, dict[str, Any]] | None = None
        self._custom_val_cache: dict[str, dict[int, Any]] = {}
        self._custom_link_cache: dict[str, dict[int, str]] = {}
        self._comments_cache: dict[int, str] | None = None
        self._pages_col_cache: Any = _UNSET
        self._pages_cache: dict[int, int] | None = None
        self._format_path_index: dict[str, int] | None = None
        self._prefs_cache: dict[str, Any] | None = None
        self._cc_schema_cache: dict[str, bool] | None = None
        self._annotations_text_cache: dict[int, str] | None = None
        # PRAGMA data_version at last observation (external_changes_detected).
        self._data_version: int | None = None
        # FTS sidecar (full-text-search.db) connection state; refresh()
        # drops it so sidecar reads re-open against current data.
        self._fts_conn: sqlite3.Connection | None = None
        self._fts_tmp_path: str | None = None
        self._fts_missing = False

    def refresh(self) -> None:
        """Drop every cache so subsequent reads re-query the database.

        The coherence boundary for long-lived holders (Hermitage/Carrel keep
        a connection open while Calibre writes externally): caches normally
        populate at different moments, so one connection can contradict
        itself, with ``count_books`` answering from one snapshot and
        ``search`` from another. One ``refresh()`` call clears everything,
        the built search engine and the FTS-sidecar connection included; the
        next read repopulates from current database state.

        One boundary: when the connection itself rides a locked-database
        snapshot copy (see ``_open``), the snapshot is NOT retaken -- the
        next read still answers from the copy taken at open time. Reopen
        the ``CalibreDB`` for truly current data in that situation.
        """
        if self._fts_conn is not None:
            self._fts_conn.close()
        self._fts_conn = None
        self._fts_missing = False
        if self._fts_tmp_path:
            with contextlib.suppress(OSError):
                for suffix in ("", "-wal", "-shm"):
                    os.unlink(self._fts_tmp_path + suffix)
            self._fts_tmp_path = None
        self._init_caches()

    def _open(self, db_path: str) -> sqlite3.Connection:
        """Open the database read-only; fall back to a temp copy if locked."""
        conn = sqlite3.connect(db_uri_ro(db_path), uri=True)
        try:
            conn.execute("SELECT 1 FROM books LIMIT 1")
            return conn
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" not in str(e).lower():
                raise
        # Calibre has the DB locked — snapshot through the backup API and
        # read from the copy.
        print(
            "NOTE: Database is locked (Calibre is running). "
            "Reading from a snapshot copy.",
            file=sys.stderr,
        )
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="cquarry_")
        os.close(fd)
        _snapshot_copy(db_path, tmp, self.SNAPSHOT_LEASH)
        self._tmp_path = tmp
        return sqlite3.connect(db_uri_ro(tmp), uri=True)

    def close(self) -> None:
        self.conn.close()
        if self._fts_conn is not None:
            self._fts_conn.close()
            self._fts_conn = None
        if self._tmp_path:
            with contextlib.suppress(OSError):
                for suffix in ("", "-wal", "-shm"):
                    os.unlink(self._tmp_path + suffix)
            self._tmp_path = None
        if self._fts_tmp_path:
            with contextlib.suppress(OSError):
                for suffix in ("", "-wal", "-shm"):
                    os.unlink(self._fts_tmp_path + suffix)
            self._fts_tmp_path = None

    def backup_to(self, dest: str) -> str:
        """Copy the library's ``metadata.db`` to ``dest`` as one consistent
        snapshot, via sqlite3's backup API (1.23).

        A plain file copy of main+``-wal``+``-shm`` can tear when Calibre
        checkpointed mid-copy; the backup API cannot. ``dest`` is created
        (an existing file is replaced wholesale) and returned as the
        absolute path. When this connection rides a locked-database
        snapshot copy, the snapshot is what gets copied -- reopen for a
        copy of the live file. Consumers backing up a library
        (CalibreQuarry's ``--backup`` shape) belong here instead of
        re-deriving the copy.
        """
        dest = os.path.abspath(os.path.expanduser(dest))
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        dst = sqlite3.connect(dest)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
        return dest

    def external_changes_detected(self) -> bool:
        """True when another connection has committed writes to this
        database file since this connection last looked (1.23), via
        ``PRAGMA data_version``.

        The cheap staleness token: long-lived holders (Hermitage, Carrel)
        poll this between user actions and call :meth:`refresh()` only when
        it answers True, instead of clearing every cache defensively. The
        answer stays True until a :meth:`refresh()` re-primes the baseline,
        so a polling loop cannot miss a change by reading twice. This
        connection's own reads never move the value; on a locked-database
        snapshot connection it can never answer True at all (the copy is
        isolated from the live file), which is exactly the reopen boundary
        :meth:`refresh` documents.
        """
        row = self.conn.execute("PRAGMA data_version").fetchone()
        version = row[0]
        if self._data_version is None:
            self._data_version = version  # first observation primes only
            return False
        return version != self._data_version

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Core queries ---

    def get_all_books(self) -> list[dict[str, Any]]:
        """Fetch all books with full metadata via joins. Results are cached.

        Rows deliberately omit comment text (it can be huge); the sanctioned
        reads are :meth:`get_comments` and ``get_book(include_comments=True)``.
        """
        if self._books_cache is not None:
            return self._books_cache
        cur = self.conn.cursor()
        cur.execute(_BOOK_SELECT + " ORDER BY b.author_sort, b.sort")
        # First row wins per book: odd schemas carry duplicate
        # books_ratings_link rows, and the LEFT JOIN would fan a book out
        # into one row per duplicate link.
        books: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for row in cur.fetchall():
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            books.append(dict(row))

        # Book UUIDs (per-book; the library-level UUID lives in library_id).
        # Ancient schemas without the column degrade to empty strings.
        uuidmap: dict[int, str] = {}
        try:
            for row in self.conn.execute("SELECT id, uuid FROM books"):
                uuidmap[row["id"]] = row["uuid"] or ""
        except sqlite3.OperationalError:
            pass  # schema predates the uuid column
        # Identifiers (EAV store: one value per type per book).
        imap: dict[int, dict[str, str]] = {}
        try:
            for row in self.conn.execute("SELECT book, type, val FROM identifiers"):
                imap.setdefault(row["book"], {})[row["type"]] = row["val"]
        except sqlite3.OperationalError:
            pass  # schema predates the identifiers table

        # Authors with their secondary columns (true sort key, link URL) as
        # arrays parallel to `authors` — link-table order throughout. Ancient
        # schemas without authors.sort/link degrade to empty strings.
        amap: dict[int, list[str]] = {}
        asortmap: dict[int, list[str]] = {}
        alinkmap: dict[int, list[str]] = {}
        try:
            rows = self.conn.execute(
                "SELECT bal.book, a.name, a.sort, a.link "
                "FROM books_authors_link bal JOIN authors a ON a.id = bal.author "
                "ORDER BY bal.id"
            )
            for row in rows:
                bid = row["book"]
                amap.setdefault(bid, []).append(row["name"])
                asortmap.setdefault(bid, []).append(row["sort"] or "")
                alinkmap.setdefault(bid, []).append(row["link"] or "")
        except sqlite3.OperationalError:
            for row in self.conn.execute(
                "SELECT bal.book, a.name FROM books_authors_link bal "
                "JOIN authors a ON a.id = bal.author ORDER BY bal.id"
            ):
                bid = row["book"]
                amap.setdefault(bid, []).append(row["name"])
                asortmap.setdefault(bid, []).append("")
                alinkmap.setdefault(bid, []).append("")
        # Tags
        tmap = {}
        for row in self.conn.execute(
            "SELECT btl.book, t.name FROM books_tags_link btl JOIN tags t ON t.id = btl.tag ORDER BY t.name"
        ):
            tmap.setdefault(row["book"], []).append(row["name"])
        # Languages: Calibre orders a book's languages by the link table's
        # item_order column (link id as tiebreaker); schemas predating
        # item_order keep plain insertion order.
        lmap: dict[int, list[str]] = {}
        try:
            lang_rows = list(
                self.conn.execute(
                    "SELECT bll.book, l.lang_code FROM books_languages_link bll "
                    "JOIN languages l ON l.id = bll.lang_code "
                    "ORDER BY bll.book, bll.item_order, bll.id"
                )
            )
        except sqlite3.OperationalError:
            lang_rows = self.conn.execute(
                "SELECT bll.book, l.lang_code FROM books_languages_link bll "
                "JOIN languages l ON l.id = bll.lang_code ORDER BY bll.book, bll.id"
            )
        for row in lang_rows:
            lmap.setdefault(row["book"], []).append(row["lang_code"])
        # Formats
        fmap = {}
        for row in self.conn.execute("SELECT book, format FROM data"):
            fmap.setdefault(row["book"], []).append(row["format"])
        # Total on-disk size across all formats (for the size: search location)
        smap = {}
        try:
            for row in self.conn.execute(
                "SELECT book, SUM(uncompressed_size) as total FROM data GROUP BY book"
            ):
                smap[row["book"]] = row["total"]
        except sqlite3.OperationalError:
            pass  # very old schemas may lack uncompressed_size

        # Page counts: Calibre now manages them natively in books_pages_link;
        # older conventions put them in an int custom column labelled 'pages'.
        pmap = self._page_counts()

        for b in books:
            b["authors"] = amap.get(b["id"], [])
            b["author_sorts"] = asortmap.get(b["id"], [])
            b["author_links"] = alinkmap.get(b["id"], [])
            b["tags"] = tmap.get(b["id"], [])
            b["languages"] = lmap.get(b["id"], [])
            b["formats"] = fmap.get(b["id"], [])
            b["size"] = smap.get(b["id"])
            b["pages"] = pmap.get(b["id"])
            b["uuid"] = uuidmap.get(b["id"], "")
            b["identifiers"] = imap.get(b["id"], {})

        self._books_cache = books
        return self._books_cache

    def _page_counts(self) -> dict[int, int]:
        """Book-id -> page-count map, native table first, custom column second.

        ``books_pages_link`` is upstream-managed (cache.py maintains it and
        ships the CountPages plugin results there); when absent, fall back to
        an int/float custom column labelled 'pages'. Both lookups are cached.
        """
        if self._pages_cache is not None:
            return self._pages_cache
        pages: dict[int, int] = {}
        cur = self.conn.cursor()
        try:
            for row in cur.execute(
                "SELECT book, pages FROM books_pages_link WHERE pages IS NOT NULL"
            ):
                pages[row["book"]] = int(row["pages"])
        except sqlite3.OperationalError:
            pass  # schema predates the native table
        if not pages:
            col = self._pages_column()
            if col is not None:
                for bid, val in self.load_custom_column(col["name"]).items():
                    if isinstance(val, (int, float)):
                        pages[bid] = int(val)
        self._pages_cache = pages
        return self._pages_cache

    def get_page_metadata(
        self, book_id: int | None = None
    ) -> dict[int, dict[str, Any]]:
        """The full ``books_pages_link`` row per book: provenance for the
        displayed page count.

        ``pages`` alone comes through :meth:`get_all_books` /
        :meth:`get_book` / ``field()``; this surfaces the auxiliary columns
        Calibre maintains beside the count: ``algorithm`` (the CountPages
        profile that produced it), ``format`` (the format it was computed
        from), ``format_size``, ``timestamp``, and ``needs_scan`` (True =
        Calibre has queued a recount, so the stored count is stale until
        that runs). ``needs_scan`` is surfaced as a bool. Returns
        ``{book_id: {...}}`` in ascending id order, or an empty dict on
        schemas predating the native table (the ``#pages`` custom-column
        fallback is invisible here, exactly as it is to the native rows).
        """
        sql = (
            "SELECT book, pages, algorithm, format, format_size, "
            "timestamp, needs_scan FROM books_pages_link"
        )
        params: tuple = ()
        if book_id is not None:
            sql += " WHERE book = ?"
            params = (book_id,)
        sql += " ORDER BY book"
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return {}  # schema predates the native table
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            rec = dict(row)
            rec["needs_scan"] = bool(rec["needs_scan"])
            out[row["book"]] = rec
        return out

    def get_identifiers(self, book_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
        except sqlite3.OperationalError:
            return {}  # schema predates the identifiers table
        return {row["type"]: row["val"] for row in cur.fetchall()}

    def get_book(
        self, book_id: int, include_comments: bool = False
    ) -> dict[str, Any] | None:
        """Fetch a single hydrated book record without scanning the library.

        Returns the same shape as one ``get_all_books()`` row — including
        ``size``, ``uuid``, and ``identifiers`` — or None when the id does
        not exist. Rows deliberately omit comment text (it can be huge);
        pass ``include_comments=True`` to add a ``comments`` key with the
        raw stored HTML, or use :meth:`get_comments` for bulk reads.
        """
        cur = self.conn.cursor()
        cur.execute(_BOOK_SELECT + " WHERE b.id = ?", (book_id,))
        row = cur.fetchone()
        if row is None:
            return None
        b = dict(row)
        try:
            cur.execute(
                "SELECT a.name, a.sort, a.link FROM books_authors_link bal "
                "JOIN authors a ON a.id = bal.author WHERE bal.book = ? "
                "ORDER BY bal.id",
                (book_id,),
            )
            rows = cur.fetchall()
            b["authors"] = [r["name"] for r in rows]
            b["author_sorts"] = [r["sort"] or "" for r in rows]
            b["author_links"] = [r["link"] or "" for r in rows]
        except sqlite3.OperationalError:
            cur.execute(
                "SELECT a.name FROM books_authors_link bal "
                "JOIN authors a ON a.id = bal.author WHERE bal.book = ? "
                "ORDER BY bal.id",
                (book_id,),
            )
            rows = cur.fetchall()
            b["authors"] = [r["name"] for r in rows]
            b["author_sorts"] = [""] * len(rows)
            b["author_links"] = [""] * len(rows)
        cur.execute(
            "SELECT t.name FROM books_tags_link btl "
            "JOIN tags t ON t.id = btl.tag WHERE btl.book = ? ORDER BY t.name",
            (book_id,),
        )
        b["tags"] = [r["name"] for r in cur.fetchall()]
        # Languages follow the link table's item_order (see get_all_books).
        try:
            lang_rows = list(
                cur.execute(
                    "SELECT l.lang_code FROM books_languages_link bll "
                    "JOIN languages l ON l.id = bll.lang_code WHERE bll.book = ? "
                    "ORDER BY bll.item_order, bll.id",
                    (book_id,),
                )
            )
        except sqlite3.OperationalError:
            lang_rows = cur.execute(
                "SELECT l.lang_code FROM books_languages_link bll "
                "JOIN languages l ON l.id = bll.lang_code WHERE bll.book = ? "
                "ORDER BY bll.id",
                (book_id,),
            ).fetchall()
        b["languages"] = [r["lang_code"] for r in lang_rows]
        cur.execute("SELECT format FROM data WHERE book = ?", (book_id,))
        b["formats"] = [r["format"] for r in cur.fetchall()]
        try:
            srow = cur.execute(
                "SELECT SUM(uncompressed_size) AS total FROM data WHERE book = ?",
                (book_id,),
            ).fetchone()
            b["size"] = srow["total"] if srow is not None else None
        except sqlite3.OperationalError:
            b["size"] = None  # very old schemas lack uncompressed_size
        try:
            urow = cur.execute(
                "SELECT uuid FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            b["uuid"] = (urow["uuid"] or "") if urow is not None else ""
        except sqlite3.OperationalError:
            b["uuid"] = ""  # schema predates the uuid column
        b["identifiers"] = self.get_identifiers(book_id)
        b["pages"] = self._page_counts().get(book_id)
        if include_comments:
            b["comments"] = self.field(book_id, "comments")
        return b

    def get_book_by_uuid(self, uuid: str) -> dict[str, Any] | None:
        """Fetch one hydrated book by its per-book ``uuid`` (1.24).

        The Calibre-Companion endpoint dependency (upstream
        ``lookup_by_uuid``): mobile clients cache libraries by book uuid and
        come back asking for the row. Matching is case-insensitive (uuids
        are stored lowercase, but hand-built rows may not be); an unknown
        uuid is None, never an error. The row is the standard
        :meth:`get_book` shape."""
        if not uuid or not str(uuid).strip():
            return None
        row = self.conn.execute(
            "SELECT id FROM books WHERE uuid = ? COLLATE NOCASE", (str(uuid).strip(),)
        ).fetchone()
        if row is None:
            return None
        return self.get_book(row["id"])

    def get_comments(self, book_id: int | None = None) -> dict[int, str]:
        """Raw comments HTML keyed by book id.

        Only books that actually have a comments row appear; pass
        ``book_id`` to scope the read. Rows from ``get_book()`` /
        ``get_all_books()`` deliberately omit comment text (it can be
        huge) — this and ``get_book(include_comments=True)`` are the
        sanctioned reads. Pass results through
        :func:`cquarry.helpers.strip_html` before rendering.
        """
        try:
            if book_id is not None:
                row = self.conn.execute(
                    "SELECT text FROM comments WHERE book = ?", (book_id,)
                ).fetchone()
                return {book_id: row["text"] or ""} if row else {}
            return {
                row["book"]: row["text"] or ""
                for row in self.conn.execute("SELECT book, text FROM comments")
            }
        except sqlite3.OperationalError:
            return {}  # schema predates the comments table

    def search_books(self, query: str) -> list[dict[str, Any]]:
        """Search with Calibre grammar and return the hydrated matching books."""
        ids = self.search(query)
        return [b for b in self.get_all_books() if b["id"] in ids]

    # The default facet set: the browse-bar fields a result set is narrowed
    # by. Custom columns join by explicit "#label" location.
    _FACET_LOCATIONS = (
        "authors",
        "tags",
        "series",
        "publisher",
        "languages",
        "formats",
        "rating",
    )

    def facet_counts(
        self, query: str, *, locations: Sequence[str] | None = None
    ) -> dict[str, list[tuple[Any, int]]]:
        """Browse facets over a restricted search result (1.24).

        Per-value counts of every field the RESULT SET carries: search
        first, then count, so the facets answer "what is in here" and not
        "what is in the library" (the distinction
        :meth:`get_tag_browser_counts` cannot make; its views are
        whole-library). The default facet set is the browse-bar fields
        (:data:`_FACET_LOCATIONS`); custom columns join by explicit
        ``#label`` location, values exactly as :meth:`field` yields them
        (ratings in stars). Returns ``{location: [(value, count), ...]}``
        per location, count-descending then value-ascending; empty and
        None values produce no facet entry. Unknown locations raise
        ValueError. The restriction plumbing is
        :meth:`facet_counts_for_ids`, which this composes with
        :meth:`search`.
        """
        return self.facet_counts_for_ids(self.search(query), locations=locations)

    def facet_counts_for_ids(
        self, ids: set[int] | None, locations: Sequence[str] | None = None
    ) -> dict[str, list[tuple[Any, int]]]:
        """Per-value counts over a caller-restricted book-id set (1.24).

        The Phase 18 seam: any id set works (a search result, a virtual
        library, a hand-picked shelf); ``None`` means the whole library.
        See :meth:`facet_counts` for the counting rules.
        """
        if locations is None:
            locations = self._FACET_LOCATIONS
        custom_labels = self._custom_by_label()
        for location in locations:
            if location in self._FACET_LOCATIONS:
                continue
            if location.startswith("#") and location[1:].lower() in custom_labels:
                continue
            raise ValueError(
                f"Unknown facet location {location!r}. Builtins: "
                + ", ".join(self._FACET_LOCATIONS)
                + "; custom columns by #label"
            )
        wanted = ids
        out: dict[str, list[tuple[Any, int]]] = {}
        for location in locations:
            counts: dict[Any, int] = {}
            for b in self.get_all_books():
                if wanted is not None and b["id"] not in wanted:
                    continue
                if location.startswith("#"):
                    value: Any = self._custom_value(b["id"], location)
                elif location == "rating":
                    # Stars, exactly as the engine's field() yields them;
                    # the hydrated row stores the internal 0-10 value.
                    value = calibre_rating_to_stars(b.get("rating"))
                else:
                    value = b.get(location)
                if value is None or value == "":
                    continue
                values = value if isinstance(value, (list, tuple)) else [value]
                for one in values:
                    if one is None or (isinstance(one, str) and not one.strip()):
                        continue
                    counts[one] = counts.get(one, 0) + 1
            out[location] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return out

    def _date_field_values(
        self, field: str, ids: Sequence[int] | None
    ) -> dict[int, Any]:
        """Per-book raw date values for ``books_by_year``/``books_by_month``.

        ``field`` is a builtin date location (``pubdate``, ``timestamp``,
        ``last_modified``) or a date-typed custom column by ``#label``, bare
        label, or display name; anything else raises ValueError. Custom
        columns read through :meth:`load_custom_column` (cached like every
        read); builtin fields off the hydrated rows.
        """
        if field in ("pubdate", "timestamp", "last_modified"):
            rows = self.get_all_books()
            values = {b["id"]: b[field] for b in rows}
        else:
            col = self.find_custom_column(field)
            if col is None:
                raise ValueError(f"Unknown date field: {field!r}")
            if col["datatype"] != "datetime":
                raise ValueError(
                    f"{field!r} is a {col['datatype']} column, not a date field"
                )
            values = self.load_custom_column(col["name"])
        if ids is not None:
            wanted = set(ids)
            values = {k: v for k, v in values.items() if k in wanted}
        return values

    @staticmethod
    def _date_bucket(value: Any, width: int) -> tuple[int, ...] | None:
        """(year[, month]) integers from a stored date, None when undated.

        Accepts the stored ISO text (``YYYY-MM-DD ...``, the schema's shape)
        and real ``datetime`` objects. The 0101/0100 undefined-date sentinels
        and blank values land here as None -- cquarry treats them as no-date
        everywhere (the search engine's rule), and hunting them is
        :func:`cquarry.integrity.find_sentinel_pubdates`' job, not a bucket's.
        """
        if isinstance(value, datetime):
            return (value.year, value.month)[:width]
        if not isinstance(value, str) or not value.strip():
            return None
        head = value.strip()[: width * 3 + 1]  # 'YYYY' or 'YYYY-MM'
        parts = head.split("-")
        if len(parts) < width:
            return None
        try:
            bucket = tuple(int(p) for p in parts[:width])
        except ValueError:
            return None
        if bucket[0] in (100, 101):  # the undefined-date sentinels
            return None
        return bucket

    def books_by_year(
        self, field: str = "pubdate", ids: Sequence[int] | None = None
    ) -> dict[int, set[int]]:
        """Books bucketed by year over any date field (upstream
        ``Cache.books_by_year``): ``{year: {book_ids}}``.

        ``field`` is a builtin date location or a date-typed custom column
        (see :meth:`_date_field_values`); ``ids`` restricts the books
        counted (the browse-over-a-search-result shape). Books with no
        value -- including the 0101/0100 sentinel dates -- appear nowhere.
        Years key as plain ints, ascending order not guaranteed (dict over
        insertion; sort keys before display).
        """
        out: dict[int, set[int]] = {}
        for book_id, value in self._date_field_values(field, ids).items():
            bucket = self._date_bucket(value, 1)
            if bucket is not None:
                out.setdefault(bucket[0], set()).add(book_id)
        return out

    def books_by_month(
        self, field: str = "pubdate", ids: Sequence[int] | None = None
    ) -> dict[tuple[int, int], set[int]]:
        """Books bucketed by (year, month) over any date field (upstream
        ``Cache.books_by_month``): ``{(year, month): {book_ids}}``.

        Same contract as :meth:`books_by_year`, one level finer; the key is
        the ``(year, month)`` tuple.
        """
        out: dict[tuple[int, int], set[int]] = {}
        for book_id, value in self._date_field_values(field, ids).items():
            bucket = self._date_bucket(value, 2)
            if bucket is not None:
                out.setdefault(bucket, set()).add(book_id)
        return out

    def get_next_series_num_for(
        self, series: str, field: str = "series", current_indices: bool = False
    ) -> float | dict[int, float]:
        """The preference-aware next series number (upstream
        ``Cache.get_next_series_num_for``): what Calibre's own "next in
        series" would assign to a new book in ``series``.

        ``field`` is the builtin ``series`` location (default) or a
        series-typed custom column by ``#label``, bare label, or display
        name. The behavior follows the ``series_index_auto_increment``
        setting: a number is returned verbatim (and also for a series with
        no books), ``"next"`` is the highest index plus one,
        ``"first_free"``/``"next_free"``/``"last_free"`` fill the
        smallest/lowest-anchored/largest-anchored gap, and an unknown value
        degrades to 1.0 (upstream's fallback). One boundary named: upstream
        reads this from its tweaks files (``default_tweaks.py`` plus the
        user's ``tweaks.py``), which are process-side state metadata.db
        never carries -- this reads the library's ``preferences`` table
        instead (a consumer can write the row there) and falls back to
        upstream's shipped default ``"next"``; a local tweaks.py override is
        invisible to any database-side reader. Indices are compared as
        floats; ``current_indices=True`` returns the members'
        ``{book_id: index}`` map instead of the next number.
        """
        pref = self.get_preference("series_index_auto_increment", "next")

        def _next_from(indices: list[float]) -> float:
            if isinstance(pref, (int, float)) and not isinstance(pref, bool):
                return float(pref)
            ordered = sorted(indices, key=lambda s: s or 0)
            if not ordered:
                return 1.0
            if pref == "next":
                return float(math.floor(ordered[-1])) + 1
            if pref == "first_free":
                return float(next(i for i in range(1, 10000) if i not in ordered))
            if pref == "next_free":
                return float(
                    next(
                        i
                        for i in range(math.ceil(ordered[0]), 10000)
                        if i not in ordered
                    )
                )
            if pref == "last_free":
                for i in range(math.ceil(ordered[-1]), 0, -1):
                    if i not in ordered:
                        return float(i)
                return float(ordered[-1]) + 1
            return 1.0

        if field == "series":
            index_map = {
                b["id"]: b["series_index"]
                for b in self.get_all_books()
                if b["series"]
                and b["series"].lower() == (series or "").lower()
                and isinstance(b["series_index"], (int, float))
            }
        else:
            col = self.find_custom_column(field)
            if col is None:
                raise ValueError(f"Unknown series field: {field!r}")
            if col["datatype"] != "series":
                raise ValueError(
                    f"{field!r} is a {col['datatype']} column, not a series field"
                )
            name = col["name"]
            values = self.load_custom_column(name)
            token = "#" + col["label"]
            if token not in self._custom_val_cache:
                self._custom_val_cache[token] = values
            index_map = {
                book_id: extra
                for book_id, extra in self._custom_val_cache.get(
                    token + "_index", {}
                ).items()
                if values.get(book_id)
                and values[book_id].lower() == (series or "").lower()
                and isinstance(extra, (int, float))
            }
        if current_indices:
            return index_map
        return _next_from(list(index_map.values()))

    def get_format_path(self, book_id: int, fmt: str, verify: bool = True) -> str:
        """Resolve the absolute filesystem path of a book's format file.

        Builds ``<library root>/<books.path>/<data.name>.<lower(fmt)>`` per
        Calibre's storage layout. The library root is derived from the original
        database location (not a lock-escape snapshot). Raises ValueError when
        the book or format is unknown and FileNotFoundError when ``verify`` is
        set and the file is missing on disk.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT path FROM books WHERE id = ?", (book_id,))
        brow = cur.fetchone()
        if brow is None:
            raise ValueError(f"Book {book_id} not found")
        cur.execute(
            "SELECT name FROM data WHERE book = ? AND upper(format) = upper(?)",
            (book_id, fmt),
        )
        drow = cur.fetchone()
        if drow is None:
            avail = [
                r["format"]
                for r in cur.execute(
                    "SELECT format FROM data WHERE book = ?", (book_id,)
                )
            ]
            raise ValueError(
                f"Book {book_id} has no {fmt.upper()} format. Available: "
                f"{', '.join(avail) or 'none'}"
            )
        root = os.path.dirname(os.path.abspath(self.db_path))
        path = os.path.join(root, brow["path"], drow["name"] + "." + fmt.lower())
        if verify and not os.path.exists(path):
            raise FileNotFoundError(f"Format file missing on disk: {path}")
        return path

    def get_formats(self, book_id: int) -> dict[str, dict[str, Any]]:
        """Per-format detail for a book: ``{fmt: {path, size_bytes, name}}``.

        ``path`` follows Calibre's storage layout from the original DB location
        (not verified against disk — pair with ``os.path.exists`` or use
        :meth:`get_format_path` for verification). ``size_bytes`` is the
        catalogued uncompressed size (None on schemas lacking the column);
        ``name`` is the filename stem Calibre stores in ``data.name``.
        Returns ``{}`` for unknown books.
        """
        cur = self.conn.cursor()
        brow = cur.execute("SELECT path FROM books WHERE id = ?", (book_id,)).fetchone()
        if brow is None:
            return {}
        root = os.path.dirname(os.path.abspath(self.db_path))
        out: dict[str, dict[str, Any]] = {}
        try:
            rows = cur.execute(
                "SELECT format, name, uncompressed_size FROM data WHERE book = ?",
                (book_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = cur.execute(
                "SELECT format, name, NULL as uncompressed_size FROM data WHERE book = ?",
                (book_id,),
            ).fetchall()
        for row in rows:
            fmt = row["format"]
            if not fmt:
                continue
            out[fmt.upper()] = {
                "path": os.path.join(
                    root, brow["path"], row["name"] + "." + fmt.lower()
                ),
                "size_bytes": row["uncompressed_size"],
                "name": row["name"],
            }
        return out

    def get_cover_path(self, book_id: int, verify: bool = True) -> str | None:
        """Resolve a book's cover image path (``cover.jpg``, falling back to
        ``cover.png``).

        Builds ``<library root>/<books.path>/cover.jpg`` per Calibre's storage
        layout from the original DB location (snapshot-safe). With ``verify``
        set (default), returns None instead of a path when no cover file
        exists on disk; without it, returns the .jpg path unconditionally so
        callers can distinguish 'catalogued' from 'present'. Raises ValueError
        when the book is unknown.
        """
        cur = self.conn.cursor()
        brow = cur.execute(
            "SELECT path, has_cover FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if brow is None:
            raise ValueError(f"Book {book_id} not found")
        root = os.path.dirname(os.path.abspath(self.db_path))
        jpg = os.path.join(root, brow["path"], "cover.jpg")
        png = os.path.join(root, brow["path"], "cover.png")
        if not verify:
            return jpg
        if os.path.exists(jpg):
            return jpg
        if os.path.exists(png):
            return png
        return None

    def get_cover_bytes(self, book_id: int) -> bytes | None:
        """A book's raw cover image bytes (``cover.jpg``, falling back to
        ``cover.png``), or None when no cover file exists on disk.

        The bytes half of :meth:`get_cover_path` (upstream ``Cache.cover()``,
        the bytestring mode web frontends serve and thumbnailers consume);
        resolution and the unknown-book ValueError are exactly
        :meth:`get_cover_path`'s. The catalogued ``has_cover`` flag is not
        consulted: the file's presence is the answer, so a catalogued-but-
        missing cover reads as None the same way the verified path read does.
        """
        path = self.get_cover_path(book_id)
        if path is None:
            return None
        with open(path, "rb") as f:
            return f.read()

    def get_cover_last_modified(self, book_id: int) -> datetime | None:
        """The cover file's mtime as a UTC datetime, or None without a file.

        The conditional-GET half (upstream ``cover_last_modified``): a web
        frontend compares this against its cached copy's timestamp instead of
        re-serving bytes that have not changed. Resolution follows
        :meth:`get_cover_path`; the datetime is timezone-aware UTC (upstream
        surfaces a naive UTC stamp).
        """
        path = self.get_cover_path(book_id)
        if path is None:
            return None
        return datetime.fromtimestamp(os.stat(path).st_mtime, tz=UTC)

    def format_hash(self, book_id: int, fmt: str) -> str:
        """A format file's SHA-256 hex digest (upstream ``Cache.format_hash``).

        This is the file-changed detector: the FTS sidecar's
        ``format_hash``/``text_hash`` columns are compared against it to
        decide whether Calibre's extracted text is stale, and frontends use
        it the same way to detect on-disk edits. Resolution rides
        :meth:`get_format_path` (verified against disk), so its errors are
        this API's: ``ValueError`` for an unknown book or format,
        ``FileNotFoundError`` when the catalogued file is absent.
        """
        path = self.get_format_path(book_id, fmt)
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def format_metadata(self, book_id: int, fmt: str) -> dict[str, Any]:
        """A format file's on-disk facts (upstream ``Cache.format_metadata``):
        ``{"path", "size", "mtime"}``.

        ``path`` is the verified resolution (:meth:`get_format_path`'s
        errors apply), ``size`` the file's real byte count -- which can
        drift from the catalogued ``uncompressed_size`` until Calibre
        rescans -- and ``mtime`` a timezone-aware UTC ``datetime``. Empty
        dict is never returned: an unresolvable pair raises.
        """
        path = self.get_format_path(book_id, fmt)
        st = os.stat(path)
        return {
            "path": path,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC),
        }

    def read_backup(self, book_id: int) -> bytes | None:
        """Calibre's stored sidecar ``metadata.opf`` for a book, as bytes.

        Upstream ``Cache.read_backup``: the backup Calibre's own thread
        writes after every metadata change, readable so a caller can diff
        Calibre's last write against the rows (a sync auditor's ground
        truth). This READS what Calibre wrote and never generates an OPF;
        the generation family stays declined. None when the book has no
        directory or no backup file yet (upstream's missing-file answer);
        ``ValueError`` for an unknown book, like every path-riding read.
        """
        cur = self.conn.cursor()
        brow = cur.execute("SELECT path FROM books WHERE id = ?", (book_id,)).fetchone()
        if brow is None:
            raise ValueError(f"Book {book_id} not found")
        if not brow["path"]:
            return None  # nowhere to look, the empty-path rule
        path = os.path.join(
            os.path.dirname(os.path.abspath(self.db_path)),
            brow["path"],
            "metadata.opf",
        )
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def get_size_stats(self) -> dict[str, int]:
        """The library's file sizes in bytes (upstream ``Cache.size_stats``):
        ``{"main", "fts", "notes"}``.

        ``main`` is metadata.db itself, ``fts`` the full-text-search.db
        sidecar (0 when absent), ``notes`` always 0 here -- the notes DB is
        a recorded decline (``.calnotes/`` is never read), kept in the shape
        so consumers can render the same three columns.
        """
        main_size = 0
        with contextlib.suppress(OSError):
            main_size = os.path.getsize(self.db_path)
        fts_size = 0
        fts_path = os.path.join(
            os.path.dirname(os.path.abspath(self.db_path)), "full-text-search.db"
        )
        with contextlib.suppress(OSError):
            fts_size = os.path.getsize(fts_path)
        return {"main": main_size, "fts": fts_size, "notes": 0}

    def is_fts_enabled(self) -> bool:
        """Whether Calibre's FTS indexing is switched on for this library
        (upstream ``Cache.is_fts_enabled``, database-side).

        Reads the ``fts_enabled`` preference (upstream's default False);
        the in-process extraction-pool state the property form answers is
        GUI/process state metadata.db cannot carry. The sidecar's presence
        is a separate question: the sidecar reads degrade to empty when
        ``full-text-search.db`` is missing regardless of this flag.
        """
        return bool(self.get_preference("fts_enabled", False))

    def get_all_link_maps_for_book(self, book_id: int) -> dict[str, dict[str, str]]:
        """All of one book's entity links in one map (upstream
        ``Cache.get_all_link_maps_for_book``):
        ``{field: {value: link_url}}``.

        Builtin fields first -- authors, publisher, series, tags, the four
        upstream maps -- then every custom column that carries a link for
        this book through cquarry's ``custom_column_links`` seam, keyed by
        ``#label``. Empty fields are omitted, so an unlinked book answers
        ``{}``; unknown books answer ``{}`` too (upstream's ``_has_id``
        rule). Schemas whose entity tables predate the ``link`` column
        degrade field by field.
        """
        out: dict[str, dict[str, str]] = {}
        for field, etable, ltable, fk in (
            ("authors", "authors", "books_authors_link", "author"),
            ("publisher", "publishers", "books_publishers_link", "publisher"),
            ("series", "series", "books_series_link", "series"),
            ("tags", "tags", "books_tags_link", "tag"),
        ):
            try:
                rows = self.conn.execute(
                    f"SELECT e.name AS name, e.link AS link FROM {ltable} l "
                    f"JOIN {etable} e ON e.id = l.{fk} WHERE l.book = ?",
                    (book_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # table or link column predates this schema
            links = {r["name"]: r["link"] for r in rows if r["link"]}
            if links:
                out[field] = links
        for col in self.get_custom_columns().values():
            # custom_column_links fills as a side effect of load_custom_column:
            # load first, then look the book's url up.
            value = self.load_custom_column(col["name"]).get(book_id)
            url = self.custom_column_links(col["name"]).get(book_id)
            if not url:
                continue
            names = value if isinstance(value, (list, tuple)) else [value]
            out["#" + col["label"]] = {str(v): url for v in names if v is not None}
        return out

    def list_data_files(self, book_id: int) -> list[dict[str, Any]]:
        """Every file under the book's ``data/`` directory (upstream's
        extra-files family, the ``DATA_FILE_PATTERN`` half the content
        server serves): ``[{relpath, path, size, mtime}]``.

        ``relpath`` is the forward-slash path under ``data/`` (the portable
        spelling every other verb takes); ``path`` the absolute location;
        ``size``/``mtime`` from ``os.stat``. Calibre's own book-directory
        residents (formats, covers, ``metadata.opf``) live outside ``data/``
        and never appear here. Sorted by ``relpath``; empty list when the
        book has no ``data/`` directory; ``ValueError`` for unknown books.
        """
        data_dir = self._book_data_dir(book_id)
        if data_dir is None:
            return []
        out: list[dict[str, Any]] = []
        for dirpath, _dirnames, filenames in os.walk(data_dir):
            for name in filenames:
                path = os.path.join(dirpath, name)
                relpath = os.path.relpath(path, data_dir).replace(os.sep, "/")
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                out.append(
                    {
                        "relpath": relpath,
                        "path": path,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
        out.sort(key=lambda entry: entry["relpath"])
        return out

    def get_data_file(self, book_id: int, relpath: str) -> bytes | None:
        """One extra file's bytes from the book's ``data/`` directory, or
        None when absent.

        ``relpath`` is the forward-slash path under ``data/``; traversal
        guards reject absolute spellings and any ``..`` component (a
        ``ValueError``, not a read outside the book). The bytes sibling of
        :meth:`read_backup`, for the files upstream's content server
        serves under ``data/``.
        """
        path = self._resolve_data_file(book_id, relpath)
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _book_data_dir(self, book_id: int) -> str | None:
        """The book's ``data/`` directory, or None when the book/dir is absent."""
        cur = self.conn.cursor()
        brow = cur.execute("SELECT path FROM books WHERE id = ?", (book_id,)).fetchone()
        if brow is None:
            raise ValueError(f"Book {book_id} not found")
        if not brow["path"]:
            return None
        book_dir = os.path.join(
            os.path.dirname(os.path.abspath(self.db_path)), brow["path"]
        )
        data_dir = os.path.join(book_dir, "data")
        return data_dir if os.path.isdir(data_dir) else None

    @staticmethod
    def _safe_data_relpath(relpath: str) -> list[str] | None:
        """Split a ``data/`` relpath into components, None when unsafe.

        Absolute spellings, empty components, ``.``, and ``..`` all refuse:
        the guard both the read and the write side share.
        """
        if not isinstance(relpath, str) or not relpath.strip():
            return None
        normalized = relpath.replace("\\", "/")
        if normalized.startswith("/"):
            return None
        parts = list(normalized.split("/"))
        if any(p in ("", ".", "..") for p in parts):
            return None
        return parts

    def _resolve_data_file(self, book_id: int, relpath: str) -> str | None:
        """Resolve one data-file relpath to an absolute path, safely.

        None when the book has no data dir or the file does not exist;
        ``ValueError`` for an unsafe relpath (the guard both sides share).
        """
        parts = self._safe_data_relpath(relpath)
        if parts is None:
            raise ValueError(f"Unsafe data-file path: {relpath!r}")
        data_dir = self._book_data_dir(book_id)
        if data_dir is None:
            return None
        path = os.path.join(data_dir, *parts)
        if not os.path.isfile(path):
            return None
        return path

    def format_path_index(self) -> dict[str, int]:
        """Map every catalogued format file path to its book id.

        One ``data ⋈ books`` query; each path is built exactly as
        :meth:`get_format_path` builds it (library root from the original DB
        location), keyed ``normcase(normpath())``: redundant separators and
        dot segments collapse, but ``normcase`` is the identity on POSIX, so
        differently-CASED spellings do NOT resolve (callers needing caseless
        lookup re-normalize, as bindery does with ``resolve().lower()``).
        Cached like the row cache: the database is read-only and the
        connection short-lived. Bindery's id resolver was the seed consumer;
        anything reverse-looking-up a file belongs here.
        """
        if self._format_path_index is not None:
            return self._format_path_index
        root = os.path.dirname(os.path.abspath(self.db_path))
        idx: dict[str, int] = {}
        try:
            rows = self.conn.execute(
                "SELECT d.book AS book, d.format AS format, d.name AS name, "
                "b.path AS path FROM data d JOIN books b ON b.id = d.book"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            fmt = row["format"]
            if not fmt:
                continue
            p = os.path.join(root, row["path"], row["name"] + "." + fmt.lower())
            idx[os.path.normcase(os.path.normpath(p))] = row["book"]
        self._format_path_index = idx
        return idx

    def find_book_by_path(self, path: str) -> int | None:
        """Reverse :meth:`format_path_index`: the book id owning this file.

        Accepts relative spellings and redundant separators/dot segments
        (``normcase``/``normpath`` on the lookup side); on POSIX ``normcase``
        is the identity, so differently-cased spellings do not resolve.
        Returns None when no catalogued format resolves there.
        """
        key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        return self.format_path_index().get(key)

    def find_candidate_duplicates(
        self, title: str, authors: list[str] | str, isbn: str | None = None
    ) -> list[dict[str, Any]]:
        """Library-side duplicate screening for the creation path: the
        search-parity complement of ``add_book``'s byte-identity floor.

        Rules, highest confidence first (mirroring the frontend screener
        that prompted this API): ISBN -- the book carries an ``isbn``
        identifier equal to the given one, separator- and case-insensitive;
        then title+author -- the normalized title (folded, subtitle and
        leading article scrubbed) AND the first author (folded) match
        exactly. Returns one ``{"id": ..., "matched_by": "isbn" |
        "title_author"}`` per matching book, sorted by id; a book matching
        both rules reports ``"isbn"``. Reads over the cached rows, so a
        caller screening a whole import batch should hold one connection.
        """
        if isinstance(authors, str):
            authors = [a.strip() for a in authors.split(",") if a.strip()]
        want_isbn = isbn_normalize(isbn) if isbn else ""
        want_author = _fold(authors[0]) if authors else ""
        want_title = _duplicate_title_key(title)
        out: list[dict[str, Any]] = []
        for b in self.get_all_books():
            rule = None
            if (
                want_isbn
                and b["identifiers"].get("isbn")
                and (isbn_normalize(b["identifiers"]["isbn"]) == want_isbn)
            ):
                rule = "isbn"
            elif (
                want_title
                and want_author
                and b["authors"]
                and _duplicate_title_key(b["title"]) == want_title
                and _fold(b["authors"][0]) == want_author
            ):
                rule = "title_author"
            if rule is not None:
                out.append({"id": b["id"], "matched_by": rule})
        return out

    def export_rows(
        self, *, ids: set[int] | None = None, include_custom: bool = True
    ) -> list[dict[str, Any]]:
        """Flat one-dict-per-book rows for exporters and CSV writers.

        Composes the hydrated rows with every custom column flattened in as
        a ``#label`` key (values exactly as :meth:`field` yields; ``None``
        when the book has no value, so the key set is uniform across rows;
        composite columns omitted -- computed, not stored), so a frontend
        needs no SQL of its own. ``rating`` stays the raw internal 0-10 row value and
        ``pubdate`` the raw TEXT (sentinel included): conversion and
        formatting are the renderer's job. ``ids`` restricts the result;
        rows are ordered as ``get_all_books()`` orders them.
        """
        out: list[dict[str, Any]] = []
        for b in self.get_all_books():
            if ids is not None and b["id"] not in ids:
                continue
            row = dict(b)
            if include_custom:
                for meta in self.get_custom_columns().values():
                    datatype = (meta.get("datatype") or "").lower()
                    if datatype == "composite":
                        continue
                    row["#" + (meta.get("label") or "")] = self.field(
                        b["id"], "#" + (meta.get("label") or "")
                    )
            out.append(row)
        return out

    def get_book_dossier(
        self, book_id: int, *, include_comments: bool = False
    ) -> dict[str, Any] | None:
        """The composed deep fetch frontends hand-assemble today.

        One call returns everything a detail view renders: ``book`` (the
        standard :meth:`get_book` row), ``cover_path`` (:meth:`get_cover_path`
        with defaults — the row's ``has_cover`` distinguishes catalogued-but-
        missing from present), ``formats`` (:meth:`get_formats`),
        ``custom_columns`` keyed by ``#label`` with ``{name, datatype, value}``
        (values exactly as the search engine's ``field()`` yields;
        comments-typed columns stay raw HTML), ``annotations``,
        ``reading_positions``, ``plugin_data``, and ``conversion_overrides``.
        ``comments`` (``{html, plain}``, plain via :func:`strip_html`) is added
        only when ``include_comments`` is set. Returns None for unknown books.
        """
        book = self.get_book(book_id)
        if book is None:
            return None
        dossier: dict[str, Any] = {
            "book": book,
            "cover_path": self.get_cover_path(book_id),
            "formats": self.get_formats(book_id),
            "custom_columns": {},
            "annotations": self.get_annotations(book_id),
            "reading_positions": self.get_last_read_positions(book_id),
            "plugin_data": self.get_plugin_data(book_id),
            "conversion_overrides": self.get_conversion_profiles(book_id),
        }
        for meta in self.get_custom_columns().values():
            label = "#" + (meta.get("label") or "")
            dossier["custom_columns"][label] = {
                "name": meta.get("name", ""),
                "datatype": meta.get("datatype", ""),
                "value": self.field(book_id, label),
            }
        if include_comments:
            html = self.get_comments(book_id).get(book_id, "")
            dossier["comments"] = {"html": html, "plain": strip_html(html)}
        return dossier

    def get_library_uuid(self) -> str | None:
        """The library's identity UUID from the ``library_id`` table.

        Book uuids are per-copy; this one identifies the library itself and
        survives moves/restores, making it the right key for consumers that
        cache state per library (a bundled copy of the same library yields a
        different UUID than the original). Returns None when the table or row
        is missing (very old schemas).
        """
        try:
            row = self.conn.execute("SELECT uuid FROM library_id LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return None
        return row["uuid"] if row else None

    def get_format_stats(self) -> dict[str, dict[str, int]]:
        """Per-format aggregates across the library: ``{fmt: {count, bytes}}``.

        ``count`` is how many books carry the format and ``bytes`` the total
        catalogued uncompressed size — one query for disk-usage reports
        instead of N x :meth:`get_formats`. Formats lacking size data report
        ``bytes`` as 0. Returns ``{}`` on schemas without a ``data`` table.
        """
        try:
            rows = self.conn.execute(
                "SELECT upper(format) AS fmt, COUNT(*) AS count, "
                "COALESCE(SUM(uncompressed_size), 0) AS bytes "
                "FROM data GROUP BY upper(format) ORDER BY fmt"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {
            row["fmt"]: {"count": row["count"], "bytes": row["bytes"]}
            for row in rows
            if row["fmt"]
        }

    def get_all_tags(self) -> list[str]:
        """Every distinct tag name, sorted alphabetically."""
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT name FROM tags ORDER BY name")
        return [row["name"] for row in cur.fetchall()]

    def get_tag_counts(self) -> list[tuple[str, int]]:
        """Return [(tag_name, book_count), ...] sorted by tag name."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT t.name as name, COUNT(btl.book) as count
            FROM tags t
            LEFT JOIN books_tags_link btl ON btl.tag = t.id
            GROUP BY t.id, t.name
            ORDER BY t.name
        """)
        return [(row["name"], row["count"]) for row in cur.fetchall()]

    def tag_rollup_ids(
        self, ids: Sequence[int] | None = None
    ) -> dict[str, frozenset[int]]:
        """The id-set sibling of :func:`cquarry.helpers.tag_rollup` (1.25):
        ``{tag_path: frozenset(book_ids)}`` for every node including implied
        ones.

        Only leaf tags may be assigned in a library; intermediate dot-path
        levels are implied by the name, so every prefix of every tag becomes
        a browsable node and accumulates its descendants' books -- the same
        rule the engine applies for ``tags:Fic.Fantasy``, so a browser built
        on these sets agrees with search by construction. Counts come from
        ``len()`` of each set, which is where the counts-only
        :func:`helpers.tag_rollup` lands when fed ``get_tag_counts()``.
        ``ids`` restricts the books counted (the browse-over-a-subset shape;
        None means the whole library). Promoted from Carrel's private
        ``_rollup`` with that waiver: the private copy stays until a
        consumer wave.
        """
        out: dict[str, set[int]] = {}
        for b in self.get_all_books():
            if ids is not None and b["id"] not in ids:
                continue
            for tag in b["tags"] or []:
                parts = [p for p in str(tag).split(".") if p]
                for i in range(1, len(parts) + 1):
                    out.setdefault(".".join(parts[:i]), set()).add(b["id"])
        return {k: frozenset(v) for k, v in out.items()}

    def precedent_tags(self, authors: list[str], limit: int = 12) -> list[str]:
        """Tag-by-precedent: distinct tags across the named authors' books.

        The curation prompt's suggestion source (a fresh import carries no
        tags; the same author's catalogued books do). Authors match
        case-insensitively; the result is capped at ``limit`` names,
        alphabetically ordered for stability. Promoted from
        CalibreQuarry's run.py (1.22): a four-table JOIN is an engine
        read, not frontend logic.
        """
        if not authors:
            return []
        marks = ",".join("?" * len(authors))
        rows = self.conn.execute(
            f"SELECT DISTINCT t.name FROM books_tags_link l "
            f"JOIN tags t ON t.id = l.tag "
            f"JOIN books_authors_link al ON al.book = l.book "
            f"JOIN authors a ON a.id = al.author "
            f"WHERE a.name COLLATE NOCASE IN ({marks}) "
            f"ORDER BY t.name LIMIT ?",
            (*authors, limit),
        ).fetchall()
        return [row[0] for row in rows]

    _LIST_BOOKS_SORT_KEYS = (
        "sort",
        "title",
        "timestamp",
        "pubdate",
        "rating",
        "series_index",
        "author_sort",
        "series",
        "id",
        "ids",
    )

    # Public API key -> hydrated-row field, where the two differ.
    _ROW_KEY_ALIASES = {"sort": "title_sort"}

    def list_books(
        self,
        *,
        ids: list[int] | None = None,
        sort: str | Sequence[str] = "sort",
        descending: bool = False,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Paginated, sorted book listing over the cached rows (cquarry 1.10).

        The read-side answer to "page me this slice of the library": web
        frontends resolve a book-id set through the search engine, then page
        it. Pure over get_all_books()'s cache — no SQL of its own.

        ``ids`` restricts the listing (None = whole library; the listing's
        order comes from ``sort``). ``sort`` is one key or a sequence of
        keys (primary first, one direction for all — author sort tie-breaks
        on series name then series index); each is one of ``sort``
        (Calibre's title-sort), ``title``, ``timestamp``, ``pubdate``,
        ``rating``, ``series_index``, ``author_sort``, ``series``, ``id``;
        None values sort last regardless of direction. The special key
        ``ids`` (since 1.21.0, requires ``ids``) replaces the sort instead
        of naming one: the rows come back in the caller's id sequence, so
        a frontend that carries its own ordering (relevance rank, shelf
        order, download counts) keeps it; a duplicated id keeps its first
        slot, ids absent from the library are skipped, ``descending``
        reverses the sequence, and ``offset``/``limit`` slice after the
        ordering (``limit=None`` runs to the end). Unknown keys raise
        ValueError.
        """
        keys = (sort,) if isinstance(sort, str) else tuple(sort)
        if not keys:
            raise ValueError("sort must name at least one key")
        for key in keys:
            if key not in self._LIST_BOOKS_SORT_KEYS:
                raise ValueError(
                    f"Unknown sort key {key!r}. Available: "
                    + ", ".join(self._LIST_BOOKS_SORT_KEYS)
                )
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")

        end = offset + limit if limit is not None else None
        if "ids" in keys:
            if len(keys) > 1:
                raise ValueError("sort='ids' must stand alone")
            if ids is None:
                raise ValueError("sort='ids' requires ids (the caller's id order)")
            rank: dict[int, int] = {}
            for n, bid in enumerate(ids):
                if bid not in rank:  # a duplicated id keeps its first slot
                    rank[bid] = n
            rows = [r for r in self.get_all_books() if r["id"] in rank]
            rows.sort(key=lambda r: rank[r["id"]], reverse=descending)
            return rows[offset:end]

        wanted = set(ids) if ids is not None else None
        rows = [r for r in self.get_all_books() if wanted is None or r["id"] in wanted]

        # Multi-key with one direction flag, None-valued keys last regardless
        # of direction; numbers (rating, series_index) and ISO-ish strings
        # both compare naturally within their type. The public ``sort`` key
        # names Calibre's title-sort, stored on the row as ``title_sort``
        # (v1.11.1: an absent row key used to make the sort a silent no-op
        # that only looked right because the cache arrives title-sorted).
        specs = []
        for key in keys:
            specs.append((self._ROW_KEY_ALIASES.get(key, key), descending))

        def _sort_value(row: dict[str, Any], row_key: str) -> Any:
            val = row.get(row_key)
            # The undefined-date sentinel sorts as dateless (the search
            # engine already treats it that way), not as a real early date.
            if (
                row_key == "pubdate"
                and isinstance(val, str)
                and val.startswith(("0101-01-01", "0100-01-01"))
            ):
                return None
            return val

        def _cmp(ra: dict[str, Any], rb: dict[str, Any]) -> int:
            for row_key, desc in specs:
                a, b = _sort_value(ra, row_key), _sort_value(rb, row_key)
                if a is None and b is None:
                    continue
                if a is None:
                    return 1
                if b is None:
                    return -1
                if a < b:
                    return 1 if desc else -1
                if a > b:
                    return -1 if desc else 1
            return 0

        rows.sort(key=functools.cmp_to_key(_cmp))

        return rows[offset:end]

    def get_all_series(self) -> list[dict[str, Any]]:
        """Return per-series rollups, computed in Python from get_all_books().

        Computing this here (rather than via SQL GROUP_CONCAT(... ORDER BY ...))
        keeps cquarry working on SQLite older than 3.44, where the in-aggregate
        ORDER BY is a syntax error.
        """
        groups: dict[str, dict[str, Any]] = {}
        for b in self.get_all_books():
            name = b["series"]
            if not name:
                continue
            g = groups.setdefault(name, {"indices": [], "titles": []})
            g["indices"].append(b["series_index"])
            g["titles"].append((b["series_index"], b["title"]))

        out: list[dict[str, Any]] = []
        for name in sorted(groups):
            g = groups[name]
            present = [i for i in g["indices"] if i is not None]
            present.sort()
            titles_sorted = [
                t
                for _, t in sorted(g["titles"], key=lambda x: (x[0] is None, x[0] or 0))
                if t
            ]
            out.append(
                {
                    "name": name,
                    "book_count": len(g["indices"]),
                    "indices": ",".join(str(i) for i in present),
                    "max_index": max(present) if present else None,
                    "titles": ",".join(titles_sorted),
                }
            )
        return out

    def _custom_columns_schema(self) -> dict[str, bool]:
        """Which optional columns the custom_columns table has (cached).

        ``editable``/``display``/``normalized`` arrived after the earliest
        schemas; consumers on ancient databases get documented defaults
        instead of an OperationalError.
        """
        if self._cc_schema_cache is None:
            cols = {
                row[1] for row in self.conn.execute("PRAGMA table_info(custom_columns)")
            }
            self._cc_schema_cache = {
                "editable": "editable" in cols,
                "display": "display" in cols,
                "normalized": "normalized" in cols,
            }
        return self._cc_schema_cache

    def get_custom_columns(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all custom columns, keyed by display name.

        Each value carries ``id``, ``label``, ``name``, ``datatype`` and
        ``is_multiple``, plus the display-config fields (cquarry >= 1.4):
        ``editable`` (bool), ``normalized`` (bool) and ``display`` — the
        decoded JSON blob holding ``enum_values``/``enum_colors``/
        ``composite_template`` etc. Schemas predating a column get the
        documented defaults (editable=True, normalized=False, display={}).

        The mapping stays keyed by display name (the historical key); column
        lookup that accepts ``#label`` or a bare label goes through
        :meth:`find_custom_column` / :meth:`load_custom_column`.
        """
        cur = self.conn.cursor()
        schema = self._custom_columns_schema()
        extra = []
        if schema["editable"]:
            extra.append("editable")
        if schema["normalized"]:
            extra.append("normalized")
        if schema["display"]:
            extra.append("display")
        select = "SELECT id, label, name, datatype, is_multiple"
        if extra:
            select += ", " + ", ".join(extra)
        try:
            cur.execute(select + " FROM custom_columns")
        except sqlite3.OperationalError:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            rec = dict(row)
            rec.setdefault("editable", True)
            rec.setdefault("normalized", False)
            raw_display = rec.pop("display", None) if schema["display"] else None
            decoded: Any = None
            if isinstance(raw_display, str) and raw_display.strip():
                try:
                    decoded = json.loads(raw_display)
                except json.JSONDecodeError:
                    decoded = None
            rec["display"] = decoded if isinstance(decoded, dict) else {}
            out[rec["name"]] = rec
        return out

    def get_all_formats(self) -> dict[int, list[str]]:
        """Every book's format list in one map: ``{book_id: [FMT, ...]}`` (1.24).

        The bulk shape of :meth:`get_formats`'s keys (Carrel's residue trio):
        a web frontend rendering a whole shelf of format badges takes one
        cache pass instead of one call per book. Values are the same native
        uppercase lists the hydrated rows carry; the map is cached like
        them (a long-lived holder's ``refresh()`` covers it)."""
        if self._all_formats_cache is None:
            self._all_formats_cache = {
                b["id"]: list(b["formats"]) for b in self.get_all_books()
            }
        return self._all_formats_cache

    def get_entities(self, kind: str) -> list[dict[str, Any]]:
        """Entity rows with secondary columns and book counts.

        ``kind`` is one of ``authors``, ``series``, ``publishers``,
        ``tags``, ``languages``, ``ratings``. Returns
        ``[{id, name, sort, link, count}]`` sorted by name — the data behind
        author cards and browse facets. ``sort``/``link`` are ``""`` when the
        entity has none or the schema predates the column; languages key
        their name under ``lang_code`` but still surface it as ``name``;
        ratings have no name column, so ``name`` carries the half-star
        integer as text. Unknown kinds raise ValueError.
        """
        table_map = {
            "authors": ("authors", "books_authors_link", "author"),
            "series": ("series", "books_series_link", "series"),
            "publishers": ("publishers", "books_publishers_link", "publisher"),
            "tags": ("tags", "books_tags_link", "tag"),
            "languages": ("languages", "books_languages_link", "lang_code"),
            "ratings": ("ratings", "books_ratings_link", "rating"),
        }
        if kind not in table_map:
            raise ValueError(
                f"Unknown entity kind {kind!r}. Available: {', '.join(sorted(table_map))}"
            )
        table, link_table, fk = table_map[kind]
        if kind == "ratings":
            # ratings has no `name` column; surface the rating value itself.
            name_expr, order_expr = "CAST(e.rating AS TEXT)", "e.rating"
        else:
            name_col = "lang_code" if kind == "languages" else "name"
            name_expr, order_expr = f"e.{name_col}", f"e.{name_col}"

        # Which secondary columns does this entity table have?
        cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        sort_expr = "COALESCE(sort, '')" if "sort" in cols else "''"
        link_expr = "COALESCE(link, '')" if "link" in cols else "''"
        pk = "id"
        try:
            rows = self.conn.execute(
                f"SELECT e.{pk} AS id, {name_expr} AS name, "
                f"{sort_expr} AS sort, {link_expr} AS link, "
                f"COUNT(l.book) AS count "
                f"FROM {table} e LEFT JOIN {link_table} l ON l.{fk} = e.{pk} "
                f"GROUP BY e.{pk} ORDER BY {order_expr} COLLATE NOCASE"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def get_entity_book_ids(self, kind: str, name: str) -> set[int]:
        """The book ids carrying one entity value: the id-set half of
        :meth:`get_entities` (1.24, Carrel's residue trio).

        ``kind`` is one of ``authors``, ``series``, ``publishers``,
        ``tags``, ``languages`` (the named entities; ratings have no name
        to resolve, a rating slice is ``search("rating:...")`'s job).
        Names match case-insensitively and exactly. Tags are the engine's
        anchored rule (see spec §3.2): ``Foo`` resolves Foo AND its
        ``Foo.*`` subtree, so a browse tree node's id set covers its
        children -- the shape Carrel's category pages build privately
        today. Pure over the cached rows; unknown names are an empty set,
        like the search engine's unknown-location rule."""
        named = {"authors", "series", "publishers", "tags", "languages"}
        kind = (kind or "").strip().lower()
        if kind not in named:
            raise ValueError(
                f"Unknown entity kind {kind!r}. Available: {', '.join(sorted(named))}"
            )
        name = (name or "").strip()
        if not name:
            return set()
        low = name.lower()
        out: set[int] = set()
        for b in self.get_all_books():
            # authors/tags/languages are native lists; series/publisher are
            # scalar strings on the hydrated rows.
            values = b.get(kind) or []
            if isinstance(values, str):
                values = [values]
            if kind == "tags":
                if any(
                    v.lower() == low or v.lower().startswith(low + ".") for v in values
                ):
                    out.add(b["id"])
            elif any(v.lower() == low for v in values):
                out.add(b["id"])
        return out

    def find_custom_column(self, key: str) -> dict[str, Any] | None:
        """One custom-columns record by ``#label``, bare label, or display name.

        A leading ``#`` matches the ``label`` only (labels are unique, so that
        is never ambiguous). Otherwise an exact display-name match wins — the
        historical key, so existing callers keep working — and a bare label is
        the graceful fallback. Label matching is case-insensitive, mirroring
        the write module's ``_custom_column_meta``. Returns None when nothing
        matches.
        """
        cols = self.get_custom_columns()
        key = key.strip()
        if key.startswith("#"):
            want = key[1:].lower()
            return next((c for c in cols.values() if c["label"].lower() == want), None)
        if key in cols:
            return cols[key]
        return next(
            (c for c in cols.values() if c["label"].lower() == key.lower()), None
        )

    def load_custom_column(self, col_name: str) -> dict[int, Any]:
        """Load values for one custom column, addressed by ``#label``, bare
        label, or display name (see :meth:`find_custom_column`). Returns
        {book_id: value(s)}."""
        col = self.find_custom_column(col_name)
        if col is None:
            cols = self.get_custom_columns()
            raise ValueError(
                f"Custom column '{col_name}' not found. Available (name → #label): "
                + ", ".join(f"{c['name']} (#{c['label']})" for c in cols.values())
            )
        if col["datatype"] == "composite":
            # The documented answer instead of a stderr warning from a
            # failed value-table probe: composite columns are computed by
            # Calibre's template engine and have no storage to read.
            return {}

        # int() before any f-string SQL: a corrupt store's TEXT id must hit
        # the table names as a number, never as raw SQL text.
        cid = int(col["id"])
        cur = self.conn.cursor()

        # Calibre normalizes text/enumeration/series columns into a value table
        # plus a books_custom_column_N_link table (regardless of is_multiple);
        # int/float/bool/datetime/comments are stored directly with a `book`
        # column. Detect which by whether the link table exists, rather than
        # keying off is_multiple (a single-valued enumeration is still
        # normalized, and SELECT book FROM its value table would error).
        link_table = f"books_custom_column_{cid}_link"
        has_link = bool(
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (link_table,),
            ).fetchone()
        )

        results: dict[int, Any] = {}
        try:
            if has_link:
                # Series custom columns carry the book's index in the link
                # table's `extra` float, and normalized value tables may
                # carry a `link` URL column (both optional on old schemas;
                # PRAGMA-probed so ancient layouts degrade to NULLs instead
                # of erroring). The index feeds the `#label_index` search
                # location; the links are exposed via custom_column_links().
                link_cols = {
                    r[1] for r in self.conn.execute(f"PRAGMA table_info({link_table})")
                }
                value_cols = {
                    r[1]
                    for r in self.conn.execute(
                        f"PRAGMA table_info(custom_column_{cid})"
                    )
                }
                extra_expr = "l.extra" if "extra" in link_cols else "NULL"
                link_expr = "c.link" if "link" in value_cols else "NULL"
                cur.execute(f"""
                    SELECT l.book, c.value, {extra_expr} AS extra, {link_expr} AS clink
                    FROM {link_table} l
                    JOIN custom_column_{cid} c ON c.id = l.value
                """)
                grouped: dict[int, list] = {}
                index_map: dict[int, Any] = {}
                link_map: dict[int, str] = {}
                for row in cur.fetchall():
                    grouped.setdefault(row["book"], []).append(row["value"])
                    if row["extra"] is not None:
                        index_map[row["book"]] = row["extra"]
                    if row["clink"]:
                        link_map[row["book"]] = row["clink"]
                # Every normalized column's link map is stashed, not just
                # series: custom_column_links() promises text/enumeration/
                # series/rating, and the value-table `link` column exists on
                # all of them (the stash used to be series-only, answering
                # empty for the other three).
                self._custom_link_cache["#" + col["label"]] = link_map
                if col["datatype"] == "series":
                    # Serve the registered-but-previously-unresolvable
                    # `#label_index` float location. An exact label that
                    # literally ends in `_index` keeps the token (exact
                    # label wins, mirroring find_custom_column), so the
                    # stash only happens for the derived spelling.
                    token = "#" + col["label"] + "_index"
                    if token not in self._custom_by_label():
                        self._custom_val_cache[token] = index_map
                if col["is_multiple"]:
                    # Native lists, never a comma-joined string: a stored
                    # value like "Doe, John" is ONE value, and re-splitting
                    # on commas used to turn it into phantom values.
                    return {k: list(vals) for k, vals in grouped.items()}
                # Single-valued normalized column (text, enumeration): one value.
                return {k: vals[0] for k, vals in grouped.items()}
            # Stored directly (int, float, bool, datetime, comments).
            cur.execute(f"SELECT book, value FROM custom_column_{cid}")
            for row in cur.fetchall():
                results[row["book"]] = row["value"]
            return results
        except sqlite3.OperationalError as e:
            print(
                f"Warning: could not read custom column '{col_name}': {e}",
                file=sys.stderr,
            )
            return {}

    def get_virtual_libraries(self) -> dict[str, str]:
        """Return {name: search_expression} from Calibre preferences.

        A corrupt or non-dict stored payload degrades to ``{}`` (the same
        treatment ``_preferences`` and ``get_vl_ui_state`` apply); it used
        to crash every read that touched virtual libraries.
        """
        if self._vl_cache is not None:
            return self._vl_cache
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT val FROM preferences WHERE key = 'virtual_libraries'")
            row = cur.fetchone()
        except sqlite3.OperationalError:
            row = None  # schema predates the table
        decoded: Any = None
        if row and isinstance(row["val"], str) and row["val"].strip():
            try:
                decoded = json.loads(row["val"])
            except json.JSONDecodeError:
                decoded = None
        self._vl_cache = decoded if isinstance(decoded, dict) else {}
        return self._vl_cache

    def get_saved_searches(self) -> dict[str, str]:
        """Return {name: search_expression} from Calibre preferences.

        Saved searches can reference other saved searches via ``search:"name"``
        and are interpolated by the search engine with cycle detection.
        """
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT val FROM preferences WHERE key = 'saved_searches'")
            row = cur.fetchone()
        except sqlite3.OperationalError:
            return {}  # schema predates the table
        decoded: Any = None
        if row and isinstance(row["val"], str) and row["val"].strip():
            try:
                decoded = json.loads(row["val"])
            except json.JSONDecodeError:
                decoded = None
        return decoded if isinstance(decoded, dict) else {}

    def get_vl_ui_state(self) -> dict[str, Any]:
        """Return Calibre's virtual-library sidebar layout state.

        Mirrors what the Calibre GUI stores so consumers can reproduce its tab
        layout exactly:

        - ``hidden``: list of virtual library names hidden in the browser.
        - ``order``: raw decoded ``virt_libs_order`` payload (Calibre's stored
          ordering/sort metadata for the VL tabs).
        """
        out: dict[str, Any] = {"hidden": [], "order": {}}
        cur = self.conn.cursor()

        def _pref(key: str) -> Any:
            try:
                cur.execute("SELECT val FROM preferences WHERE key = ?", (key,))
                row = cur.fetchone()
            except sqlite3.OperationalError:
                return None
            if not row:
                return None
            try:
                return json.loads(row["val"])
            except (json.JSONDecodeError, TypeError):
                return None

        hidden = _pref("virt_libs_hidden")
        if isinstance(hidden, list):
            out["hidden"] = [str(h) for h in hidden]
        order = _pref("virt_libs_order")
        if isinstance(order, dict):
            out["order"] = order
        elif isinstance(order, list):
            # Older Calibre builds stored a plain ordered list of names.
            out["order"] = {str(name): i for i, name in enumerate(order)}
        return out

    def ordered_virtual_library_names(
        self, *, include_hidden: bool = False
    ) -> list[str]:
        """Virtual library names in Calibre's own sidebar order.

        The promoted helper (1.25; near-identical copies lived in Carrel's
        wings resolver and Hermitage's sidebar): the stored tab position
        from :meth:`get_vl_ui_state` orders first, unknown names follow
        alphabetically, and a position that will not parse as a float ranks
        with the unknowns rather than crashing the sort (both consumers'
        defensive branch, now in one place). Libraries hidden in the GUI
        are dropped unless ``include_hidden`` is set.
        """
        ui = self.get_vl_ui_state()
        hidden = {str(h).lower() for h in ui.get("hidden", [])}
        order = ui.get("order") or {}

        def _sort_key(name: str) -> tuple[int, float, str]:
            for key, pos in order.items():
                if str(key).lower() == name.lower():
                    try:
                        return (0, float(pos), name.lower())
                    except (TypeError, ValueError):
                        break
            return (1, 0.0, name.lower())

        names = self.get_virtual_libraries()
        return sorted(
            (n for n in names if include_hidden or n.lower() not in hidden),
            key=_sort_key,
        )

    def count_books(self) -> int:
        """Total book count; the caches when populated, else a COUNT(*)."""
        if self._all_ids_cache is not None:
            return len(self._all_ids_cache)
        if self._books_cache is not None:
            return len(self._books_cache)
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM books")
        return cur.fetchone()["c"]

    # --- Metadata portability (Phase 2 extractors) ---

    def get_annotations(self, book_id: int | None = None) -> list[dict[str, Any]]:
        """Extract e-reader highlights, bookmarks and notes.

        Reads the ``annotations`` table (populated by Calibre's wireless
        reader driver and the viewer). Returns a list of dicts with keys
        ``id``, ``book``, ``format``, ``user_type``, ``user``, ``timestamp``,
        ``annot_id``, ``annot_type`` and ``annot_data`` (decoded JSON when the
        payload parses, else the raw string). Older databases without the
        table return an empty list.
        """
        cur = self.conn.cursor()
        sql = (
            "SELECT id, book, format, user_type, user, timestamp, annot_id, "
            "annot_type, annot_data FROM annotations"
        )
        params: tuple = ()
        if book_id is not None:
            sql += " WHERE book = ?"
            params = (book_id,)
        sql += " ORDER BY book, timestamp, id"
        try:
            cur.execute(sql, params)
        except sqlite3.OperationalError:
            return []
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            rec = dict(row)
            data = rec.get("annot_data")
            if isinstance(data, str):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    rec["annot_data"] = json.loads(data)
            out.append(rec)
        return out

    def get_last_read_positions(
        self,
        book_id: int | None = None,
        *,
        fmt: str | None = None,
        user: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Map reading progress per device from ``last_read_positions``.

        Each row carries ``book``, ``format``, ``user``, ``device``, ``cfi``,
        ``epoch`` (unix seconds — sort key for "most recent") and
        ``pos_frac`` (0.0-1.0 progress fraction). Columns follow Calibre's
        real schema exactly (there is no ``user_type`` and the time column is
        ``epoch``, not ``epoch_time``). The 1.25 filters mirror upstream's:
        ``fmt``/``user`` narrow the rows (format case-insensitive),
        ``order_by`` accepts ``"pos_frac"`` or ``"epoch"`` (descending, the
        most-progressed / most-recent first) and ``limit`` caps the count
        -- the "where was I in THIS book" read. Rows default to
        ``ORDER BY book, device``.
        """
        cur = self.conn.cursor()
        sql = (
            "SELECT id, book, format, user, device, cfi, epoch, pos_frac "
            "FROM last_read_positions"
        )
        conds: list[str] = []
        params: list[Any] = []
        if book_id is not None:
            conds.append("book = ?")
            params.append(book_id)
        if fmt:
            conds.append("format = ?")
            params.append(fmt.upper())
        if user:
            conds.append("user = ?")
            params.append(user)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if order_by in ("pos_frac", "epoch"):
            sql += f" ORDER BY {order_by} DESC"
        else:
            sql += " ORDER BY book, device"
        if limit:
            sql += f" LIMIT {int(limit)}"
        try:
            cur.execute(sql, tuple(params))
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in cur.fetchall()]

    def get_plugin_data(
        self, book_id: int | None = None, name: str | None = None
    ) -> list[dict[str, Any]]:
        """Read third-party plugin payloads from ``books_plugin_data``.

        Plugins such as Goodreads sync or WordCount store their values here
        keyed by ``name`` (e.g. ``goodreads_id``, ``wordcount``, ``pages``).
        Filter by ``name`` to pull one metric across the library.
        """
        cur = self.conn.cursor()
        sql = "SELECT book, name, val FROM books_plugin_data"
        conds, params = [], []
        if book_id is not None:
            conds.append("book = ?")
            params.append(book_id)
        if name is not None:
            conds.append("name = ?")
            params.append(name)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY book, name"
        try:
            cur.execute(sql, tuple(params))
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in cur.fetchall()]

    def get_conversion_profiles(
        self, book_id: int | None = None
    ) -> list[dict[str, Any]]:
        """List books with manual conversion overrides.

        Reads the ``conversion_options`` table. The ``data`` column is
        Calibre's pickled recipe blob; it is surfaced as raw bytes under
        ``data`` (and its length under ``data_size``) so consumers can detect
        overrides without unpickling untrusted payloads.
        """
        cur = self.conn.cursor()
        sql = "SELECT book, format, data FROM conversion_options"
        params: tuple = ()
        if book_id is not None:
            sql += " WHERE book = ?"
            params = (book_id,)
        sql += " ORDER BY book, format"
        try:
            cur.execute(sql, params)
        except sqlite3.OperationalError:
            return []
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            rec = dict(row)
            blob = rec.get("data")
            rec["data_size"] = len(blob) if isinstance(blob, (bytes, bytearray)) else 0
            out.append(rec)
        return out

    def get_dirtied_books(self) -> list[int]:
        """List book ids queued for OPF resync in ``metadata_dirtied``.

        Calibre regenerates a book's sidecar .opf (and re-pushes metadata to
        wireless readers) only for ids in this table, consuming it at startup.
        Consumers can use the returned ids to show what Calibre will resync —
        e.g. a "pending OPF sync" section in audit/doctor output. Returns an
        empty list on schemas predating the table. Read-only: clearing the
        queue remains Calibre's job (``mark_book_as_clean()``).
        """
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT DISTINCT book FROM metadata_dirtied ORDER BY book")
        except sqlite3.OperationalError:
            return []
        return [row["book"] for row in cur.fetchall()]

    def get_annotations_dirtied_books(self) -> list[int]:
        """List book ids queued for annotation sync in ``annotations_dirtied``.

        The annotations sibling of :meth:`get_dirtied_books`: Calibre consumes
        this queue to push highlights/bookmarks to connected devices.
        Read-only observation — clearing entries is Calibre's job. Returns an
        empty list on schemas predating the table.
        """
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT DISTINCT book FROM annotations_dirtied ORDER BY book")
        except sqlite3.OperationalError:
            return []
        return [row["book"] for row in cur.fetchall()]

    def get_dirtied_formats(self) -> list[tuple[int, str]]:
        """The FTS sidecar's extraction queue: ``(book_id, format)`` pairs.

        The formats sibling of :meth:`get_dirtied_books` (1.24): every pair
        the sidecar's ``dirtied_formats`` table holds for (re-)extraction and
        a pages rescan -- Calibre's extraction pool consumes it at startup
        and while running. Formats come back uppercase as stored; the list
        is sorted by book then format. Read-only observation: Calibre clears
        an entry when its extraction commits, and cquarry's own format verbs
        manage the pairs they are responsible for. Empty list when the
        sidecar is absent, unreadable, or predates the table.
        """
        conn = self._fts_connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT book, format FROM dirtied_formats ORDER BY book, format"
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # sidecar lost its table mid-session: degrade
        return [(row["book"], (row["format"] or "").upper()) for row in rows]

    def get_feeds(self) -> list[dict[str, Any]]:
        """Registered news feeds: ``[{id, title, script}]``.

        The ``feeds`` table stores the recipe scripts behind Calibre's news
        download feature. Empty list when the schema predates the table or it
        is absent.
        """
        try:
            cur = self.conn.execute(
                "SELECT id, title, script FROM feeds ORDER BY title COLLATE NOCASE"
            )
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    # --- FTS sidecar reads (full-text-search.db, cquarry >= 1.18) ---

    def _fts_connect(self) -> sqlite3.Connection | None:
        """Open the library's full-text-search.db read-only (cached).

        Same lock-escape as metadata.db: a locked sidecar is snapshot-copied
        (with ``-wal``/``-shm``) and read from the copy. A missing sidecar,
        or one without the plain ``books_text`` table, is cached as absent so
        every sidecar read degrades to empty instead of erroring. Only the
        plain table is readable here: the FTS5 index tables tokenize through
        Calibre's custom ``calibre`` tokenizer, which does not exist in the
        stdlib sqlite build.
        """
        if self._fts_missing:
            return None
        if self._fts_conn is not None:
            return self._fts_conn
        path = os.path.join(
            os.path.dirname(os.path.abspath(self.db_path)), "full-text-search.db"
        )
        if not os.path.exists(path):
            self._fts_missing = True
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_uri_ro(path), uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM books_text LIMIT 1")
            self._fts_conn = conn
            return conn
        except sqlite3.OperationalError as e:
            if conn is not None:
                conn.close()
            if "locked" not in str(e).lower():
                # Created but never initialized (no books_text): absent.
                self._fts_missing = True
                return None
        print(
            "NOTE: full-text-search.db is locked (Calibre is running). "
            "Reading from a snapshot copy.",
            file=sys.stderr,
        )
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="cquarry_fts_")
        os.close(fd)
        _snapshot_copy(path, tmp, self.SNAPSHOT_LEASH)
        self._fts_tmp_path = tmp
        self._fts_conn = sqlite3.connect(db_uri_ro(tmp), uri=True)
        self._fts_conn.row_factory = sqlite3.Row
        return self._fts_conn

    def get_book_text(self, book_id: int, fmt: str) -> dict[str, Any] | None:
        """One format's extracted plain text from ``full-text-search.db``.

        Returns the sidecar's plain ``books_text`` row (``book``, ``format``,
        ``format_size``, ``format_hash``, ``searchable_text``, ``text_size``,
        ``text_hash``, ``err_msg``, ``timestamp``) or None when the sidecar
        is absent or the (book, format) pair has no row. ``fmt`` is
        case-insensitive. The text is what Calibre extracted for its reader
        search; ``err_msg`` is non-empty when extraction failed (see
        :func:`cquarry.integrity.find_failed_text_extraction`). No FTS5
        machinery is involved: the index tables are unqueryable outside
        Calibre, the plain table is all there is here.
        """
        conn = self._fts_connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT book, format, format_size, format_hash, searchable_text, "
                "text_size, text_hash, err_msg, timestamp "
                "FROM books_text WHERE book = ? AND upper(format) = upper(?)",
                (book_id, fmt),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # sidecar lost its table mid-session: degrade
        return dict(row) if row is not None else None

    def get_text_extractions(self, book_id: int | None = None) -> list[dict[str, Any]]:
        """Bulk extraction-status rows from the FTS sidecar, WITHOUT the text.

        One dict per ``books_text`` row carrying ``book``, ``format``,
        ``format_size``, ``format_hash``, ``text_size``, ``text_hash``,
        ``err_msg`` and ``timestamp`` -- everything except
        ``searchable_text``, which can be megabytes per format (read one
        row's text via :meth:`get_book_text`). Feeds integrity predicates
        (``err_msg``) and format-hash change detection. Empty list when the
        sidecar is absent or the schema predates the table.
        """
        conn = self._fts_connect()
        if conn is None:
            return []
        sql = (
            "SELECT book, format, format_size, format_hash, text_size, "
            "text_hash, err_msg, timestamp FROM books_text"
        )
        params: tuple = ()
        if book_id is not None:
            sql += " WHERE book = ?"
            params = (book_id,)
        sql += " ORDER BY book, format"
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def search_book_text(
        self,
        query: str,
        *,
        fmt: str | None = None,
        ids: set[int] | None = None,
    ) -> dict[int, set[str]]:
        """Python-side content search over the sidecar's extracted text.

        Case- and accent-folded substring match (the search engine's default
        text semantics) of ``query`` against each format's
        ``searchable_text``; returns ``{book_id: {FORMAT, ...}}`` naming the
        formats that matched. ``fmt`` restricts to one format; ``ids``
        restricts the book set (the usual ``search()``-then-look-inside
        composition). An empty query raises ValueError. The scan is linear
        over the sidecar's rows -- the FTS5 index is unqueryable outside
        Calibre (custom tokenizer), so this read IS the content index here.
        Empty dict when the sidecar is absent.
        """
        if not query:
            raise ValueError("query must not be empty")
        conn = self._fts_connect()
        if conn is None:
            return {}
        sql = "SELECT book, format, searchable_text FROM books_text"
        params: tuple = ()
        if fmt is not None:
            sql += " WHERE upper(format) = upper(?)"
            params = (fmt,)
        sql += " ORDER BY book, format"
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return {}
        q = _fold(query)
        out: dict[int, set[str]] = {}
        for row in rows:
            if ids is not None and row["book"] not in ids:
                continue
            text = row["searchable_text"]
            if text and q in _fold(text):
                out.setdefault(row["book"], set()).add(row["format"].upper())
        return out

    def get_tag_browser_counts(self) -> dict[str, list[dict[str, Any]]]:
        """Calibre's own tag-browser rollups from the ``tag_browser_*`` views.

        Reads every pure-SQL ``tag_browser_*`` view and returns
        ``{category: [{id, name, count, avg_rating, sort}]}`` — the exact
        per-entity counts (and mean rating over the entity's rated books)
        Calibre's browse sidebar shows. Custom-column categories are rekeyed
        from ``custom_column_N`` to their ``#label`` search location; native
        categories keep their entity names. Two view quirks are worked around
        without touching the database: ``tag_browser_series`` sorts through
        Calibre's ``title_sort()`` UDF, which this supplies from the stdlib
        ``helpers`` implementation for the duration of the read (registered
        on this connection only, then removed); and the ratings/custom views
        name their value column ``rating``/``value`` rather than ``name``.
        The ``tag_browser_filtered_*`` variants are deliberately skipped:
        they call Calibre's GUI-state ``books_list_filter()`` SQL function,
        which only exists inside a running Calibre (as does the
        ``sortconcat()`` aggregate behind the ``meta`` view). Views that
        still cannot be evaluated outside Calibre are silently skipped, so
        the result degrades gracefully on such schemas.
        """
        try:
            names = [
                r[0]
                for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view' "
                    "AND name LIKE 'tag_browser_%'"
                )
            ]
        except sqlite3.OperationalError:
            return {}
        label_by_id: dict[int, str] = {}
        with contextlib.suppress(sqlite3.Error):
            label_by_id = {
                col["id"]: col["label"] for col in self.get_custom_columns().values()
            }
        out: dict[str, list[dict[str, Any]]] = {}
        # tag_browser_series calls title_sort() — a UDF that only exists
        # inside Calibre's process. Supply the stdlib equivalent on this
        # connection for the duration of the read, then remove it again; no
        # database state is touched (read-only connection, SELECTs only).
        self.conn.create_function("title_sort", 1, title_sort)
        try:
            for name in sorted(names):
                if name.startswith("tag_browser_filtered_"):
                    continue  # books_list_filter() is GUI state, not data
                key = name[len("tag_browser_") :]
                m = re.fullmatch(r"custom_column_(\d+)", key)
                if m:
                    key = "#" + label_by_id.get(int(m.group(1)), key)
                rows = None
                # Defense-in-depth: the name comes from sqlite_master, but
                # it still enters SQL only as a quoted identifier.
                quoted = '"' + name.replace('"', '""') + '"'
                for select in (
                    f"SELECT id, name, count, avg_rating, sort FROM {quoted} ",
                    f"SELECT id, value AS name, count, avg_rating, sort FROM {quoted} ",
                    f"SELECT id, CAST(rating AS TEXT) AS name, count, avg_rating, sort FROM {quoted} ",
                ):
                    try:
                        # ORDER BY lives outside the attempted SQL so a
                        # missing name column can fall through to the next
                        # value-column form.
                        rows = self.conn.execute(
                            select + "ORDER BY 2 COLLATE NOCASE"
                        ).fetchall()
                        break
                    except sqlite3.OperationalError:
                        continue  # depends on a Calibre-process function
                if rows is None:
                    continue
                out[key] = [dict(r) for r in rows]
        finally:
            self.conn.create_function("title_sort", 1, None)
        return out

    # --- Restricted tag browser (1.25, Phase 18) ---

    def get_categories(
        self, book_ids: Sequence[int] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """The tag browser over a restricted result set (upstream
        ``Cache.get_categories`` -> ``categories.get_categories``, the
        portable subset the roadmap scoped): ``{category: [node, ...]}``
        where every node carries its book-id set and a reproducing search
        expression.

        Node shape: ``{id, name, sort, count, avg_rating, id_set,
        search_expression}`` -- ``count`` is ``len(id_set)`` (only books
        from the restriction land in a node, so a value no restricted book
        holds appears nowhere, upstream's zero-count rule),
        ``avg_rating`` is the mean over the node's rated books in stars
        (None with no rated books, the ``tag_browser_*`` views' AVG-over-
        nonzero rule), and ``search_expression`` is the exact-match query
        reproducing the node against the engine.

        Categories: the builtin browse fields (authors, series, publisher,
        tags, languages, formats, rating) plus every storage-backed custom
        column keyed ``#label``. Composite columns stay gated by the §7 GPM
        boundary; comments columns have no category values; user
        categories, ``search``, and ``news`` are upstream GUI
        synthesizations outside the portable subset. Where results overlap
        :meth:`get_tag_browser_counts`, counts and average ratings agree
        (same data, same rule); two documented naming differences remain:
        rating nodes surface STARS -- ``"4.0"``, matching ``field()`` and
        ``facet_counts`` -- where the views name the internal 0-10 text,
        and unlinked entity rows (the views' zero-count entries) appear
        here only when a book actually holds them, upstream's
        ``get_categories`` behavior. ``book_ids=None`` means the whole
        library; an empty restriction answers empty categories, like
        upstream. Nodes sort name-ascending (case-insensitive), rating
        descending -- Calibre's default browse order.
        """
        rows = self.get_all_books()
        if book_ids is None:
            restricted = rows
        else:
            wanted = set(book_ids)
            restricted = [b for b in rows if b["id"] in wanted]
        rating_map = {b["id"]: b["rating"] for b in restricted if b["rating"]}

        def _avg(ids: set[int]) -> float | None:
            vals = [rating_map[b] for b in ids if b in rating_map]
            return (sum(vals) / len(vals)) / 2.0 if vals else None

        def _node(
            node_id: int | None,
            name: str,
            sort: str,
            ids: set[int],
            expression: str,
        ) -> dict[str, Any]:
            return {
                "id": node_id,
                "name": name,
                "sort": sort,
                "count": len(ids),
                "avg_rating": _avg(ids),
                "id_set": frozenset(ids),
                "search_expression": expression,
            }

        def _from_rows(
            field: str, location: str, entity_kind: str | None = None
        ) -> list[dict[str, Any]]:
            meta: dict[str, dict[str, Any]] = {}
            if entity_kind is not None:
                meta = {e["name"]: e for e in self.get_entities(entity_kind)}
            buckets: dict[str, set[int]] = {}
            for b in restricted:
                value = b[field]
                # List fields bucket per element; scalar fields (series,
                # publisher) are one value, never iterated as a string.
                for one in value if isinstance(value, (list, tuple)) else [value]:
                    if one:
                        buckets.setdefault(str(one), set()).add(b["id"])
            nodes = [
                _node(
                    meta.get(name, {}).get("id"),
                    name,
                    (meta.get(name, {}) or {}).get("sort") or name,
                    ids,
                    f'{location}:="{name}"',
                )
                for name, ids in buckets.items()
            ]
            nodes.sort(key=lambda n: n["sort"].lower())
            return nodes

        out: dict[str, list[dict[str, Any]]] = {
            "authors": _from_rows("authors", "authors", "authors"),
            "series": _from_rows("series", "series", "series"),
            "publisher": _from_rows("publisher", "publisher", "publishers"),
            "tags": _from_rows("tags", "tags", "tags"),
            "languages": _from_rows("languages", "languages", "languages"),
            "formats": _from_rows("formats", "formats"),
        }

        # Rating: star nodes merged across legacy duplicate rating rows
        # (upstream merges same-star tags too), descending.
        star_buckets: dict[float, set[int]] = {}
        for b in restricted:
            stars = calibre_rating_to_stars(b["rating"])
            if stars is not None:
                star_buckets.setdefault(stars, set()).add(b["id"])
        out["rating"] = [
            _node(None, f"{stars:g}", f"{stars:g}", ids, f"rating:={stars:g}")
            for stars, ids in sorted(star_buckets.items(), reverse=True)
        ]

        # Storage-backed custom columns, keyed #label. Normalized columns
        # carry entity ids from their value tables; direct-storage columns
        # name their nodes (id None, like formats).
        for col in self.get_custom_columns().values():
            datatype = col["datatype"]
            if datatype in ("composite", "comments"):
                continue
            location = "#" + col["label"]
            try:
                values = self.load_custom_column(col["name"])
            except (ValueError, sqlite3.OperationalError):
                continue
            value_ids: dict[Any, int] = {}
            if col["normalized"]:
                cid = int(col["id"])
                try:
                    value_ids = {
                        r["value"]: r["id"]
                        for r in self.conn.execute(
                            f"SELECT id, value FROM custom_column_{cid}"
                        )
                    }
                except sqlite3.OperationalError:
                    value_ids = {}
            buckets = {}
            for b in restricted:
                value = values.get(b["id"])
                if value is None:
                    continue
                for one in value if isinstance(value, (list, tuple)) else [value]:
                    if one is None or (isinstance(one, str) and not one.strip()):
                        continue
                    buckets.setdefault(one, set()).add(b["id"])
            if datatype == "rating":
                converted: dict[float, set[int]] = {}
                for value, ids in buckets.items():
                    stars = calibre_rating_to_stars(int(value))
                    if stars is not None:
                        converted.setdefault(stars, set()).update(ids)
                nodes = [
                    _node(
                        None, f"{stars:g}", f"{stars:g}", ids, f"{location}:={stars:g}"
                    )
                    for stars, ids in sorted(converted.items(), reverse=True)
                ]
            else:
                nodes = [
                    _node(
                        value_ids.get(value),
                        str(value),
                        str(value),
                        ids,
                        f'{location}:="{value}"',
                    )
                    for value, ids in buckets.items()
                ]
                nodes.sort(key=lambda n: n["sort"].lower())
            out[location] = nodes
        return out

    # --- Preferences (generic accessor) ---

    def _preferences(self) -> dict[str, Any]:
        """Every row of the ``preferences`` table, JSON-decoded where it parses.

        Calibre stores nearly everything as JSON; a handful of keys are plain
        strings and survive as strings. Cached — preferences are GUI state,
        not something a short-lived read connection should poll.
        """
        if self._prefs_cache is None:
            prefs: dict[str, Any] = {}
            try:
                cur = self.conn.cursor()
                cur.execute("SELECT key, val FROM preferences")
                for row in cur.fetchall():
                    raw = row["val"]
                    if isinstance(raw, str) and raw.strip():
                        try:
                            prefs[row["key"]] = json.loads(raw)
                            continue
                        except json.JSONDecodeError:
                            pass
                    prefs[row["key"]] = raw
            except sqlite3.OperationalError:
                pass  # schema predates the table
            self._prefs_cache = prefs
        return self._prefs_cache

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Typed read of one Calibre preference (JSON decoded when possible).

        Returns ``default`` when the key is absent or the schema predates the
        ``preferences`` table. See also :meth:`get_field_metadata`,
        :meth:`get_grouped_search_terms`, :meth:`get_user_categories` and
        :meth:`get_tag_browser_state` for the high-traffic keys.
        """
        return self._preferences().get(key, default)

    def get_field_metadata(self) -> dict[str, Any]:
        """Calibre's rich ``field_metadata`` preference, decoded.

        Maps custom-column label -> {column, datatype, display, category_sort,
        ...}. The ``custom_columns`` table stays authoritative for existence;
        this carries the GUI-side richness (e.g. ``display`` even on schemas
        whose table column is missing).
        """
        fm = self.get_preference("field_metadata", {})
        return fm if isinstance(fm, dict) else {}

    def get_grouped_search_terms(self) -> dict[str, list[str]]:
        """Grouped search terms: group name -> member search locations.

        Mirrors Calibre's preferences key of the same name; drives
        ``GroupName:query`` expansion in the search engine (see spec §3.2).
        """
        gst = self.get_preference("grouped_search_terms", {})
        return gst if isinstance(gst, dict) else {}

    def get_user_categories(self) -> dict[str, list[dict[str, Any]]]:
        """User-defined tag-browser categories: name -> [{name, label, ...}]."""
        uc = self.get_preference("user_categories", {})
        return uc if isinstance(uc, dict) else {}

    def get_tag_browser_state(self) -> dict[str, Any]:
        """Tag-browser layout state: {"order": [...], "hidden": [...]}.

        Reads ``tag_browser_category_order`` and
        ``tag_browser_hidden_categories`` so frontends can mirror Calibre's
        own browse-sidebar layout, the same way :meth:`get_vl_ui_state` does
        for virtual libraries.
        """
        order = self.get_preference("tag_browser_category_order", [])
        hidden = self.get_preference("tag_browser_hidden_categories", [])
        return {
            "order": order if isinstance(order, list) else [],
            "hidden": hidden if isinstance(hidden, list) else [],
        }

    def grouped_search_terms(self) -> dict[str, list[str]]:
        """MetadataProvider hook: grouped search terms for the engine."""
        return self.get_grouped_search_terms()

    def user_categories(self) -> dict[str, list]:
        """MetadataProvider hook: user-defined tag-browser categories.

        Feeds the ``@Name`` search location (see spec §4); each category maps
        to its raw member list of ``[value, location, ...]`` entries.
        """
        return self.get_user_categories()

    def _annotations_text(self, book_id: int) -> str:
        """Concatenated annotation searchable text for one book (cached).

        Feeds the ``annotations:`` search location. Empty string when the
        book has no annotations or the schema predates the table. Within a
        stored ``searchable_text``, an annotation's notes follow its
        highlighted text joined by ``\n\x1f\n`` (LF, ASCII unit separator,
        LF; upstream ``annot_db_data``). The map loads in ONE query for the
        whole library (the per-book query was an N+1: one annotations:
        token swept the library one probe per book).
        """
        if self._annotations_text_cache is None:
            rows: dict[int, list[str]] = {}
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT book, searchable_text FROM annotations "
                    "WHERE searchable_text IS NOT NULL ORDER BY id"
                )
                for r in cur.fetchall():
                    if r[1]:
                        rows.setdefault(r[0], []).append(r[1])
            except sqlite3.OperationalError:
                pass  # schema predates the table
            self._annotations_text_cache = {
                book: "\n".join(parts) for book, parts in rows.items()
            }
        return self._annotations_text_cache.get(book_id, "")

    def get_annotations_decoded(
        self, book_id: int | None = None
    ) -> list[dict[str, Any]]:
        """The renderer-facing annotation view (1.23): Calibre's raw
        annotations rows projected onto the fields a detail pane shows.

        One dict per annotation carrying ``book``, ``format``, ``kind``
        (the raw ``annot_type``), ``annot_id``, ``timestamp``, and the
        decoded payload's ``text`` (the highlighted passage), ``notes``,
        and ``title`` (bookmarks), each None when the payload lacks it.
        Renders the same content :meth:`get_annotations` returns without
        every consumer re-learning ``annot_data``'s shape. Empty list on
        schemas predating the table.
        """
        out: list[dict[str, Any]] = []
        for row in self.get_annotations(book_id):
            data = row.get("annot_data")
            if not isinstance(data, dict):
                data = {}

            def _str(value: Any) -> str | None:
                return value if isinstance(value, str) else None

            out.append(
                {
                    "book": row.get("book"),
                    "format": row.get("format"),
                    "kind": row.get("annot_type"),
                    "annot_id": row.get("annot_id"),
                    "timestamp": row.get("timestamp"),
                    "text": _str(data.get("text")),
                    "notes": _str(data.get("notes")),
                    "title": _str(data.get("title")),
                }
            )
        return out

    def get_annotations_filtered(
        self,
        *,
        book_id: int | None = None,
        user_type: str | None = None,
        user: str | None = None,
        kind: str | None = None,
        style: dict[str, str] | None = None,
        include_removed: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """The annotation conveniences in one read (upstream
        ``Cache.all_annotations``): :meth:`get_annotations_decoded`'s rows
        plus ``user_type``/``user``/``removed``, filtered.

        ``user_type``/``user`` scope to one device account (both must match
        when given), ``kind`` filters the annotation type (``highlight``,
        ``bookmark``, ...), and ``style`` keeps only highlights whose stored
        style carries every given key/value (upstream's exact-dict-match
        rule, e.g. ``{"kind": "color", "which": "yellow"}``). Removed
        annotations are tombstones -- Calibre's own deletes rewrite the
        payload to a ``removed: True`` skeleton and blank the searchable
        text -- and are hidden unless ``include_removed`` is set (upstream's
        ``ignore_removed`` default keeps them; the renderer-facing read
        hides them and opts in). Rows order by book, then timestamp, then
        id -- :meth:`get_annotations`' order -- and ``limit`` takes the
        first N of that ordering. Empty list on schemas predating the
        table, like every annotations read.
        """
        rows = self.get_annotations(book_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            if user_type is not None and row.get("user_type") != user_type:
                continue
            if user is not None and row.get("user") != user:
                continue
            if kind is not None and row.get("annot_type") != kind:
                continue
            data = row.get("annot_data")
            if not isinstance(data, dict):
                data = {}

            def _str(value: Any) -> str | None:
                return value if isinstance(value, str) else None

            removed = bool(data.get("removed"))
            if removed and not include_removed:
                continue
            if style is not None:
                stored = data.get("style")
                if not isinstance(stored, dict) or not all(
                    stored.get(k) == v for k, v in style.items()
                ):
                    continue
            out.append(
                {
                    "book": row.get("book"),
                    "format": row.get("format"),
                    "kind": row.get("annot_type"),
                    "annot_id": row.get("annot_id"),
                    "timestamp": row.get("timestamp"),
                    "text": _str(data.get("text")),
                    "notes": _str(data.get("notes")),
                    "title": _str(data.get("title")),
                    "user_type": row.get("user_type"),
                    "user": row.get("user"),
                    "removed": removed,
                }
            )
            if limit is not None and len(out) >= limit:
                break
        return out

    def get_annotation_users(self) -> list[tuple[str, str]]:
        """Every ``(user_type, user)`` account holding annotations, sorted.

        The discovery half of the filtered read (upstream
        ``all_annotation_users``): what a frontend offers as the
        "whose highlights" picker. Empty list on schemas predating the
        table.
        """
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT user_type, user FROM annotations "
                "ORDER BY user_type, user"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["user_type"], r["user"]) for r in rows]

    def get_annotation_types(self) -> list[str]:
        """Every annotation type present (upstream
        ``all_annotation_types``): ``highlight``, ``bookmark``, ... sorted.
        Empty list on schemas predating the table.
        """
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT annot_type FROM annotations "
                "WHERE annot_type IS NOT NULL ORDER BY annot_type"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["annot_type"] for r in rows]

    def get_annotation_styles(self) -> list[dict[str, str]]:
        """The highlight styles this library actually holds (the DB half of
        upstream ``all_annotation_styles``).

        Each highlight payload can carry a ``style`` dict (``kind`` =
        color/decoration, ``which`` = the viewer's name); the distinct
        values come back sorted, so a frontend can build a legend from
        what is really in the library. Upstream also folds in its viewer's
        builtin style catalog, which is GUI constants, not data -- the
        styles nobody used appear in Calibre's picker and not here.
        """
        styles: set[tuple[str, str]] = set()
        for row in self.get_annotations():
            data = row.get("annot_data")
            if isinstance(data, dict):
                style = data.get("style")
                if isinstance(style, dict):
                    which = style.get("which")
                    kind = style.get("kind")
                    if isinstance(kind, str) and isinstance(which, str):
                        styles.add((kind, which))
        return [{"kind": k, "which": w} for k, w in sorted(styles)]

    # --- Search & virtual library resolution ---

    def _engine(self) -> SearchEngine:
        if self._search_engine is None:
            self._search_engine = SearchEngine(self)
        return self._search_engine

    def search(self, query: str) -> set[int]:
        """Resolve an arbitrary Calibre search expression to a set of book IDs."""
        return self._engine().search(query)

    def resolve_vl(self, vl_name: str) -> set[int]:
        """Resolve a virtual library name to a set of book IDs.

        Parses Calibre's VL search expressions (tags, vl cross-references,
        boolean operators, and all other field locations the engine supports).
        Name matching is case-insensitive, with surrounding padding and
        quotes stripped (cquarry >= 1.16, matching the lookup
        :meth:`vl_expression` already did); unknown names raise ValueError.
        """
        vls = self.get_virtual_libraries()
        canonical = _canonical_pref_name(vl_name, vls)
        if canonical is None:
            raise ValueError(
                f"Unknown virtual library: '{vl_name}'. "
                f"Available: {', '.join(sorted(vls.keys()))}"
            )
        return self._engine()._match_vl(canonical, self.all_ids(), set())

    def resolve_saved_search(self, name: str) -> set[int]:
        """Resolve a saved search name to a set of book IDs.

        Name matching is case-insensitive, with surrounding padding and
        quotes stripped (cquarry >= 1.16, matching the lookup
        :meth:`saved_search` already did); unknown names raise ValueError.
        """
        sss = self.get_saved_searches()
        canonical = _canonical_pref_name(name, sss)
        if canonical is None:
            raise ValueError(
                f"Unknown saved search: '{name}'. "
                f"Available: {', '.join(sorted(sss.keys()))}"
            )
        return self._engine()._match_saved_search(canonical, self.all_ids(), set())

    def virtual_libraries_for_books(
        self, book_ids: Sequence[int] | None = None
    ) -> dict[int, tuple[str, ...]]:
        """The inverse virtual-library map (upstream ``Cache.
        virtual_libraries_for_books``): every requested book id -> the sorted
        names of the virtual libraries containing it.

        Each library resolves through the same engine path
        :meth:`resolve_vl` uses, so the answers agree by construction. A
        library whose expression fails to evaluate (a ``vl:`` target renamed
        or deleted in Calibre, a corrupt preference) is skipped with a
        warning rather than failing the map -- upstream splices an error
        string into the name tuple there, which would pollute every set
        algebra a consumer does with the values. ``book_ids=None`` means
        every book; ids absent from the library come back as empty tuples
        exactly like members of no library.
        """
        if self._vl_for_books_cache is None:
            owners: dict[int, list[str]] = {}
            for name in self.get_virtual_libraries():
                try:
                    members = self.resolve_vl(name)
                except ParseException as e:
                    print(
                        f"Warning: virtual library {name!r} failed to evaluate, "
                        f"skipped in the inverse map: {e}",
                        file=sys.stderr,
                    )
                    continue
                for book_id in members:
                    owners.setdefault(book_id, []).append(name)
            self._vl_for_books_cache = {
                book: tuple(sorted(names)) for book, names in owners.items()
            }
        cache = self._vl_for_books_cache
        ids = self._get_all_book_ids() if book_ids is None else book_ids
        return {book_id: cache.get(book_id, ()) for book_id in ids}

    def user_categories_for_books(
        self, book_ids: Sequence[int] | None = None
    ) -> dict[int, dict[str, list[list[str]]]]:
        """The inverse user-category map (upstream
        ``Cache.user_categories_for_books``): every requested book id ->
        ``{category: [[value, location], ...]}`` naming only the members the
        book actually holds.

        Members are probed exactly like the ``@Name`` search location
        (upstream's rule): an exact match of the stored member value on the
        member's own location, via the same :meth:`field` path the engine
        uses, so the answers agree with ``@Name:Category`` by construction.
        Members whose location cannot be evaluated match nothing rather than
        erroring: composite columns (the §7 GPM boundary -- upstream
        evaluates them through its template engine) and unknown locations
        both fall out of ``field()`` as no match, the same way unknown
        ``@Names`` match nothing in search. Scalar members compare with
        ``==`` and list members with membership, mirroring upstream; a
        rating member written as the string ``"4"`` therefore does not match
        the 4.0-star float ``field()`` yields, which is upstream's own
        string-vs-number behavior. ``book_ids=None`` means every book.
        """
        user_cats = self.get_user_categories()
        ids = self._get_all_book_ids() if book_ids is None else book_ids
        out: dict[int, dict[str, list[list[str]]]] = {}
        for book_id in ids:
            per_book: dict[str, list[list[str]]] = {}
            for ucat, members in user_cats.items():
                held: list[list[str]] = []
                for member in members:
                    if not isinstance(member, (list, tuple)) or len(member) < 2:
                        continue  # corrupt member entry: degrade like the prefs do
                    name, location = member[0], member[1]
                    try:
                        value = self.field(book_id, location)
                    except (sqlite3.OperationalError, ValueError):
                        value = None
                    if isinstance(value, (list, tuple)):
                        if name in value:
                            held.append([name, location])
                    elif name == value:
                        held.append([name, location])
                per_book[ucat] = held
            out[book_id] = per_book
        return out

    # --- search.MetadataProvider interface ---

    def all_ids(self) -> set[int]:
        """MetadataProvider hook: every book id in the library."""
        return set(self._get_all_book_ids())

    def vl_expression(self, name: str) -> str | None:
        """Case-insensitive lookup of a virtual library's search expression."""
        low = name.lower().strip().strip('"')
        for n, expr in self.get_virtual_libraries().items():
            if n.lower() == low:
                return expr
        return None

    def saved_search(self, name: str) -> str | None:
        """Case-insensitive lookup of a saved search expression."""
        low = name.lower().strip().strip('"')
        for n, expr in self.get_saved_searches().items():
            if n.lower() == low:
                return expr
        return None

    def custom_locations(self) -> dict[str, str]:
        """MetadataProvider hook: ``{#label: engine datatype}`` per custom column."""
        cache = self._custom_loc_cache
        if cache is None:
            cache = self._custom_loc_cache = self._build_custom_locations()
        return cache

    def field(self, book_id: int, location: str) -> Any:
        """One book's value for a canonical location (MetadataProvider hook).

        Custom columns are addressed by ``#label``; comments, pages, and
        annotations have dedicated lazy paths; everything else reads the
        search view."""
        if location.startswith("#"):
            return self._custom_value(book_id, location)

        if location == "comments":
            if self._comments_cache is None:
                self._comments_cache = {}
            if book_id not in self._comments_cache:
                try:
                    cur = self.conn.cursor()
                    cur.execute("SELECT text FROM comments WHERE book = ?", (book_id,))
                    row = cur.fetchone()
                    self._comments_cache[book_id] = (
                        row["text"] if row and row["text"] else ""
                    )
                except sqlite3.OperationalError:
                    self._comments_cache[book_id] = ""  # no comments table
            return self._comments_cache[book_id]

        if location == "pages":
            # Native books_pages_link first (upstream-managed); an int custom
            # column labelled 'pages' remains the fallback on older schemas.
            val = self._page_counts().get(book_id)
            return int(val) if isinstance(val, (int, float)) else None

        if location == "annotations":
            # Annotation highlights/bookmarks text (cquarry >= 1.4). Grammar-
            # consistent substring/exact/regex matching over the concatenated
            # searchable_text; `true`/`false` test presence naturally.
            return self._annotations_text(book_id)

        rec = self._build_search_view().get(book_id)
        return rec.get(location) if rec else None

    def _pages_column(self) -> dict[str, Any] | None:
        """Find an int/float custom column labelled 'pages', cached.

        Uses a module-level sentinel so a negative lookup (no such column) is
        also cached and never re-scanned.
        """
        if self._pages_col_cache is _UNSET:
            self._pages_col_cache = next(
                (
                    c
                    for c in self.get_custom_columns().values()
                    if c["label"].lower() == "pages"
                    and c["datatype"] in ("int", "float")
                ),
                None,
            )
        return self._pages_col_cache

    # --- search-engine internals ---

    def _get_all_book_ids(self) -> set[int]:
        """Return all book IDs, cached."""
        if self._all_ids_cache is None:
            self._all_ids_cache = {
                row["id"]
                for row in self.conn.execute("SELECT id FROM books").fetchall()
            }
        return self._all_ids_cache

    def _build_search_view(self) -> dict[int, dict[str, Any]]:
        """Build a per-book, normalized field view for the search engine."""
        if self._search_view is not None:
            return self._search_view

        view: dict[int, dict[str, Any]] = {}
        for b in self.get_all_books():
            series = b["series"]
            index = b["series_index"]
            view[b["id"]] = {
                "title": b["title"] or "",
                "title_sort": b["title_sort"] or "",
                "authors": b["authors"],
                "author_sort": b["author_sort"] or "",
                "series": series or "",
                "series_sort": (
                    f"{series} [{index:g}]" if series and index is not None else ""
                ),
                "publisher": b["publisher"] or "",
                "tags": b["tags"],
                "formats": b["formats"],
                "languages": b["languages"],
                "rating": calibre_rating_to_stars(b["rating"]),
                "series_index": index,
                "size": b.get("size"),
                "id": b["id"],
                "pubdate": b["pubdate"],
                "timestamp": b["timestamp"],
                "last_modified": b["last_modified"],
                "cover": bool(b["has_cover"]),
                "identifiers": {},
                "comments": "",
                "uuid": "",
            }

        cur = self.conn.cursor()
        try:
            for row in cur.execute("SELECT book, type, val FROM identifiers"):
                rec = view.get(row["book"])
                if rec is not None:
                    rec["identifiers"][row["type"]] = row["val"]
        except sqlite3.OperationalError:
            pass  # schema predates the identifiers table
        try:
            for row in cur.execute("SELECT id, uuid FROM books"):
                rec = view.get(row["id"])
                if rec is not None:
                    rec["uuid"] = row["uuid"] or ""
        except sqlite3.OperationalError:
            pass

        self._search_view = view
        return view

    # Calibre custom-column datatype -> search-engine datatype.
    _CUSTOM_DT_MAP = {
        "text": DT_TEXT,  # promoted to DT_TEXT_MULTI when is_multiple
        "comments": DT_TEXT,
        "enumeration": DT_TEXT,
        "series": DT_TEXT,
        "int": DT_INT,
        "float": DT_FLOAT,
        "rating": DT_RATING,
        "bool": DT_BOOL,
        "datetime": DT_DATE,
    }

    def _build_custom_locations(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for col in self.get_custom_columns().values():
            engine_dt = self._CUSTOM_DT_MAP.get(col["datatype"])
            if engine_dt is None:
                continue  # composite columns are computed, not stored
            if col["datatype"] == "text" and col["is_multiple"]:
                engine_dt = DT_TEXT_MULTI
            out["#" + col["label"]] = engine_dt
            if col["datatype"] == "series":
                out["#" + col["label"] + "_index"] = DT_FLOAT
        return out

    def _custom_by_label(self) -> dict[str, dict[str, Any]]:
        if self._custom_label_cache is None:
            self._custom_label_cache = {
                c["label"]: c for c in self.get_custom_columns().values()
            }
        return self._custom_label_cache

    def custom_column_links(self, col_name: str) -> dict[int, str]:
        """The normalized value table's `link` column: ``{book_id: url}``.

        Upstream added a per-value `link` (schema_upgrades.py:836) but no
        Calibre UI populates it, so this is a faithful, rarely-populated
        read. Normalized columns only (text/enumeration/series/rating); the
        map fills as a side effect of :meth:`load_custom_column` and is
        empty until that runs (or when the schema predates the column, or
        nothing carries a link). Column addressed as in
        :meth:`find_custom_column`.
        """
        col = self.find_custom_column(col_name)
        if col is None:
            return {}
        token = "#" + col["label"]
        if token not in self._custom_link_cache:
            self.load_custom_column(col["name"])
        return self._custom_link_cache.get(token, {})

    def _custom_value(self, book_id: int, location: str) -> Any:
        label = location[1:]
        # The derived series-index location (`#myseries_index`): a float the
        # link table's `extra` column feeds (load_custom_column stashes it
        # alongside the base values). A real column literally labeled
        # `<x>_index` keeps the token; exact labels always win.
        if label.endswith("_index") and label not in self._custom_by_label():
            base = self._custom_by_label().get(label[: -len("_index")])
            if base is not None and base["datatype"] == "series":
                base_token = "#" + base["label"]
                if base_token not in self._custom_val_cache:
                    self.load_custom_column(base["name"])
                return self._custom_val_cache.get(base_token + "_index", {}).get(
                    book_id
                )
        col = self._custom_by_label().get(label)
        if not col:
            return None

        if col["datatype"] == "comments":
            if location not in self._custom_val_cache:
                self._custom_val_cache[location] = {}
            if book_id not in self._custom_val_cache[location]:
                cid = int(col["id"])  # f-string SQL below; never interpolate raw ids
                try:
                    cur = self.conn.cursor()
                    cur.execute(
                        f"SELECT value FROM custom_column_{cid} WHERE book = ?",
                        (book_id,),
                    )
                    row = cur.fetchone()
                    self._custom_val_cache[location][book_id] = (
                        row["value"] if row else None
                    )
                except sqlite3.OperationalError:
                    self._custom_val_cache[location][book_id] = None
            return self._custom_val_cache[location][book_id]

        if location not in self._custom_val_cache:
            try:
                self._custom_val_cache[location] = self.load_custom_column(col["name"])
            except (ValueError, sqlite3.OperationalError):
                self._custom_val_cache[location] = {}
        val = self._custom_val_cache[location].get(book_id)
        if val is None:
            return None
        if col["datatype"] == "rating":
            # Calibre stores custom ratings on the same 0-10 internal scale
            # as the builtin rating; surface stars so both compare alike in
            # the engine (the writer's star input mirrors set_rating).
            return calibre_rating_to_stars(int(val))
        return val
