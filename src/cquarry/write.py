"""Opt-in write access to a Calibre ``metadata.db``.

cquarry's read path (:class:`cquarry.db.CalibreDB`) is read-only by contract.
This module is the explicitly separate write path anticipated by spec.md §2:
it is never reachable through ``CalibreDB`` and must be imported on purpose.

Safety contract:
  - Opens the database with a generous ``busy_timeout`` so Calibre holding the
    lock degrades to waiting instead of erroring out.
  - Registers the custom SQL functions Calibre's triggers call (``title_sort``,
    ``uuid4``) plus its ``PYNOCASE`` collation BEFORE any statement runs;
    without them ``books_insert_trg`` / ``books_update_trg`` abort writes.
  - Every mutation bumps ``books.last_modified`` AND records the book id in
    the ``metadata_dirtied`` queue. Calibre only regenerates a book's sidecar
    .opf (and pushes it to wireless readers) for ids present in that table
    (backend.py ``dirtied_books()``), so skipping the insert would leave
    external edits invisible to Calibre's sync machinery forever. Databases
    from before the table existed keep working: the insert is guarded by an
    existence check.
  - Mutations run inside explicit ``BEGIN IMMEDIATE`` transactions.
  - Tag deletion cleans ``books_tags_link`` before ``tags`` to satisfy the
    ``fkc_delete_on_tags`` trigger ordering.

Example::

    from cquarry.write import WritableCalibreDB

    with WritableCalibreDB("~/Calibre Library/metadata.db") as wdb:
        wdb.add_tag(42, "Audited")
        wdb.set_identifier(42, "isbn", "9780123456789")
"""

import contextlib
import json
import os
import sqlite3
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any, Self

from cquarry.helpers import title_sort

__all__ = ["WritableCalibreDB", "register_udfs", "title_sort", "uuid4"]


def uuid4(_arg: Any = None) -> str:
    """SQL-callable UUID generator matching Calibre's ``uuid4()`` UDF."""
    return str(_uuid.uuid4())


def _pynocase(lhs: str, rhs: str) -> int:
    """Case-insensitive comparison collation used by Calibre."""
    left, right = lhs.lower(), rhs.lower()
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def register_udfs(conn: sqlite3.Connection) -> None:
    """Register the SQL functions/collations Calibre triggers depend on.

    Call this on any read-write connection before touching ``books``; the
    schema triggers invoke ``title_sort()`` and ``uuid4()`` on insert/update.
    """
    conn.create_function("title_sort", 1, title_sort)
    conn.create_function("uuid4", 0, uuid4, deterministic=True)
    conn.create_collation("PYNOCASE", _pynocase)


