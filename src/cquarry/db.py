import json
import os
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

from cquarry.helpers import calibre_rating_to_stars, db_uri_ro
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
            for suffix in ("", "-wal", "-shm"):
                path = self._tmp_path + suffix
                try:
                    os.unlink(path)
                except OSError:
                    pass
            self._tmp_path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- Core queries ---

    def get_all_books(self) -> list[dict[str, Any]]:
        """Fetch all books with full metadata via joins. Results are cached."""
        if self._books_cache is not None:
            return self._books_cache
        cur = self.conn.cursor()
        cur.execute("""
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
            ORDER BY b.author_sort, b.sort
        """)
        books = [dict(row) for row in cur.fetchall()]

        # Authors
        amap = {}
        for row in self.conn.execute(
            "SELECT bal.book, a.name FROM books_authors_link bal JOIN authors a ON a.id = bal.author ORDER BY bal.id"
        ):
            amap.setdefault(row["book"], []).append(row["name"])
        # Tags
        tmap = {}
        for row in self.conn.execute(
            "SELECT btl.book, t.name FROM books_tags_link btl JOIN tags t ON t.id = btl.tag ORDER BY t.name"
        ):
            tmap.setdefault(row["book"], []).append(row["name"])
        # Languages
        lmap = {}
        for row in self.conn.execute(
            "SELECT bll.book, l.lang_code FROM books_languages_link bll JOIN languages l ON l.id = bll.lang_code"
        ):
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

        for b in books:
            b["authors"] = amap.get(b["id"], [])
            b["tags"] = tmap.get(b["id"], [])
            b["languages"] = lmap.get(b["id"], [])
            b["formats"] = fmap.get(b["id"], [])
            b["size"] = smap.get(b["id"])

        self._books_cache = books
        return self._books_cache

    def get_identifiers(self, book_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
        return {row["type"]: row["val"] for row in cur.fetchall()}

    def get_book(self, book_id: int) -> dict[str, Any] | None:
        """Fetch a single hydrated book record without scanning the library.

        Returns the same shape as one ``get_all_books()`` row, or None when
        the id does not exist.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
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
            WHERE b.id = ?
            """,
            (book_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        b = dict(row)
        cur.execute(
            "SELECT a.name FROM books_authors_link bal "
            "JOIN authors a ON a.id = bal.author WHERE bal.book = ? ORDER BY bal.id",
            (book_id,),
        )
        b["authors"] = [r["name"] for r in cur.fetchall()]
        cur.execute(
            "SELECT t.name FROM books_tags_link btl "
            "JOIN tags t ON t.id = btl.tag WHERE btl.book = ? ORDER BY t.name",
            (book_id,),
        )
        b["tags"] = [r["name"] for r in cur.fetchall()]
        cur.execute(
            "SELECT l.lang_code FROM books_languages_link bll "
            "JOIN languages l ON l.id = bll.lang_code WHERE bll.book = ?",
            (book_id,),
        )
        b["languages"] = [r["lang_code"] for r in cur.fetchall()]
        cur.execute("SELECT format FROM data WHERE book = ?", (book_id,))
        b["formats"] = [r["format"] for r in cur.fetchall()]
        return b

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

    def get_custom_columns(self) -> dict[str, dict[str, Any]]:
        """Return metadata for all custom columns, keyed by display name."""
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT id, label, name, datatype, is_multiple FROM custom_columns"
            )
            return {row["name"]: dict(row) for row in cur.fetchall()}
        except sqlite3.OperationalError:
            return {}

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
            else:
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
                try:
                    rec["annot_data"] = json.loads(data)
                except json.JSONDecodeError, TypeError:
                    pass
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
            # Calibre has no native pages table; conventions place page counts
            # in an int custom column labelled 'pages'. Resolve lazily.
            col = self._pages_column()
            if col is None:
                return None
            val = self._custom_value(book_id, "#pages")
            return int(val) if isinstance(val, (int, float)) else None

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
