"""Library integrity predicates, promoted from CalibreQuarry's frontend.

The mechanical definitions of "incomplete" — untagged, unrated, coverless,
duplicate, series-gapped — used to live as frontend SQL in CalibreQuarry's
``--audit`` (and were re-implemented again in ``scripts/validate_metadata.py``),
so every consumer had its own slightly different answer. This module is the
one shared definition; the taxonomy-driven opinionated layer stays in
Brandon's library linter.

Everything here is a pure function over the cached rows
(:meth:`cquarry.db.CalibreDB.get_all_books`); there is no SQL in this module.
The cover-file checks and :func:`find_failed_text_extraction` are the
exceptions that prove the rule: a flag over cached rows cannot see the disk,
and the FTS sidecar is a separate database, so they ride
:meth:`get_cover_path` + :func:`get_image_size` and
:meth:`get_text_extractions` respectively. Every id list is sorted.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import TYPE_CHECKING, Any

from cquarry.helpers import (
    detect_series_gaps,
    get_image_size,
    normalize_author_display,
)

if TYPE_CHECKING:
    from cquarry.db import CalibreDB

__all__ = [
    "check_library_disk",
    "find_authorless",
    "find_bad_language_codes",
    "find_coverless",
    "find_deprecated_formats",
    "find_duplicate_books",
    "find_failed_text_extraction",
    "find_formatless",
    "find_identifierless",
    "find_invalid_uuids",
    "find_low_res_covers",
    "find_missing_cover_files",
    "find_missing_format_files",
    "find_sentinel_pubdates",
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


def find_missing_format_files(db: CalibreDB) -> list[int]:
    """Books with a catalogued format whose file is absent on disk.

    Closes the integrity family's last disk hole: :func:`find_formatless`
    catches books with no ``data`` rows, :func:`find_missing_cover_files`
    the cover, and this one the format rows whose file is gone (deleted by
    hand, a moved library, a partial restore) while the catalog keeps
    offering it. Rides :meth:`CalibreDB.get_format_path`'s
    verified-by-default resolution, the same way the cover checks ride
    ``get_cover_path``. Books with an empty ``books.path`` are skipped:
    there is nowhere to look, so the check cannot be contradicted (the
    same rule as the cover check)."""
    out: list[int] = []
    for b in _books(db):
        if not b["path"] or not b["formats"]:
            continue
        for fmt in b["formats"]:
            try:
                db.get_format_path(b["id"], fmt)
            except FileNotFoundError:
                out.append(b["id"])
                break
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


def find_identifierless(db: CalibreDB) -> list[int]:
    """Books carrying no identifiers at all (the ISBN/GoodReads/ASIN EAV
    store). The curation-facing opposite of :meth:`CalibreDB.get_identifiers`;
    promoted from Hermitage's inline Insights predicate."""
    return sorted(b["id"] for b in db.get_all_books() if not b["identifiers"])


def find_invalid_uuids(db: CalibreDB) -> list[int]:
    """Books whose ``uuid`` is empty or does not parse as a UUID.

    The metadata-quality half of the uuid story: the reader degrades
    pre-``uuid``-column schemas to ``""``, and hand-built rows can carry
    any garbage, so an unparseable id is reported as seen rather than
    assumed away."""
    out: list[int] = []
    for b in _books(db):
        value = b.get("uuid") or ""
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            out.append(b["id"])
    return sorted(out)


def find_sentinel_pubdates(db: CalibreDB) -> list[int]:
    """Books whose ``pubdate`` is the undefined-date sentinel (Calibre's
    ``0101-01-01``, or its ``0100-01-01`` ancestor -- the same pair the
    search engine and the listing sort treat as dateless). A real
    publication date of year 1 would be indistinguishable; nobody has
    one."""
    return sorted(
        b["id"]
        for b in _books(db)
        if isinstance(b.get("pubdate"), str)
        and b["pubdate"].startswith(("0101-01-01", "0100-01-01"))
    )


def find_bad_language_codes(db: CalibreDB) -> list[int]:
    """Books linked to a language code that is not an ISO 639-2 code.

    Calibre stores three-letter lowercase codes in ``languages.lang_code``;
    a bare name (``English``), a two-letter code, or an empty string is
    the data-quality smell the OPF linters flag. Shape check only: the
    code must be exactly three ASCII letters (no registry, so a valid
    but rare code never false-positives)."""
    out: list[int] = []
    for b in _books(db):
        if any(
            not (
                isinstance(code, str)
                and len(code) == 3
                and code.isascii()
                and code.isalpha()
                and code.islower()
            )
            for code in b.get("languages") or []
        ):
            out.append(b["id"])
    return sorted(out)


