"""Library integrity predicates, promoted from CalibreQuarry's frontend.

The mechanical definitions of "incomplete" — untagged, unrated, coverless,
duplicate, series-gapped — used to live as frontend SQL in CalibreQuarry's
``--audit`` (and were re-implemented again in ``scripts/validate_metadata.py``),
so every consumer had its own slightly different answer. This module is the
one shared definition; the taxonomy-driven opinionated layer stays in
Brandon's library linter.

Everything here is a pure function over the cached rows
(:meth:`cquarry.db.CalibreDB.get_all_books`); there is no SQL in this module.
The two cover-file checks are the exception that proves the rule: a flag
alone cannot see the disk, so they ride :meth:`get_cover_path` and
:func:`cquarry.helpers.get_image_size`. Every id list is sorted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cquarry.helpers import (
    detect_series_gaps,
    get_image_size,
    normalize_author_display,
)

if TYPE_CHECKING:
    from cquarry.db import CalibreDB

__all__ = [
    "find_authorless",
    "find_coverless",
    "find_deprecated_formats",
    "find_duplicate_books",
    "find_formatless",
    "find_low_res_covers",
    "find_missing_cover_files",
    "find_series_gaps",
    "find_unrated",
    "find_untagged",
]


def _books(db: CalibreDB) -> list[dict[str, Any]]:
    return db.get_all_books()


def find_untagged(db: CalibreDB) -> list[int]:
    """Books carrying no tags at all."""
    return sorted(b["id"] for b in _books(db) if not b["tags"])


def find_unrated(db: CalibreDB) -> list[int]:
    """Books with no rating (``None`` and ``0`` both read as unrated)."""
    return sorted(
        b["id"] for b in _books(db) if b["rating"] is None or b["rating"] == 0
    )


def find_authorless(db: CalibreDB) -> list[int]:
    """Books with no authors, or only the placeholder ``Unknown``."""
    return sorted(
        b["id"] for b in _books(db) if not b["authors"] or b["authors"] == ["Unknown"]
    )


def find_formatless(db: CalibreDB) -> list[int]:
    """Books with no catalogued format rows (metadata-only entries)."""
    return sorted(b["id"] for b in _books(db) if not b["formats"])


def find_coverless(db: CalibreDB) -> list[int]:
    """Books whose ``has_cover`` flag is unset (the catalogued answer — the
    file question is :func:`find_missing_cover_files`)."""
    return sorted(b["id"] for b in _books(db) if not b["has_cover"])


def find_missing_cover_files(db: CalibreDB) -> list[int]:
    """Books where the flag says yes but no cover file resolves on disk.

    Deliberately skips books with an empty ``books.path`` — there is nowhere
    to look, so the flag cannot be contradicted. This is the answer to the
    question :func:`find_low_res_covers` excludes.
    """
    out: list[int] = []
    for b in _books(db):
        if not b["has_cover"] or not b["path"]:
            continue
        if db.get_cover_path(b["id"]) is None:
            out.append(b["id"])
    return sorted(out)


def find_deprecated_formats(
    db: CalibreDB, formats: set[str] | list[str] | tuple[str, ...]
) -> list[int]:
    """Books whose entire format set is inside ``formats``.

    ``formats`` is the caller's list of deprecated names (case-insensitive;
    CalibreQuarry curates ``{"MOBI", "LIT", "LRF", "DJVU", "PDB", "AZW"}``).
    cquarry owns only the subset-of mechanism — what counts as deprecated is
    a curation opinion, not a database fact. Formatless books are excluded:
    "no formats at all" is :func:`find_formatless`'s answer, not this one's.
    """
    deprecated = {f.strip().upper() for f in formats}
    out: list[int] = []
    for b in _books(db):
        fmts = {f.strip().upper() for f in b["formats"]}
        if fmts and fmts.issubset(deprecated):
            out.append(b["id"])
    return sorted(out)


def find_low_res_covers(
    db: CalibreDB, min_dimension: int = 500
) -> dict[int, tuple[int, int]]:
    """``{book_id: (width, height)}`` for covers under ``min_dimension`` px.

    Only books whose cover file actually resolves and parses are judged
    (sniffed JPEG/PNG, header-only); missing cover files are
    :func:`find_missing_cover_files`' answer, and unreadable images are
    skipped rather than guessed at.
    """
    out: dict[int, tuple[int, int]] = {}
    for b in _books(db):
        if not b["has_cover"] or not b["path"]:
            continue
        cover = db.get_cover_path(b["id"])
        if not cover:
            continue
        size = get_image_size(cover)
        if not size:
            continue
        w, h = size
        if max(w, h) < min_dimension:
            out[b["id"]] = (w, h)
    return out


def find_duplicate_books(
    db: CalibreDB,
) -> dict[tuple[str, str], list[int]]:
    """Groups of books sharing (title, primary author), lowercased.

    Multi-member groups only — a singleton is not a duplicate. Keys are
    ``(title.lower(), primary_author.lower())`` with the primary author
    taken through :func:`cquarry.helpers.normalize_author_display`'s
    ``primary_only`` mode; id lists sorted.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for b in _books(db):
        title = (b["title"] or "").strip().lower()
        authors = b["authors"] or []
        if not title or not authors:
            continue
        primary = normalize_author_display(authors, primary_only=True)
        key = (title, primary.strip().lower())
        groups.setdefault(key, []).append(b["id"])
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def find_series_gaps(db: CalibreDB) -> dict[str, list[int]]:
    """``{series_name: [missing_indices]}`` for every gapped series.

    Composes :meth:`CalibreDB.get_all_series` with
    :func:`cquarry.helpers.detect_series_gaps`; empty for fully collected or
    singleton series.
    """
    out: dict[str, list[int]] = {}
    for s in db.get_all_series():
        gaps = detect_series_gaps(s["indices"], s["max_index"])
        if gaps:
            out[s["name"]] = gaps
    return out