class WritableCalibreDB:
    """Explicitly opt-in read/write handle for metadata.db.

    This is intentionally a *different class* from cquarry.db.CalibreDB so no
    read-only code path can accidentally mutate a library.

    Transaction control is pinned to sqlite3's legacy mode so every mutating
    method can open ``BEGIN IMMEDIATE`` itself: take the write lock up front
    (``busy_timeout`` applies to acquisition), commit on success, roll back
    on any error. Nothing is written unless a method returns normally.
    """

    def __init__(self, db_path: str):
        db_path = os.path.abspath(os.path.expanduser(db_path))
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        # Pinned explicitly: the write path needs `BEGIN IMMEDIATE` for
        # take-the-write-lock-upfront semantics (busy_timeout then applies to
        # lock acquisition). PEP 249 `autocommit=False` holds a transaction
        # open from the first statement, which makes a raw BEGIN impossible —
        # verified empirically (in_transaction is True on a fresh connection).
        self.conn = sqlite3.connect(
            db_path, timeout=30.0, autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        register_udfs(self.conn)
        self._dirtied_supported: bool | None = None

    # -- lifecycle --

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.commit()
        self.close()

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _now() -> str:
        # Calibre stores 'YYYY-MM-DD HH:MM:SS.SSSSSS+00:00' style UTC stamps.
        return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"

    def _require_book(self, book_id: int) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Book {book_id} not found")

    def _mark_dirty(self, book_id: int) -> None:
        """Queue the book for OPF regeneration in ``metadata_dirtied``.

        Calibre regenerates a book's sidecar .opf — and re-pushes metadata to
        wireless devices — ONLY for ids present in this table; it consumes and
        clears the queue at startup (backend.py ``dirty_books()`` /
        ``dirtied_books()``). Without this insert, external mutations bump
        ``last_modified`` but never reach OPF/wireless sync. The insert is
        skipped on schemas predating the table (existence check cached per
        connection).
        """
        if self._dirtied_supported is None:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'metadata_dirtied'"
            ).fetchone()
            self._dirtied_supported = row is not None
        if self._dirtied_supported:
            self.conn.execute(
                "INSERT OR IGNORE INTO metadata_dirtied(book) VALUES (?)",
                (book_id,),
            )

    def _touch_book(self, book_id: int) -> None:
        """Bump ``last_modified`` and queue OPF regeneration for the book."""
        self.conn.execute(
            "UPDATE books SET last_modified = ? WHERE id = ?", (self._now(), book_id)
        )
        self._mark_dirty(book_id)

    # -- Phase 3 write APIs --

    def update_title(self, book_id: int, new_title: str) -> None:
        """Rename a book, refreshing its sort key and last_modified stamp."""
        new_title = new_title.strip()
        if not new_title:
            raise ValueError("Title must not be empty")
        self._begin()
        try:
            self._require_book(book_id)
            self.conn.execute(
                "UPDATE books SET title = ?, sort = ?, last_modified = ? WHERE id = ?",
                (new_title, title_sort(new_title), self._now(), book_id),
            )
            self._mark_dirty(book_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def add_tag(self, book_id: int, tag: str) -> bool:
        """Attach a tag to a book. Returns True if a link was created.

        Follows Calibre's sequence: INSERT OR IGNORE into ``tags``, resolve the
        id, link via ``books_tags_link``, bump ``last_modified``.
        """
        tag = tag.strip()
        if not tag:
            raise ValueError("Tag must not be empty")
        cur = self.conn.cursor()
        self._begin()
        try:
            self._require_book(book_id)
            if (
                cur.execute("SELECT 1 FROM tags WHERE name = ?", (tag,)).fetchone()
                is None
            ):
                cur.execute("INSERT INTO tags(name) VALUES (?)", (tag,))
            tag_id = cur.execute(
                "SELECT id FROM tags WHERE name = ?", (tag,)
            ).fetchone()["id"]
            already = cur.execute(
                "SELECT 1 FROM books_tags_link WHERE book = ? AND tag = ?",
                (book_id, tag_id),
            ).fetchone()
            changed = already is None
            if changed:
                cur.execute(
                    "INSERT INTO books_tags_link(book, tag) VALUES (?, ?)",
                    (book_id, tag_id),
                )
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def remove_tag(self, book_id: int, tag: str) -> bool:
        """Detach a tag from a book. Returns True if a link was removed.

        Cleans the link table first, then prunes an orphaned tag row — the
        order required by the fkc_delete_on_tags trigger.
        """
        tag = tag.strip()
        cur = self.conn.cursor()
        self._begin()
        try:
            row = cur.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()
            if row is None:
                self.conn.rollback()
                return False
            tag_id = row["id"]
            before = self.conn.total_changes
            cur.execute(
                "DELETE FROM books_tags_link WHERE book = ? AND tag = ?",
                (book_id, tag_id),
            )
            changed = self.conn.total_changes > before
            still_used = cur.execute(
                "SELECT 1 FROM books_tags_link WHERE tag = ? LIMIT 1", (tag_id,)
            ).fetchone()
            if still_used is None:
                cur.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def set_identifier(self, book_id: int, id_type: str, val: str | None) -> bool:
        """Upsert one entry in the EAV ``identifiers`` table.

        ``val=None`` (or blank) deletes the pair. The table is UNIQUE(book,
        type), so an existing value of the same type is replaced. Returns True
        when stored state changed.
        """
        id_type = id_type.strip().lower()
        if not id_type:
            raise ValueError("Identifier type must not be empty")
        clean_val = val.strip() if isinstance(val, str) else val
        cur = self.conn.cursor()
        self._begin()
        try:
            self._require_book(book_id)
            row = cur.execute(
                "SELECT id, val FROM identifiers WHERE book = ? AND type = ?",
                (book_id, id_type),
            ).fetchone()
            changed = False
            if not clean_val:
                if row is not None:
                    cur.execute("DELETE FROM identifiers WHERE id = ?", (row["id"],))
                    changed = True
            elif row is None:
                cur.execute(
                    "INSERT INTO identifiers(book, type, val) VALUES (?, ?, ?)",
                    (book_id, id_type, clean_val),
                )
                changed = True
            elif row["val"] != clean_val:
                cur.execute(
                    "UPDATE identifiers SET val = ? WHERE id = ?",
                    (clean_val, row["id"]),
                )
                changed = True
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def set_identifiers(self, book_id: int, pairs: dict[str, str | None]) -> int:
        """Batch-upsert identifiers. Returns how many entries changed."""
        changed = 0
        for id_type, val in pairs.items():
            if self.set_identifier(book_id, id_type, val):
                changed += 1
        return changed

    # -- Entity setters (Phase 6: write-side expansion) --

    _ENTITY_TABLES = {
        "authors": ("books_authors_link", "author"),
        "series": ("books_series_link", "series"),
        "publishers": ("books_publishers_link", "publisher"),
        "tags": ("books_tags_link", "tag"),
        "languages": ("books_languages_link", "lang_code"),
        "ratings": ("books_ratings_link", "rating"),
    }

    def _prune_orphans(self, table: str) -> None:
        """Delete entity rows no longer referenced by their link table.

        The fkc_delete_on_* triggers ABORT if links remain; callers must have
        cleaned the book's links first.
        """
        link_table, fk = self._ENTITY_TABLES[table]
        self.conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN (SELECT {fk} FROM {link_table})"
        )

    # Entities whose tables carry a `sort` column (tags/languages do not).
    _HAS_SORT = {
        "authors": True,
        "series": True,
        "publishers": True,
        "tags": False,
        "languages": False,
    }

    def _resolve_or_create(self, table: str, name_col: str, name: str) -> int:
        """Find an entity row by (case-insensitive) name, creating it if new.

        New rows default ``sort`` to the display value on tables that carry
        the column — Calibre's own starting point — leaving manual sort edits
        untouched on existing rows.
        """
        row = self.conn.execute(
            f"SELECT id FROM {table} WHERE {name_col} = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if row is not None:
            return row["id"]
        if self._HAS_SORT.get(table, False):
            cur = self.conn.execute(
                f"INSERT INTO {table} ({name_col}, sort) VALUES (?, ?)",
                (name, name),
            )
        else:
            cur = self.conn.execute(
                f"INSERT INTO {table} ({name_col}) VALUES (?)", (name,)
            )
        return cur.lastrowid

    def set_authors(self, book_id: int, names: list[str]) -> bool:
        """Replace the book's author list. Returns True when state changed.

        Creates missing author rows (``sort`` defaults to the display name,
        Calibre's starting point), relinks in the given order, recomputes
        ``books.author_sort`` from the authors' sort keys joined with " & "
        (upstream's ``authors_to_sort_string`` behavior) and prunes
        now-orphaned author rows.
        """
        cleaned = [n.strip() for n in names if n and n.strip()]
        if not cleaned:
            raise ValueError("Author list must not be empty")
        self._begin()
        try:
            self._require_book(book_id)
            new_ids: list[int] = []
            sorts: list[str] = []
            for name in cleaned:
                aid = self._resolve_or_create("authors", "name", name)
                if aid in new_ids:
                    raise ValueError(f"Duplicate author: {name}")
                new_ids.append(aid)
                srow = self.conn.execute(
                    "SELECT COALESCE(sort, '') AS s FROM authors WHERE id = ?",
                    (aid,),
                ).fetchone()
                sorts.append(srow["s"] or name)
            current = [
                r["author"]
                for r in self.conn.execute(
                    "SELECT author FROM books_authors_link WHERE book = ? ORDER BY id",
                    (book_id,),
                )
            ]
            new_sort = " & ".join(sorts)
            old_sort_row = self.conn.execute(
                "SELECT author_sort FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if (
                current == new_ids
                and old_sort_row is not None
                and old_sort_row["author_sort"] == new_sort
            ):
                self.conn.rollback()
                return False
            self.conn.execute(
                "DELETE FROM books_authors_link WHERE book = ?", (book_id,)
            )
            for aid in new_ids:
                self.conn.execute(
                    "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
                    (book_id, aid),
                )
            self.conn.execute(
                "UPDATE books SET author_sort = ?, last_modified = ? WHERE id = ?",
                (new_sort, self._now(), book_id),
            )
            self._prune_orphans("authors")
            self._mark_dirty(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def set_series(
        self, book_id: int, name: str | None, index: float | None = None
    ) -> bool:
        """Assign (or clear, with ``name=None``) the book's series.

        Returns True when stored state changed. ``index`` defaults to 1.0 on a
        fresh assignment; clearing nulls both the link and
        ``books.series_index``; orphaned series rows are pruned.
        """
        self._begin()
        try:
            self._require_book(book_id)
            old = self.conn.execute(
                "SELECT series FROM books_series_link WHERE book = ?",
                (book_id,),
            ).fetchone()

            def _current_index() -> float | None:
                row = self.conn.execute(
                    "SELECT series_index FROM books WHERE id = ?", (book_id,)
                ).fetchone()
                return (
                    float(row["series_index"])
                    if row["series_index"] is not None
                    else None
                )

            if name is None:
                if old is None:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_series_link WHERE book = ?", (book_id,)
                )
                self.conn.execute(
                    "UPDATE books SET series_index = NULL, last_modified = ? "
                    "WHERE id = ?",
                    (self._now(), book_id),
                )
                self._prune_orphans("series")
                self._mark_dirty(book_id)
                self.conn.commit()
                return True

            sid = self._resolve_or_create("series", "name", name.strip())
            if index is not None:
                new_index = float(index)
            elif old is not None and old["series"] == sid:
                new_index = _current_index() if _current_index() is not None else 1.0
            else:
                new_index = 1.0
            if (
                old is not None
                and old["series"] == sid
                and _current_index() == new_index
            ):
                self.conn.rollback()
                return False
            self.conn.execute(
                "DELETE FROM books_series_link WHERE book = ?", (book_id,)
            )
            self.conn.execute(
                "INSERT INTO books_series_link (book, series) VALUES (?, ?)",
                (book_id, sid),
            )
            self.conn.execute(
                "UPDATE books SET series_index = ?, last_modified = ? WHERE id = ?",
                (new_index, self._now(), book_id),
            )
            self._prune_orphans("series")
            self._mark_dirty(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def set_publisher(self, book_id: int, name: str | None) -> bool:
        """Replace (or clear, with ``name=None``) the book's publisher.

        Returns True when stored state changed; orphaned publisher rows are
        pruned.
        """
        self._begin()
        try:
            self._require_book(book_id)
            old = self.conn.execute(
                "SELECT publisher FROM books_publishers_link WHERE book = ?",
                (book_id,),
            ).fetchone()
            if name is None:
                if old is None:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_publishers_link WHERE book = ?", (book_id,)
                )
            else:
                pid = self._resolve_or_create("publishers", "name", name.strip())
                if old is not None and old["publisher"] == pid:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_publishers_link WHERE book = ?", (book_id,)
                )
                self.conn.execute(
                    "INSERT INTO books_publishers_link (book, publisher) VALUES (?, ?)",
                    (book_id, pid),
                )
            self._prune_orphans("publishers")
            self._touch_book(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def set_rating(self, book_id: int, stars: float | None) -> bool:
        """Set the rating in 0-5 stars (Calibre stores ``stars * 2``).

        Returns True when stored state changed. The ratings table is
        UNIQUE(rating): rows are found-or-created accordingly and orphaned
        rows pruned. ``None`` clears the rating.
        """
        self._begin()
        try:
            self._require_book(book_id)
            old = self.conn.execute(
                "SELECT rating FROM books_ratings_link WHERE book = ?",
                (book_id,),
            ).fetchone()
            if stars is None:
                if old is None:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_ratings_link WHERE book = ?", (book_id,)
                )
            else:
                internal = round(float(stars) * 2)
                if not 0 <= internal <= 10:
                    raise ValueError(f"Rating must be within 0-5 stars, got {stars}")
                row = self.conn.execute(
                    "SELECT id FROM ratings WHERE rating = ?", (internal,)
                ).fetchone()
                rid = (
                    row["id"]
                    if row is not None
                    else self.conn.execute(
                        "INSERT INTO ratings (rating) VALUES (?)", (internal,)
                    ).lastrowid
                )
                if old is not None and old["rating"] == rid:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_ratings_link WHERE book = ?", (book_id,)
                )
                self.conn.execute(
                    "INSERT INTO books_ratings_link (book, rating) VALUES (?, ?)",
                    (book_id, rid),
                )
            self._prune_orphans("ratings")
            self._touch_book(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def set_languages(self, book_id: int, codes: list[str] | str | None) -> bool:
        """Replace the book's languages. Returns True when changed.

        Accepts ISO 639-2 codes or English names - canonicalized through the
        same map the search engine uses (``English`` -> ``eng``). A bare
        comma-separated string is split first; ``None``/empty clears the list.
        Orphaned language rows are pruned.
        """
        from .search import canonical_language

        if codes is None or codes == "":
            raw_items: list[str] = []
        elif isinstance(codes, str):
            raw_items = [p for p in (x.strip() for x in codes.split(",")) if p]
        else:
            raw_items = [str(c).strip() for c in codes]
        cleaned: list[str] = []
        for item in raw_items:
            canon = canonical_language(item)
            if canon and canon not in cleaned:
                cleaned.append(canon)
        self._begin()
        try:
            self._require_book(book_id)
            old_ids = [
                r["lang_code"]
                for r in self.conn.execute(
                    "SELECT lang_code FROM books_languages_link WHERE book=? "
                    "ORDER BY id",
                    (book_id,),
                )
            ]
            if not cleaned:
                if not old_ids:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_languages_link WHERE book=?", (book_id,)
                )
            else:
                new_ids: list[int] = []
                for code in cleaned:
                    lid = self._resolve_or_create("languages", "lang_code", code)
                    if lid not in new_ids:
                        new_ids.append(lid)
                if new_ids == old_ids:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_languages_link WHERE book=?", (book_id,)
                )
                for lid in new_ids:
                    self.conn.execute(
                        "INSERT INTO books_languages_link (book, lang_code) "
                        "VALUES (?, ?)",
                        (book_id, lid),
                    )
            self._prune_orphans("languages")
            self._touch_book(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def set_comments(self, book_id: int, text: str | None) -> bool:
        """Upsert (or clear, with ``None``/empty) the book's comments HTML.

        The comments table is UNIQUE(book): a 1:1 upsert. Returns True when
        stored state changed. Raw HTML is stored verbatim - Calibre treats
        this column as HTML; readers sanitize via ``strip_html``.
        """
        self._begin()
        try:
            self._require_book(book_id)
            clean = text.strip() if isinstance(text, str) else None
            row = self.conn.execute(
                "SELECT id, text FROM comments WHERE book = ?", (book_id,)
            ).fetchone()
            if not clean:
                if row is None:
                    self.conn.rollback()
                    return False
                self.conn.execute("DELETE FROM comments WHERE book = ?", (book_id,))
                changed = True
            elif row is None:
                self.conn.execute(
                    "INSERT INTO comments (book, text) VALUES (?, ?)",
                    (book_id, clean),
                )
                changed = True
            else:
                changed = row["text"] != clean
                if changed:
                    self.conn.execute(
                        "UPDATE comments SET text = ? WHERE book = ?",
                        (clean, book_id),
                    )
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    # -- Custom-column writers --

    def _custom_column_meta(self, label: str) -> dict[str, Any]:
        """One custom_columns row by label (case-insensitive), or ValueError."""
        row = self.conn.execute(
            "SELECT id, label, name, datatype, is_multiple, editable, display "
            "FROM custom_columns WHERE label = ? COLLATE NOCASE",
            (label.lstrip("#"),),
        ).fetchone()
        if row is None:
            raise ValueError(f"Custom column #{label} not found")
        meta = dict(row)
        try:
            meta["display"] = json.loads(meta["display"]) if meta["display"] else {}
        except json.JSONDecodeError, TypeError:
            meta["display"] = {}
        return meta

    def set_custom_column(self, book_id: int, label: str, value: Any) -> bool:
        """Write one custom-column value (or clear with ``value=None``).

        Storage follows the column's physical layout, detected by whether the
        ``books_custom_column_N_link`` table exists (the same rule the reader
        uses):
          - Pattern A (link table): text / multi-valued text / enumeration /
            series-typed columns. Values live in ``custom_column_N`` and are
            joined through links; enumerations are validated against
            ``display.enum_values``.
          - Pattern B (direct): int / float / bool / datetime / comments.
        Composite columns are computed, not stored - writing raises.
        Returns True when stored state changed. Non-editable columns raise.
        """
        meta = self._custom_column_meta(label)
        if not meta["editable"]:
            raise ValueError(f"Custom column #{label} is not editable")
        cid = meta["id"]
        datatype = str(meta["datatype"]).lower()
        if datatype == "composite":
            raise ValueError(
                f"#{label} is a composite column; Calibre computes it and it "
                "has no storage to write"
            )
        link_table = f"books_custom_column_{cid}_link"
        value_table = f"custom_column_{cid}"
        has_link = bool(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (link_table,),
            ).fetchone()
        )
        self._begin()
        try:
            self._require_book(book_id)
            changed = False
            if has_link:
                changed = self._write_pattern_a(
                    meta, link_table, value_table, book_id, value
                )
            else:
                changed = self._write_pattern_b(meta, value_table, book_id, value)
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def _clear_pattern_a(
        self, link_table: str, value_table: str, book_id: int, keep_values: bool = True
    ) -> None:
        self.conn.execute(f"DELETE FROM {link_table} WHERE book = ?", (book_id,))
        # Value rows are shared across books only via is_multiple joins;
        # single-valued columns can orphan rows once no link references them.
        if keep_values:
            self.conn.execute(
                f"DELETE FROM {value_table} WHERE id NOT IN "
                f"(SELECT value FROM {link_table})"
            )

    def _write_pattern_a(self, meta, link_table, value_table, book_id, value) -> bool:
        old_rows = [
            r["v"]
            for r in self.conn.execute(
                f"SELECT c.value AS v FROM {link_table} l "
                f"JOIN {value_table} c ON c.id = l.value WHERE l.book = ?",
                (book_id,),
            ).fetchall()
        ]
        datatype = str(meta["datatype"]).lower()
        is_multiple = bool(meta["is_multiple"])
        if value is None or value == "":
            if not old_rows:
                return False
            self._clear_pattern_a(link_table, value_table, book_id)
            return True
        if datatype == "enumeration":
            allowed = (meta["display"] or {}).get("enum_values") or []
            sval = str(value).strip()
            if allowed and sval not in allowed:
                raise ValueError(
                    f"Value {sval!r} is not in #{meta['label']}'s enumeration: "
                    f"{', '.join(map(str, allowed))}"
                )
            new_vals = [sval]
        elif is_multiple or isinstance(value, (list, tuple)):
            items = (
                [str(x).strip() for x in value]
                if isinstance(value, (list, tuple))
                else [x.strip() for x in str(value).split(",")]
            )
            new_vals = [x for x in items if x]
        else:
            new_vals = [str(value)]
        if new_vals == old_rows:
            return False
        self._clear_pattern_a(link_table, value_table, book_id)
        for val in new_vals:
            vrow = self.conn.execute(
                f"SELECT id FROM {value_table} WHERE value = ?", (val,)
            ).fetchone()
            vid = (
                vrow["id"]
                if vrow is not None
                else self.conn.execute(
                    f"INSERT INTO {value_table} (value) VALUES (?)", (val,)
                ).lastrowid
            )
            self.conn.execute(
                f"INSERT INTO {link_table} (book, value) VALUES (?, ?)",
                (book_id, vid),
            )
        return True

    def _write_pattern_b(self, meta, value_table, book_id, value) -> bool:
        datatype = str(meta["datatype"]).lower()
        stored: Any
        if value is None or value == "":
            stored = None
        elif datatype == "bool":
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "yes", "checked", "_true", "_yes"):
                    stored = 1
                elif low in (
                    "false",
                    "no",
                    "unchecked",
                    "blank",
                    "empty",
                    "_false",
                    "_no",
                ):
                    stored = 0
                else:
                    raise ValueError(f"{value!r} is not a boolean for #{meta['label']}")
            else:
                stored = 1 if value else 0
        elif datatype == "int":
            stored = int(value)
        elif datatype == "float":
            stored = float(value)
        else:  # datetime, comments: stored as given (strings)
            stored = str(value)
        old = self.conn.execute(
            f"SELECT value FROM {value_table} WHERE book = ?", (book_id,)
        ).fetchone()
        if stored is None:
            if old is None or old["value"] is None:
                return False
            self.conn.execute(f"DELETE FROM {value_table} WHERE book = ?", (book_id,))
            return True
        if old is not None and old["value"] == stored:
            return False
        if old is None:
            self.conn.execute(
                f"INSERT INTO {value_table} (book, value) VALUES (?, ?)",
                (book_id, stored),
            )
        else:
            self.conn.execute(
                f"UPDATE {value_table} SET value = ? WHERE book = ?",
                (stored, book_id),
            )
        return True

    # -- Format management --

    def add_format(self, book_id: int, fmt: str, name: str, size: int) -> bool:
        """Register a format row in ``data``. Returns True when inserted.

        ``name`` is the filename stem (Calibre's ``data.name``) and ``size``
        the uncompressed byte size; the file itself is the caller's
        responsibility - cquarry never touches files. Raises ValueError if
        the book already carries that format.
        """
        fmt = fmt.strip().upper()
        name = name.strip()
        if not fmt or not name:
            raise ValueError("Format and name must not be empty")
        self._begin()
        try:
            self._require_book(book_id)
            exists = self.conn.execute(
                "SELECT 1 FROM data WHERE book = ? AND upper(format) = ?",
                (book_id, fmt),
            ).fetchone()
            if exists is not None:
                raise ValueError(f"Book {book_id} already has a {fmt} format")
            self.conn.execute(
                "INSERT INTO data (book, format, uncompressed_size, name) "
                "VALUES (?, ?, ?, ?)",
                (book_id, fmt, int(size), name),
            )
            self._touch_book(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def remove_format(self, book_id: int, fmt: str) -> bool:
        """Drop a format row from ``data``. Returns True when removed."""
        self._begin()
        try:
            self._require_book(book_id)
            before = self.conn.total_changes
            self.conn.execute(
                "DELETE FROM data WHERE book = ? AND upper(format) = upper(?)",
                (book_id, fmt.strip()),
            )
            changed = self.conn.total_changes > before
            if changed:
                self._touch_book(book_id)
            self.conn.commit()
            return changed
        except Exception:
            self.conn.rollback()
            raise

    def set_has_cover(self, book_id: int, has_cover: bool) -> bool:
        """Toggle the catalogued ``has_cover`` flag (the cover FILE itself is
        the caller's responsibility). Returns True when the flag flipped."""
        self._begin()
        try:
            self._require_book(book_id)
            current = self.conn.execute(
                "SELECT has_cover FROM books WHERE id = ?", (book_id,)
            ).fetchone()["has_cover"]
            new = 1 if has_cover else 0
            if int(current or 0) == new:
                self.conn.rollback()
                return False
            self.conn.execute(
                "UPDATE books SET has_cover = ?, last_modified = ? WHERE id = ?",
                (new, self._now(), book_id),
            )
            self._mark_dirty(book_id)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    # -- Book lifecycle --

    def remove_book(self, book_id: int) -> None:
        """Remove a book and all of its satellite rows.

        ``books_delete_trg`` cascades the standard link tables, data,
        annotations, comments, conversion options and plugin data when the
        books row goes. What the trigger does NOT cover is cleaned here:
        custom-column rows (both storage patterns, every column), the dirtied
        queues, and now-orphaned entity rows (pruned AFTER the cascade so the
        fkc_delete_on_* guards pass). Irreversible - callers own confirmation.
        """
        self._begin()
        try:
            self._require_book(book_id)
            # Custom columns: both patterns, for every defined column.
            col_ids = [
                r[0]
                for r in self.conn.execute("SELECT id FROM custom_columns").fetchall()
            ]
            for cid in col_ids:
                link_table = f"books_custom_column_{cid}_link"
                value_table = f"custom_column_{cid}"
                if self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (link_table,),
                ).fetchone():
                    self.conn.execute(
                        f"DELETE FROM {link_table} WHERE book = ?", (book_id,)
                    )
                # Value tables only carry a `book` column on the direct
                # (non-normalized) storage pattern; normalized ones are
                # (id, value, link) and are cleaned via their links above,
                # leaving shared values intact for other books.
                cols = {
                    r[1] for r in self.conn.execute(f"PRAGMA table_info({value_table})")
                }
                if "book" in cols:
                    self.conn.execute(
                        f"DELETE FROM {value_table} WHERE book = ?", (book_id,)
                    )
            # Dirtied queues must not outlive their book.
            for queue in ("metadata_dirtied", "annotations_dirtied"):
                with contextlib.suppress(sqlite3.OperationalError):
                    # schema predating the queue skips the delete
                    self.conn.execute(f"DELETE FROM {queue} WHERE book = ?", (book_id,))
            # The cascade trigger does the rest.
            self.conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            # Orphan pruning AFTER the cascade: links are gone, so the
            # fkc_delete_on_* guards are satisfied.
            for table in self._ENTITY_TABLES:
                self._prune_orphans(table)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