def find_failed_text_extraction(db: CalibreDB) -> dict[int, dict[str, str]]:
    """``{book_id: {FORMAT: err_msg}}`` for formats whose extraction failed.

    Reads the FTS sidecar's extraction status via
    :meth:`CalibreDB.get_text_extractions` and keeps the rows with a
    non-empty ``err_msg`` -- Calibre records scans, DRM, and corrupt files
    there when it indexes book contents. The third sanctioned SQL ride in
    this module (after the two cover-file checks): the sidecar is a separate
    database the cached rows cannot see. Empty dict when the sidecar is
    absent (nothing was ever extracted).
    """
    failed: dict[int, dict[str, str]] = {}
    rows = sorted(db.get_text_extractions(), key=lambda r: (r["book"], r["format"]))
    for row in rows:
        msg = row.get("err_msg") or ""
        if msg.strip():
            failed.setdefault(row["book"], {})[row["format"]] = msg
    return failed


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


# ---------------------------------------------------------------------------
# The check_library walk (1.24, Phase 15): the extra side of the disk story.
# find_missing_format_files / find_missing_cover_files above answer "the
# catalog points at files that are gone"; this walk answers the mirror image,
# upstream `calibre/utils/check_library.py` shape: files and directories the
# catalog does not know about, paths that stopped matching, and folders that
# could not be read at all. One pass over the library tree, categorized.
# ---------------------------------------------------------------------------

# Files every Calibre book directory may legitimately hold (plus cquarry's
# cover.png, which set_cover can write beside upstream's cover.jpg).
_BOOK_DIR_NORMALS = frozenset({"metadata.opf", "cover.jpg", "cover.png", "data"})

# Library-root entries that are not book directories (upstream
# IGNORE_AT_TOP_LEVEL, plus the retired-DB backup spellings).
_TOP_LEVEL_IGNORES = frozenset(
    {
        "metadata.db",
        "metadata_db_prefs_backup.json",
        "metadata_pre_restore.db",
        "full-text-search.db",
        ".caltrash",
        ".calnotes",
    }
)

# Extensions that are never ebook formats (upstream restore.py
# NON_EBOOK_EXTENSIONS); everything else with a plausible token extension
# reads as a format file.
_NON_EBOOK_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "gif", "png", "bmp", "opf", "swp", "swo"}
)

# upstream's `^.* \((\d+)\)$`: the book id rides the title dir in
# parentheses.
_BOOK_DIR_ID_RE = re.compile(r"^.* \((\d+)\)$")

_PLAGIBLE_EXT_RE = re.compile(r"^[a-z0-9_]+$")


def _looks_like_ebook_file(filename: str) -> bool:
    """Structural format-file classifier: a plausible token extension that
    is not an image/OPF/junk suffix.

    Deliberately NOT upstream's curated ``BOOK_EXTENSIONS`` list (stdlib
    has none): a ``.foo`` data file reads as a format candidate here where
    upstream would call it an unknown file. Callers filter with
    ``extension_ignores``; ``ORIGINAL_<FMT>`` save copies classify as
    formats, exactly like upstream's ``removeprefix("original_")``."""
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem or not ext:
        return False
    ext = ext.lower().removeprefix("original_")
    if ext in _NON_EBOOK_EXTENSIONS:
        return False
    return _PLAGIBLE_EXT_RE.fullmatch(ext) is not None


