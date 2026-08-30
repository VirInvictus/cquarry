# cquarry API reference

The full per-method reference. The [README](README.md) keeps the hero, the
quick-starts, and the search grammar; everything callable lives here.

**Version:** 1.8.0

## Public API

### `CalibreDB` (from `cquarry.db`)

The primary interface. Constructed with a path to `metadata.db`.

```python
db = CalibreDB(db_path: str)
```

Raises `FileNotFoundError` if the path does not exist. If the database is locked by Calibre, transparently copies it (including `-wal` and `-shm`) to a temp file and reads from the snapshot instead.

Supports the context manager protocol:

```python
with CalibreDB("/path/to/metadata.db") as db:
    ...  # db.close() called automatically on exit
```

#### Core queries

| Method | Returns | Description |
|--------|---------|-------------|
| `get_all_books()` | `list[dict[str, Any]]` | Every book in the library, pre-hydrated with `authors`, `author_sorts`, `author_links` (parallel arrays), `tags`, `series`, `rating`, `publisher`, `languages`, `formats`, `title_sort`, `author_sort`, `timestamp`, `pubdate`, `last_modified`, `has_cover`, `series_index`, `size`, `pages`, `uuid`, `identifiers`, and `path`. `authors`, `tags`, `languages`, and `formats` are exposed natively as `list[str]` arrays. Results are cached after the first call. |
| `get_book(book_id)` | `dict[str, Any] \| None` | Fetch one hydrated record (same shape as a `get_all_books()` row, including `size`, `uuid`, and `identifiers`) without scanning the library. |
| `search_books(query)` | `list[dict[str, Any]]` | Evaluate a search expression and return the hydrated matching books. |
| `get_format_path(book_id, fmt, verify=True)` | `str` | Absolute filesystem path for a book's format file, built from the original DB location. Raises `ValueError` for unknown book/format, `FileNotFoundError` when `verify` is set and the file is missing. |
| `get_formats(book_id)` | `dict[str, dict[str, Any]]` | Per-format detail: `{fmt: {path, size_bytes, name}}` (path unverified; size from the catalogued uncompressed size). `{}` for unknown books. |
| `get_cover_path(book_id, verify=True)` | `str \| None` | Resolved cover image path (`cover.jpg`, falling back to `cover.png`) from the original DB location. With `verify` (default) returns None when no file exists on disk; without it returns the `.jpg` path unconditionally. Raises `ValueError` for unknown books. |
| `get_library_uuid()` | `str \| None` | The library's identity UUID (`library_id` table) — stable across moves/restores, unlike per-book uuids; the right cache key for per-library state. None on very old schemas. |
| `get_entities(kind)` | `list[dict[str, Any]]` | Entity rows for `authors` / `series` / `publishers` / `tags` / `languages` / `ratings`: `{id, name, sort, link, count}`, name-sorted (ratings carry the half-star integer as `name`). Raises `ValueError` for unknown kinds. |
| `get_preference(key, default=None)` | `Any` | Typed read of any Calibre preference from the `preferences` table (JSON decoded where it parses). |
| `get_field_metadata()` | `dict[str, Any]` | The rich `field_metadata` preference: per-custom-column GUI metadata keyed by label. |
| `get_grouped_search_terms()` | `dict[str, list[str]]` | Grouped search terms driving `GroupName:query` expansion in the search engine. |
| `get_user_categories()` | `dict[str, list[dict]]` | User-defined tag-browser categories (name -> member descriptors). |
| `get_tag_browser_state()` | `dict[str, Any]` | `{"order": [...], "hidden": [...]}` from the `tag_browser_*` preferences — mirror Calibre's browse-sidebar layout. |
| `get_identifiers(book_id)` | `dict[str, str]` | All identifiers for a book (e.g. `isbn`, `amazon`, `lcc`), keyed by type. |
| `get_all_tags()` | `list[str]` | Every distinct tag name, sorted alphabetically. |
| `get_tag_counts()` | `list[tuple[str, int]]` | `(tag_name, book_count)` pairs, sorted by tag name. |
| `get_all_series()` | `list[dict[str, Any]]` | Per-series rollups: `name`, `book_count`, `indices` (comma-separated), `max_index`, `titles` (comma-separated, sorted by index). |
| `get_custom_columns()` | `dict[str, dict[str, Any]]` | Metadata for all user-defined custom columns, keyed by display name. Each value contains `id`, `label`, `name`, `datatype`, and `is_multiple`. |
| `load_custom_column(col_name)` | `dict[int, Any]` | Values for a specific custom column (by display name), returned as `{book_id: value}`. Normalized columns (text, enumeration, series) are read via their link table; direct columns (int, float, bool, datetime, comments) are read from the value table. Multi-valued columns return comma-separated strings. Raises `ValueError` if the column does not exist. |
| `get_virtual_libraries()` | `dict[str, str]` | Virtual library names mapped to their Calibre search expressions, read from the `preferences` table. Cached after the first call. |
| `get_saved_searches()` | `dict[str, str]` | Saved-search names mapped to their expressions (the source for `search:"Name"` interpolation). |
| `get_vl_ui_state()` | `dict[str, Any]` | Calibre's sidebar layout state: `{"hidden": [names], "order": {...}}` decoded from `virt_libs_hidden` / `virt_libs_order`. |
| `count_books()` | `int` | Total book count. Uses the cache if available; otherwise issues a `SELECT COUNT(*)`. |

