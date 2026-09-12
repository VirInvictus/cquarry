# cquarry API reference

The full per-method reference. The [README](README.md) keeps the hero, the
quick-starts, and the search grammar; everything callable lives here.

**Version:** 1.17.0

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


#### Properties

- `db.db_path` (`str`): The normalized absolute path to `metadata.db`.
- `db.conn` (`sqlite3.Connection`): The active SQLite connection object.

#### Core queries

| Method | Returns | Description |
|--------|---------|-------------|
| `get_all_books()` | `list[dict[str, Any]]` | Every book in the library, pre-hydrated with `authors`, `author_sorts`, `author_links` (parallel arrays), `tags`, `series`, `rating`, `publisher`, `languages`, `formats`, `title_sort`, `author_sort`, `timestamp`, `pubdate`, `last_modified`, `has_cover`, `series_index`, `size`, `pages`, `uuid`, `identifiers`, and `path`. `authors`, `tags`, `languages`, and `formats` are exposed natively as `list[str]` arrays. Results are cached after the first call. |
| `get_book(book_id, include_comments=False)` | `dict[str, Any] \| None` | Fetch one hydrated record (same shape as a `get_all_books()` row, including `size`, `uuid`, and `identifiers`) without scanning the library. |
| `get_comments(book_id=None)` | `dict[int, str]` | Raw comments HTML keyed by book id. Pass `book_id` to scope the read, otherwise returns all catalogued comments. |
| `get_format_stats()` | `dict[str, dict[str, int]]` | Per-format aggregates: `{fmt: {"count": int, "bytes": int}}`. `count` is the number of books with the format, `bytes` is the total uncompressed size. |
| `field(book_id, location)` | `Any` | Return a book's value for a canonical location. Series custom columns expose their index as a float at `#<label>_index` (the link table's `extra`). |
| `all_ids()` | `set[int]` | Return the full set of all book IDs in the library. |
| `vl_expression(name)` | `str \| None` | Case-insensitive lookup of a virtual library search expression. |
| `saved_search(name)` | `str \| None` | Case-insensitive lookup of a saved search expression. |
| `custom_locations()` | `dict[str, str]` | Return `{location_token: datatype}` for all user custom columns. |
| `grouped_search_terms()` | `dict[str, list[str]]` | Return grouped search terms *(optional hook)*. |
| `user_categories()` | `dict[str, list]` | Return user categories for `@Name` searches *(optional hook)*. |
| `search_books(query)` | `list[dict[str, Any]]` | Evaluate a search expression and return the hydrated matching books. |
| `get_format_path(book_id, fmt, verify=True)` | `str` | Absolute filesystem path for a book's format file, built from the original DB location. Raises `ValueError` for unknown book/format, `FileNotFoundError` when `verify` is set and the file is missing. |
| `get_formats(book_id)` | `dict[str, dict[str, Any]]` | Per-format detail: `{fmt: {path, size_bytes, name}}` (path unverified; size from the catalogued uncompressed size). `{}` for unknown books. |
| `get_cover_path(book_id, verify=True)` | `str \| None` | Resolved cover image path (`cover.jpg`, falling back to `cover.png`) from the original DB location. With `verify` (default) returns None when no file exists on disk; without it returns the `.jpg` path unconditionally. Raises `ValueError` for unknown books. |
| `get_library_uuid()` | `str \| None` | The library's identity UUID (`library_id` table); stable across moves/restores, unlike per-book uuids; the right cache key for per-library state. None on very old schemas. |
| `get_entities(kind)` | `list[dict[str, Any]]` | Entity rows for `authors` / `series` / `publishers` / `tags` / `languages` / `ratings`: `{id, name, sort, link, count}`, name-sorted (ratings carry the half-star integer as `name`). Raises `ValueError` for unknown kinds. |
| `get_preference(key, default=None)` | `Any` | Typed read of any Calibre preference from the `preferences` table (JSON decoded where it parses). |
| `get_field_metadata()` | `dict[str, Any]` | The rich `field_metadata` preference: per-custom-column GUI metadata keyed by label. |
| `get_grouped_search_terms()` | `dict[str, list[str]]` | Grouped search terms driving `GroupName:query` expansion in the search engine. |
| `get_user_categories()` | `dict[str, list[dict[str, Any]]]` | User-defined tag-browser categories (name -> member descriptors). |
| `get_tag_browser_state()` | `dict[str, Any]` | `{"order": [...], "hidden": [...]}` from the `tag_browser_*` preferences; mirror Calibre's browse-sidebar layout. |
| `get_identifiers(book_id)` | `dict[str, str]` | All identifiers for a book (e.g. `isbn`, `amazon`, `lcc`), keyed by type. Empty on schemas predating the table. |
| `get_all_tags()` | `list[str]` | Every distinct tag name, sorted alphabetically. |
| `get_tag_counts()` | `list[tuple[str, int]]` | `(tag_name, book_count)` pairs, sorted by tag name. |
| `get_all_series()` | `list[dict[str, Any]]` | Per-series rollups: `name`, `book_count`, `indices` (comma-separated), `max_index`, `titles` (comma-separated, sorted by index). |
| `get_custom_columns()` | `dict[str, dict[str, Any]]` | Metadata for all user-defined custom columns, keyed by display name (the historical key; since 1.9.0 column *lookup* that accepts `#label` or a bare label goes through `find_custom_column()` / `load_custom_column()`). Each value contains `id`, `label`, `name`, `datatype`, `is_multiple`, `editable`, `normalized`, and `display` (a decoded JSON config dict). |
| `find_custom_column(key)` | `dict[str, Any] \| None` | One custom-columns record by `#label`, bare label, or display name. A leading `#` matches the label only (never ambiguous); otherwise an exact display-name match wins (the historical key) and a bare label is the graceful fallback; label matching is case-insensitive, mirroring the write module. Returns `None` when nothing matches. |
| `list_books(*, ids=None, sort="sort", descending=False, offset=0, limit=None)` | `list[dict[str, Any]]` | Paginated, sorted listing over the cached rows (1.10). `ids` restricts (order still comes from `sort`); `sort` is one key or a sequence of keys (primary first, one direction for all) drawn from `sort`/`title`/`timestamp`/`pubdate`/`rating`/`series_index`/`author_sort`/`series`/`id`; None-valued keys sort last regardless of direction; `offset`/`limit` slice after sorting. Pure — no SQL of its own. Raises `ValueError` on unknown keys or negative offsets/limits. |
| `load_custom_column(col_name)` | `dict[int, Any]` | Values for one custom column, addressed by `#label`, bare label, or display name (since 1.9.0; resolution via `find_custom_column()`), returned as `{book_id: value}`. Normalized columns (text, enumeration, series, rating) are read via their link table; direct columns (int, float, bool, datetime, comments) are read from the value table. Multi-valued columns return a native `list[str]` (since 1.16.0; the old comma-joined string made stored values like `Doe, John` round-trip as phantom values; do not `.split(",")` these, same rule as book rows); rating-typed columns surface the same 0-5 star scale as the builtin rating (Calibre's stored 0-10 converted), so `field(book, "#myrat")` and `field(book, "rating")` compare alike. Raises `ValueError` if the column does not exist. |
| `custom_column_links(col_name)` | `dict[int, str]` | The normalized value table's `link` URL column (upstream schema_upgrades.py:836; no Calibre UI populates it): `{book_id: url}`, filled as a side effect of `load_custom_column`. Empty until then, and for direct-storage columns, unknown columns, and schemas predating the column. |
| `get_virtual_libraries()` | `dict[str, str]` | Virtual library names mapped to their Calibre search expressions, read from the `preferences` table. Cached after the first call. A corrupt or non-dict stored payload degrades to `{}` (since 1.16.0; it used to crash every read touching virtual libraries). |
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
| `get_dirtied_books()` | `list[int]` | Book ids queued for OPF resync in `metadata_dirtied`; i.e. what Calibre will regenerate/push at its next startup. Sorted, deduplicated; read-only (clearing the queue remains Calibre's job). |
| `get_annotations_dirtied_books()` | `list[int]` | The annotations sibling queue (`annotations_dirtied`): ids whose highlights/bookmarks Calibre will push to devices. Same read-only contract. |
| `get_feeds()` | `list[dict[str, Any]]` | Registered news-download recipes from the `feeds` table: `[{id, title, script}]`. |
| `get_page_metadata(book_id=None)` | `dict[int, dict[str, Any]]` | The full `books_pages_link` row per book: `{book: {pages, algorithm, format, format_size, timestamp, needs_scan}}` -- provenance for the displayed count; `needs_scan=True` means Calibre has queued a recount. `{}` on schemas predating the native table. |
| `get_book_text(book_id, fmt)` | `dict[str, Any] \| None` | One format's extracted plain text from the `full-text-search.db` sidecar: the full `books_text` row (`searchable_text`, `format_hash`, `err_msg`, ...). `fmt` is case-insensitive; None when the sidecar is absent or the pair has no row. The FTS5 index tables are never touched (Calibre-only custom tokenizer). |
| `get_text_extractions(book_id=None)` | `list[dict[str, Any]]` | Bulk extraction-status rows WITHOUT the texts (`searchable_text` omitted; it can be megabytes per format): `err_msg` audit and format-hash change detection. `[]` when the sidecar is absent. |
| `search_book_text(query, *, fmt=None, ids=None)` | `dict[int, set[str]]` | Python-side content search over the sidecar's `searchable_text`: case- and accent-folded substring match returning `{book_id: {FORMAT, ...}}`. Linear scan by design (no FTS5 outside Calibre). Empty query raises `ValueError`. |
| `get_tag_browser_counts()` | `dict[str, list[dict[str, Any]]]` | Calibre's own browse-sidebar rollups from the `tag_browser_*` views: `{category: [{id, name, count, avg_rating, sort}]}`, custom columns rekeyed to `#label`. The `filtered_*` variants (GUI-state `books_list_filter()`) are skipped. |

All of these degrade to `[]`/`{}` on databases whose schema predates the tables; the FTS sidecar reads likewise when `full-text-search.db` is absent.

#### Search and virtual library resolution

| Method | Returns | Description |
|--------|---------|-------------|
| `search(query)` | `set[int]` | Parse and evaluate a Calibre search expression, returning matching book IDs. An empty query returns all IDs. Raises `ParseException` for unknown virtual libraries or saved searches. |
| `resolve_vl(vl_name)` | `set[int]` | Resolve a virtual library by name to its set of book IDs. Case-insensitive, with surrounding padding and quotes stripped (since 1.16.0, matching the `vl_expression()` lookup); raises `ValueError` if the name is not found. |
| `resolve_saved_search(name)` | `set[int]` | Resolve a saved-search name to its set of book IDs. Case-insensitive, with surrounding padding and quotes stripped (since 1.16.0, matching the `saved_search()` lookup); raises `ValueError` if the name is not found. |

#### Lifecycle

| Method | Description |
|--------|-------------|
| `close()` | Close the database connection and remove any temporary snapshot files. |
| `refresh()` | Drop every cache (rows, ids, search view and engine, preferences, custom columns, path index) so subsequent reads re-query the database; the coherence boundary for long-lived holders after an external write (since 1.16.0). |
| `__enter__()` / `__exit__()` | Context manager support. Calls `close()` on exit. |

#### Composed reads

| Method | Returns | Description |
|--------|---------|-------------|
| `get_book_dossier(book_id, *, include_comments=False)` | `dict[str, Any] \| None` | The composed deep fetch detail views hand-assembled before this existed: `book` (standard row), `cover_path` (`get_cover_path()` defaults; the row's `has_cover` distinguishes catalogued-but-missing), `formats`, `custom_columns` keyed `#label` as `{name, datatype, value}` (values exactly as `field()` yields; comments-typed columns stay raw HTML), `annotations`, `reading_positions`, `plugin_data`, `conversion_overrides`, and `comments` (`{html, plain}`) only when flagged. `None` for unknown books. |
| `format_path_index()` | `dict[str, int]` | Every catalogued format file path → book id, one `data ⋈ books` query, paths built exactly as `get_format_path()` builds them, `normcase(normpath())` keys, cached. |
| `find_book_by_path(path)` | `int \| None` | Reverse the index: the book owning this file, tolerant of relative spellings and redundant separators. `None` when nothing catalogued resolves there. |
| `find_candidate_duplicates(title, authors, isbn=None)` | `list[dict[str, Any]]` | Library-side duplicate screening for the creation path (since 1.17.0): ISBN rule first (separator/case-insensitive match on the book's `isbn` identifier), then title+author (normalized title -- folded, subtitle and leading article scrubbed -- plus folded first author, both exact). Returns `{"id", "matched_by": "isbn" \| "title_author"}` per match, id-sorted; a book matching both reports `isbn`. Pure over the cached rows. |
| `export_rows(*, ids=None, include_custom=True)` | `list[dict[str, Any]]` | Flat one-dict-per-book rows for exporters and CSV writers (since 1.17.0): the hydrated row with every non-composite custom column flattened in as a `#label` key (`None` when the book has no value, so the key set is uniform). `rating` stays the raw internal 0-10 value and `pubdate` the raw TEXT; conversion is the renderer's job. `ids` restricts. |

### `SearchEngine` (from `cquarry.search`)


#### Constants

| Name | Returns | Description |
|------|---------|-------------|
| `CONTAINS` | `int` | Match kind 0. |
| `EQUALS` | `int` | Match kind 1. |
| `REGEXP` | `int` | Match kind 2. |
| `ACCENT` | `int` | Match kind 3. |
| `DT_TEXT` ... `DT_VL` | `str` | Datatype constants. |

#### Helpers

| Function | Returns | Description |
|----------|---------|-------------|
| `canonical_language(name)` | `str` | Canonicalize one language name to its ISO 639-2 code (e.g. `English` -> `eng`). Unknown tokens pass through untouched. |

The search engine can be used standalone by implementing the `MetadataProvider` protocol. `CalibreDB` implements this protocol, so most consumers never touch `SearchEngine` directly.

```python
from cquarry.search import SearchEngine, MetadataProvider

engine = SearchEngine(provider)  # provider implements MetadataProvider
results = engine.search("tags:Fiction and rating:>3")
```



#### `MetadataProvider` protocol

Any object implementing these methods can serve as a search backend:

| Method | Returns | Description |
|--------|-----------|----------|
| `all_ids()` | `set[int]` | Return every book ID in the collection. |
| `field(book_id, location)` | `Any` | Return a book's value for a canonical location. See datatype contract below. |
| `vl_expression(name)` | `str \| None` | Return a virtual library's search expression, or `None` if unknown. |
| `saved_search(name)` | `str \| None` | Return a saved search's expression, or `None` if unknown. |
| `grouped_search_terms()` | `dict[str, list[str]]` | Return grouped search terms *(optional hook)*. |
| `user_categories()` | `dict[str, list]` | Return user categories for `@Name` searches *(optional hook)*. |
| `custom_locations()` | `dict[str, str]` | Return `{location_token: datatype}` for custom columns (e.g. `{"#read": "bool"}`). |

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

| Function | Returns | Description |
|----------|-----------|-------------|
| `find_db(explicit=None)` | `str` | Locate `metadata.db` through a resolution chain: explicit argument, saved config (`~/.config/cquarry/config.json`), default paths (`./metadata.db`, `~/Calibre Library/metadata.db`, `~/calibre/metadata.db`), then an interactive TTY prompt. Raises `FileNotFoundError` if nothing is found. |
| `title_sort(title)` | `str` | Generate Calibre's title sort key by moving leading articles ('The ', 'A ', 'An ') to the end of the string. |
| `db_uri_ro(path)` | `str` | Build a percent-encoded read-only SQLite `file:` URI. Handles paths containing `?` or `#` that would otherwise be parsed as URI syntax. |

#### Constants

| Name | Returns | Description |
|------|---------|-------------|
| `C_HEADER` | `str` | ANSI bold yellow. |
| `C_TITLE` | `str` | ANSI bold cyan. |
| `C_ERR` | `str` | ANSI bold red. |
| `C_WARN` | `str` | ANSI bold magenta. |
| `C_DIM` | `str` | ANSI dim. |

#### Rating and display

| Function | Returns | Description |
|----------|-----------|-------------|
| `normalize_rating(rating)` | `float \| None` | Canonical name for the conversion; identical to `calibre_rating_to_stars` (kept as an alias). Converts Calibre's internal 0-10 scale to 0.0-5.0 stars; returns `None` for unrated (0 or `None`). |
| `format_stars(rating)` | `str` | Render a 0.0-5.0 rating as Unicode star glyphs (★★★½☆☆) with a numeric suffix. Half-stars use U+00BD. Returns an empty string for `None`. |
| `strip_html(html)` | `str` | Reduce comments HTML payloads to safe plain text (tags stripped, entities unescaped, whitespace collapsed). Run any raw HTML through this before terminal or GTK rendering. |
| `tags_to_tree(tags)` | `dict[str, Any]` | Build a nested tree from dot-delimited hierarchical tags (`["Fic.Scifi"]` → `{"Fic": {"Scifi": {}}}`). |
| `tag_rollup(counts)` | `dict[str, int]` | Roll up leaf/partial dot-path counts into subtree totals: every node carries its own count plus everything below it (render-identical with Hermitage's `_total_count` and Carrel's category union). |
| `isbn_normalize(raw)` | `str` | Strip separators, uppercase, keep a trailing `X`. No validity judgement. |
| `isbn_check_digit_is_valid(isbn)` | `bool` | Validate an ISBN-10 (mod 11) or ISBN-13 (EAN) check digit; wrong lengths are invalid, not errors. |
| `to_isbn13(raw)` | `str \| None` | ISBN-10 → 13 via the 978 prefix with a recomputed check digit; a 13-digit input passes through; anything else `None`. Deliberately no source check-digit validation (the LibraryThing exporter's contract); pair with `isbn_check_digit_is_valid` for strictness. |
| `normalize_author_display(authors, primary_only=False)` | `str` | Format an author string (comma-separated or `list[str]`) for display. With `primary_only`, returns only the first author. Returns `"Unknown Author"` for empty input. |
| `author_sort_key(author_sort, primary_only=False)` | `str` | Generate a lowercase sort key from `author_sort`. With `primary_only`, splits on `&` and uses the first segment. |

#### Series analysis

| Function | Returns | Description |
|----------|-----------|-------------|
| `detect_series_gaps(indices_str, max_index)` | `list[int]` | Given a comma-separated string of series indices and the maximum index, return the sorted list of missing integer entries (e.g. indices `"1,3,5"` with max 5 returns `[2, 4]`). |

#### Image dimensions

| Function | Returns | Description |
|----------|-----------|-------------|
| `sniff_image_format(data)` | `str \| None` | The bytes-level sibling (since 1.14.0): `'jpg'`, `'png'`, or `None` from the same signatures, no file needed; `add_book`'s cover sniff-or-raise builds on it. |
| `get_image_size(filepath)` | `tuple[int, int] \| None` | Return `(width, height)` for a JPEG or PNG by sniffing the file signature. Returns `None` for unrecognized formats or read errors. |
| `get_jpeg_size(filepath)` | `tuple[int, int] \| None` | Seek through JPEG segment markers to find the SOF frame dimensions. Handles large EXIF/ICC blocks that a fixed header read would miss. |
| `get_png_size(filepath)` | `tuple[int, int] \| None` | Read the IHDR chunk of a PNG for its dimensions. |

#### Terminal output

| Function | Returns | Description |
|----------|-----------|-------------|
| `color(text, code)` | `str` | Wrap text in ANSI escape codes if stdout is a TTY; return the text unchanged otherwise. Predefined codes: `C_HEADER` (bold yellow), `C_TITLE` (bold cyan), `C_ERR` (bold red), `C_WARN` (bold magenta), `C_DIM` (dim). |

### Config (from `cquarry.config`)

Persistent configuration for database path discovery.

| Name | Returns | Description |
|------|------|-------------|
| `VERSION` | `str` | Package version string. |
| `CALIBRE_RATING_SCALE` | `int` | The divisor for Calibre's internal rating (2, since Calibre stores 5 stars as 10). |
| `DEFAULT_DB_PATHS` | `list[str]` | Paths checked during auto-discovery: `./metadata.db`, `~/Calibre Library/metadata.db`, `~/calibre/metadata.db`. |
| `CONFIG_FILE` | `str` | Location of the saved config: `~/.config/cquarry/config.json`. |
| `load_config()` | `dict` | Load the config file. Returns `{}` on missing or corrupt files. |
| `save_config(config)` | `None` | Write the config dict to disk, creating parent directories as needed. |
| `get_db_path()` | `str \| None` | Read the saved `db_path` from config. |
| `set_db_path(path)` | `None` | Save an absolute, expanded `db_path` to config. |

### Package metadata

```python
import cquarry

print(cquarry.__version__)  # "1.17.0"
```

### Writes (from `cquarry.write`)


#### Properties

- `wdb.db_path` (`str`): The normalized absolute path to `metadata.db`.
- `wdb.conn` (`sqlite3.Connection`): The active read/write SQLite connection object.

| Member | Returns | Description |
|--------|---------|-------------|
| `WritableCalibreDB(db_path)` | `Handle` | Read/write handle. Registers Calibre's trigger dependencies (`title_sort()`, `uuid4()`, `PYNOCASE`) before any statement; context-manager supported. |
| `__enter__()` | `Self` | Context manager entry. |
| `__exit__(*exc)` | `None` | Context manager exit: commits on a clean exit; rolls back when an exception is in flight (`BaseException` included, so Ctrl-C unwinds instead of committing a torn edit); commit failures propagate; then closes the connection. |
| `close()` | `None` | Close the database connection and context. |
| `register_udfs(conn)` | `None` | Register the trigger-required SQL functions/collations on any read-write connection. |
| `uuid4([_arg])` | `str` | SQL-callable UUID generator matching Calibre's `uuid4()` UDF. |
| `title_sort(title)` | `str` | Re-exported from `cquarry.helpers`. |
| `update_title(book_id, new_title)` | `None` | Rename with refreshed sort key and `last_modified`. |
| `add_tag(book_id, tag)` / `remove_tag(book_id, tag)` | `bool` | Idempotent tag mutation following Calibre's link-table sequence; returns whether state changed. |
| `clear_tags(book_id)` | `int` | Detach every tag from the book (since 1.13.0): links deleted, orphaned tag rows pruned after (the `fkc_delete_on_tags` order), OPF resync queued only on change. Returns the count of links removed; an already-untagged book is an honest 0. |
| `set_identifier(book_id, id_type, val)` | `bool` | EAV upsert honoring `UNIQUE(book, type)`; `None` deletes. Returns `True` if state changed. |
| `set_identifiers(book_id, pairs)` | `int` | Batch upsert identifiers. Returns count of changed entries. |
| `clear_identifier(book_id, id_type)` | `bool` | Delete one identifier pair (since 1.9.0). The type is normalized exactly like `set_identifier` (stripped, lowercased; empty raises); a pair already absent is an honest no-op. Deletion queues OPF regeneration. |
| `set_authors(book_id, names)` | `bool` | Replace the author list; recomputes `books.author_sort` from per-author sort keys (" & "-joined); prunes orphans. |
| `set_series(book_id, name, index=None)` | `bool` | Assign/clear series + `series_index` (defaults 1.0 fresh, preserves on reassign). |
| `set_publisher(book_id, name)` | `bool` | Replace/clear publisher; case-insensitive match; orphans pruned. |
| `set_rating(book_id, stars)` | `bool` | 0-5 stars stored as x2; UNIQUE(rating) rows deduplicated via find-or-create. |
| `clear_rating(book_id)` | `bool` | Self-documenting alias of `set_rating(book_id, None)` (since 1.13.0); the orphaned rating row prunes with it. False when the book was already unrated. |
| `set_languages(book_id, codes)` | `bool` | Replace languages (supports `list[str]` or comma-separated `str`); English names canonicalized to ISO 639-2 via the search engine's map. |
| `set_comments(book_id, text)` | `bool` | 1:1 upsert/clear of the comments HTML row. |
| `set_pubdate(book_id, value)` | `bool` | Publication-date setter accepting `str` / `date` / `datetime` / `None` (sentinel); stored as Calibre TEXT in UTC. |
| `set_custom_column(book_id, label, value)` | `bool` | Generic custom-column writer: storage layout auto-detected (link-table vs direct) with a datatype dispatch; unknown datatypes (or a datatype on the wrong layout) raise instead of being stringified. Enumerations validate against `display.enum_values` (an empty `enum_values` rejects every value), tristate bools accepted, rating-typed columns take 0-5 stars (stored x2 on Calibre's internal 0-10 scale like `set_rating`; 0 stars clears), datetime-typed columns normalize like `set_pubdate` (ISO text in UTC), non-editable/composite columns raise. |
| `add_custom_column_values(book_id, label, values)` | `int` | Append semantics for multi-valued (Pattern A) columns (since 1.13.0): dedupes against the book's existing values (the link table is `UNIQUE(book, value)`) and within the input, inserts only the new links, returns the honest count; a no-op bumps nothing. Single-valued and direct-storage columns raise toward `set_custom_column`; a bare string raises `TypeError` rather than being comma-split; `None` entries inside `values` are skipped (never stringified into `'None'`); enumeration-typed columns validate each appended value. |
| `add_format(book_id, fmt, name, size)` / `remove_format(book_id, fmt)` | `bool` | Register/drop `data` rows (the file itself is the caller's responsibility). |
| `set_format(book_id, fmt, name, size)` | `bool` | Sanctioned replace of one format row, remove+add in one transaction (since 1.17.0). `True` when a row was written, `False` when the identical row already exists; the file swap itself stays the caller's atomic-replace job. |
| `set_has_cover(book_id, has_cover)` | `bool` | Toggle the catalogued flag. |
| `add_book(title, authors, *, formats=None, cover=None, identifiers=None, language=None, pubdate=None, publisher=None, dry_run=False)` | `int \| dict` | The creation path (since 1.14.0): inserts `(title, series_index, author_sort)` first and lets `books_insert_trg` fill `sort`/`uuid`, then links authors/identifiers/language/pubdate/publisher, copies format files and an optional cover (JPEG/PNG sniff-or-raise) into the `Author/Title (id)` directory with truthful `data` rows, and queues OPF resync. ONE `batch()`: any failure rolls the SQL back and removes the created directory; inside a caller's shared `batch()`, a failure ANYWHERE in the pass also removes the directories of books added earlier in that same batch (batch-scoped filesystem compensation), while books added outside the failing batch are never touched. A format seed byte-identical to an already-catalogued file raises `ValueError` before anything is written (dry runs included; the 'never imported twice' invariant since 1.15.0; metadata-based duplicate screening stays a frontend concern). Empty title becomes `Unknown` (Calibre parity); an empty author list is legal (no links, what `find_authorless` expects). `dry_run=True` writes nothing and returns the plan (predicted id from `sqlite_sequence`, resolved authors, format filenames, the row diff). Copy only: sources are never moved or deleted. |
| `remove_book(book_id, delete_files=None)` | `None` | Full book removal: custom columns (both patterns) + dirtied queues cleaned, cascade trigger fires, orphaned entities pruned. Irreversible (the rows). `delete_files` extends the removal to the book's on-disk directory (since 1.17.0): `"trash"` moves it into the library-local `.caltrash/b/<id>/` (upstream's own layout, recoverable by hand), `"permanent"` deletes it outright, `None` leaves the files for the caller. File removal lands only after the rows COMMIT -- inside a `batch()` it defers to the outermost commit, so a rollback never leaves resurrected rows fileless. |
| `batch()` | `ContextManager` | Defer commits across a multi-book, multi-field pass. Explicit context manager that batches writes into one transaction. |
| `transaction()` | `ContextManager` | Alias for `batch()`, kept for backwards compatibility. |

Every state-changing mutation also inserts the book id into `metadata_dirtied` (`INSERT OR IGNORE`; the table's `UNIQUE(book)` keeps it one row per book), which is what tells Calibre to regenerate that book's sidecar `.opf` and re-push metadata to wireless readers on its next startup. No-op mutations queue nothing, and databases predating the table keep working (the insert is guarded by a cached existence check).


### Integrity (from `cquarry.integrity`)

Pure predicates over the cached rows; the one shared definition of "incomplete"
(mined from CalibreQuarry's `--audit` frontend). No SQL of their own; the two
cover-file checks ride `get_cover_path()` + `get_image_size()`. Every id list is
sorted.

| Function | Returns | Description |
|----------|---------|-------------|
| `find_untagged(db)` | `list[int]` | Books carrying no tags. |
| `find_unrated(db)` | `list[int]` | Books with no rating (`None` or `0`). |
| `find_authorless(db)` | `list[int]` | Books with no authors, or only the `Unknown` placeholder. |
| `find_formatless(db)` | `list[int]` | Books with no catalogued format rows. |
| `find_coverless(db)` | `list[int]` | Books whose `has_cover` flag is unset (the catalogued answer). |
| `find_missing_cover_files(db)` | `list[int]` | Flag set but no cover file resolves on disk (empty `books.path` skipped; nowhere to look). |
| `find_deprecated_formats(db, formats)` | `list[int]` | Books whose whole format set sits inside the caller's deprecated set (case-insensitive). cquarry owns the subset mechanism; what counts as deprecated is a curation opinion. Formatless books excluded. |
| `find_low_res_covers(db, min_dimension=500)` | `dict[int, tuple[int, int]]` | `{id: (w, h)}` for resolvable, parseable covers under the dimension floor. Missing files are `find_missing_cover_files`' answer; unreadable images are skipped. |
| `find_duplicate_books(db)` | `dict[tuple[str, str], list[int]]` | `(title.lower(), primary_author.lower())` groups with more than one member. |
| `find_series_gaps(db)` | `dict[str, list[int]]` | `{series: [missing indices]}` composing `get_all_series()` + `detect_series_gaps()`. |
| `find_identifierless(db)` | `list[int]` | Books carrying no identifiers at all (since 1.17.0; promoted from Hermitage's inline Insights predicate). |

### Analytics (from `cquarry.analytics`)

Derivations promoted from CalibreQuarry's `--analytics` frontend so every
consumer shares them; the frontend keeps formatting. APIs that already exist
(`get_format_stats`, `get_entities`, `get_tag_counts`) deliberately do not
appear here.

| Function | Returns | Description |
|----------|---------|-------------|
| `addition_timeline(db, granularity="month")` | `dict[str, int]` | Books added per calendar bucket, chronological: `"YYYY-MM"` (or `"YYYY"` with `granularity="year"`). Books without a timestamp are skipped; anything but `month`/`year` raises `ValueError`. |
| `author_stats(db)` | `list[dict[str, Any]]` | Per primary author: `{author, book_count, avg_rating, rated_count, formats}`; star-scale average over rated books only (`0.0` when none), sorted count-descending then name; authorless books skipped. |
| `genre_distribution(db)` | `dict[str, float]` | Share of the whole library per hierarchical-tag node: subtree rollup (a book counted once per node even when its tags share an ancestor, so multi-genre books can push the sum over 1.0), depth-first with parents before children, siblings share-descending then name, `"untagged"` last. |
| `rating_distribution(db)` | `dict[float \| str, int]` | Books per star rating, ascending on the half-step scale, `"unrated"` last. |
| `vl_overlap(db, names=None)` | `dict[tuple[str, ...], list[int]]` | Books shared by two or more virtual libraries, wing names sorted in each key. `names` restricts the wings (unknown names raise through `resolve_vl`); single-wing books appear nowhere. |


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
| `=.` | Subtree match on all text fields |
| `=..` | Component exact match on all text fields |
| `~` | Regular expression (stdlib `re`, case-insensitive) |
| `^` | Accent-folded substring |
| `\` | Escape the next character (treat literally) |

*(Note: Tristate keywords `true`/`false`, `checked`/`unchecked`, `blank`/`empty`, and `_`-prefixed variants are supported for presence/absence on numeric and rating fields; anything else on a boolean location raises `ParseException` (since 1.16.0, upstream parity). An empty query after ANY location matches nothing, never everything (since 1.16.0). Dates accept both `-` and `/` separators, and undefined date sentinels (`0101-01-01`, `0100-01-01`) evaluate as `None`. Multi-token queries in `languages:` split on commas and canonicalize each independently; two-letter ISO 639-1 codes canonicalize too (`languages:ja` matches `jpn`).)*


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
| `annotations` | | text | Book's concatenated annotation text; `true`/`false` test presence |
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
| `@Name` | | user category | Books holding any member value: `@Favorites:true`; leading `.` includes subcategories, `false` inverts |
| `all` | *(bare terms)* | | Searches title, authors, author_sort, series, publisher, tags, comments, formats, languages (canonicalized) + custom text columns; identifier KEYS sweep as text; numeric fields are probed by exact equality (dates match nothing) |

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

### Grouped search terms

Calibre lets users define groups (`preferences.grouped_search_terms`: group name -> member
locations). cquarry resolves them with upstream's semantics:

```
People:leckie        # union over the group's member locations
People:false         # books where NO member matches
```

Real field names always win over same-named groups, and nesting a group inside a group is a
parse error.

### User categories

Calibre lets users define tag-browser pseudo-categories (`preferences.user_categories`).
cquarry searches them with upstream's exact semantics:

```
@Favorites:true      # books holding any member value (exact match per member location)
@Favorites:false     # the inverse
@Favorites:.true     # include subcategories (category names starting with "Favorites.")
```

As in Calibre, any query text other than `false`/a leading `.` is ignored (the GUI always
writes `@Name:true`); groups and real fields win over same-named categories; unknown
`@Names` match nothing.

### Documented deviations from Calibre

- **Regex engine.** `~` uses stdlib `re`, not Calibre's third-party `regex` module (`VERSION1`/`\X` are unavailable; otherwise compatible).
- **Accent folding.** Uses `unicodedata` NFKD decomposition rather than ICU collation, so punctuation-insensitivity is not reproduced.
- **GPM templates.** `@...:` template expressions tokenize for parse parity but are not evaluated.
- **GUI-state locations.** `marked`, `ondevice`, and `in_tag_browser` exist only inside Calibre's own UI session and are not implemented.
- **Hierarchical tag matching.** `tags:` uses cquarry's anchored match (`Foo` matches `Foo` and `Foo.*`) rather than Calibre's raw substring default. This is a long-standing project invariant.
- **`annotations:` matching.** Calibre searches annotations through its FTS tables (with stemming and rank ordering); cquarry matches the concatenated `searchable_text` with ordinary text semantics; same result set for typical queries, no stemming or ranking.
- **`series_sort` format.** Computed as `"Series [index]"`.
