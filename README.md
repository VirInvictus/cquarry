<div align="center">
  <img src="logo.svg" width="96" height="96" alt="cquarry logo"/>
  <h1>cquarry</h1>
  <p>Canonical read-only database layer and search grammar engine for Calibre libraries.</p>
</div>

This library powers [CalibreQuarry](https://github.com/VirInvictus/CalibreQuarry) (CLI/TUI), [Hermitage](https://github.com/VirInvictus/Hermitage) (GTK4 gallery), and [Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) (web reader). By centralizing the search grammar parser and metadata access, cquarry ensures that virtual library definitions and search queries evaluate identically across every frontend in the ecosystem.

## Features

- **Direct SQLite access.** No `calibredb` binary required, no Calibre Python initialization overhead.
- **Lock-safe snapshots.** Automatically detects if Calibre holds an exclusive write lock on `metadata.db` and routes queries through a temporary WAL-consistent copy.
- **Full search grammar parity.** A recursive-descent parser faithfully porting Calibre's native search capabilities: boolean logic, field prefixes, date math (hyphen *and* slash separators), hierarchical tags with `.`/`..` component modifiers on every text field, custom columns, identifiers, saved-search interpolation (`search:"Name"`), multi-valued count operators (`tags:#>3`), language canonicalization (`languages:English` → `eng`), and nested virtual library cross-references.
- **Native page counts.** The `pages:` location reads Calibre's own `books_pages_link` table first (maintained by upstream's CountPages integration) and falls back to an int custom column labelled `pages`; counts also ride along in every book row.
- **Metadata portability.** Read e-reader annotations, per-device reading progress, third-party plugin data, and conversion profiles; sanitize comments HTML for display.
- **Opt-in write path.** `cquarry.write.WritableCalibreDB` offers trigger-safe title/tag/identifier mutations in a separate module the read-only API can never touch — every mutation bumps `last_modified` *and* queues the book in `metadata_dirtied`, so Calibre regenerates the sidecar OPF (and re-pushes to wireless readers) on its next run.
- **Context manager.** `CalibreDB` supports `with` statements for automatic cleanup of snapshot files.
- **Zero dependencies.** Pure Python 3.14+ stdlib (`sqlite3`, `re`, `json`, `unicodedata`).

## Usage

```python
from cquarry.db import CalibreDB

# Open a library (creates a snapshot if Calibre has the lock)
with CalibreDB("~/Calibre Library/metadata.db") as db:
    # Fetch all books with pre-joined metadata
    books = db.get_all_books()

    # Search using Calibre's native grammar
    sci_fi = db.search("tags:Fic.SciFi and rating:>=4")
    print(f"Found {len(sci_fi)} highly rated Sci-Fi books.")

    # Resolve a virtual library to a set of book IDs
    wing = db.resolve_vl("To Read")

    # Interpolate a saved search straight from Calibre's preferences
    award_winners = db.search('search:"Award Winners"')

    # Inspect custom columns
    cols = db.get_custom_columns()
    status = db.load_custom_column("Reading Status")

    # Single-entity helpers (no whole-library scan)
    book = db.get_book(42)
    epub = db.get_format_path(42, "EPUB")

    # Metadata portability
    highlights = db.get_annotations(42)
    progress = db.get_last_read_positions(42)
    wordcounts = db.get_plugin_data(name="wordcount")
```

Writes live behind an explicit opt-in import:

```python
from cquarry.write import WritableCalibreDB

with WritableCalibreDB("~/Calibre Library/metadata.db") as wdb:
    wdb.add_tag(42, "Audited")
    wdb.set_identifier(42, "isbn", "9780123456789")

# Every mutation queues an OPF regeneration; check what Calibre will resync:
with CalibreDB("~/Calibre Library/metadata.db") as db:
    print(db.get_dirtied_books())  # e.g. [42]
```

## Installation

```sh
pip install git+https://github.com/VirInvictus/cquarry.git
```

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
| `get_all_books()` | `list[dict[str, Any]]` | Every book in the library, pre-hydrated with `authors`, `tags`, `series`, `rating`, `publisher`, `languages`, `formats`, `title_sort`, `author_sort`, `timestamp`, `pubdate`, `last_modified`, `has_cover`, `series_index`, `size`, `pages`, and `path`. `authors`, `tags`, `languages`, and `formats` are exposed natively as `list[str]` arrays. Results are cached after the first call. |
| `get_book(book_id)` | `dict[str, Any] \| None` | Fetch one hydrated record (same shape as a `get_all_books()` row) without scanning the library. |
| `search_books(query)` | `list[dict[str, Any]]` | Evaluate a search expression and return the hydrated matching books. |
| `get_format_path(book_id, fmt, verify=True)` | `str` | Absolute filesystem path for a book's format file, built from the original DB location. Raises `ValueError` for unknown book/format, `FileNotFoundError` when `verify` is set and the file is missing. |
| `get_formats(book_id)` | `dict[str, dict[str, Any]]` | Per-format detail: `{fmt: {path, size_bytes, name}}` (path unverified; size from the catalogued uncompressed size). `{}` for unknown books. |
| `get_cover_path(book_id, verify=True)` | `str \| None` | Resolved cover image path (`cover.jpg`, falling back to `cover.png`) from the original DB location. With `verify` (default) returns None when no file exists on disk; without it returns the `.jpg` path unconditionally. Raises `ValueError` for unknown books. |
| `get_library_uuid()` | `str \| None` | The library's identity UUID (`library_id` table) — stable across moves/restores, unlike per-book uuids; the right cache key for per-library state. None on very old schemas. |
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

All five return `[]` on databases whose schema predates the tables.

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