def check_library_disk(
    db: CalibreDB,
    *,
    name_ignores: set[str] | list[str] | tuple[str, ...] = (),
    extension_ignores: set[str] | list[str] | tuple[str, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    """The extra-side disk checks of upstream's ``check_library`` (1.24).

    One walk over the library tree beside ``metadata.db``, returning
    categorized findings as ``{category: [entry]}``; every entry is
    ``{"book_id": int, "title_dir": str, "path": str}`` (``book_id`` 0 when
    the finding belongs to no catalogued book; ``failed_folders`` entries
    add ``{"error": traceback}``):

    - ``extra_titles``: directories whose ``Title (id)`` id is unknown, or
      whose canonical row already has its own directory on disk.
    - ``extra_authors``: author directories holding no recognized title.
    - ``malformed_paths``: a directory carrying a live book's id under a
      spelling ``books.path`` does not say (the canonical directory is
      missing, so this dir is the book's files under the wrong name).
    - ``extra_formats``: format-like files on disk no ``data`` row names.
    - ``extra_files``: non-format unknown files in book directories
      (anything outside the catalog, the covers, ``metadata.opf``, and the
      ``data/`` extras dir).
    - ``extra_covers``: a cover file on disk while ``has_cover`` is unset
      (the mirror of :func:`find_missing_cover_files`).
    - ``failed_folders``: directories that raised while being read.

    The missing side is deliberately not re-answered here:
    :func:`find_missing_format_files` and :func:`find_missing_cover_files`
    are the predicates for "catalogued but gone". Names in
    ``name_ignores`` (exact) and extensions in ``extension_ignores``
    (``"jpg"`` or ``".jpg"``) are skipped during the walk, upstream's
    fnmatch shape narrowed to the common case. POSIX case sensitivity is
    assumed (the ecosystem's target; upstream branches on
    ``is_case_sensitive``)."""
    root = os.path.dirname(os.path.abspath(db.db_path))
    rows = db.get_all_books()
    row_by_id = {b["id"]: b for b in rows}
    books_by_path = {b["path"]: b for b in rows if b["path"]}
    ignores = frozenset(name_ignores)
    ext_ignores = {"." + str(e).lower().lstrip(".") for e in extension_ignores}
    out: dict[str, list[dict[str, Any]]] = {
        key: []
        for key in (
            "extra_authors",
            "extra_titles",
            "malformed_paths",
            "malformed_formats",
            "extra_formats",
            "extra_files",
            "extra_covers",
            "failed_folders",
        )
    }
    book_dirs: list[tuple[str, str, int, dict[str, Any]]] = []

    for auth_dir in sorted(os.listdir(root)):
        if auth_dir in ignores or auth_dir in _TOP_LEVEL_IGNORES:
            continue
        auth_path = os.path.join(root, auth_dir)
        if not os.path.isdir(auth_path):
            continue  # a stray file at the root is nobody's book
        found_titles = False
        try:
            for title_dir in sorted(os.listdir(auth_path)):
                if title_dir in ignores:
                    continue
                title_path = os.path.join(auth_path, title_dir)
                m = _BOOK_DIR_ID_RE.fullmatch(title_dir)
                if m is None or not os.path.isdir(title_path):
                    out["extra_titles"].append(
                        {
                            "book_id": 0,
                            "title_dir": title_dir,
                            "path": auth_dir + "/" + title_dir,
                        }
                    )
                    continue
                book_id = int(m.group(1))
                rel = auth_dir + "/" + title_dir
                row = books_by_path.get(rel)
                if row is None:
                    canonical = row_by_id.get(book_id, {}).get("path")
                    if canonical is None or os.path.exists(
                        os.path.join(root, canonical)
                    ):
                        # Unknown id, or the id's real directory exists
                        # elsewhere: a stale directory nobody owns.
                        out["extra_titles"].append(
                            {"book_id": book_id, "title_dir": title_dir, "path": rel}
                        )
                        continue
                    # A live book's id under the wrong spelling, with no
                    # canonical directory on disk: the files are the book's,
                    # the path is wrong.
                    out["malformed_paths"].append(
                        {"book_id": book_id, "title_dir": title_dir, "path": rel}
                    )
                    row = row_by_id.get(book_id)
                    if row is None:
                        continue
                found_titles = True
                book_dirs.append((rel, title_dir, book_id, row))
        except OSError:
            out["failed_folders"].append(
                {
                    "book_id": 0,
                    "title_dir": auth_dir,
                    "path": auth_dir,
                    "error": "unreadable author directory",
                }
            )
            continue
        if not found_titles:
            out["extra_authors"].append(
                {"book_id": 0, "title_dir": auth_dir, "path": auth_dir}
            )

    for rel, title_dir, book_id, row in book_dirs:
        book_path = os.path.join(root, rel)
        try:
            filenames = {
                f
                for f in os.listdir(book_path)
                if f not in ignores
                and (
                    "." + f.rpartition(".")[2].lower() not in ext_ignores
                    or f in ("cover.jpg", "cover.png")
                )
            }
        except OSError:
            out["failed_folders"].append(
                {
                    "book_id": book_id,
                    "title_dir": title_dir,
                    "path": rel,
                    "error": "unreadable book directory",
                }
            )
            continue
        on_disk_formats = {f for f in filenames if _looks_like_ebook_file(f)}
        catalogued = {
            b["name"] + "." + fmt.lower()
            for fmt, b in db.get_formats(book_id).items()
            if b.get("name")
        }
        # upstream's case-sensitive shape: an on-disk format file the
        # catalog names only under a different case is malformed; one it
        # does not name at all is extra.
        missing_lower = {c.lower() for c in catalogued - on_disk_formats}
        for name in sorted(on_disk_formats - catalogued - _BOOK_DIR_NORMALS):
            entry = {
                "book_id": book_id,
                "title_dir": title_dir,
                "path": rel + "/" + name,
            }
            if name.lower() in missing_lower:
                out["malformed_formats"].append(entry)
            else:
                out["extra_formats"].append(entry)
        # Unknown non-format files (the catalogued unknown-extension names
        # and the data/ extras dir aside).
        for name in sorted(filenames - on_disk_formats - _BOOK_DIR_NORMALS):
            if name in catalogued:
                continue
            out["extra_files"].append(
                {"book_id": book_id, "title_dir": title_dir, "path": rel + "/" + name}
            )
        # Extra covers: files on disk, flag down (the missing side is
        # find_missing_cover_files).
        if not row.get("has_cover"):
            for cover in ("cover.jpg", "cover.png"):
                if cover in filenames:
                    out["extra_covers"].append(
                        {
                            "book_id": book_id,
                            "title_dir": title_dir,
                            "path": rel + "/" + cover,
                        }
                    )
    return out
