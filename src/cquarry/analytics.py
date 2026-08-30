"""Library analytics derivations, promoted from CalibreQuarry's frontend.

Reading pace, per-author stats, and wing overlap were computed inside
CalibreQuarry's ``--analytics`` mode from pure database data; Hermitage's
stats views and Carrel's collection-pace views would each have re-derived
them. This module is the shared derivation layer — the frontend keeps
formatting. Anything an existing cquarry API already serves
(:meth:`get_format_stats`, :meth:`get_entities`, :meth:`get_tag_counts`)
deliberately does not appear here.

Same shape as :mod:`cquarry.integrity`: pure functions over the cached rows,
no SQL of their own.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from cquarry.helpers import calibre_rating_to_stars, normalize_author_display

if TYPE_CHECKING:
    from cquarry.db import CalibreDB

__all__ = [
    "addition_timeline",
    "author_stats",
    "rating_distribution",
    "vl_overlap",
]


def addition_timeline(db: CalibreDB, granularity: str = "month") -> dict[str, int]:
    """Books added per calendar bucket, chronological.

    ``granularity="month"`` (the default) keys ``"YYYY-MM"``; ``"year"``
    keys ``"YYYY"``. Books with no timestamp are skipped — they have no
    bucket to land in. Anything else raises ValueError.
    """
    if granularity not in ("month", "year"):
        raise ValueError(f"granularity must be 'month' or 'year', not {granularity!r}")
    width = 7 if granularity == "month" else 4
    out: dict[str, int] = {}
    for b in db.get_all_books():
        ts = b["timestamp"] or ""
        if not ts:
            continue
        key = ts[:width]
        if not key[0].isdigit():
            continue
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def author_stats(db: CalibreDB) -> list[dict[str, Any]]:
    """Per-primary-author rollups, count-descending then name.

    Each entry: ``author`` (primary author via
    :func:`cquarry.helpers.normalize_author_display`'s ``primary_only``
    mode), ``book_count``, ``avg_rating`` (star-scale average over rated
    books only; ``0.0`` when the author has none), ``rated_count`` (how many
    of the author's books carry a rating — renderers need it for
    "(N rated)" and the average alone cannot give it back), and ``formats``
    (sorted distinct format names across the author's books). Authorless
    books are skipped — they have no author to roll up into.
    """
    data: dict[str, dict[str, Any]] = {}
    for b in db.get_all_books():
        if not b["authors"]:
            continue
        author = normalize_author_display(b["authors"], primary_only=True)
        ad = data.setdefault(author, {"count": 0, "ratings": [], "formats": set()})
        ad["count"] += 1
        stars = calibre_rating_to_stars(b["rating"])
        if stars is not None:
            ad["ratings"].append(stars)
        if b["formats"]:
            ad["formats"].update(f.strip() for f in b["formats"])

    out = []
    for author, ad in sorted(data.items(), key=lambda x: (-x[1]["count"], x[0])):
        ratings = ad["ratings"]
        out.append(
            {
                "author": author,
                "book_count": ad["count"],
                "avg_rating": sum(ratings) / len(ratings) if ratings else 0.0,
                "rated_count": len(ratings),
                "formats": sorted(ad["formats"]),
            }
        )
    return out


def rating_distribution(db: CalibreDB) -> dict[float | str, int]:
    """How many books sit at each star rating, ascending, ``"unrated"`` last.

    Keys are star floats on the half-step scale Calibre's 0-10 integers map
    to (``normalize_rating``); books with no rating (``None``/``0``) count
    under the ``"unrated"`` string key.
    """
    out: dict[float | str, int] = {}
    for b in db.get_all_books():
        stars = calibre_rating_to_stars(b["rating"])
        key: float | str = stars if stars is not None else "unrated"
        out[key] = out.get(key, 0) + 1
    floats = sorted(k for k in out if isinstance(k, float))
    ordered: dict[float | str, int] = {k: out[k] for k in floats}
    if "unrated" in out:
        ordered["unrated"] = out["unrated"]
    return ordered


def vl_overlap(
    db: CalibreDB, names: list[str] | tuple[str, ...] | None = None
) -> dict[tuple[str, ...], list[int]]:
    """Books shared by two or more virtual libraries (wings).

    Returns ``{(wing, wing, ...): [ids]}`` for every combination of two or
    more wings that actually overlaps, wing names sorted inside each key.
    ``names`` restricts the wings considered (unknown names raise through
    :meth:`CalibreDB.resolve_vl`); None means every virtual library. Books
    in exactly one wing appear nowhere — single-wing membership is not
    overlap.
    """
    if names is None:
        names = sorted(db.get_virtual_libraries())
    ids_by_wing: dict[str, set[int]] = {n: db.resolve_vl(n) for n in names}

    wings_of: dict[int, list[str]] = {}
    for wing, ids in ids_by_wing.items():
        for book_id in ids:
            wings_of.setdefault(book_id, []).append(wing)

    out: dict[tuple[str, ...], list[int]] = {}
    for book_id, wings in wings_of.items():
        if len(wings) < 2:
            continue
        for r in range(2, len(wings) + 1):
            for combo in itertools.combinations(sorted(wings), r):
                out.setdefault(combo, []).append(book_id)
    return {k: sorted(v) for k, v in sorted(out.items())}