print(cquarry.__version__)  # "1.3.0"
```

### Writes (`cquarry.write`) — opt-in

| Member | Description |
|--------|-------------|
| `WritableCalibreDB(db_path)` | Read/write handle. Registers Calibre's trigger dependencies (`title_sort()`, `uuid4()`, `PYNOCASE`) before any statement; context-manager supported. |
| `register_udfs(conn)` | Register the trigger-required SQL functions/collations on any read-write connection. |
| `update_title(book_id, new_title)` | Rename with refreshed sort key and `last_modified`. |
| `add_tag(book_id, tag)` / `remove_tag(book_id, tag)` | Idempotent tag mutation following Calibre's link-table sequence; returns whether state changed. |
| `set_identifier(book_id, type, val)` / `set_identifiers(book_id, pairs)` | EAV upserts honoring `UNIQUE(book, type)`; `None` deletes. |

Every state-changing mutation also inserts the book id into `metadata_dirtied` (`INSERT OR IGNORE`; the table's `UNIQUE(book)` keeps it one row per book), which is what tells Calibre to regenerate that book's sidecar `.opf` and re-push metadata to wireless readers on its next startup. No-op mutations queue nothing, and databases predating the table keep working (the insert is guarded by a cached existence check).

## Search Grammar

cquarry implements a three-stage pipeline (lexer, recursive-descent parser, candidate-set evaluator) ported from Calibre's `search_query_parser.py` and `calibre/db/search.py`.

### Operators

| Syntax | Meaning |
|--------|---------|
| `and` | Logical AND (also implicit: `title:foo author:bar` is `title:foo and author:bar`) |
| `or` | Logical OR |
| `not` | Logical NOT |
| `( )` | Grouping |

### Match prefixes

| Prefix | Meaning |
|--------|---------|
| *(none)* | Substring match (case- and accent-folded) |
| `=` | Exact match (case- and accent-folded) |
| `~` | Regular expression (stdlib `re`, case-insensitive) |
| `^` | Accent-folded substring |
| `\` | Escape the next character (treat literally) |

### Field locations

| Location | Aliases | Datatype | Notes |
|----------|---------|----------|-------|
| `title` | | text | |
| `title_sort` | | text | |
| `authors` | `author` | text_multi | |
| `author_sort` | | text | |
| `series` | | text | |
| `series_sort` | | text | `"Series [index]"` |
| `publisher` | | text | |
| `tags` | `tag` | hierarchical | Anchored prefix: `Foo` matches `Foo` and `Foo.*` |
| `comments` | `comment` | text | |
| `rating` | | rating | Numeric; `true`/`false` for presence |
| `series_index` | | float | |
| `formats` | `format` | text_multi | |
| `languages` | `language`, `lang` | text_multi | English names canonicalized to ISO codes |
| `size` | | float (bytes) | Total across formats; `k`/`m`/`g` suffixes |
| `pages` | | int | Native `books_pages_link` first; `#pages` custom column fallback |
| `pubdate` | | date | |
| `timestamp` | `date` | date | |
| `last_modified` | | date | |
| `identifiers` | `identifier`, `ids` | identifiers | Keypair search; see below |
| `isbn` | | identifiers | Shorthand for `identifiers:=isbn:<value>` |
| `cover` | | bool | |
| `id` | | int | |
| `uuid` | | text | |
| `#<label>` | | *(per column)* | Custom columns by label |
| `vl` | | virtual library | Cross-reference: `vl:"Wing Name"` |
| `search` | | saved search | Cross-reference: `search:"Saved Name"` |
| `all` | *(bare terms)* | | Searches title, authors, author_sort, series, publisher, tags, comments + custom text columns |

Multi-valued locations additionally accept the count operator: `tags:#>3`, `identifiers:#=0`, `formats:#<5`.

### Date queries

```
pubdate:>30daysago
timestamp:<2024-06
last_modified:=today
pubdate:>=yesterday
timestamp:thismonth
pubdate:2024          # matches any date in 2024
pubdate:2024-06       # matches any date in June 2024
pubdate:2024-06-15    # matches that exact day
```

### Identifier queries

```
identifiers:isbn:true          # has any ISBN
identifiers:amazon:B0...       # specific Amazon ASIN
isbn:9780123456789             # shorthand for identifiers:=isbn:9780123456789
identifiers:true               # has any identifier at all
```

### Documented deviations from Calibre

- **Regex engine.** `~` uses stdlib `re`, not Calibre's third-party `regex` module (`VERSION1`/`\X` are unavailable; otherwise compatible).
- **Accent folding.** Uses `unicodedata` NFKD decomposition rather than ICU collation, so punctuation-insensitivity is not reproduced.
- **GPM templates.** `@...:` template expressions tokenize for parse parity but are not evaluated.
- **GUI-state locations.** `marked`, `ondevice`, and `in_tag_browser` exist only inside Calibre's own UI session and are not implemented.
- **Hierarchical tag matching.** `tags:` uses cquarry's anchored match (`Foo` matches `Foo` and `Foo.*`) rather than Calibre's raw substring default. This is a long-standing project invariant.
- **`series_sort` format.** Computed as `"Series [index]"`.

## Development

```sh
python -m pytest tests/           # full suite
python -m pytest tests/ -v        # verbose
```

Run with `PYTHONPATH=src` to exercise this checkout rather than any installed copy.

Four test modules: `test_db.py` (CalibreDB against fixture databases), `test_helpers.py` (utility functions), `test_search.py` (parser, matcher, and integration tests), `test_write.py` (opt-in write module with trigger-hazard fixtures).

See [spec.md](spec.md) for the full contract and [roadmap.md](roadmap.md) for planned work.

## License

MIT. See [LICENSE](LICENSE).
