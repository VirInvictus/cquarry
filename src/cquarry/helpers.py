import os
import struct
import sys
from urllib.parse import quote

from cquarry.config import (
    CALIBRE_RATING_SCALE,
    DEFAULT_DB_PATHS,
    get_db_path,
    set_db_path,
)

C_HEADER = "1;33"  # Bold Yellow
C_TITLE = "1;36"  # Bold Cyan
C_ERR = "1;31"  # Bold Red
C_WARN = "1;35"  # Bold Magenta
C_DIM = "2"  # Dim


def db_uri_ro(path: str) -> str:
    """Build the read-only SQLite ``file:`` URI for a database path.

    The path must be percent-encoded: '?' and '#' are URI syntax, so a library
    at "Books #2/metadata.db" interpolated raw opens some other file entirely
    and fails later with "no such table: books". quote() leaves '/' alone, so
    path separators survive.
    """
    return f"file:{quote(path)}?mode=ro"


def color(text: str, code: str) -> str:
    """Wrap text in ANSI color codes if stdout is a TTY."""
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text


# SOF markers that carry frame dimensions (baseline, progressive, lossless, ...).
# DHP (0xC4), DAC (0xCC) and the RST/SOI/EOI markers are deliberately excluded.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def get_jpeg_size(filepath: str) -> tuple[int, int] | None:
    """Return a JPEG's (width, height) by seeking through its segment markers.

    Reads only segment headers, so a large EXIF/ICC block ahead of the SOF (which
    a fixed 1 KB header read would miss) no longer hides the dimensions.
    """
    try:
        with open(filepath, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                byte = f.read(1)
                if not byte:
                    return None
                if byte != b"\xff":
                    continue
                # Skip any fill 0xff bytes to land on the marker code.
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if not marker:
                    return None
                m = marker[0]
                # Standalone markers (SOI/EOI and RSTn) carry no length payload.
                if m == 0xD8 or m == 0xD9 or 0xD0 <= m <= 0xD7:
                    continue
                seg = f.read(2)
                if len(seg) < 2:
                    return None
                length = struct.unpack(">H", seg)[0]
                if length < 2:
                    return None
                if m in _JPEG_SOF_MARKERS:
                    payload = f.read(5)  # precision(1) + height(2) + width(2)
                    if len(payload) < 5:
                        return None
                    height, width = struct.unpack(">HH", payload[1:5])
                    return width, height
                f.seek(length - 2, os.SEEK_CUR)
    except Exception:
        return None


def get_png_size(filepath: str) -> tuple[int, int] | None:
    """Return a PNG's (width, height) from its IHDR chunk."""
    try:
        with open(filepath, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            chunk = f.read(8)  # length(4) + type(4)
            if len(chunk) < 8 or chunk[4:8] != b"IHDR":
                return None
            wh = f.read(8)
            if len(wh) < 8:
                return None
            width, height = struct.unpack(">II", wh)
            return width, height
    except Exception:
        return None


def get_image_size(filepath: str) -> tuple[int, int] | None:
    """Return (width, height) for a JPEG or PNG, sniffing the format by signature."""
    try:
        with open(filepath, "rb") as f:
            sig = f.read(8)
    except OSError:
        return None
    if sig[:2] == b"\xff\xd8":
        return get_jpeg_size(filepath)
    if sig == b"\x89PNG\r\n\x1a\n":
        return get_png_size(filepath)
    return None


def calibre_rating_to_stars(rating: int | None) -> float | None:
    """Convert Calibre's internal rating (0-10) to stars (0-5)."""
    if rating is None or rating == 0:
        return None
    return rating / CALIBRE_RATING_SCALE


def format_stars(rating: float | None) -> str:
    if rating is None:
        return ""
    rating = max(0.0, min(5.0, rating))
    full = int(rating)
    half = rating - full >= 0.5
    empty = 5 - full - (1 if half else 0)
    # Half stars use the Latin-1 fraction glyph (U+00BD) rather than another
    # outline star, so a 2.5 reads as \u2605\u2605\u00bd\u2606\u2606 and is visibly distinct from 2.0.
    # U+00BD is universally available; a dedicated half-star glyph is not.
    s = "\u2605" * full
    if half:
        s += "\u00bd"
    s += "\u2606" * empty
    return f" [{s} {rating:.1f}/5]"


def normalize_author_display(authors: str | None, primary_only: bool = False) -> str:
    """Format author string for display."""
    if not authors:
        return "Unknown Author"
    parts = [a.strip() for a in authors.split(",")]
    if primary_only:
        return parts[0]
    return " & ".join(parts)


def author_sort_key(author_sort: str | None, primary_only: bool = False) -> str:
    key = (author_sort or "").lower()
    if primary_only:
        key = key.split("&")[0].strip()
    return key


def detect_series_gaps(indices_str: str, max_index: float | None) -> list[int]:
    """Detect missing entries in a series based on index numbers."""
    if not indices_str or max_index is None:
        return []
    indices = set()
    for s in indices_str.split(","):
        try:
            idx = float(s)
            if idx == int(idx):
                indices.add(int(idx))
        except ValueError:
            continue
    expected = set(range(1, int(max_index) + 1))
    return sorted(expected - indices)


def _resolve_path(path: str) -> str | None:
    """Expand and validate a path to metadata.db. Returns None if not found."""
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        path = os.path.join(path, "metadata.db")
    if os.path.exists(path):
        return os.path.abspath(path)
    return None


def find_db(explicit: str | None = None) -> str:
    """Locate metadata.db.

    Resolution order:
      1. Explicit --db argument
      2. Saved config (~/.config/cquarry/config.json)
      3. Default paths (./metadata.db, ~/Calibre Library/metadata.db, etc.)
      4. Interactive prompt (if stdin is a TTY)
    """
    # 1. Explicit argument
    if explicit:
        resolved = _resolve_path(explicit)
        if resolved:
            return resolved
        raise FileNotFoundError(f"Database not found: {explicit}")

    # 2. Saved config
    saved = get_db_path()
    if saved and os.path.exists(saved):
        return saved

    # 3. Default paths
    for p in DEFAULT_DB_PATHS:
        if os.path.exists(p):
            path = os.path.abspath(p)
            set_db_path(path)
            return path

    # 4. Interactive prompt (TTY only)
    if sys.stdin.isatty():
        print("First run: no Calibre database configured.")
        try:
            raw = input("  Path to metadata.db (or directory containing it): ").strip()
        except EOFError, KeyboardInterrupt:
            # Deliberate translation: an aborted prompt means "no DB found",
            # and the EOFError/KeyboardInterrupt context is noise there.
            raise FileNotFoundError(
                "Could not find metadata.db. Specify with --db /path/to/metadata.db"
            ) from None
        if raw:
            resolved = _resolve_path(raw)
            if resolved:
                set_db_path(resolved)
                print(f"  Saved: {resolved}")
                return resolved
            raise FileNotFoundError(f"Database not found: {raw}")

    raise FileNotFoundError(
        "Could not find metadata.db. Specify with --db /path/to/metadata.db"
    )
