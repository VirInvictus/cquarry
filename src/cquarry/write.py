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
  - Every mutation bumps ``books.last_modified`` so Calibre regenerates the
    book's sidecar .opf on its next run.
  - Mutations run inside explicit ``BEGIN IMMEDIATE`` transactions.
  - Tag deletion cleans ``books_tags_link`` before ``tags`` to satisfy the
    ``fkc_delete_on_tags`` trigger ordering.

Example::

    from cquarry.write import WritableCalibreDB

    with WritableCalibreDB("~/Calibre Library/metadata.db") as wdb:
        wdb.add_tag(42, "Audited")
        wdb.set_identifier(42, "isbn", "9780123456789")
"""

import os
import sqlite3
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any, Self


def title_sort(title: str) -> str:
    """Calibre's title sort key: leading articles move to the end."""
    if not title:
        return ""
    stripped = title.strip()
    lowered = stripped.lower()
    for art in ("the ", "a ", "an "):
        if lowered.startswith(art):
            rest = stripped[len(art) :].strip()
            return f"{rest}, {stripped[: len(art) - 1]}"
    return stripped


def uuid4(_arg: Any = None) -> str:
    """SQL-callable UUID generator matching Calibre's ``uuid4()`` UDF."""
    return str(_uuid.uuid4())


def _pynocase(lhs: str, rhs: str) -> int:
    """Case-insensitive comparison collation used by Calibre."""
    l, r = lhs.lower(), rhs.lower()
    if l < r:
        return -1
    if l > r:
        return 1
    return 0


def register_udfs(conn: sqlite3.Connection) -> None:
    """Register the SQL functions/collations Calibre triggers depend on.

    Call this on any read-write connection before touching ``books``; the
    schema triggers invoke ``title_sort()`` and ``uuid4()`` on insert/update.
    """
    conn.create_function("title_sort", 1, title_sort)
    conn.create_function("uuid4", 0, lambda: uuid4(), deterministic=True)
    conn.create_collation("PYNOCASE", _pynocase)


class WritableCalibreDB:
    """Explicitly opt-in read/write handle for metadata.db.

    This is intentionally a *different class* from cquarry.db.CalibreDB so no
    read-only code path can accidentally mutate a library.
    """

    def __init__(self, db_path: str):
        db_path = os.path.abspath(os.path.expanduser(db_path))
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        register_udfs(self.conn)

    # -- lifecycle --

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
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

    def _touch_book(self, book_id: int) -> None:
        self.conn.execute(
            "UPDATE books SET last_modified = ? WHERE id = ?", (self._now(), book_id)
        )

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
