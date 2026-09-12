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
import filecmp
import json
import os
import re
import shutil
import sqlite3
import uuid as _uuid
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime
from typing import Any, Self

from cquarry.helpers import sniff_image_format, title_sort

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


# Calibre's undefined-date sentinel: what the GUI writes when a pubdate is
# cleared, and what the search engine's date matcher treats as absent.
_UNDEFINED_PUBDATE = "0101-01-01 00:00:00+00:00"


# Calibre's custom-column datatypes by storage pattern (upstream
# create_custom_column: normalized is everything except datetime, comments,
# int, bool, float, composite). The writers dispatch on these; anything
# else raises instead of being silently stringified into the column.
_PATTERN_A_DATATYPES = frozenset({"text", "enumeration", "series", "rating"})
_PATTERN_B_DATATYPES = frozenset({"int", "float", "bool", "datetime", "comments"})


def _normalize_pubdate(value: str | date | datetime | None) -> str:
    """Normalize a pubdate input to the TEXT form Calibre stores.

    The column is TEXT (``datetime.isoformat(' ')`` in UTC) — writing a raw
    unix integer surfaces downstream as 'sentinel pubdate' / 'unparseable
    pubdate' linter errors. Naive datetimes are taken as UTC.
    """
    if value is None:
        return _UNDEFINED_PUBDATE
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"Unparseable pubdate: {value!r}") from None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(" ")
    return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat(" ")


def _same_instant(current: str | None, new: str) -> bool:
    """True when the stored TEXT parses to the same instant as ``new``.

    Unparseable legacy values (and naive-vs-aware mismatches) never match,
    so they always rewrite.
    """
    if not current:
        return False
    try:
        return datetime.fromisoformat(current) == datetime.fromisoformat(new)
    except ValueError, TypeError:
        return False


# ---------------------------------------------------------------------------
# Calibre's book-path layout, reproduced stdlib-only for ``add_book``
# (backend.py ``construct_path_name`` / ``construct_file_name``). The ONE
# documented deviation (roadmap Phase 10): upstream runs an ICU user-codec
# before the ASCII fold; here the fold is a plain ``encode("ascii",
# "replace")`` — visible only if Calibre later re-derives the path on a
# rename. ``PATH_LIMIT`` pins the POSIX budget (backend.py: PATH_LIMIT = 40
# if iswindows else 100); the ecosystem targets POSIX.
# ---------------------------------------------------------------------------

_PATH_LIMIT = 100
_BOOK_ID_PATH_TEMPLATE = " ({})"  # backend.py BOOK_ID_PATH_TEMPLATE
_WINDOWS_RESERVED_NAMES = frozenset(
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    ]
)
# backend.py: upstream's union of Windows/macOS/Linux-illegal characters
# plus control characters (calibre/__init__.py _filename_sanitize_unicode).
_FILENAME_ILLEGAL = frozenset('\\|?*<>":+/') | {chr(i) for i in range(32)}


def _ascii_filename(text: str, substitute: str = "_") -> str:
    """Sanitize one path component the way upstream's ``ascii_filename`` +
    ``sanitize_file_name`` chain does, with the plain ASCII fold."""
    one = text.encode("ascii", "replace").decode("ascii").replace("?", substitute)
    one = "".join(substitute if ch in _FILENAME_ILLEGAL else ch for ch in one)
    one = re.sub(r"\s", " ", one).strip()
    bname, ext = os.path.splitext(one)
    one = re.sub(r"^\.+$", "_", bname)
    one = one.replace("..", substitute)
    one += ext
    # Windows dislikes components ending in a period or space, and Unix
    # hides leading periods; upstream applies both guards unconditionally.
    if one and one[-1] in (".", " "):
        one = one[:-1] + substitute
    if one.startswith("."):
        one = substitute + one[1:]
    return one


