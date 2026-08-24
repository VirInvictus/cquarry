<div align="center">
  <img src="logo.svg" width="96" height="96" alt="cquarry logo"/>
  <h1>cquarry</h1>
  <p>Canonical read-only database layer and search grammar engine for Calibre libraries.</p>
</div>

This library powers [CalibreQuarry](https://github.com/VirInvictus/CalibreQuarry) (CLI/TUI), [Hermitage](https://github.com/VirInvictus/Hermitage) (GTK4 gallery), and [Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) (web reader). By centralizing the search grammar parser and metadata access, cquarry ensures that virtual library definitions and search queries evaluate identically across every frontend in the ecosystem.

## Features

- **Direct SQLite access.** No `calibredb` binary required, no Calibre Python initialization overhead.
- **Lock-safe snapshots.** Automatically detects if Calibre holds an exclusive write lock on `metadata.db` and routes queries through a temporary WAL-consistent copy.
- **Full search grammar parity.** A recursive-descent parser faithfully porting Calibre's native search capabilities: boolean logic, field prefixes, date math, hierarchical tags, custom columns, identifiers, and nested virtual library cross-references.
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

    # Inspect custom columns
    cols = db.get_custom_columns()
    status = db.load_custom_column("Reading Status")
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
| `get_all_books()` | `list[dict[str, Any]]` | Every book in the library, pre-hydrated with `authors`, `tags`, `series`, `rating`, `publisher`, `languages`, `formats`, `title_sort`, `author_sort`, `timestamp`, `pubdate`, `last_modified`, `has_cover`, `series_index`, and `path`. Results are cached after the first call. |
| `get_identifiers(book_id)` | `dict[str, str]` | All identifiers for a book (e.g. `isbn`, `amazon`, `lcc`), keyed by type. |
| `get_all_tags()` | `list[str]` | Every distinct tag name, sorted alphabetically. |
| `get_tag_counts()` | `list[tuple[str, int]]` | `(tag_name, book_count)` pairs, sorted by tag name. |
| `get_all_series()` | `list[dict[str, Any]]` | Per-series rollups: `name`, `book_count`, `indices` (comma-separated), `max_index`, `titles` (comma-separated, sorted by index). |
| `get_custom_columns()` | `dict[str, dict[str, Any]]` | Metadata for all user-defined custom columns, keyed by display name. Each value contains `id`, `label`, `name`, `datatype`, and `is_multiple`. |
| `load_custom_column(col_name)` | `dict[int, Any]` | Values for a specific custom column (by display name), returned as `{book_id: value}`. Normalized columns (text, enumeration, series) are read via their link table; direct columns (int, float, bool, datetime, comments) are read from the value table. Multi-valued columns return comma-separated strings. Raises `ValueError` if the column does not exist. |
| `get_virtual_libraries()` | `dict[str, str]` | Virtual library names mapped to their Calibre search expressions, read from the `preferences` table. Cached after the first call. |
| `count_books()` | `int` | Total book count. Uses the cache if available; otherwise issues a `SELECT COUNT(*)`. |

#### Search and virtual library resolution

| Method | Returns | Description |
|--------|---------|-------------|
| `search(query)` | `set[int]` | Parse and evaluate a Calibre search expression, returning matching book IDs. An empty query returns all IDs. |
| `resolve_vl(vl_name)` | `set[int]` | Resolve a virtual library by name to its set of book IDs. Raises `ValueError` if the name is not found. |

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

Any object implementing these four methods can serve as a search backend:

| Method | Signature | Contract |
|--------|-----------|----------|
| `all_ids()` | `-> set[int]` | Return every book ID in the collection. |
| `field(book_id, location)` | `-> Any` | Return a book's value for a canonical location. See datatype contract below. |
| `vl_expression(name)` | `-> str \| None` | Return a virtual library's search expression, or `None` if unknown. |
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
| `calibre_rating_to_stars(rating)` | `-> float \| None` | Convert Calibre's internal 0-10 scale to 0.0-5.0 stars. Returns `None` for unrated (0 or `None`). |
| `format_stars(rating)` | `-> str` | Render a 0.0-5.0 rating as Unicode star glyphs (★★★½☆☆) with a numeric suffix. Half-stars use U+00BD. Returns an empty string for `None`. |
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

print(cquarry.__version__)  # "1.0.0"
```

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
| `authors` | `author` | text_multi | |
| `author_sort` | | text | |
| `series` | | text | |
| `publisher` | | text | |
| `tags` | `tag` | hierarchical | Anchored prefix: `Foo` matches `Foo` and `Foo.*` |
| `comments` | `comment` | text | |
| `rating` | | rating | Numeric; `true`/`false` for presence |
| `series_index` | | float | |
| `formats` | `format` | text_multi | |
| `languages` | `language`, `lang` | text_multi | |
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
| `all` | *(bare terms)* | | Searches title, authors, author_sort, series, publisher, tags, comments |

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
- **GPM templates.** `@...:` template expressions are not evaluated.
- **Saved searches.** `search:"name"` interpolation is not yet supported (planned in cquarry's roadmap).
- **Hierarchical tag matching.** `tags:` uses cquarry's anchored match (`Foo` matches `Foo` and `Foo.*`) rather than Calibre's raw substring default. This is a long-standing project invariant.

## Development

```sh
python -m pytest tests/           # full suite
python -m pytest tests/ -v        # verbose
```

Three test modules: `test_db.py` (CalibreDB against a fixture database), `test_helpers.py` (utility functions), `test_search.py` (parser, matcher, and integration tests).

See [spec.md](spec.md) for the full contract and [roadmap.md](roadmap.md) for planned work.

## License

MIT. See [LICENSE](LICENSE).
