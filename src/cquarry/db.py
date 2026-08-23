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
    DT_TEXT,
    DT_TEXT_MULTI,
    SearchEngine,
)


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
                (SELECT GROUP_CONCAT(name, ', ') FROM (SELECT a_inner.name as name FROM books_authors_link bal JOIN authors a_inner ON a_inner.id = bal.author WHERE bal.book = b.id ORDER BY bal.id)) as authors,
                (SELECT GROUP_CONCAT(format, ', ') FROM data d WHERE d.book = b.id) as formats,
                (SELECT GROUP_CONCAT(name, ', ') FROM (SELECT t_inner.name as name FROM books_tags_link btl JOIN tags t_inner ON t_inner.id = btl.tag WHERE btl.book = b.id ORDER BY t_inner.name)) as tags,
                s.name as series,
                r.rating,
                p.name as publisher,
                (SELECT GROUP_CONCAT(l.lang_code, ', ') FROM books_languages_link bll JOIN languages l ON l.id = bll.lang_code WHERE bll.book = b.id) as languages
            FROM books b
            LEFT JOIN books_series_link bsl ON bsl.book = b.id
            LEFT JOIN series s ON s.id = bsl.series
            LEFT JOIN books_ratings_link brl ON brl.book = b.id
            LEFT JOIN ratings r ON r.id = brl.rating
            LEFT JOIN books_publishers_link bpl ON bpl.book = b.id
            LEFT JOIN publishers p ON p.id = bpl.publisher
            ORDER BY b.author_sort, b.sort
        """)
        self._books_cache = [dict(row) for row in cur.fetchall()]
        return self._books_cache

    def get_identifiers(self, book_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT type, val FROM identifiers WHERE book = ?", (book_id,))
        return {row["type"]: row["val"] for row in cur.fetchall()}

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

    def count_books(self) -> int:
        if self._all_ids_cache is not None:
            return len(self._all_ids_cache)
        if self._books_cache is not None:
            return len(self._books_cache)
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM books")
        return cur.fetchone()["c"]

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
        """
        vls = self.get_virtual_libraries()
        if vl_name not in vls:
            raise ValueError(
                f"Unknown virtual library: '{vl_name}'. "
                f"Available: {', '.join(sorted(vls.keys()))}"
            )
        return self._engine().search(vls[vl_name])

    # --- search.MetadataProvider interface ---

    def all_ids(self) -> set[int]:
        return set(self._get_all_book_ids())

    def vl_expression(self, name: str) -> str | None:
        return self.get_virtual_libraries().get(name)

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
                self._comments_cache[book_id] = row["text"] if row and row["text"] else ""
            return self._comments_cache[book_id]

        rec = self._build_search_view().get(book_id)
        return rec.get(location) if rec else None

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

        def _split(s: str | None) -> list[str]:
            return [p.strip() for p in s.split(",")] if s else []

        view: dict[int, dict[str, Any]] = {}
        for b in self.get_all_books():
            view[b["id"]] = {
                "title": b["title"] or "",
                "authors": _split(b["authors"]),
                "author_sort": b["author_sort"] or "",
                "series": b["series"] or "",
                "publisher": b["publisher"] or "",
                "tags": _split(b["tags"]),
                "formats": _split(b["formats"]),
                "languages": _split(b["languages"]),
                "rating": calibre_rating_to_stars(b["rating"]),
                "series_index": b["series_index"],
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
        "rating": DT_FLOAT,
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
                    cur.execute(f"SELECT value FROM custom_column_{cid} WHERE book = ?", (book_id,))
                    row = cur.fetchone()
                    self._custom_val_cache[location][book_id] = row["value"] if row else None
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
