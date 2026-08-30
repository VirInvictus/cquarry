import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

from cquarry.helpers import calibre_rating_to_stars, db_uri_ro, strip_html, title_sort
from cquarry.search import (
    DT_BOOL,
    DT_DATE,
    DT_FLOAT,
    DT_INT,
    DT_RATING,
    DT_TEXT,
    DT_TEXT_MULTI,
    SearchEngine,
)

# Sentinel distinguishing "cache not populated" from a cached None result.
_UNSET = object()

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

    def __init__(self, db_path: str):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        self._tmp_path: str | None = None
        self._vl_cache: dict[str, str] | None = None
        self._books_cache: list[dict[str, Any]] | None = None
        self._all_ids_cache: set[int] | None = None

        # Search-engine state (lazily built).
        self._search_engine: SearchEngine | None = None
        self._search_view: dict[int, dict[str, Any]] | None = None
        self._custom_loc_cache: dict[str, str] | None = None
        self._custom_label_cache: dict[str, dict[str, Any]] | None = None
        self._custom_val_cache: dict[str, dict[int, Any]] = {}
        self._comments_cache: dict[int, str] | None = None
        self._pages_col_cache: Any = _UNSET
        self._pages_cache: dict[int, int] | None = None
        self._format_path_index: dict[str, int] | None = None
        self._prefs_cache: dict[str, Any] | None = None
        self._cc_schema_cache: dict[str, bool] | None = None
        self._annotations_text_cache: dict[int, str] | None = None

        self.conn = self._open(db_path)
        self.conn.row_factory = sqlite3.Row

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
        # Calibre has the DB locked — copy to a temp file and read from there
        print(
            "NOTE: Database is locked (Calibre is running). "
            "Reading from a snapshot copy.",
            file=sys.stderr,
        )
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="cquarry_")
        os.close(fd)
        shutil.copy2(db_path, tmp)
        # Also copy the WAL and SHM files if they exist so the snapshot is consistent
        for suffix in ("-wal", "-shm"):
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copy2(src, tmp + suffix)
        self._tmp_path = tmp
        return sqlite3.connect(db_uri_ro(tmp), uri=True)

    def close(self):
        self.conn.close()
        if self._tmp_path:
            with contextlib.suppress(OSError):
                for suffix in ("", "-wal", "-shm"):
                    os.unlink(self._tmp_path + suffix)
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
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
        books = [dict(row) for row in cur.fetchall()]

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

    def get_identifiers(self, book_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
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

    def format_path_index(self) -> dict[str, int]:
        """Map every catalogued format file path to its book id.

        One ``data ⋈ books`` query; each path is built exactly as
        :meth:`get_format_path` builds it (library root from the original DB
        location), with ``normcase(normpath())`` keys so the same file spelled
        differently (case, redundant separators) collapses to one entry.
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

        Accepts relative or differently-cased spellings of the same file
        (``normcase``/``normpath`` on the lookup side). Returns None when no
        catalogued format resolves there.
        """
        key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        return self.format_path_index().get(key)

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

    def load_custom_column(self, col_name: str) -> dict[int, Any]:
        """Load values for a specific custom column (by display name). Returns {book_id: value(s)}."""
        cols = self.get_custom_columns()
        if col_name not in cols:
            raise ValueError(
                f"Custom column '{col_name}' not found. Available: {', '.join(cols.keys())}"
            )

        col = cols[col_name]
        cid = col["id"]
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
                cur.execute(f"""
                    SELECT l.book, c.value
                    FROM {link_table} l
                    JOIN custom_column_{cid} c ON c.id = l.value
                """)
                grouped: dict[int, list] = {}
                for row in cur.fetchall():
                    grouped.setdefault(row["book"], []).append(row["value"])
                if col["is_multiple"]:
                    # Join to a comma-separated string for parity with other fields.
                    return {
                        k: ", ".join(str(v) for v in vals)
                        for k, vals in grouped.items()
                    }
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
        """Return {name: search_expression} from Calibre preferences."""
        if self._vl_cache is not None:
            return self._vl_cache
        cur = self.conn.cursor()
        cur.execute("SELECT val FROM preferences WHERE key = 'virtual_libraries'")
        row = cur.fetchone()
        if row:
            self._vl_cache = json.loads(row["val"])
        else:
            self._vl_cache = {}
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
            return {}
        return json.loads(row["val"]) if row else {}

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
            except json.JSONDecodeError, TypeError:
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

    def count_books(self) -> int:
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
        self, book_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Map reading progress per device from ``last_read_positions``.

        Each row carries ``book``, ``format``, ``user``, ``device``, ``cfi``,
        ``epoch`` (unix seconds — sort key for "most recent") and
        ``pos_frac`` (0.0-1.0 progress fraction). Columns follow Calibre's
        real schema exactly (there is no ``user_type`` and the time column is
        ``epoch``, not ``epoch_time``).
        """
        cur = self.conn.cursor()
        sql = (
            "SELECT id, book, format, user, device, cfi, epoch, pos_frac "
            "FROM last_read_positions"
        )
        params: tuple = ()
        if book_id is not None:
            sql += " WHERE book = ?"
            params = (book_id,)
        sql += " ORDER BY book, device"
        try:
            cur.execute(sql, params)
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
            return []
        label_by_id: dict[int, str] = {}
        with contextlib.suppress(Exception):
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
                for select in (
                    f"SELECT id, name, count, avg_rating, sort FROM {name} ",
                    f"SELECT id, value AS name, count, avg_rating, sort FROM {name} ",
                    f"SELECT id, CAST(rating AS TEXT) AS name, count, avg_rating, sort FROM {name} ",
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
        book has no annotations or the schema predates the table.
        """
        if self._annotations_text_cache is None:
            self._annotations_text_cache = {}
        if book_id not in self._annotations_text_cache:
            parts: list[str] = []
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT searchable_text FROM annotations "
                    "WHERE book = ? AND searchable_text IS NOT NULL "
                    "ORDER BY id",
                    (book_id,),
                )
                parts = [r[0] for r in cur.fetchall() if r[0]]
            except sqlite3.OperationalError:
                pass
            self._annotations_text_cache[book_id] = "\n".join(parts)
        return self._annotations_text_cache[book_id]

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
        Name matching is case-insensitive; unknown names raise ValueError.
        """
        vls = self.get_virtual_libraries()
        expr = self.vl_expression(vl_name)
        if expr is None:
            raise ValueError(
                f"Unknown virtual library: '{vl_name}'. "
                f"Available: {', '.join(sorted(vls.keys()))}"
            )
        return self._engine()._match_vl(
            next(n for n in vls if n.lower() == vl_name.lower()),
            self.all_ids(),
            set(),
        )

    def resolve_saved_search(self, name: str) -> set[int]:
        """Resolve a saved search name to a set of book IDs (case-insensitive)."""
        sss = self.get_saved_searches()
        if name.lower() not in {n.lower() for n in sss}:
            raise ValueError(
                f"Unknown saved search: '{name}'. "
                f"Available: {', '.join(sorted(sss.keys()))}"
            )
        return self._engine()._match_saved_search(name, self.all_ids(), set())

    # --- search.MetadataProvider interface ---

    def all_ids(self) -> set[int]:
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
        cache = self._custom_loc_cache
        if cache is None:
            cache = self._custom_loc_cache = self._build_custom_locations()
        return cache

    def field(self, book_id: int, location: str) -> Any:
        if location.startswith("#"):
            return self._custom_value(book_id, location)

        if location == "comments":
            if self._comments_cache is None:
                self._comments_cache = {}
            if book_id not in self._comments_cache:
                cur = self.conn.cursor()
                cur.execute("SELECT text FROM comments WHERE book = ?", (book_id,))
                row = cur.fetchone()
                self._comments_cache[book_id] = (
                    row["text"] if row and row["text"] else ""
                )
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
        for row in cur.execute("SELECT book, type, val FROM identifiers"):
            rec = view.get(row["book"])
            if rec is not None:
                rec["identifiers"][row["type"]] = row["val"]
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

    def _custom_value(self, book_id: int, location: str) -> Any:
        col = self._custom_by_label().get(location[1:])
        if not col:
            return None

        if col["datatype"] == "comments":
            if location not in self._custom_val_cache:
                self._custom_val_cache[location] = {}
            if book_id not in self._custom_val_cache[location]:
                cid = col["id"]
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
            except ValueError, sqlite3.OperationalError:
                self._custom_val_cache[location] = {}
        val = self._custom_val_cache[location].get(book_id)
        if val is None:
            return None
        if col["is_multiple"] and isinstance(val, str):
            return [p.strip() for p in val.split(",") if p.strip()]
        return val