def _construct_path_name(book_id: int, title: str, author: str) -> str:
    """The relative ``Author/Title (id)`` directory (backend.py parity)."""
    id_part = _BOOK_ID_PATH_TEMPLATE.format(book_id)
    limit = _PATH_LIMIT - (len(id_part) // 2) - 2
    author = _ascii_filename(author)[:limit]
    title = _ascii_filename(title.lstrip())[:limit].rstrip()
    if not title:
        title = "Unknown"[:limit]
    while author and author[-1] in (" ", "."):
        author = author[:-1]
    if not author:
        author = _ascii_filename("Unknown")
    if author.upper() in _WINDOWS_RESERVED_NAMES:
        author += "w"
    return f"{author}/{title}{id_part}"


def _construct_file_name(title: str, author: str, extlen: int) -> str:
    """The ``Title - Author`` format-file stem (backend.py parity).

    ``extlen`` counts the dot plus the extension, floored like upstream so
    ``ORIGINAL_EPUB``-sized names always fit.
    """
    extlen = max(extlen, 14)  # 14 accounts for ORIGINAL_EPUB
    limit = (_PATH_LIMIT - extlen - 2) // 2
    author = _ascii_filename(author)[:limit]
    title = _ascii_filename(title.lstrip())[:limit].rstrip()
    if not title:
        title = "Unknown"[:limit]
    name = title + " - " + author
    while name.endswith("."):
        name = name[:-1]
    if not name:
        name = _ascii_filename("Unknown")
    return name


def _place_stream(src: str, dest: str) -> int:
    """Copy a file into place atomically; returns the bytes on disk.

    The bindery ``install_format`` precedent: temp name, fsync the file,
    ``os.replace``, then fsync the directory so the rename survives a crash.
    """
    tmp = dest + ".cquarry-tmp"
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(tmp, dest)
    dfd = os.open(os.path.dirname(dest) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return os.path.getsize(dest)


def _place_bytes(data: bytes, dest: str) -> None:
    """Write bytes into place atomically (the cover path)."""
    tmp = dest + ".cquarry-tmp"
    with open(tmp, "wb") as fout:
        fout.write(data)
        fout.flush()
        os.fsync(fout.fileno())
    os.replace(tmp, dest)
    dfd = os.open(os.path.dirname(dest) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class WritableCalibreDB:
    """Explicitly opt-in read/write handle for metadata.db.

    This is intentionally a *different class* from cquarry.db.CalibreDB so no
    read-only code path can accidentally mutate a library.

    Transaction control is pinned to sqlite3's legacy mode so every mutating
    method can open ``BEGIN IMMEDIATE`` itself: take the write lock up front
    (``busy_timeout`` applies to acquisition), commit on success, roll back
    on any error. Nothing is written unless a method returns normally; the
    rollback paths catch ``BaseException`` so a ``KeyboardInterrupt`` mid-set
    unwinds instead of committing a torn edit, and the context manager's exit
    rolls back whenever an exception is in flight. A ``batch()`` block moves
    the commit boundary to the end of the block so a multi-book, multi-field
    pass commits exactly once; any failure inside rolls the whole pass back.
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
        self._batch_depth = 0
        # Directories created by add_book inside the current outermost
        # batch(); a failed outermost exit removes them all so earlier
        # books in the pass cannot strand orphan directories behind.
        self._batch_dirs: list[str] = []
        # Set when any batch level exits with an exception and cleared at
        # the outermost exit: an inner failure caught by the caller must
        # still roll the whole pass back, never commit it.
        self._batch_poisoned = False
        # File removals registered by remove_book(delete_files=...) inside
        # the current outermost batch, performed only after its COMMIT (a
        # rollback drops them instead: resurrected rows keep their files).
        self._pending_removals: list[tuple[int, str, str]] = []
        # Path re-lays registered by update_title/set_authors inside the
        # current outermost batch: SQL applies immediately, the filesystem
        # half (dir rename + format-file renames) runs only after the
        # COMMIT, so a rollback leaves the old layout for the old rows.
        self._pending_relayouts: list[
            tuple[int, str, str, list[tuple[str, str]], str]
        ] = []
        # full-text-search.db attach state: None = not tried yet, True =
        # attached as fts_db, False = absent or unusable for this
        # connection. Tried lazily before the first format write (ATTACH
        # is illegal inside a transaction, so never after _begin()).
        self._fts_state: bool | None = None
        # File placements/removals deferred by set_cover and the
        # original-format verbs: callables appended inside the transaction,
        # run only after the outermost COMMIT (a rollback drops them).
        self._pending_fs_ops: list[Callable[[], None]] = []

    # -- lifecycle --

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        try:
            if exc[0] is not None:
                # An exception is unwinding (KeyboardInterrupt and friends
                # included): nothing still uncommitted may survive. Committing
                # through the unwind is what turned a mid-write Ctrl-C into a
                # torn edit (a link row without its metadata_dirtied row).
                with contextlib.suppress(sqlite3.Error):
                    self._rollback()
            else:
                # A commit failure propagates, exactly as batch()'s exit does;
                # suppressing it used to discard the transaction silently.
                self._commit()
        finally:
            self.close()

    def _begin(self) -> None:
        """BEGIN IMMEDIATE unless inside a batch() — the batch opened it."""
        if self._batch_depth:
            return
        self.conn.execute("BEGIN IMMEDIATE")

    @contextlib.contextmanager
    def batch(self) -> Generator[Self]:
        """Defer commits across a multi-book, multi-field pass.

        The outermost ``batch()`` takes the write lock immediately (BEGIN
        IMMEDIATE) and commits exactly once on clean exit; any exception
        inside rolls the whole pass back. Nesting is allowed: inner batches
        join the outer transaction. Individual setters keep their signatures
        and return values — only their commit boundary moves.

        An inner batch's exception poisons the pass for its lifetime: even
        if the caller catches it, the outermost exit rolls back instead of
        committing, because the failed inner segment may have left partial
        writes pending in the shared transaction.

        Filesystem compensation is batch-scoped too: directories created by
        ``add_book`` inside the pass are tracked on the instance, and a
        failed outermost exit removes every one of them (the SQL rollback
        alone would strand the earlier books' directories as orphans that
        look like real books no row points at). A committed batch clears the
        registry without touching the directories; adds made OUTSIDE any
        batch never register, so a later failed batch cannot touch them.
        """
        self._batch_depth += 1
        ok = False
        try:
            if self._batch_depth == 1:
                # Raw call on purpose: the batch is the transaction owner.
                self.conn.execute("BEGIN IMMEDIATE")
            yield self
            ok = True
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                if ok and not self._batch_poisoned:
                    self.conn.commit()
                    self._flush_pending_removals()
                    self._flush_pending_relayouts()
                    self._flush_pending_fs_ops()
                else:
                    with contextlib.suppress(sqlite3.Error):
                        self.conn.rollback()
                    # The SQL is undone; directories created inside the
                    # batch would survive as orphans that look like real
                    # books no row points at. add_book's own failure path
                    # already removed its directory; rmtree of a missing
                    # path is a no-op (ignore_errors).
                    for orphan in self._batch_dirs:
                        shutil.rmtree(orphan, ignore_errors=True)
                    # The rollback resurrected whatever the queued removals
                    # and re-lays were aimed at: both queues are dropped
                    # with the transaction, never flushed by a LATER batch
                    # against rows the rollback brought back.
                    self._pending_removals.clear()
                    self._pending_relayouts.clear()
                    self._pending_fs_ops.clear()
                self._batch_dirs.clear()
                self._batch_poisoned = False
            elif not ok:
                # An inner batch's exception must stick even when the caller
                # catches it: the inner segment's partial writes are still
                # pending in the shared transaction, so letting the outer
                # block commit would land a half-finished pass.
                self._batch_poisoned = True

    def transaction(self) -> contextlib.AbstractContextManager[Self]:
        """Pre-1.7.0 name for :meth:`batch`, kept as an exact alias.

        A phase-3 import on 2026-08-29 called ``with wdb.transaction():`` and
        hit an AttributeError: 1.7.0 shipped the deferred-commit context as
        ``batch()`` with no ``transaction()``. The alias keeps the old call
        shape working; new code should use ``batch()``.
        """
        return self.batch()

    def _commit(self) -> None:
        """Commit unless inside a batch() — the batch owns that commit."""
        if self._batch_depth:
            return
        self.conn.commit()

    def _rollback(self) -> None:
        """Roll back unless inside a batch() — the batch owns that rollback."""
        if self._batch_depth:
            return
        self.conn.rollback()

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
        """Rename a book, refreshing its sort key and last_modified stamp.

        Re-lays the on-disk layout to match: the ``Author/Title (id)``
        directory and every format file move to the new stems, and
        ``books.path``/``data.name`` follow (the fs half defers to the
        outermost commit inside a :meth:`batch`).
        """
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
            first_author = self._first_author_name(book_id)
            self._relayout_book_path(book_id, new_title, first_author)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    def _first_author_name(self, book_id: int) -> str:
        """The book's first author (link order), '' when authorless -- the
        same input ``add_book`` uses for the path components. Schemas
        predating the link tables degrade to '' (the path's Unknown rule)."""
        try:
            row = self.conn.execute(
                "SELECT a.name FROM books_authors_link bal JOIN authors a "
                "ON a.id = bal.author WHERE bal.book = ? ORDER BY bal.id LIMIT 1",
                (book_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return ""  # schema predates the author link tables
        return row["name"] if row is not None else ""

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
            self._commit()
            return changed
        except BaseException:
            self._rollback()
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
                self._rollback()
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
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

    def clear_tags(self, book_id: int) -> int:
        """Detach every tag from a book. Returns how many links were removed.

        The caller no longer needs to know the tags first (per-name
        ``remove_tag`` does). Cleans the link table first, then prunes
        now-orphaned tag rows; the order the fkc_delete_on_tags trigger
        requires. An already-untagged book is an honest no-op: zero
        returned, no ``last_modified`` bump, nothing queued.
        """
        cur = self.conn.cursor()
        self._begin()
        try:
            self._require_book(book_id)
            before = self.conn.total_changes
            cur.execute("DELETE FROM books_tags_link WHERE book = ?", (book_id,))
            removed = self.conn.total_changes - before
            if removed:
                self._prune_orphans("tags")
                self._touch_book(book_id)
            self._commit()
            return removed
        except BaseException:
            self._rollback()
            raise

    def set_identifier(self, book_id: int, id_type: str, val: str | None) -> bool:
        """Upsert one entry in the EAV ``identifiers`` table.

        ``val=None`` (or blank) deletes the pair. The table is UNIQUE(book,
        type), so an existing value of the same type is replaced. Returns
        True when stored state changed. Both halves are cleaned exactly
        like upstream's ``clean_identifier`` (db/write.py:118-121, 1.18):
        the type is stripped, lowercased, and stripped of ``:``/``,``; the
        value is stripped with ``,`` mapped to ``|`` -- Calibre's stored
        shape, since a comma in a value collides with the keypair query
        grammar downstream.
        """
        id_type = (id_type or "").strip().lower().replace(":", "").replace(",", "")
        if not id_type:
            raise ValueError("Identifier type must not be empty")
        clean_val = val.strip().replace(",", "|") if isinstance(val, str) else val
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
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

    def set_identifiers(self, book_id: int, pairs: dict[str, str | None]) -> int:
        """Batch-upsert identifiers. Returns how many entries changed."""
        changed = 0
        for id_type, val in pairs.items():
            if self.set_identifier(book_id, id_type, val):
                changed += 1
        return changed

    def clear_identifier(self, book_id: int, id_type: str) -> bool:
        """Delete one entry from the EAV ``identifiers`` table.

        The type is normalized exactly like :meth:`set_identifier`
        (stripped, lowercased, ``:``/`,` stripped; empty raises). Returns
        True when a row was deleted; a pair that is already absent is an
        honest no-op (False). Deletion queues the book for OPF
        regeneration via ``_touch_book()``.
        """
        id_type = (id_type or "").strip().lower().replace(":", "").replace(",", "")
        if not id_type:
            raise ValueError("Identifier type must not be empty")
        cur = self.conn.cursor()
        self._begin()
        try:
            self._require_book(book_id)
            row = cur.execute(
                "SELECT id FROM identifiers WHERE book = ? AND type = ?",
                (book_id, id_type),
            ).fetchone()
            changed = False
            if row is not None:
                cur.execute("DELETE FROM identifiers WHERE id = ?", (row["id"],))
                self._touch_book(book_id)
                changed = True
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

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
                self._rollback()
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
            # The path's author component follows the new first author.
            title_row = self.conn.execute(
                "SELECT title FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            self._relayout_book_path(
                book_id,
                title_row["title"] or "",
                self._first_author_name(book_id),
            )
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    # -- Entity-wide renames and removals (1.19; the approved C.2) --

    # The many-one/many-many entity kinds a rename/removal can address.
    # Ratings have no name to rename; languages are out of scope (no
    # consumer names them).
    _ENTITY_NAME_TABLES = {
        "authors": ("authors", "name", "author"),
        "series": ("series", "name", "series"),
        "publishers": ("publishers", "name", "publisher"),
        "tags": ("tags", "name", "tag"),
    }

    def rename_entity(self, kind: str, old: str, new: str) -> int:
        """Rename an author, series, publisher, or tag everywhere.

        The fix-the-misspelled-name verb (upstream ``rename_items``,
        cache.py:2758-2862). ``old`` resolves case-insensitively; ``new``
        is the row's new spelling. When ``new`` already exists as another
        row (a case-variant or duplicate spelling) the rows MERGE: the old
        row's links move to the survivor, duplicate links are dropped, and
        the old row is deleted. Every affected book is touched and queued
        for OPF resync. For authors, ``books.author_sort`` is recomputed
        from the surviving authors' sort keys and each affected book's
        on-disk layout re-lays (the directory carries the author name; the
        fs half lands after the commit, riding the batch machinery). For a
        series MERGE, incoming books get the next free series index
        (``max + 1`` over the survivor's books); an in-place series rename
        keeps every index. Returns the number of affected books; renaming
        to the row's current spelling is an honest 0.

        Raises ValueError for an unknown kind, an empty name, or when no
        row matches ``old``.
        """
        table, name_col, fk = self._entity_name_table(kind)
        old = old.strip() if isinstance(old, str) else ""
        new = new.strip() if isinstance(new, str) else ""
        if not old or not new:
            raise ValueError("Entity names must not be empty")
        affected: list[int] = []
        with self.batch():
            # Exact spelling wins; the NOCASE fallback (lowest id) only
            # fires when no exact match exists, so a library with several
            # case variants resolves deterministically.
            row = self.conn.execute(
                f"SELECT id, {name_col} AS name FROM {table} WHERE {name_col} = ?",
                (old,),
            ).fetchone()
            if row is None:
                row = self.conn.execute(
                    f"SELECT id, {name_col} AS name FROM {table} "
                    f"WHERE {name_col} = ? COLLATE NOCASE ORDER BY id LIMIT 1",
                    (old,),
                ).fetchone()
            if row is None:
                raise ValueError(f"No {kind} row matching {old!r}")
            if row["name"] == new:
                return 0  # identical spelling: honest no-op
            other = self.conn.execute(
                f"SELECT id FROM {table} WHERE {name_col} = ? COLLATE NOCASE "
                f"AND id != ?",
                (new, row["id"]),
            ).fetchone()
            affected = [
                r["book"]
                for r in self.conn.execute(
                    f"SELECT book FROM books_{table}_link WHERE {fk} = ? ORDER BY book",
                    (row["id"],),
                )
            ]
            merge = other is not None
            if merge:
                survivor = other["id"]
                # Drop links that would collide with the survivor's
                # UNIQUE(book, fk), then move the rest.
                self.conn.execute(
                    f"DELETE FROM books_{table}_link WHERE {fk} = ? AND book IN "
                    f"(SELECT book FROM books_{table}_link WHERE {fk} = ?)",
                    (row["id"], survivor),
                )
                self.conn.execute(
                    f"UPDATE OR IGNORE books_{table}_link SET {fk} = ? WHERE {fk} = ?",
                    (survivor, row["id"]),
                )
                self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                if kind == "series":
                    for book_id in affected:
                        # Next free index over the survivor's OTHER books
                        # (the moving book's stale index must not count).
                        nxt = self.conn.execute(
                            "SELECT COALESCE(MAX(series_index), 0) + 1 AS nxt "
                            "FROM books_series_link l JOIN books b ON b.id = l.book "
                            "WHERE l.series = ? AND b.id != ?",
                            (survivor, book_id),
                        ).fetchone()["nxt"]
                        self.conn.execute(
                            "UPDATE books SET series_index = ? WHERE id = ?",
                            (nxt, book_id),
                        )
            else:
                self.conn.execute(
                    f"UPDATE {table} SET {name_col} = ? WHERE id = ?",
                    (new, row["id"]),
                )
            for book_id in affected:
                if kind == "authors":
                    # Recompute the book's author_sort from the surviving
                    # authors' sort keys, exactly like set_authors does.
                    sorts = [
                        r["s"] or r["name"]
                        for r in self.conn.execute(
                            "SELECT a.sort AS s, a.name AS name "
                            "FROM books_authors_link bal JOIN authors a "
                            "ON a.id = bal.author WHERE bal.book = ? "
                            "ORDER BY bal.id",
                            (book_id,),
                        )
                    ]
                    self.conn.execute(
                        "UPDATE books SET author_sort = ? WHERE id = ?",
                        (" & ".join(sorts), book_id),
                    )
                self._touch_book(book_id)
            if kind == "authors":
                for book_id in affected:
                    title = self.conn.execute(
                        "SELECT title FROM books WHERE id = ?", (book_id,)
                    ).fetchone()
                    self._relayout_book_path(
                        book_id,
                        title["title"] or "",
                        self._first_author_name(book_id),
                    )
        return len(affected)

    def remove_entity_everywhere(self, kind: str, name: str) -> int:
        """Remove an author, series, publisher, or tag from every book.

        Resolves ``name`` case-insensitively, deletes the entity's links
        (the fkc_delete_on_* order), then the row itself, and touches +
        queues OPF resync for every affected book. For a series the
        affected books' ``series_index`` is nulled too, matching
        :meth:`set_series`'s clear semantics; for authors the books'
        ``author_sort`` is recomputed from the remaining authors and the
        on-disk layout re-lays (fs after the commit). Returns the number
        of affected books; a name matching no row is an honest 0.
        """
        table, name_col, fk = self._entity_name_table(kind)
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise ValueError("Entity name must not be empty")
        affected: list[int] = []
        with self.batch():
            row = self.conn.execute(
                f"SELECT id FROM {table} WHERE {name_col} = ?", (name,)
            ).fetchone()
            if row is None:
                row = self.conn.execute(
                    f"SELECT id FROM {table} WHERE {name_col} = ? COLLATE NOCASE "
                    f"ORDER BY id LIMIT 1",
                    (name,),
                ).fetchone()
            if row is None:
                return 0
            affected = [
                r["book"]
                for r in self.conn.execute(
                    f"SELECT book FROM books_{table}_link WHERE {fk} = ? ORDER BY book",
                    (row["id"],),
                )
            ]
            self.conn.execute(
                f"DELETE FROM books_{table}_link WHERE {fk} = ?", (row["id"],)
            )
            if kind == "series" and affected:
                self.conn.execute(
                    f"UPDATE books SET series_index = NULL WHERE id IN "
                    f"({','.join('?' * len(affected))})",
                    affected,
                )
            self.conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
            for book_id in affected:
                if kind == "authors":
                    sorts = [
                        r["s"] or r["name"]
                        for r in self.conn.execute(
                            "SELECT a.sort AS s, a.name AS name "
                            "FROM books_authors_link bal JOIN authors a "
                            "ON a.id = bal.author WHERE bal.book = ? "
                            "ORDER BY bal.id",
                            (book_id,),
                        )
                    ]
                    self.conn.execute(
                        "UPDATE books SET author_sort = ? WHERE id = ?",
                        (" & ".join(sorts), book_id),
                    )
                self._touch_book(book_id)
            if kind == "authors":
                for book_id in affected:
                    title = self.conn.execute(
                        "SELECT title FROM books WHERE id = ?", (book_id,)
                    ).fetchone()
                    self._relayout_book_path(
                        book_id,
                        title["title"] or "",
                        self._first_author_name(book_id),
                    )
        return len(affected)

    def _entity_name_table(self, kind: str) -> tuple[str, str, str]:
        kind = (kind or "").strip().lower()
        if kind not in self._ENTITY_NAME_TABLES:
            raise ValueError(
                f"Unknown entity kind {kind!r}. Available: "
                + ", ".join(sorted(self._ENTITY_NAME_TABLES))
            )
        return self._ENTITY_NAME_TABLES[kind]

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
                    self._rollback()
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
                self._commit()
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
                self._rollback()
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
            self._commit()
            return True
        except BaseException:
            self._rollback()
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
                    self._rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_publishers_link WHERE book = ?", (book_id,)
                )
            else:
                pid = self._resolve_or_create("publishers", "name", name.strip())
                if old is not None and old["publisher"] == pid:
                    self._rollback()
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
            self._commit()
            return True
        except BaseException:
            self._rollback()
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
                    self._rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_ratings_link WHERE book = ?", (book_id,)
                )
            else:
                internal = round(float(stars) * 2)
                if not 0 <= internal <= 10:
                    raise ValueError(f"Rating must be within 0-5 stars, got {stars}")
                if internal == 0:
                    # Calibre maps 0 to unrated: no row (upstream purges
                    # 0-rating rows), so 0 stars clears like None.
                    if old is None:
                        self._rollback()
                        return False
                    self.conn.execute(
                        "DELETE FROM books_ratings_link WHERE book = ?", (book_id,)
                    )
                else:
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
                        self._rollback()
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
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    def clear_rating(self, book_id: int) -> bool:
        """Clear the book's rating. Self-documenting alias of
        ``set_rating(book_id, None)`` so an audit trail names the operation.

        Deletes the link and prunes the orphaned rating row; returns True
        when stored state changed, False when the book was already unrated.
        """
        return self.set_rating(book_id, None)

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
                    self._rollback()
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
                    self._rollback()
                    return False
                self.conn.execute(
                    "DELETE FROM books_languages_link WHERE book=?", (book_id,)
                )
                # item_order is written when the schema carries it: Calibre
                # orders a book's languages by it, and leaving every row at
                # 0 handed ordering to the link-id tiebreaker instead.
                has_item_order = "item_order" in {
                    r[1]
                    for r in self.conn.execute(
                        "PRAGMA table_info(books_languages_link)"
                    )
                }
                for pos, lid in enumerate(new_ids):
                    if has_item_order:
                        self.conn.execute(
                            "INSERT INTO books_languages_link "
                            "(book, lang_code, item_order) VALUES (?, ?, ?)",
                            (book_id, lid, pos),
                        )
                    else:
                        self.conn.execute(
                            "INSERT INTO books_languages_link (book, lang_code) "
                            "VALUES (?, ?)",
                            (book_id, lid),
                        )
            self._prune_orphans("languages")
            self._touch_book(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
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
                    self._rollback()
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
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

    def set_pubdate(self, book_id: int, value: str | date | datetime | None) -> bool:
        """Set the publication date. Returns True if the stored value changed.

        Accepts ``'YYYY-MM-DD'``, a full ISO datetime string, :class:`date`,
        or :class:`datetime` (naive taken as UTC); ``None`` stores Calibre's
        undefined-date sentinel, which Calibre and the search engine both
        treat as "no pubdate".
        """
        new = _normalize_pubdate(value)
        self._begin()
        try:
            self._require_book(book_id)
            row = self.conn.execute(
                "SELECT pubdate FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if _same_instant(row["pubdate"] if row else None, new):
                self._commit()
                return False
            self.conn.execute(
                "UPDATE books SET pubdate = ? WHERE id = ?", (new, book_id)
            )
            self._touch_book(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    # -- Passthrough sort/timestamp setters (1.19; the approved C.6) --

    def set_author_sort(self, book_id: int, value: str) -> bool:
        """Override the book's ``author_sort`` string verbatim.

        The passthrough for hand-tuned corrections: unlike
        :meth:`set_authors`, which recomputes the sort from the authors'
        sort keys, this stores exactly what you pass. Returns True when
        stored state changed. A later ``set_authors``/author rename will
        recompute over the override -- that is their job, not a bug.
        """
        return self._set_book_text_column(book_id, "author_sort", value)

    def set_title_sort(self, book_id: int, value: str) -> bool:
        """Override the book's ``title_sort`` (``books.sort``) verbatim.

        The passthrough for mangled sort keys; unlike
        :meth:`update_title`, which recomputes the sort through
        ``title_sort()``, this stores exactly what you pass. Returns True
        when stored state changed. A later ``update_title`` recomputes
        over the override."""
        return self._set_book_text_column(book_id, "sort", value)

    def set_timestamp(self, book_id: int, value: str | date | datetime | None) -> bool:
        """Set the book's ``timestamp`` (its addition date).

        Normalized exactly like :meth:`set_pubdate` (ISO text in UTC,
        ``None`` writes the undefined-date sentinel, an equal instant is
        an honest no-op). Note the search grammar's bare ``timestamp``
        location reads this column; Calibre sorts "recently added" by it.
        """
        new = _normalize_pubdate(value)
        self._begin()
        try:
            self._require_book(book_id)
            row = self.conn.execute(
                "SELECT timestamp FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if _same_instant(row["timestamp"] if row else None, new):
                self._commit()
                return False
            self.conn.execute(
                "UPDATE books SET timestamp = ? WHERE id = ?", (new, book_id)
            )
            self._touch_book(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    def _set_book_text_column(self, book_id: int, column: str, value: str) -> bool:
        new = value.strip() if isinstance(value, str) else ""
        if not new:
            raise ValueError(f"{column} must not be empty")
        self._begin()
        try:
            self._require_book(book_id)
            row = self.conn.execute(
                f"SELECT {column} AS cur FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if row is not None and row["cur"] == new:
                self._rollback()
                return False
            self.conn.execute(
                f"UPDATE books SET {column} = ?, last_modified = ? WHERE id = ?",
                (new, self._now(), book_id),
            )
            self._mark_dirty(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
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

    def _validate_enum(self, meta: dict[str, Any], sval: str) -> None:
        """Enumeration membership check shared by the Pattern-A writers.

        An empty ``enum_values`` rejects every value: upstream's writer
        filters any non-member, so an empty allowed set silently drops the
        whole write; cquarry says so with a raise instead.
        """
        allowed = (meta["display"] or {}).get("enum_values") or []
        if sval in allowed:
            return
        if allowed:
            raise ValueError(
                f"Value {sval!r} is not in #{meta['label']}'s enumeration: "
                f"{', '.join(map(str, allowed))}"
            )
        raise ValueError(
            f"#{meta['label']} has an empty enumeration (display.enum_values "
            "is missing or []); no value is writable"
        )

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
        # Datatype dispatch: only the datatypes Calibre ships may be written,
        # each through its own storage pattern. Unknown datatypes (and known
        # datatypes on the wrong layout) raise rather than being stringified.
        known = _PATTERN_A_DATATYPES if has_link else _PATTERN_B_DATATYPES
        if datatype not in known:
            raise ValueError(
                f"#{label} is a {datatype!r} column on "
                f"{'link-table' if has_link else 'direct'} storage; refusing to "
                "write it (unsupported datatype or datatype/layout mismatch)"
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
            self._commit()
            return changed
        except BaseException:
            self._rollback()
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
                f"JOIN {value_table} c ON c.id = l.value WHERE l.book = ? "
                f"ORDER BY l.rowid",
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
            sval = str(value).strip()
            self._validate_enum(meta, sval)
            new_vals: list[Any] = [sval]
        elif datatype == "rating":
            # Custom ratings share builtin ratings' storage scale (0-10,
            # UNIQUE value rows) but the API takes stars, exactly like
            # set_rating, so one library never carries two conventions.
            # 0 stars means unrated: Calibre purges 0-rating rows.
            stars = float(value)
            if not 0 <= stars <= 5:
                raise ValueError(f"Rating must be within 0-5 stars, got {stars:g}")
            internal = round(stars * 2)
            if internal == 0:
                if not old_rows:
                    return False
                self._clear_pattern_a(link_table, value_table, book_id)
                return True
            new_vals = [internal]
        elif is_multiple or isinstance(value, (list, tuple)):
            items: list[str] = []
            if isinstance(value, (list, tuple)):
                for v in value:
                    if v is None:
                        # A None entry is skipped, never stringified into
                        # the literal 'None'.
                        continue
                    s = str(v).strip()
                    if s:
                        items.append(s)
            else:
                # A bare string is ONE value: comma-splitting it made
                # "Last, First" round-trip as two phantom values. Callers
                # with multiple values pass a list (both writers do).
                items = [str(value).strip()]
            new_vals = [x for x in items if x]
        else:  # text (single-valued) and series: one text value
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
        elif datatype == "datetime":
            # The set_pubdate convention: ISO text in UTC, never a naive
            # str() without the offset (Calibre reads these as timestamps).
            stored = _normalize_pubdate(value)
        elif datatype == "comments":
            stored = str(value)
        else:  # unreachable: set_custom_column's dispatch gate raised first
            raise ValueError(
                f"#{meta['label']}: {datatype!r} is not writable on direct storage"
            )
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

    def add_custom_column_values(
        self, book_id: int, label: str, values: list[str] | tuple[str, ...]
    ) -> int:
        """Append values to a multi-valued custom column. Returns how many
        links were added.

        ``set_custom_column`` is replace-only, so adding a second audience
        beside an existing one took a read-modify-replace dance. This
        appends: values already on the book are deduped away (the link
        table is ``UNIQUE(book, value)``), duplicates within ``values``
        collapse, only genuinely new ones insert, and the returned count is
        honest. A no-op append does not bump ``last_modified`` or queue
        anything.

        Only ``is_multiple`` Pattern-A (link-table) columns qualify; a
        single-valued column has one value to replace, so it raises with a
        pointer to ``set_custom_column``. A bare string is rejected rather
        than comma-split: for an append, ``"Rin, Brandon"`` naming one
        value or two is the caller's knowledge, not the API's guess.
        """
        meta = self._custom_column_meta(label)
        if not meta["editable"]:
            raise ValueError(f"Custom column #{label} is not editable")
        if str(meta["datatype"]).lower() == "composite":
            raise ValueError(
                f"#{label} is a composite column; Calibre computes it and it "
                "has no storage to write"
            )
        if not meta["is_multiple"]:
            raise ValueError(
                f"#{label} is not multi-valued; add_custom_column_values "
                "appends, and a single-valued column only has a value to "
                "replace (set_custom_column)"
            )
        datatype = str(meta["datatype"]).lower()
        if datatype not in _PATTERN_A_DATATYPES:
            raise ValueError(
                f"#{label}: {datatype!r} columns are not append targets; use "
                "set_custom_column"
            )
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise TypeError(
                "values must be a list of strings; a bare string is "
                "ambiguous for an append (one value or comma-joined?) and "
                "is rejected"
            )
        items: list[str] = []
        for v in values:
            if v is None:
                # A None entry is skipped, never stringified into 'None'.
                continue
            s = str(v).strip()
            if not s:
                continue
            if datatype == "enumeration":
                self._validate_enum(meta, s)
            if s not in items:
                items.append(s)
        cid = meta["id"]
        link_table = f"books_custom_column_{cid}_link"
        value_table = f"custom_column_{cid}"
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (link_table,),
        ).fetchone():
            raise ValueError(
                f"#{label} has no link table (direct storage); use set_custom_column"
            )
        self._begin()
        try:
            self._require_book(book_id)
            existing = {
                r["v"]
                for r in self.conn.execute(
                    f"SELECT c.value AS v FROM {link_table} l "
                    f"JOIN {value_table} c ON c.id = l.value WHERE l.book = ?",
                    (book_id,),
                ).fetchall()
            }
            added = 0
            for val in items:
                if val in existing:
                    continue
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
                existing.add(val)
                added += 1
            if added:
                self._touch_book(book_id)
            self._commit()
            return added
        except BaseException:
            self._rollback()
            raise

    # -- Format management --

    def _ensure_fts_attached(self) -> bool:
        """Attach the library's full-text-search.db for writing, once.

        Returns True when ``fts_db`` is attached and the dirty-queue writes
        can run. The attach must happen BEFORE a transaction opens (SQLite
        forbids ATTACH inside one), so format setters call this before
        ``_begin()``; a missing sidecar or a failed attach (locked,
        unreadable) marks this connection FTS-unavailable -- dirtying is
        best-effort and degrades to a no-op, never blocks a format write.
        """
        if self._fts_state is None:
            path = os.path.join(os.path.dirname(self.db_path), "full-text-search.db")
            if not os.path.exists(path):
                self._fts_state = False
            else:
                try:
                    self.conn.execute("ATTACH DATABASE ? AS fts_db", (path,))
                    self._fts_state = True
                except sqlite3.Error:
                    self._fts_state = False
        return self._fts_state

    def _mark_fts_dirty(self, book_id: int, fmt: str) -> None:
        """Queue (book, format) for FTS re-extraction and a pages rescan.

        Upstream's TEMP triggers (``fts_triggers.sql``) insert into the
        sidecar's ``dirtied_formats`` on every data INSERT/UPDATE, and
        format adds queue a pages scan (``cache.py:2472``). Without this, a
        repaired format file leaves Calibre's content index and page counts
        stale forever -- Calibre never re-reads a file on its own; it only
        processes its queues. The writes join the caller's transaction, so
        a batch rollback undoes them. ``needs_scan`` is skipped on schemas
        predating ``books_pages_link``.
        """
        if not self._ensure_fts_attached():
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO fts_db.dirtied_formats(book, format) VALUES (?, ?)",
            (book_id, fmt.upper()),
        )
        with contextlib.suppress(sqlite3.OperationalError):
            self.conn.execute(
                "UPDATE books_pages_link SET needs_scan = 1 WHERE book = ?",
                (book_id,),
            )

    def _clear_fts_dirty(self, book_id: int, fmt: str) -> None:
        """Drop the (book, format) FTS queue entry alongside a removal, so
        Calibre never re-extracts a vanished file.

        The stale ``books_text`` row stays: only Calibre itself can delete
        it, because the sidecar's delete triggers maintain the FTS5 index
        through Calibre's custom tokenizer, which does not exist here --
        deleting from ``books_text`` outside Calibre would corrupt the
        index, so this deliberately does not try (Calibre's re-extraction
        or next index maintenance cleans the row).
        """
        if not self._ensure_fts_attached():
            return
        self.conn.execute(
            "DELETE FROM fts_db.dirtied_formats WHERE book = ? AND format = ?",
            (book_id, fmt.upper()),
        )

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
        if size < 0:
            raise ValueError(f"Format size must not be negative, got {size}")
        # ATTACH must precede the transaction (see _ensure_fts_attached).
        self._ensure_fts_attached()
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
            self._mark_fts_dirty(book_id, fmt)
            self._touch_book(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    def remove_format(self, book_id: int, fmt: str) -> bool:
        """Drop a format row from ``data``. Returns True when removed.

        The FTS sidecar's queue entry for the pair is cleared too (when the
        sidecar exists), so Calibre never re-extracts a vanished file; the
        stale ``books_text`` row is Calibre's own to clean (see
        :meth:`_clear_fts_dirty`)."""
        self._ensure_fts_attached()
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
                self._clear_fts_dirty(book_id, fmt.strip().upper())
                self._touch_book(book_id)
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

    def set_format(self, book_id: int, fmt: str, name: str, size: int) -> bool:
        """Sanctioned replace of one format row, remove+add in one
        transaction. Returns True when a row was written, False when the
        identical row (same format, name, and size) already exists.

        This is the row half of swapping a repaired file into place: the
        file itself stays the caller's atomic-replace job (Calibre's layout
        never changes for a same-format swap), and this only keeps ``data``
        truthful. The replacement is queued for FTS re-extraction and a
        pages rescan when the sidecar exists, so Calibre's content index
        and page counts follow the repaired file. Raises ValueError on a
        negative size or empty fmt/name, exactly like :meth:`add_format`.
        """
        fmt = fmt.strip().upper()
        name = name.strip()
        if not fmt or not name:
            raise ValueError("Format and name must not be empty")
        if size < 0:
            raise ValueError(f"Format size must not be negative, got {size}")
        # ATTACH must precede the transaction (see _ensure_fts_attached).
        self._ensure_fts_attached()
        self._begin()
        try:
            self._require_book(book_id)
            old = self.conn.execute(
                "SELECT id, name, uncompressed_size FROM data "
                "WHERE book = ? AND upper(format) = ?",
                (book_id, fmt),
            ).fetchone()
            if (
                old is not None
                and old["name"] == name
                and old["uncompressed_size"] == size
            ):
                self._rollback()
                return False
            if old is not None:
                self.conn.execute("DELETE FROM data WHERE id = ?", (old["id"],))
            self.conn.execute(
                "INSERT INTO data (book, format, uncompressed_size, name) "
                "VALUES (?, ?, ?, ?)",
                (book_id, fmt, size, name),
            )
            self._mark_fts_dirty(book_id, fmt)
            self._touch_book(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
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
                self._rollback()
                return False
            self.conn.execute(
                "UPDATE books SET has_cover = ?, last_modified = ? WHERE id = ?",
                (new, self._now(), book_id),
            )
            self._mark_dirty(book_id)
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    # -- Cover management (1.19; the approved C.3) --

    def set_cover(self, book_id: int, data: bytes | str | os.PathLike) -> bool:
        """Write the book's cover file and set ``has_cover``.

        ``data`` is the image bytes, or a path to read them from. The
        sniff-or-raise rule is ``add_book``'s: a JPEG lands as
        ``cover.jpg``, a PNG as ``cover.png``, and anything unparseable
        raises rather than being catalogued with ``has_cover=1``; a stale
        cover under the OTHER extension is removed so
        :meth:`cquarry.db.CalibreDB.get_cover_path` can never prefer it.
        The file lands only after the commit (deferred inside a
        :meth:`batch`), so a failed pass never catalogues a cover it did
        not keep. Returns True; use :meth:`remove_cover` to clear.
        """
        if data is None:
            raise TypeError(
                "set_cover(book_id, None) is not supported; use remove_cover()"
            )
        if isinstance(data, (str, os.PathLike)):
            with open(os.fspath(data), "rb") as f:
                data = f.read()
        data = bytes(data)
        ext = sniff_image_format(data)
        if ext is None:
            raise ValueError(
                "Cover is neither JPEG nor PNG (sniff-or-raise: an "
                "unparseable cover must not be catalogued with has_cover=1)"
            )
        with self.batch():
            self._require_book(book_id)
            book_dir = self._book_dir_path(book_id)
            if book_dir is None:
                raise ValueError(f"Book {book_id} has no path to place a cover in")
            self.conn.execute(
                "UPDATE books SET has_cover = 1, last_modified = ? WHERE id = ?",
                (self._now(), book_id),
            )
            self._mark_dirty(book_id)
            new_name = f"cover.{ext}"
            other = "cover.png" if ext == "jpg" else "cover.jpg"

            def _place(book_dir=book_dir, new_name=new_name, other=other, data=data):
                os.makedirs(book_dir, exist_ok=True)
                _place_bytes(data, os.path.join(book_dir, new_name))
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(book_dir, other))

            self._pending_fs_ops.append(_place)
        return True

    def remove_cover(self, book_id: int) -> bool:
        """Clear the cover: ``has_cover`` goes to 0 and both cover files
        (``cover.jpg``/``cover.png``) are removed after the commit.
        Returns True when the catalogued flag actually changed."""
        with self.batch():
            self._require_book(book_id)
            current = self.conn.execute(
                "SELECT has_cover FROM books WHERE id = ?", (book_id,)
            ).fetchone()["has_cover"]
            changed = bool(current)
            book_dir = self._book_dir_path(book_id)
            if changed:
                self.conn.execute(
                    "UPDATE books SET has_cover = 0, last_modified = ? WHERE id = ?",
                    (self._now(), book_id),
                )
                self._mark_dirty(book_id)

            def _remove(book_dir=book_dir):
                if book_dir is None:
                    return
                for name in ("cover.jpg", "cover.png"):
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(book_dir, name))

            self._pending_fs_ops.append(_remove)
        return changed

    # -- Original-format save/restore (1.19; the approved C.8) --

    def save_original_format(self, book_id: int, fmt: str) -> bool:
        """Save a copy of the format as ``ORIGINAL_<FMT>`` (upstream
        cache.py:1581), overwriting any previously saved original.

        The undo-able repair primitive: call before swapping a repaired
        file into place, and :meth:`restore_original_format` can put the
        bytes back. The copy is a real ``data`` row plus a
        ``<stem>.original_<ext>`` file beside the original, so Calibre sees
        it exactly as it sees one of its own. Returns True when saved,
        False when the book has no such format (or its file is missing on
        disk). Raises ValueError when ``fmt`` is itself an ORIGINAL
        format."""
        fmt = (fmt or "").strip().upper()
        if not fmt:
            raise ValueError("Format must not be empty")
        if "ORIGINAL" in fmt:
            raise ValueError("Cannot save an original of an original format")
        with self.batch():
            self._require_book(book_id)
            src = self.conn.execute(
                "SELECT id, name, uncompressed_size FROM data "
                "WHERE book = ? AND upper(format) = ?",
                (book_id, fmt),
            ).fetchone()
            if src is None:
                return False
            book_dir = self._book_dir_path(book_id)
            if book_dir is None:
                return False
            src_path = os.path.join(book_dir, f"{src['name']}.{fmt.lower()}")
            if not os.path.isfile(src_path):
                return False
            with open(src_path, "rb") as f:
                data = f.read()
            nfmt = "ORIGINAL_" + fmt
            orig = self.conn.execute(
                "SELECT id, name FROM data WHERE book = ? AND upper(format) = ?",
                (book_id, nfmt),
            ).fetchone()
            if orig is None:
                self.add_format(book_id, nfmt, src["name"], len(data))
            else:
                self.set_format(book_id, nfmt, src["name"], len(data))
                old_stem = orig["name"]
                old_name = src["name"]

                def _sweep_old_stem(
                    book_dir=book_dir, old_stem=old_stem, old_name=old_name, nfmt=nfmt
                ):
                    if old_stem != old_name:
                        with contextlib.suppress(OSError):
                            os.unlink(
                                os.path.join(book_dir, f"{old_stem}.{nfmt.lower()}")
                            )

                self._pending_fs_ops.append(_sweep_old_stem)
            dest = os.path.join(book_dir, f"{src['name']}.{nfmt.lower()}")

            def _place_copy(dest=dest, data=data):
                _place_bytes(data, dest)

            self._pending_fs_ops.append(_place_copy)
        return True

    def restore_original_format(self, book_id: int, original_fmt: str) -> bool:
        """Restore the format from a previously saved ``ORIGINAL_<FMT>``,
        removing the original afterwards (upstream cache.py:1595).

        The ORIGINAL file's bytes become the target format's file (its
        ``data`` row keeps the target's filename stem), and the ORIGINAL
        row and file are removed. Returns True on success, False when no
        accessible ORIGINAL row/file exists. The restored format is queued
        for FTS re-extraction and a pages rescan (the same machinery a
        :meth:`set_format` repair uses)."""
        nfmt = (original_fmt or "").strip().upper()
        if not nfmt.startswith("ORIGINAL_") or len(nfmt) <= len("ORIGINAL_"):
            raise ValueError(f"Expected an ORIGINAL_<FMT> format, got {original_fmt!r}")
        fmt = nfmt[len("ORIGINAL_") :]
        # Attach before the batch opens so the restored format can always
        # be queued (see _ensure_fts_attached).
        self._ensure_fts_attached()
        with self.batch():
            self._require_book(book_id)
            orig = self.conn.execute(
                "SELECT id, name FROM data WHERE book = ? AND upper(format) = ?",
                (book_id, nfmt),
            ).fetchone()
            if orig is None:
                return False
            book_dir = self._book_dir_path(book_id)
            if book_dir is None:
                return False
            orig_path = os.path.join(book_dir, f"{orig['name']}.{nfmt.lower()}")
            if not os.path.isfile(orig_path):
                return False
            with open(orig_path, "rb") as f:
                data = f.read()
            target = self.conn.execute(
                "SELECT id, name FROM data WHERE book = ? AND upper(format) = ?",
                (book_id, fmt),
            ).fetchone()
            if target is None:
                self.add_format(book_id, fmt, orig["name"], len(data))
                dest_stem = orig["name"]
            else:
                self.set_format(book_id, fmt, target["name"], len(data))
                dest_stem = target["name"]
            # The bytes on disk changed even when the row was already
            # correct (an honest set_format no-op): queue re-extraction
            # unconditionally -- INSERT OR IGNORE dedupes when the row
            # change already queued it.
            self._mark_fts_dirty(book_id, fmt)
            dest = os.path.join(book_dir, f"{dest_stem}.{fmt.lower()}")

            def _swap(dest=dest, data=data, orig_path=orig_path):
                _place_bytes(data, dest)
                with contextlib.suppress(OSError):
                    os.unlink(orig_path)

            self._pending_fs_ops.append(_swap)
            # The ORIGINAL row and its FTS queue entry do not survive a
            # restore (remove_format clears the queue entry).
            self.remove_format(book_id, nfmt)
        return True

    # -- Book creation (Phase 10) --

    def add_book(
        self,
        title: str,
        authors: list[str],
        *,
        formats: list[str | os.PathLike] | None = None,
        cover: str | os.PathLike | bytes | None = None,
        identifiers: dict[str, str] | None = None,
        language: str | None = None,
        pubdate: str | date | datetime | None = None,
        publisher: str | None = None,
        dry_run: bool = False,
    ) -> int | dict[str, Any]:
        """Create a book row with triggers intact and return the new id.

        The creation path the automated phase-2 import stands on. Mirrors
        Calibre's own add sequence: insert ``(title, series_index,
        author_sort)`` FIRST and let ``books_insert_trg`` fill ``sort`` and
        ``uuid``; id from ``lastrowid``; the ``Author/Title (id)`` directory
        and format files (``Title - Author.ext``, truthful ``data`` sizes)
        written after. Everything lands in ONE ``batch()``: a failure rolls
        the SQL back and the tracked directory is removed, so a failed add
        leaves zero rows, zero links, and no directory. ``_touch_book()``
        queues the id so Calibre generates the sidecar ``.opf`` on next
        startup; no ``metadata.opf`` is written here.

        Seeds, all optional: format FILES copied into the book directory
        (``data`` rows follow, one per file, type taken from the file
        extension), a cover (path or bytes; JPEG/PNG only, sniff-or-raise:
        an unparseable cover raises rather than being catalogued with
        ``has_cover=1``), ``identifiers`` (types normalized like
        ``set_identifier``), one ``language`` (canonicalized through the
        search engine's map, ``English`` -> ``eng``; unknown names pass
        through verbatim, exactly like ``set_languages``), ``pubdate``, and
        a ``publisher``.
        No tags, series, ratings, comments, or custom columns at creation:
        phase 2 clears/sets those itself and phase 3 curates. An empty or
        blank title becomes ``Unknown`` (Calibre parity); an EMPTY author
        list is legal and leaves the book with no author links, exactly
        what ``find_authorless`` expects (the path component falls back to
        ``Unknown``).

        With ``dry_run=True`` nothing is written: the computed plan is
        returned instead, with the id predicted from ``sqlite_sequence``
        (labeled ``predicted_id``) and the row that would be inserted.

        Copy only by design: sources are never moved or deleted; queue
        hygiene is the runner's post-success policy.
        """
        title = (title or "").strip() or "Unknown"
        cleaned_authors: list[str] = []
        for name in authors or []:
            cleaned = name.strip()
            if cleaned and not any(
                cleaned.lower() == existing.lower() for existing in cleaned_authors
            ):
                cleaned_authors.append(cleaned)
        first_author = cleaned_authors[0] if cleaned_authors else ""
        author_sort = " & ".join(
            sort for _name, sort, _new in self._author_sort_keys(cleaned_authors)
        )

        fmt_entries: list[tuple[str, str, str, int]] = []
        seen_fmts: set[str] = set()
        for raw in formats or []:
            src = os.path.abspath(os.fspath(raw))
            if not os.path.isfile(src):
                raise FileNotFoundError(f"Format file not found: {src}")
            ext = os.path.splitext(src)[1][1:]
            fmt = ext.upper()
            if not fmt:
                raise ValueError(f"Format file has no extension: {src!r}")
            if fmt in seen_fmts:
                raise ValueError(f"Duplicate format seed: {fmt}")
            seen_fmts.add(fmt)
            fmt_entries.append((fmt, src, ext, os.path.getsize(src)))
        # The 'never imported twice' invariant: a byte-identical re-import
        # of a file the library already catalogues raises before anything
        # is written (dry runs included).
        self._reject_catalogued_duplicates(fmt_entries)

        cover_data: bytes | None = None
        cover_ext: str | None = None
        if cover is not None:
            if isinstance(cover, (bytes, bytearray)):
                cover_data = bytes(cover)
            else:
                cover_path = os.fspath(cover)
                if not os.path.isfile(cover_path):
                    raise FileNotFoundError(f"Cover file not found: {cover_path}")
                with open(cover_path, "rb") as f:
                    cover_data = f.read()
            cover_ext = sniff_image_format(cover_data)
            if cover_ext is None:
                raise ValueError(
                    "Cover is neither JPEG nor PNG (sniff-or-raise: an "
                    "unparseable cover must not be catalogued with has_cover=1)"
                )

        clean_identifiers: dict[str, str] = {}
        for id_type, val in (identifiers or {}).items():
            id_type = id_type.strip().lower()
            if not id_type:
                raise ValueError("Identifier type must not be empty")
            if not str(val).strip():
                raise ValueError(
                    f"Identifier value for {id_type!r} must not be empty "
                    "(set_identifier deletes; add_book has nothing to delete)"
                )
            clean_identifiers[id_type] = str(val).strip()

        clean_language: str | None = None
        if language is not None:
            from .search import canonical_language

            clean_language = canonical_language(str(language).strip())

        clean_publisher = (publisher or "").strip() or None
        pubdate_text = _normalize_pubdate(pubdate)

        if dry_run:
            return self._add_book_plan(
                title,
                cleaned_authors,
                author_sort,
                first_author,
                fmt_entries,
                cover_ext,
                clean_identifiers,
                clean_language,
                clean_publisher,
                pubdate_text,
            )

        book_dir: str | None = None
        try:
            with self.batch():
                # Insert FIRST, id from lastrowid (books_insert_trg fills
                # sort/uuid; the caller never passes either); path written
                # after, now that the id exists.
                cur = self.conn.execute(
                    "INSERT INTO books (title, series_index, author_sort) "
                    "VALUES (?, ?, ?)",
                    (title, 1.0, author_sort),
                )
                book_id = cur.lastrowid
                rel_path = _construct_path_name(book_id, title, first_author)
                self.conn.execute(
                    "UPDATE books SET path = ? WHERE id = ?", (rel_path, book_id)
                )
                self.conn.execute(
                    "UPDATE books SET pubdate = ? WHERE id = ?",
                    (pubdate_text, book_id),
                )
                # The directory name carries the fresh id, so it cannot
                # belong to any live book; a leftover orphan from a crashed
                # prior attempt is absorbed (and removed wholesale if this
                # attempt fails).
                book_dir = os.path.join(os.path.dirname(self.db_path), rel_path)
                os.makedirs(book_dir, exist_ok=True)
                if self._batch_depth:
                    # Batch-scoped compensation: the outermost batch's failed
                    # exit removes every directory created inside the pass.
                    self._batch_dirs.append(book_dir)
                for fmt, src, ext, _size in fmt_entries:
                    stem = _construct_file_name(title, first_author, len(ext) + 1)
                    placed = _place_stream(
                        src, os.path.join(book_dir, f"{stem}.{ext.lower()}")
                    )
                    self.conn.execute(
                        "INSERT INTO data (book, format, uncompressed_size, name) "
                        "VALUES (?, ?, ?, ?)",
                        (book_id, fmt, placed, stem),
                    )
                if cover_data is not None and cover_ext is not None:
                    _place_bytes(
                        cover_data, os.path.join(book_dir, f"cover.{cover_ext}")
                    )
                    self.conn.execute(
                        "UPDATE books SET has_cover = 1 WHERE id = ?", (book_id,)
                    )
                # The setters reuse their tested link/sort/prune logic; the
                # batch makes their per-call transaction control a no-op.
                if cleaned_authors:
                    self.set_authors(book_id, cleaned_authors)
                if clean_publisher:
                    self.set_publisher(book_id, clean_publisher)
                if clean_language:
                    self.set_languages(book_id, clean_language)
                if clean_identifiers:
                    self.set_identifiers(book_id, clean_identifiers)
                self._touch_book(book_id)
            return book_id
        except BaseException:
            # Failure compensation for the filesystem half: the SQL undoes
            # itself via the batch above; the created directory goes too.
            if book_dir is not None:
                shutil.rmtree(book_dir, ignore_errors=True)
            raise

    def _reject_catalogued_duplicates(
        self, fmt_entries: list[tuple[str, str, str, int]]
    ) -> None:
        """Refuse a byte-identical re-import (the CQ Phase 18 carve-out).

        Any catalogued ``data`` row with the same format and byte size is a
        candidate; identical CONTENT raises, so a file that already lives in
        the library can never be added twice. Content-agnostic duplicate
        SCREENING (metadata-based, fuzzier) stays a frontend concern.
        """
        root = os.path.dirname(self.db_path)
        for fmt, src, _ext, size in fmt_entries:
            rows = self.conn.execute(
                "SELECT d.name AS name, b.path AS path, d.book AS book "
                "FROM data d JOIN books b ON b.id = d.book "
                "WHERE d.uncompressed_size = ? AND upper(d.format) = ?",
                (size, fmt),
            ).fetchall()
            for row in rows:
                if not row["path"] or not row["name"]:
                    continue
                existing = os.path.join(
                    root, row["path"], row["name"] + "." + fmt.lower()
                )
                if os.path.isfile(existing) and filecmp.cmp(
                    src, existing, shallow=False
                ):
                    raise ValueError(
                        f"Refusing to import a duplicate: this file is already "
                        f"catalogued as book {row['book']}'s {fmt} "
                        "(byte-identical re-import)"
                    )

    def _author_sort_keys(self, names: list[str]) -> list[tuple[str, str, bool]]:
        """Resolve author names to (name, sort, is_new) without writing.

        Existing rows keep their hand-tuned ``sort``; new ones default it
        to the display name, Calibre's starting point.
        """
        resolved: list[tuple[str, str, bool]] = []
        for name in names:
            row = self.conn.execute(
                "SELECT sort FROM authors WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            if row is None:
                resolved.append((name, name, True))
            else:
                resolved.append((name, row["sort"] or name, False))
        return resolved

    def _add_book_plan(
        self,
        title: str,
        authors: list[str],
        author_sort: str,
        first_author: str,
        fmt_entries: list[tuple[str, str, str, int]],
        cover_ext: str | None,
        identifiers: dict[str, str],
        language: str | None,
        publisher: str | None,
        pubdate_text: str,
    ) -> dict[str, Any]:
        """The ``dry_run=True`` plan: predicted id/path, filenames, row diff."""
        try:
            seq = self.conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", ("books",)
            ).fetchone()
            predicted_id = (seq["seq"] if seq else 0) + 1
        except sqlite3.OperationalError:
            # No sqlite_sequence (no AUTOINCREMENT): max-id fallback.
            predicted_id = self.conn.execute(
                "SELECT IFNULL(MAX(id), 0) + 1 AS nxt FROM books"
            ).fetchone()["nxt"]
        return {
            "dry_run": True,
            "predicted_id": predicted_id,
            "books_row": {
                "title": title,
                "sort": title_sort(title),
                "author_sort": author_sort,
                "series_index": 1.0,
                "pubdate": pubdate_text,
                "path": _construct_path_name(predicted_id, title, first_author),
            },
            "authors": [
                {"name": name, "sort": sort, "new": is_new}
                for name, sort, is_new in self._author_sort_keys(authors)
            ],
            "publisher": publisher,
            "language": language,
            "identifiers": dict(identifiers),
            "formats": [
                {
                    "format": fmt,
                    "filename": f"{_construct_file_name(title, first_author, len(ext) + 1)}.{ext.lower()}",
                    "size": size,
                    "source": src,
                }
                for fmt, src, ext, size in fmt_entries
            ],
            "cover": f"cover.{cover_ext}" if cover_ext else None,
            "author_sort_computed": author_sort,
        }

    # -- Book lifecycle --

    # Upstream's library-local trash directory (constants.py TRASH_DIR_NAME);
    # a plain shutil.move target, so stdlib-only trash is exact parity.
    _TRASH_DIR_NAME = ".caltrash"

    def _book_dir_path(self, book_id: int) -> str | None:
        """The absolute directory of a book, or None when it has no path."""
        row = self.conn.execute(
            "SELECT path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None or not row["path"]:
            return None
        return os.path.join(os.path.dirname(self.db_path), row["path"])

    # -- Path re-laying (C.1, upstream backend.update_path) --

    def _relayout_book_path(self, book_id: int, title: str, first_author: str) -> None:
        """Re-lay a book's directory and format filenames after a rename.

        The completion of the curation-rename story: ``update_title`` and
        ``set_authors`` used to move the rows while the on-disk layout kept
        the old ``Author/Title (id)`` directory and ``Title - Author.ext``
        stems, silently corrupting the human-navigable library. Computes the
        new layout (upstream ``construct_path_name``/``construct_file_name``
        via the same helpers ``add_book`` uses), writes ``books.path`` and
        ``data.name`` in the transaction, and defers the filesystem half
        (dir rename, per-format file renames, emptied-parent removal) to
        after the outermost COMMIT -- a rollback drops it, so rows and files
        never disagree inside a failed pass. Books with no stored path get
        the db-only correction, exactly like upstream.
        """
        new_rel = _construct_path_name(book_id, title, first_author)
        try:
            formats = [
                r["format"]
                for r in self.conn.execute(
                    "SELECT format FROM data WHERE book = ?", (book_id,)
                )
                if r["format"]
            ]
        except sqlite3.OperationalError:
            formats = []  # schema predates the data table
        extlen = max((len(f) for f in formats), default=9) + 1
        new_stem = _construct_file_name(title, first_author, extlen)
        try:
            renames = [
                (r["format"], r["name"])
                for r in self.conn.execute(
                    "SELECT format, name FROM data WHERE book = ?", (book_id,)
                )
                if r["format"] and r["name"] and r["name"] != new_stem
            ]
        except sqlite3.OperationalError:
            renames = []  # schema predates the data table
        old_row = self.conn.execute(
            "SELECT path FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        old_rel = old_row["path"] or ""
        if old_rel == new_rel and not renames:
            return
        # SQL first: in-transaction, so a failed pass rolls the rows back.
        self.conn.execute("UPDATE books SET path = ? WHERE id = ?", (new_rel, book_id))
        for fmt, _old_name in renames:
            self.conn.execute(
                "UPDATE data SET name = ? WHERE book = ? AND format = ?",
                (new_stem, book_id, fmt),
            )
        op = (book_id, old_rel, new_rel, renames, new_stem)
        if self._batch_depth:
            self._pending_relayouts.append(op)
        else:
            self._apply_relayout(op)

    def _flush_pending_fs_ops(self) -> None:
        """Run the file placements/removals deferred by set_cover and the
        original-format verbs, only after the outermost COMMIT: a rollback
        that resurrected old rows must not leave new cover or format files
        on disk, so the queued ops are dropped instead."""
        for op in self._pending_fs_ops:
            op()
        self._pending_fs_ops.clear()

    def _flush_pending_relayouts(self) -> None:
        """Perform path re-lays deferred by setters inside a batch.

        Called only after the outermost COMMIT, mirroring
        :meth:`_flush_pending_removals`: a rollback that resurrected the
        rows must not leave them pointing at renamed directories.
        """
        for op in self._pending_relayouts:
            self._apply_relayout(op)
        self._pending_relayouts.clear()

    def _apply_relayout(self, op: tuple) -> None:
        """The filesystem half of one re-lay: dir rename, file renames,
        emptied-parent removal. Best-effort like upstream (a missing source
        directory is a db-only correction; the target of a move is removed
        only when it is a stale leftover -- ids are unique in paths, so it
        cannot be a live book's directory)."""
        _book_id, old_rel, new_rel, renames, new_stem = op
        root = os.path.dirname(self.db_path)
        old_dir = os.path.join(root, *old_rel.split("/")) if old_rel else None
        new_dir = os.path.join(root, *new_rel.split("/"))
        if old_dir and os.path.exists(old_dir):
            if os.path.exists(new_dir) and os.path.samefile(old_dir, new_dir):
                # Same directory under a case-differing spelling
                # (case-insensitive filesystems): fix the spelling segment
                # by segment, exactly like upstream.
                segs_old, segs_new = old_rel.split("/"), new_rel.split("/")
                if len(segs_old) == len(segs_new):
                    cur = root
                    for o_seg, n_seg in zip(segs_old, segs_new):
                        if o_seg.lower() == n_seg.lower() and o_seg != n_seg:
                            with contextlib.suppress(OSError):
                                os.replace(
                                    os.path.join(cur, o_seg), os.path.join(cur, n_seg)
                                )
                        cur = os.path.join(cur, n_seg)
            else:
                if os.path.exists(new_dir):
                    shutil.rmtree(new_dir)
                os.makedirs(os.path.dirname(new_dir), exist_ok=True)
                try:
                    os.rename(old_dir, new_dir)
                except OSError:
                    shutil.move(old_dir, new_dir)
                parent = os.path.dirname(old_dir)
                if parent != root:
                    with contextlib.suppress(OSError):
                        os.rmdir(parent)  # only succeeds when empty
        # Format files ride the dir rename; rename them to the new stem.
        if renames and os.path.isdir(new_dir):
            for fmt, old_name in renames:
                src = os.path.join(new_dir, old_name + "." + fmt.lower())
                dest = os.path.join(new_dir, new_stem + "." + fmt.lower())
                if src != dest and os.path.exists(src):
                    os.replace(src, dest)

    def _flush_pending_removals(self) -> None:
        """Perform file removals deferred by remove_book inside a batch.

        Called only after the outermost COMMIT: a rollback that resurrected
        the rows must not leave them fileless, so the removals registered
        inside the pass are simply dropped on rollback instead.
        """
        for book_id, path, mode in self._pending_removals:
            self._remove_book_dir(book_id, path, mode)
        self._pending_removals.clear()

    def _remove_book_dir(self, book_id: int, book_dir: str, mode: str) -> None:
        if mode == "trash":
            trash_b = os.path.join(
                os.path.dirname(self.db_path), self._TRASH_DIR_NAME, "b"
            )
            os.makedirs(trash_b, exist_ok=True)
            dest = os.path.join(trash_b, str(book_id))
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(book_dir, dest)
        else:  # permanent
            shutil.rmtree(book_dir, ignore_errors=True)
        # Upstream removes an emptied parent (the author directory) too.
        parent = os.path.dirname(book_dir)
        if parent != os.path.dirname(self.db_path):
            with contextlib.suppress(OSError):
                os.rmdir(parent)  # only succeeds when empty

    def remove_book(self, book_id: int, delete_files: str | None = None) -> None:
        """Remove a book and all of its satellite rows.

        ``books_delete_trg`` cascades the standard link tables, data,
        annotations, comments, conversion options and plugin data when the
        books row goes. What the trigger does NOT cover is cleaned here:
        custom-column rows (both storage patterns, every column), the dirtied
        queues, and now-orphaned entity rows (pruned AFTER the cascade so the
        fkc_delete_on_* guards pass). Irreversible - callers own confirmation.

        ``delete_files`` extends the removal to the book's on-disk directory
        (``Author/Title (id)/``), matching upstream's remove flow:
        ``"trash"`` moves it into the library-local ``.caltrash/b/<id>/``
        (upstream's own trash layout, recoverable by hand), ``"permanent"``
        deletes it outright, and ``None`` (default) leaves the files for the
        caller to sweep. File removal happens only after the rows COMMIT:
        inside a ``batch()`` it is deferred to the outermost commit, so a
        rollback that resurrects the rows never leaves them fileless.
        """
        if delete_files not in (None, "permanent", "trash"):
            raise ValueError(
                f"delete_files must be None, 'permanent', or 'trash', "
                f"got {delete_files!r}"
            )
        self._begin()
        try:
            self._require_book(book_id)
            # Capture the directory before the cascade: the row (and its
            # path) must exist to resolve it, and the fs step only runs
            # after a commit anyway.
            book_dir = self._book_dir_path(book_id) if delete_files else None
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
            self._commit()
            # File removal strictly after the rows commit: outside a batch
            # this runs now; inside one it defers to the outermost commit.
            if book_dir is not None:
                if self._batch_depth:
                    self._pending_removals.append((book_id, book_dir, delete_files))
                else:
                    self._remove_book_dir(book_id, book_dir, delete_files)
        except BaseException:
            self._rollback()
            raise