#### Annotations, progress & plugin data

| Method | Returns | Description |
|--------|---------|-------------|
| `get_annotations(book_id=None)` | `list[dict[str, Any]]` | E-reader highlights, bookmarks, and notes from the `annotations` table; `annot_data` is decoded JSON when possible. |
| `get_last_read_positions(book_id=None)` | `list[dict[str, Any]]` | Per-device reading progress (`device`, `cfi`, `pos_frac` 0.0–1.0, `epoch`). |
| `get_plugin_data(book_id=None, name=None)` | `list[dict[str, Any]]` | Third-party payloads from `books_plugin_data` (Goodreads IDs, word counts, ...). |
| `get_conversion_profiles(book_id=None)` | `list[dict[str, Any]]` | Books with manual conversion overrides; the pickled recipe blob stays raw bytes (`data_size` gives its length). |
| `get_dirtied_books()` | `list[int]` | Book ids queued for OPF resync in `metadata_dirtied` — i.e. what Calibre will regenerate/push at its next startup. Sorted, deduplicated; read-only (clearing the queue remains Calibre's job). |
| `get_annotations_dirtied_books()` | `list[int]` | The annotations sibling queue (`annotations_dirtied`): ids whose highlights/bookmarks Calibre will push to devices. Same read-only contract. |
| `get_feeds()` | `list[dict[str, Any]]` | Registered news-download recipes from the `feeds` table: `[{id, title, script}]`. |
| `get_tag_browser_counts()` | `dict[str, list[dict[str, Any]]]` | Calibre's own browse-sidebar rollups from the `tag_browser_*` views: `{category: [{id, name, count, avg_rating, sort}]}`, custom columns rekeyed to `#label`. The `filtered_*` variants (GUI-state `books_list_filter()`) are skipped. |

All eight return `[]`/`{}` on databases whose schema predates the tables.

#### Search and virtual library resolution

| Method | Returns | Description |
|--------|---------|-------------|
| `search(query)` | `set[int]` | Parse and evaluate a Calibre search expression, returning matching book IDs. An empty query returns all IDs. Raises `ParseException` for unknown virtual libraries or saved searches. |
| `search_books(query)` | `list[dict[str, Any]]` | `search()` + hydration: the matching books as full records. |
| `resolve_vl(vl_name)` | `set[int]` | Resolve a virtual library by name to its set of book IDs. Case-insensitive; raises `ValueError` if the name is not found. |
| `resolve_saved_search(name)` | `set[int]` | Resolve a saved-search name to its set of book IDs. Case-insensitive; raises `ValueError` if the name is not found. |

#### Lifecycle

| Method | Description |
|--------|-------------|
| `close()` | Close the database connection and remove any temporary snapshot files. |
| `__enter__()` / `__exit__()` | Context manager support. Calls `close()` on exit. |

### `SearchEngine` (from `cquarry.search`)

The search engine can be used standalone by implementing the `MetadataProvider` protocol. `CalibreDB` implements this protocol, so most consumers never touch `SearchEngine` directly.

```python
from cquarry.search import SearchEngine, MetadataProvider

engine = SearchEngine(provider)  # provider implements MetadataProvider
results = engine.search("tags:Fiction and rating:>3")
```

#### `MetadataProvider` protocol

Any object implementing these methods can serve as a search backend:

| Method | Signature | Contract |
|--------|-----------|----------|
| `all_ids()` | `-> set[int]` | Return every book ID in the collection. |
| `field(book_id, location)` | `-> Any` | Return a book's value for a canonical location. See datatype contract below. |
| `vl_expression(name)` | `-> str \| None` | Return a virtual library's search expression, or `None` if unknown. |
| `saved_search(name)` | `-> str \| None` | Return a saved search's expression, or `None` if unknown. |
| `custom_locations()` | `-> dict[str, str]` | Return `{location_token: datatype}` for custom columns (e.g. `{"#read": "bool"}`). |

**`field()` return contract by datatype:**

| Datatype | Expected return |
|----------|----------------|
| `text` / `text_multi` / `hier` | `list[str]` |
| `rating` / `int` / `float` | number or `None` |
| `date` | raw date string or `None` |
| `bool` | `bool` |
| `identifiers` | `dict[str, str]` |

#### `ParseException`

Raised for malformed search expressions. Callers of `CalibreDB.search()` should catch this.

```python
from cquarry.search import ParseException

try:
    results = db.search("tags:(unclosed")
except ParseException as e:
    print(f"Bad query: {e}")
```

#### Datatype constants

Exported from `cquarry.search` for consumers building custom `MetadataProvider` implementations:

`DT_TEXT`, `DT_TEXT_MULTI`, `DT_HIER`, `DT_RATING`, `DT_INT`, `DT_FLOAT`, `DT_DATE`, `DT_BOOL`, `DT_IDENTIFIERS`, `DT_ALL`, `DT_VL`

### Helpers (from `cquarry.helpers`)

Utility functions used across the ecosystem. All are importable from `cquarry.helpers`.

#### Database discovery

| Function | Signature | Description |
|----------|-----------|-------------|
| `find_db(explicit=None)` | `-> str` | Locate `metadata.db` through a resolution chain: explicit argument, saved config (`~/.config/cquarry/config.json`), default paths (`./metadata.db`, `~/Calibre Library/metadata.db`, `~/calibre/metadata.db`), then an interactive TTY prompt. Raises `FileNotFoundError` if nothing is found. |
| `db_uri_ro(path)` | `-> str` | Build a percent-encoded read-only SQLite `file:` URI. Handles paths containing `?` or `#` that would otherwise be parsed as URI syntax. |

#### Rating and display

| Function | Signature | Description |
|----------|-----------|-------------|
| `normalize_rating(rating)` | `-> float \| None` | Canonical name for the conversion; identical to `calibre_rating_to_stars` (kept as an alias). Converts Calibre's internal 0-10 scale to 0.0-5.0 stars; returns `None` for unrated (0 or `None`). |
| `format_stars(rating)` | `-> str` | Render a 0.0-5.0 rating as Unicode star glyphs (★★★½☆☆) with a numeric suffix. Half-stars use U+00BD. Returns an empty string for `None`. |
| `strip_html(html)` | `-> str` | Reduce comments HTML payloads to safe plain text (tags stripped, entities unescaped, whitespace collapsed). Run any raw HTML through this before terminal or GTK rendering. |
| `tags_to_tree(tags)` | `-> dict[str, Any]` | Build a nested tree from dot-delimited hierarchical tags (`["Fic.Scifi"]` → `{"Fic": {"Scifi": {}}}`). |
| `tag_rollup(counts)` | `-> dict[str, int]` | Roll up leaf/partial dot-path counts into subtree totals: every node carries its own count plus everything below it (render-identical with Hermitage's `_total_count` and Carrel's category union). |
| `isbn_normalize(raw)` | `-> str` | Strip separators, uppercase, keep a trailing `X`. No validity judgement. |
| `isbn_check_digit_is_valid(isbn)` | `-> bool` | Validate an ISBN-10 (mod 11) or ISBN-13 (EAN) check digit; wrong lengths are invalid, not errors. |
| `to_isbn13(raw)` | `-> str \| None` | ISBN-10 → 13 via the 978 prefix with a recomputed check digit; a 13-digit input passes through; anything else `None`. Deliberately no source check-digit validation (the LibraryThing exporter's contract); pair with `isbn_check_digit_is_valid` for strictness. |
| `normalize_author_display(authors, primary_only=False)` | `-> str` | Format a comma-separated author string for display. With `primary_only`, returns only the first author. Returns `"Unknown Author"` for empty input. |
| `author_sort_key(author_sort, primary_only=False)` | `-> str` | Generate a lowercase sort key from `author_sort`. With `primary_only`, splits on `&` and uses the first segment. |

#### Series analysis

| Function | Signature | Description |
|----------|-----------|-------------|
| `detect_series_gaps(indices_str, max_index)` | `-> list[int]` | Given a comma-separated string of series indices and the maximum index, return the sorted list of missing integer entries (e.g. indices `"1,3,5"` with max 5 returns `[2, 4]`). |

#### Image dimensions

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_image_size(filepath)` | `-> tuple[int, int] \| None` | Return `(width, height)` for a JPEG or PNG by sniffing the file signature. Returns `None` for unrecognized formats or read errors. |
| `get_jpeg_size(filepath)` | `-> tuple[int, int] \| None` | Seek through JPEG segment markers to find the SOF frame dimensions. Handles large EXIF/ICC blocks that a fixed header read would miss. |
| `get_png_size(filepath)` | `-> tuple[int, int] \| None` | Read the IHDR chunk of a PNG for its dimensions. |

#### Terminal output

| Function | Signature | Description |
|----------|-----------|-------------|
| `color(text, code)` | `-> str` | Wrap text in ANSI escape codes if stdout is a TTY; return the text unchanged otherwise. Predefined codes: `C_HEADER` (bold yellow), `C_TITLE` (bold cyan), `C_ERR` (bold red), `C_WARN` (bold magenta), `C_DIM` (dim). |

### Config (from `cquarry.config`)

Persistent configuration for database path discovery.

| Name | Type | Description |
|------|------|-------------|
| `VERSION` | `str` | Package version string. |
| `CALIBRE_RATING_SCALE` | `int` | The divisor for Calibre's internal rating (2, since Calibre stores 5 stars as 10). |
| `DEFAULT_DB_PATHS` | `list[str]` | Paths checked during auto-discovery: `./metadata.db`, `~/Calibre Library/metadata.db`, `~/calibre/metadata.db`. |
| `CONFIG_FILE` | `str` | Location of the saved config: `~/.config/cquarry/config.json`. |
| `load_config()` | `-> dict` | Load the config file. Returns `{}` on missing or corrupt files. |
| `save_config(config)` | `-> None` | Write the config dict to disk, creating parent directories as needed. |
| `get_db_path()` | `-> str \| None` | Read the saved `db_path` from config. |
| `set_db_path(path)` | `-> None` | Save an absolute, expanded `db_path` to config. |

### Package metadata

```python
import cquarry

print(cquarry.__version__)  # "1.8.0"
```

### Writes (`cquarry.write`) — opt-in

| Member | Description |
|--------|-------------|
| `WritableCalibreDB(db_path)` | Read/write handle. Registers Calibre's trigger dependencies (`title_sort()`, `uuid4()`, `PYNOCASE`) before any statement; context-manager supported. |
| `register_udfs(conn)` | Register the trigger-required SQL functions/collations on any read-write connection. |
| `update_title(book_id, new_title)` | Rename with refreshed sort key and `last_modified`. |
| `add_tag(book_id, tag)` / `remove_tag(book_id, tag)` | Idempotent tag mutation following Calibre's link-table sequence; returns whether state changed. |
| `set_identifier(book_id, type, val)` / `set_identifiers(book_id, pairs)` | EAV upserts honoring `UNIQUE(book, type)`; `None` deletes. |
| `set_authors(book_id, names)` | Replace the author list; recomputes `books.author_sort` from per-author sort keys (" & "-joined); prunes orphans. |
| `set_series(book_id, name \| None, index=None)` | Assign/clear series + `series_index` (defaults 1.0 fresh, preserves on reassign). |
| `set_publisher(book_id, name \| None)` | Replace/clear publisher; case-insensitive match; orphans pruned. |
| `set_rating(book_id, stars \| None)` | 0–5 stars stored as ×2; UNIQUE(rating) rows deduplicated via find-or-create. |
| `set_languages(book_id, codes \| None)` | Replace languages; English names canonicalized to ISO 639-2 via the search engine's map. |
| `set_comments(book_id, text \| None)` | 1:1 upsert/clear of the comments HTML row. |
| `set_pubdate(book_id, value)` | Publication-date setter accepting `str` / `date` / `datetime` / `None` (sentinel); stored as Calibre TEXT in UTC. |
| `set_custom_column(book_id, label, value)` | Generic custom-column writer: storage layout auto-detected (link-table vs direct), enumerations validated against `display.enum_values`, tristate bools accepted, non-editable/composite columns raise. |
| `add_format(book_id, fmt, name, size)` / `remove_format(book_id, fmt)` | Register/drop `data` rows (the file itself is the caller's responsibility). |
| `set_has_cover(book_id, has_cover)` | Toggle the catalogued flag. |
| `remove_book(book_id)` | Full book removal: custom columns (both patterns) + dirtied queues cleaned, cascade trigger fires, orphaned entities pruned. Irreversible. |

Every state-changing mutation also inserts the book id into `metadata_dirtied` (`INSERT OR IGNORE`; the table's `UNIQUE(book)` keeps it one row per book), which is what tells Calibre to regenerate that book's sidecar `.opf` and re-push metadata to wireless readers on its next startup. No-op mutations queue nothing, and databases predating the table keep working (the insert is guarded by a cached existence check).

### Composed reads (from `cquarry.db`)

| Method | Returns | Description |
|--------|---------|-------------|
| `get_book_dossier(book_id, *, include_comments=False)` | `dict \| None` | The composed deep fetch detail views hand-assembled before this existed: `book` (standard row), `cover_path` (`get_cover_path()` defaults; the row's `has_cover` distinguishes catalogued-but-missing), `formats`, `custom_columns` keyed `#label` as `{name, datatype, value}` (values exactly as `field()` yields; comments-typed columns stay raw HTML), `annotations`, `reading_positions`, `plugin_data`, `conversion_overrides`, and `comments` (`{html, plain}`) only when flagged. `None` for unknown books. |
| `format_path_index()` | `dict[str, int]` | Every catalogued format file path → book id, one `data ⋈ books` query, paths built exactly as `get_format_path()` builds them, `normcase(normpath())` keys, cached. |
| `find_book_by_path(path)` | `int \| None` | Reverse the index: the book owning this file, tolerant of relative spellings and redundant separators. `None` when nothing catalogued resolves there. |

### Integrity (from `cquarry.integrity`)

Pure predicates over the cached rows — the one shared definition of "incomplete"
( mined from CalibreQuarry's `--audit` frontend). No SQL of their own; the two
cover-file checks ride `get_cover_path()` + `get_image_size()`. Every id list is
sorted.

| Function | Returns | Description |
|----------|---------|-------------|
| `find_untagged(db)` | `list[int]` | Books carrying no tags. |
| `find_unrated(db)` | `list[int]` | Books with no rating (`None` or `0`). |
| `find_authorless(db)` | `list[int]` | Books with no authors, or only the `Unknown` placeholder. |
| `find_formatless(db)` | `list[int]` | Books with no catalogued format rows. |
| `find_coverless(db)` | `list[int]` | Books whose `has_cover` flag is unset (the catalogued answer). |
| `find_missing_cover_files(db)` | `list[int]` | Flag set but no cover file resolves on disk (empty `books.path` skipped — nowhere to look). |
| `find_deprecated_formats(db, formats)` | `list[int]` | Books whose whole format set sits inside the caller's deprecated set (case-insensitive). cquarry owns the subset mechanism; what counts as deprecated is a curation opinion. Formatless books excluded. |
| `find_low_res_covers(db, min_dimension=500)` | `dict[int, tuple[int, int]]` | `{id: (w, h)}` for resolvable, parseable covers under the dimension floor. Missing files are `find_missing_cover_files`' answer; unreadable images are skipped. |
| `find_duplicate_books(db)` | `dict[tuple[str, str], list[int]]` | `(title.lower(), primary_author.lower())` groups with more than one member. |
| `find_series_gaps(db)` | `dict[str, list[int]]` | `{series: [missing indices]}` composing `get_all_series()` + `detect_series_gaps()`. |

### Analytics (from `cquarry.analytics`)

Derivations promoted from CalibreQuarry's `--analytics` frontend so every
consumer shares them; the frontend keeps formatting. APIs that already exist
(`get_format_stats`, `get_entities`, `get_tag_counts`) deliberately do not
appear here.

| Function | Returns | Description |
|----------|---------|-------------|
| `addition_timeline(db, granularity="month")` | `dict[str, int]` | Books added per calendar bucket, chronological: `"YYYY-MM"` (or `"YYYY"` with `granularity="year"`). Books without a timestamp are skipped; anything but `month`/`year` raises `ValueError`. |
| `author_stats(db)` | `list[dict]` | Per primary author: `{author, book_count, avg_rating, rated_count, formats}` — star-scale average over rated books only (`0.0` when none), sorted count-descending then name; authorless books skipped. |
| `rating_distribution(db)` | `dict[float \| str, int]` | Books per star rating, ascending on the half-step scale, `"unrated"` last. |
| `vl_overlap(db, names=None)` | `dict[tuple[str, ...], list[int]]` | Books shared by two or more virtual libraries, wing names sorted in each key. `names` restricts the wings (unknown names raise through `resolve_vl`); single-wing books appear nowhere. |
