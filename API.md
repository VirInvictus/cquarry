# cquarry API reference

The full per-method reference. The [README](README.md) keeps the hero, the
quick-starts, and the search grammar; everything callable lives here.

**Version:** 1.25.0

## Public API

### `CalibreDB` (from `cquarry.db`)

The primary interface. Constructed with a path to `metadata.db`.

```python
db = CalibreDB(db_path: str)
```

Raises `FileNotFoundError` if the path does not exist. If the database is locked by Calibre, transparently snapshots it through sqlite3's backup API into a temp file and reads from the snapshot instead (since 1.23.0; immune to a mid-copy checkpoint, unlike a file copy of the database plus its `-wal`/`-shm`).

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
| `get_book_by_uuid(uuid)` | `dict[str, Any] \| None` | One hydrated book by its per-book uuid (since 1.24.0, the Calibre-Companion endpoint shape; upstream `lookup_by_uuid`): case-insensitive match, `None` on miss. |
| `get_all_formats()` | `dict[int, list[str]]` | Every book's format list in one cached map (since 1.24.0): the bulk shape of `get_formats()`'s keys for whole-shelf rendering. |
| `get_entity_book_ids(kind, name)` | `set[int]` | The id-set half of `get_entities` (since 1.24.0): books carrying one entity value, case-insensitive. Tags follow the engine's anchored rule (`Foo` includes `Foo.*`); kinds are authors/series/publishers/tags/languages (ratings have no name; a rating slice is `search("rating:...")`). Unknown names are an empty set. |
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
| `facet_counts(query, *, locations=None)` | `dict[str, list[tuple[Any, int]]]` | Browse facets over a restricted search result (since 1.24.0): per-value counts of what the RESULT SET carries, not the library. Default facets: authors, tags, series, publisher, languages, formats, rating (stars); custom columns by `#label`. Count-descending then value-ascending; empty values produce no entry. |
| `facet_counts_for_ids(ids, *, locations=None)` | `dict[str, list[tuple[Any, int]]]` | The restriction plumbing `facet_counts()` composes with `search()` (since 1.24.0): the same counting over any caller-supplied id set (a virtual library, a shelf, Phase 18's restricted tag browser). `None` means the whole library; unknown locations raise `ValueError`. |
| `books_by_year(field="pubdate", ids=None)` | `dict[int, set[int]]` | Books bucketed by year over any date field (since 1.25.0, upstream `Cache.books_by_year`): builtin date locations (`pubdate`, `timestamp`, `last_modified`) plus date-typed custom columns by `#label`/bare label/display name; `ids` restricts the books counted. Books with no value, the `0101`/`0100` sentinels included, appear nowhere (hunting sentinels is `integrity.find_sentinel_pubdates`' job). |
| `books_by_month(field="pubdate", ids=None)` | `dict[tuple[int, int], set[int]]` | The same bucketing one level finer (since 1.25.0, upstream `Cache.books_by_month`): `(year, month)` tuple keys. |
| `get_next_series_num_for(series, field="series", current_indices=False)` | `float \| dict[int, float]` | The preference-aware next series number (since 1.25.0, upstream `Cache.get_next_series_num_for`): what Calibre's own "next in series" would assign. `field` is the builtin `series` or a series-typed custom column by `#label`/bare label/display name; the `series_index_auto_increment` behavior follows the library's `preferences` table with upstream's shipped default `"next"` (a number verbatim; `next`/`first_free`/`next_free`/`last_free` gap-filling; unknown values degrade to 1.0). Boundary: upstream reads the setting from its tweaks files, which metadata.db never carries, so a local tweaks.py override is invisible here. `current_indices=True` returns the members' `{book_id: index}` map instead. Unknown/non-series fields raise `ValueError`. |
| `get_format_path(book_id, fmt, verify=True)` | `str` | Absolute filesystem path for a book's format file, built from the original DB location. Raises `ValueError` for unknown book/format, `FileNotFoundError` when `verify` is set and the file is missing. |
| `get_formats(book_id)` | `dict[str, dict[str, Any]]` | Per-format detail: `{fmt: {path, size_bytes, name}}` (path unverified; size from the catalogued uncompressed size). `{}` for unknown books. |
| `format_hash(book_id, fmt)` | `str` | The format file's SHA-256 hex digest (since 1.25.0, upstream `Cache.format_hash`): the file-changed detector the FTS sidecar's `format_hash`/`text_hash` columns are compared against. Rides `get_format_path`'s verified resolution, so its errors apply (`ValueError` unknown book/format, `FileNotFoundError` when the catalogued file is absent). |
| `format_metadata(book_id, fmt)` | `dict[str, Any]` | `{"path", "size", "mtime"}` from the file on disk (since 1.25.0, upstream `Cache.format_metadata`): the verified path, the real byte count (which can drift from the catalogued `uncompressed_size` until Calibre rescans), and a timezone-aware UTC mtime. Unresolvable pairs raise, never an empty dict. |
| `get_cover_path(book_id, verify=True)` | `str \| None` | Resolved cover image path (`cover.jpg`, falling back to `cover.png`) from the original DB location. With `verify` (default) returns None when no file exists on disk; without it returns the `.jpg` path unconditionally. Raises `ValueError` for unknown books. |
| `get_cover_bytes(book_id)` | `bytes \| None` | The cover image's raw bytes (since 1.25.0, upstream `Cache.cover()`'s bytestring mode; what web frontends serve and thumbnailers consume): same `cover.jpg`-then-`cover.png` resolution and unknown-book `ValueError` as `get_cover_path()`. `None` when no cover file exists on disk, catalogued flag or not. |
| `get_cover_last_modified(book_id)` | `datetime \| None` | The cover file's mtime as a timezone-aware UTC `datetime` (since 1.25.0, upstream `cover_last_modified`; the conditional-GET stamp a web frontend compares before re-serving bytes). `None` without a cover file; `ValueError` for unknown books. |
| `read_backup(book_id)` | `bytes \| None` | Calibre's stored sidecar `metadata.opf` for the book (since 1.25.0, upstream `Cache.read_backup`): the backup Calibre's own thread writes after every metadata change, readable so a caller can diff Calibre's last write against the rows. Reads what Calibre wrote, never generates. `None` when the book has no directory or no backup file yet; `ValueError` for unknown books. |
| `list_data_files(book_id)` | `list[dict[str, Any]]` | Every file under the book's `data/` directory (since 1.25.0, the extra-files family the content server serves): `[{relpath, path, size, mtime}]`, `relpath` the forward-slash path under `data/`. Formats/covers/`metadata.opf` live outside `data/` and never appear. Empty list without a `data/` directory; `ValueError` for unknown books. |
| `get_data_file(book_id, relpath)` | `bytes \| None` | One extra file's bytes (since 1.25.0), None when absent. Unsafe relpaths (absolute, `..`, empty components) raise `ValueError` -- the traversal guard the write side shares. |
| `get_library_uuid()` | `str \| None` | The library's identity UUID (`library_id` table); stable across moves/restores, unlike per-book uuids; the right cache key for per-library state. None on very old schemas. |
| `backup_to(dest)` | `str` | Copy `metadata.db` to `dest` as one consistent snapshot via sqlite3's backup API (since 1.23.0; a file copy of main+`-wal`+`-shm` can tear when Calibre checkpoints mid-copy). Creates `dest` (replacing an existing file) and returns its absolute path. On a locked-DB snapshot connection, the snapshot is what gets copied. |
| `external_changes_detected()` | `bool` | True when another connection has committed writes to this database file since this connection last looked (since 1.23.0, via `PRAGMA data_version`): the cheap staleness token for long-lived holders -- poll it and call `refresh()` only on True. Stays True until `refresh()` re-primes the baseline; on a snapshot connection it can never answer True (the copy is isolated), which is that boundary's reminder to reopen. |
| `get_entities(kind)` | `list[dict[str, Any]]` | Entity rows for `authors` / `series` / `publishers` / `tags` / `languages` / `ratings`: `{id, name, sort, link, count}`, name-sorted (ratings carry the half-star integer as `name`). Raises `ValueError` for unknown kinds. |
| `get_preference(key, default=None)` | `Any` | Typed read of any Calibre preference from the `preferences` table (JSON decoded where it parses). |
| `get_field_metadata()` | `dict[str, Any]` | The rich `field_metadata` preference: per-custom-column GUI metadata keyed by label. |
| `get_grouped_search_terms()` | `dict[str, list[str]]` | Grouped search terms driving `GroupName:query` expansion in the search engine. |
| `get_user_categories()` | `dict[str, list[dict[str, Any]]]` | User-defined tag-browser categories (name -> member descriptors). |
| `get_tag_browser_state()` | `dict[str, Any]` | `{"order": [...], "hidden": [...]}` from the `tag_browser_*` preferences; mirror Calibre's browse-sidebar layout. |
| `get_identifiers(book_id)` | `dict[str, str]` | All identifiers for a book (e.g. `isbn`, `amazon`, `lcc`), keyed by type. Empty on schemas predating the table. |
| `get_all_tags()` | `list[str]` | Every distinct tag name, sorted alphabetically. |
| `get_tag_counts()` | `list[tuple[str, int]]` | `(tag_name, book_count)` pairs, sorted by tag name. |
| `tag_rollup_ids(ids=None)` | `dict[str, frozenset[int]]` | The id-set sibling of `helpers.tag_rollup` (since 1.25.0): `{tag_path: frozenset(book_ids)}` for every implied dot-path node, the engine's anchored-subtree rule, so a browser built on these sets answers `tags:` queries consistently; `len()` per set is where the counts-only rollup lands. `ids` restricts the books counted. Promoted from Carrel's private `_rollup`; that copy retires in a consumer wave. |
| `get_size_stats()` | `dict[str, int]` | The library's file sizes in bytes (since 1.25.0, upstream `Cache.size_stats`): `{"main", "fts", "notes"}`. `notes` is always 0 here; the notes DB is a recorded decline, the key kept so consumers render the same three columns. |
| `is_fts_enabled()` | `bool` | Whether Calibre's FTS indexing is switched on for the library (since 1.25.0): the `fts_enabled` preference (upstream's default False). The in-process extraction-pool state is GUI/process state metadata.db cannot carry; the sidecar's presence is a separate question (the sidecar reads degrade to empty when the file is missing regardless of this flag). |
| `get_all_link_maps_for_book(book_id)` | `dict[str, dict[str, str]]` | All of one book's entity links in one map (since 1.25.0, upstream's name and shape): `{field: {value: link_url}}` over authors/publisher/series/tags plus every custom column carrying a link for the book (keyed `#label`, via the `custom_column_links()` seam). Empty fields are omitted, so an unlinked book answers `{}`; unknown books answer `{}` too. |
| `precedent_tags(authors, limit=12)` | `list[str]` | Tag-by-precedent (1.22.0): distinct tag names across the named authors' books (NOCASE author match), capped at `limit`, sorted alphabetically for stability; `[]` for no authors or no matches. The curation prompt's suggestion source; promoted from CalibreQuarry's phase-3 prompt. |
| `get_all_series()` | `list[dict[str, Any]]` | Per-series rollups: `name`, `book_count`, `indices` (comma-separated), `max_index`, `titles` (comma-separated, sorted by index). |
| `get_custom_columns()` | `dict[str, dict[str, Any]]` | Metadata for all user-defined custom columns, keyed by display name (the historical key; since 1.9.0 column *lookup* that accepts `#label` or a bare label goes through `find_custom_column()` / `load_custom_column()`). Each value contains `id`, `label`, `name`, `datatype`, `is_multiple`, `editable`, `normalized`, and `display` (a decoded JSON config dict). |
| `find_custom_column(key)` | `dict[str, Any] \| None` | One custom-columns record by `#label`, bare label, or display name. A leading `#` matches the label only (never ambiguous); otherwise an exact display-name match wins (the historical key) and a bare label is the graceful fallback; label matching is case-insensitive, mirroring the write module. Returns `None` when nothing matches. |
| `list_books(*, ids=None, sort="sort", descending=False, offset=0, limit=None)` | `list[dict[str, Any]]` | Paginated, sorted listing over the cached rows (1.10). `ids` restricts (order still comes from `sort`); `sort` is one key or a sequence of keys (primary first, one direction for all) drawn from `sort`/`title`/`timestamp`/`pubdate`/`rating`/`series_index`/`author_sort`/`series`/`id`; None-valued keys sort last regardless of direction; `offset`/`limit` slice after sorting. Since 1.21.0 the special key `sort="ids"` (requires `ids`, stands alone) returns the rows in the CALLER's id sequence instead: a duplicated id keeps its first slot, ids absent from the library are skipped, `descending` reverses the sequence, and `offset`/`limit` slice after -- the frontend-ordering mode that retires Carrel-calibre-web's preserve_order re-sort shim. Pure, no SQL of its own. Raises `ValueError` on unknown keys or negative offsets/limits. |
| `load_custom_column(col_name)` | `dict[int, Any]` | Values for one custom column, addressed by `#label`, bare label, or display name (since 1.9.0; resolution via `find_custom_column()`), returned as `{book_id: value}`. Normalized columns (text, enumeration, series, rating) are read via their link table; direct columns (int, float, bool, datetime, comments) are read from the value table. Multi-valued columns return a native `list[str]` (since 1.16.0; the old comma-joined string made stored values like `Doe, John` round-trip as phantom values; do not `.split(",")` these, same rule as book rows); rating-typed columns surface the same 0-5 star scale as the builtin rating (Calibre's stored 0-10 converted), so `field(book, "#myrat")` and `field(book, "rating")` compare alike. Raises `ValueError` if the column does not exist. |
| `custom_column_links(col_name)` | `dict[int, str]` | The normalized value table's `link` URL column (upstream schema_upgrades.py:836; no Calibre UI populates it): `{book_id: url}`, filled as a side effect of `load_custom_column`. Empty until then, and for direct-storage columns, unknown columns, and schemas predating the column. |
| `get_virtual_libraries()` | `dict[str, str]` | Virtual library names mapped to their Calibre search expressions, read from the `preferences` table. Cached after the first call. A corrupt or non-dict stored payload degrades to `{}` (since 1.16.0; it used to crash every read touching virtual libraries). |
| `get_saved_searches()` | `dict[str, str]` | Saved-search names mapped to their expressions (the source for `search:"Name"` interpolation). |
| `get_vl_ui_state()` | `dict[str, Any]` | Calibre's sidebar layout state: `{"hidden": [names], "order": {...}}` decoded from `virt_libs_hidden` / `virt_libs_order`. |
| `ordered_virtual_library_names(include_hidden=False)` | `list[str]` | Virtual library names in Calibre's own sidebar order (since 1.25.0): stored tab position first, unknown names alphabetical, unparseable positions ranking with the unknowns rather than crashing the sort. GUI-hidden libraries dropped unless `include_hidden`. The promoted helper; Carrel and Hermitage carried near-identical private copies. |
| `virtual_libraries_for_books(book_ids=None)` | `dict[int, tuple[str, ...]]` | The inverse virtual-library map (since 1.25.0, upstream `Cache.virtual_libraries_for_books`): every requested book id -> the sorted names of the wings containing it; `None` means every book, and ids absent from the library come back as empty tuples like members of no wing. Wings resolve through the same engine path `resolve_vl()` uses, so the answers always match `resolve_vl()`; a wing whose expression fails to evaluate is skipped with a stderr warning rather than failing the map (upstream splices an error string into the name tuple there, which would pollute set algebra on the values). |
| `user_categories_for_books(book_ids=None)` | `dict[int, dict[str, list[list[str]]]]` | The inverse user-category map (since 1.25.0, upstream `Cache.user_categories_for_books`): every requested book id -> `{category: [[value, location], ...]}` naming only the members the book actually holds, probed exactly like the `@Name` search location, so `@Name:Category` and this map answer identically. Composite members (the §7 GPM boundary) and unknown locations match nothing rather than erroring; scalar members compare with `==`, so a rating member written `"4"` does not match the 4.0-star float, which is upstream's own behavior. `None` means every book. |
| `count_books()` | `int` | Total book count. Uses the cache if available; otherwise issues a `SELECT COUNT(*)`. |

#### Annotations, progress & plugin data

| Method | Returns | Description |
|--------|---------|-------------|
| `get_annotations(book_id=None)` | `list[dict[str, Any]]` | E-reader highlights, bookmarks, and notes from the `annotations` table; `annot_data` is decoded JSON when possible. In `searchable_text`, an annotation's notes follow its highlighted text joined by `\n\x1f\n` (LF, ASCII unit separator, LF). |
| `get_annotations_decoded(book_id=None)` | `list[dict[str, Any]]` | The renderer-facing annotation view (since 1.23.0): one dict per annotation with `book`, `format`, `kind` (the raw `annot_type`), `annot_id`, `timestamp`, and the decoded payload's `text` (the highlighted passage), `notes`, and `title` (bookmarks), each None when the payload lacks it. The same content `get_annotations()` returns without every consumer re-learning `annot_data`'s shape. |
| `get_annotations_filtered(*, book_id=None, user_type=None, user=None, kind=None, style=None, include_removed=False, limit=None)` | `list[dict[str, Any]]` | The annotation conveniences in one read (since 1.25.0, upstream `Cache.all_annotations`): `get_annotations_decoded()`'s rows plus `user_type`/`user`/`removed`, filtered by device account (`user_type`/`user`), annotation `kind`, and highlight `style` (upstream's exact-dict match: every key/value given must equal the stored style). Removed annotations are Calibre's tombstones (the payload rewritten to a `removed: True` skeleton); hidden unless `include_removed`, upstream's `ignore_removed` inverted to the renderer-facing default. `limit` applies after filtering, over `get_annotations()`' ordering. |
| `get_annotation_users()` | `list[tuple[str, str]]` | Every `(user_type, user)` account holding annotations (since 1.25.0, upstream `all_annotation_users`): the "whose highlights" picker's options. |
| `get_annotation_types()` | `list[str]` | Every annotation type present (since 1.25.0, upstream `all_annotation_types`): `highlight`, `bookmark`, ... sorted. |
| `get_annotation_styles()` | `list[dict[str, str]]` | The distinct highlight styles the library actually holds (since 1.25.0, the DB half of upstream `all_annotation_styles`): `{"kind", "which"}` dicts from the stored payloads, sorted. Upstream also lists its viewer's builtin catalog, which is GUI constants, not data; styles nobody used appear in Calibre's picker and not here. |
| `get_last_read_positions(book_id=None, *, fmt=None, user=None, order_by=None, limit=None)` | `list[dict[str, Any]]` | Per-device reading progress (`device`, `cfi`, `pos_frac` 0.0–1.0, `epoch`). The 1.25.0 filters mirror upstream's: `fmt` (case-insensitive) and `user` narrow the rows, `order_by` accepts `"pos_frac"` or `"epoch"` (descending: most-progressed / most-recent first), `limit` caps the count -- the "where was I in THIS book" read. Defaults unchanged (`ORDER BY book, device`). |
| `get_plugin_data(book_id=None, name=None)` | `list[dict[str, Any]]` | Third-party payloads from `books_plugin_data` (Goodreads IDs, word counts, ...). |
| `get_conversion_profiles(book_id=None)` | `list[dict[str, Any]]` | Books with manual conversion overrides; the pickled recipe blob stays raw bytes (`data_size` gives its length). |
| `get_dirtied_books()` | `list[int]` | Book ids queued for OPF resync in `metadata_dirtied`; i.e. what Calibre will regenerate/push at its next startup. Sorted, deduplicated; read-only (clearing the queue remains Calibre's job). |
| `get_annotations_dirtied_books()` | `list[int]` | The annotations sibling queue (`annotations_dirtied`): ids whose highlights/bookmarks Calibre will push to devices. Same read-only contract. |
| `get_dirtied_formats()` | `list[tuple[int, str]]` | The FTS sidecar's extraction queue (since 1.24.0): `(book_id, format)` pairs awaiting (re-)extraction and a pages rescan, sorted, formats uppercase. Read-only observation; empty when the sidecar is absent or predates the table. |
| `get_feeds()` | `list[dict[str, Any]]` | Registered news-download recipes from the `feeds` table: `[{id, title, script}]`. |
| `get_page_metadata(book_id=None)` | `dict[int, dict[str, Any]]` | The full `books_pages_link` row per book: `{book: {pages, algorithm, format, format_size, timestamp, needs_scan}}` -- provenance for the displayed count; `needs_scan=True` means Calibre has queued a recount. `{}` on schemas predating the native table. |
| `get_book_text(book_id, fmt)` | `dict[str, Any] \| None` | One format's extracted plain text from the `full-text-search.db` sidecar: the full `books_text` row (`searchable_text`, `format_hash`, `err_msg`, ...). `fmt` is case-insensitive; None when the sidecar is absent or the pair has no row. The FTS5 index tables are never touched (Calibre-only custom tokenizer). |
| `get_text_extractions(book_id=None)` | `list[dict[str, Any]]` | Bulk extraction-status rows WITHOUT the texts (`searchable_text` omitted; it can be megabytes per format): `err_msg` audit and format-hash change detection. `[]` when the sidecar is absent. |
| `search_book_text(query, *, fmt=None, ids=None)` | `dict[int, set[str]]` | Python-side content search over the sidecar's `searchable_text`: case- and accent-folded substring match returning `{book_id: {FORMAT, ...}}`. Linear scan by design (no FTS5 outside Calibre). Empty query raises `ValueError`. |
| `get_tag_browser_counts()` | `dict[str, list[dict[str, Any]]]` | Calibre's own browse-sidebar rollups from the `tag_browser_*` views: `{category: [{id, name, count, avg_rating, sort}]}`, custom columns rekeyed to `#label`. The `filtered_*` variants (GUI-state `books_list_filter()`) are skipped. Known upstream quirk (verified live): the ratings view's `avg_rating` is an uncorrelated cross join -- every row carries the same value -- so it is an artifact, not a per-row mean. |
| `get_categories(book_ids=None)` | `dict[str, list[dict[str, Any]]]` | The restricted tag browser (since 1.25.0, upstream `get_categories`, portable subset): `{category: [node, ...]}` over the builtin browse fields, the `identifiers` category (one node per identifier key, since 1.26.0), and NORMALIZED custom columns keyed `#label` -- since 1.26.0 under upstream's own `is_category = normalized` rule, so the direct-storage datatypes (int, float, bool, datetime, comments) that 1.25 over-included are out, and expression values are escaped. Each node carries `{id, name, sort, count, avg_rating, id_set, search_expression}` -- the book-id set and the exact-match query reproducing it (searching the expression returns the node's `id_set` by construction). `book_ids` restricts the browser to a search result; values no restricted book holds appear nowhere, and an empty restriction answers empty categories. Counts (and average ratings on the entity views) agree with `get_tag_browser_counts()` where they overlap; the named differences are rating nodes surfacing stars (matching `field()`/`facet_counts`, not the views' internal 0-10 text), unlinked zero-count entity rows absent (upstream's own rule), and composite columns staying out behind the §7 GPM boundary (user categories, `search`, and `news` are likewise out of the portable subset). Nodes sort name-ascending, rating descending. |

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
| `refresh()` | Drop every cache (rows, ids, search view and engine, preferences, custom columns, path index) so subsequent reads re-query the database; the coherence boundary for long-lived holders after an external write (since 1.16.0). One boundary: when the connection rides a locked-database snapshot copy, the snapshot is NOT retaken -- the next read still answers from the copy taken at open time; reopen the `CalibreDB` for truly current data in that situation. |
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
| `find_db(explicit=None)` | `str` | Locate `metadata.db` through a resolution chain: explicit argument, saved config (`~/.config/cquarry/config.json`), default paths (`./metadata.db`, `~/Calibre Library/metadata.db`, `~/calibre/metadata.db`), then an interactive TTY prompt. Raises `FileNotFoundError` if nothing is found. Side effect (since 1.8): a default-path hit is persisted to the config automatically, as are interactive-prompt answers; an explicit argument is never persisted. |
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
| `unpipe_author(name)` | `str` | Resolve Calibre's legacy pipe separator in one author display name (since 1.25.0): the `"|"` -> `","` flattening, render-identical with the consumer copies it promotes (Carrel/Hermitage). None-safe: `""` in, `""` out. |
| `identifier_link(id_type, value)` | `tuple[str, str] \| None` | `(label, url)` for a known identifier type (since 1.25.0), None when unknown; the type is normalized before lookup and the raw value substituted unescaped. ISBN resolves to Open Library, the 2026-09-29 canonical call (Carrel's WorldCat mapping switches in its consumer wave). The table is `IDENTIFIER_LINKS` (isbn/goodreads/google/amazon/asin/mobi-asin/barnesnoble/storygraph/hardcover/fictiondb/doi/url/uri), Hermitage's canonical mapping promoted. |

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

print(cquarry.__version__)  # the release version, e.g. "1.25.0"
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
| `update_title(book_id, new_title)` | `None` | Rename with refreshed sort key and `last_modified`; re-lays the on-disk layout (directory + format files move to the new stems; since 1.18.0). The fs half lands only after the rows commit -- after the outermost batch commit inside a `batch()`, right after the setter's own commit otherwise (since 1.20.1 a failed commit leaves rows and files in agreement). |
| `add_tag(book_id, tag)` / `remove_tag(book_id, tag)` | `bool` | Idempotent tag mutation following Calibre's link-table sequence; returns whether state changed. |
| `clear_tags(book_id)` | `int` | Detach every tag from the book (since 1.13.0): links deleted, orphaned tag rows pruned after (the `fkc_delete_on_tags` order), OPF resync queued only on change. Returns the count of links removed; an already-untagged book is an honest 0. |
| `set_identifier(book_id, id_type, val)` | `bool` | EAV upsert honoring `UNIQUE(book, type)`; `None` deletes. Returns `True` if state changed. |
| `set_identifiers(book_id, pairs)` | `int` | Batch upsert identifiers. Returns count of changed entries. |
| `clear_identifier(book_id, id_type)` | `bool` | Delete one identifier pair (since 1.9.0). The type is normalized exactly like `set_identifier` (stripped, lowercased; empty raises); a pair already absent is an honest no-op. Deletion queues OPF regeneration. |
| `set_authors(book_id, names)` | `bool` | Replace the author list; recomputes `books.author_sort` from per-author sort keys (" & "-joined); prunes orphans. Re-lays the on-disk layout when the first author changes (since 1.18.0), with the same commit-before-fs ordering as `update_title`. |
| `rename_entity(kind, old, new)` | `int` | Rename an author/series/publisher/tag everywhere (since 1.19.0): case-variant spellings merge (colliding links dropped, old row deleted), author merges recompute `author_sort` and re-lay paths, series merges renumber incoming books. Returns the affected book count. |
| `set_author_sort_name(name, sort)` | `int` | Set one author's per-author sort key (since 1.25.0, upstream `set_sort_for_authors`): `authors.sort` verbatim (empty raises), then every book of the author recomputes `books.author_sort` (" & "-joined, link order) and queues OPF resync. Exact-spelling-first NOCASE name resolution; returns the affected book count. |
| `set_link_map(kind, value_to_link, *, only_set_if_no_existing_link=False)` | `int` | Write link URLs per value (since 1.25.0, upstream's generic `set_link_map`): `{value: link}` over authors/series/publishers/tags or a custom column by `#label` (the per-value `link` column of its value table). Names resolve exact-spelling-first with NOCASE fallback; unknown values raise (upstream skips silently); `None` clears; the fill-in-the-blanks mode leaves values that already carry a link. Touched books queue OPF resync; returns the affected book count. |
| `remove_entity_everywhere(kind, name)` | `int` | Remove an author/series/publisher/tag from every book (since 1.19.0): links first (the fkc order), then the row; series removal also resets `series_index` to 1.0 (the NOT NULL column's no-series value). Returns the affected book count; an unknown name is an honest 0. |
| `set_cover(book_id, data)` | `bool` | Write the cover file (JPEG/PNG sniff-or-raise; since 1.19.0) and set `has_cover`; a stale cover under the other extension is removed. The file lands only after the commit. |
| `remove_cover(book_id)` | `bool` | Clear the cover (since 1.19.0): both cover files swept, `has_cover` cleared, OPF resync queued. True when the flag actually changed. |
| `set_author_sort(book_id, value)` / `set_title_sort(book_id, value)` | `bool` | Verbatim sort corrections (since 1.19.0); empty raises; a later `set_authors`/`update_title` recomputes over them. |
| `set_timestamp(book_id, value)` | `bool` | Set the addition timestamp (since 1.19.0), normalized like `set_pubdate`; `None` writes the sentinel. |
| `save_original_format(book_id, fmt)` | `bool` | Copy the format as an `ORIGINAL_<FMT>` row + file (since 1.19.0) for undo-able repair; False when the format/file is missing; saving an original of an original raises. Since 1.20.1 the sidecar is attached before the save's transaction opens, so the ORIGINAL row's queue entry lands even when the save is the connection's first format verb. |
| `restore_original_format(book_id, original_fmt)` | `bool` | Swap the ORIGINAL bytes back into the format (since 1.19.0), remove the original row/file/queue entry, and queue the restored format for FTS re-extraction. |
| `fts_reindex_book(book_id, fmts=None)` | `int` | Queue a book's formats for FTS re-extraction and a pages rescan (since 1.24.0, upstream `reindex_fts_book`): `fmts=None` queues every catalogued format, explicit formats queue verbatim, already-queued pairs do not duplicate. Returns the inserted count; 0 when the sidecar is absent (nothing to write to). |
| `fts_reindex_all()` | `int` | Queue every catalogued format for re-extraction (since 1.24.0, upstream's `dirty_existing` sweep): the close cousin of upstream's delete-the-sidecar `reindex_fts` that keeps the index tables untouched and lets Calibre's extraction pool re-write every row through its own triggers. |
| `fts_queue_clear(book_id=None, fmt=None)` | `int` | Remove FTS extraction-queue entries (since 1.24.0): everything, one book's, or one pair (`fmt` without `book_id` raises). The queue half of upstream's `fts_unindex`/`remove_dirty`/`clear_all_dirty`; the index rows themselves are process-bound (the `books_text` delete triggers tokenize through Calibre's custom FTS5 tokenizer), so removing indexed text stays Calibre's job. |
| `maintain(*, vacuum=True, analyze=True, integrity_check=False, include_fts=True)` | `dict[str, Any]` | Vacuum / analyze / integrity-check the library DB and its attached FTS sidecar (since 1.24.0, upstream `backend.py` `vacuum` plus the two statements calibredb users run by hand; the notes DB stays out of scope). Returns `{vacuumed, analyzed, fts_attached, integrity_check, fts_integrity_check}`; the check rows are `["ok"]` on a healthy file and `None` when not requested. Raises `RuntimeError` inside `batch()` or mid-transaction (VACUUM cannot run in a transaction). |
| `set_preference(key, value)` | `bool` | Typed upsert of one search-grammar preference row (since 1.24.0): `saved_searches`, `virtual_libraries`, `user_categories`, `grouped_search_terms` (each validated by key before anything lands), or `fts_enabled` (bool). Stored as one JSON row in Calibre's shape; an equal payload is an honest False. |
| `saved_search_add(name, expression)` / `saved_search_delete(name)` / `saved_search_rename(old, new)` | `bool` | Single-entry saved-search mutations (since 1.24.0, the calibredb `saved_searches` parity item): add is a stripped upsert, delete pops the exact stored spelling (an unknown name is an honest False), rename resolves case-insensitively to the stored spelling and REFUSES to overwrite an existing name where upstream silently overwrites. |
| `create_custom_column(label, name, datatype, *, is_multiple=False, editable=True, display=None)` | `int` | Create a custom column with upstream's exact DDL (since 1.20.0); returns the column number. Label rules are upstream's (lowercase word chars, letter first). |
| `delete_custom_column(label)` | `bool` | Flag the column `mark_for_delete=1` (since 1.20.0) -- the physical purge is Calibre's next-startup job; the column stays functional until then. |
| `set_custom_column_metadata(label, *, name=None, editable=None, display=None)` | `bool` | Modify an existing column's name, editable flag, or display payload (since 1.24.0, upstream `backend.py:1407`): the verb that populates an enumeration's `enum_values` without Calibre open (the display dict replaces the stored payload wholesale). Honest no-op on equal values; a change sets `update_all_last_mod_dates_on_start` like upstream so the next Calibre start refreshes every book. No datatype or label parameter: type is baked into the storage layout and the label is the `#label` search token, so both are refused by omission. |
| `list_trash()` | `list[dict[str, Any]]` | Inventory the trash (since 1.20.0): `{category, book_id, mtime, files}` per entry across `.caltrash/b` and `.caltrash/f`. |
| `empty_trash()` | `int` | Permanently remove every trash entry and recreate the empty directories (since 1.20.0); returns the count. |
| `expire_trash(older_than=None)` | `int` | Remove trash entries older than the age (since 1.20.0; seconds or `timedelta`; upstream's 14-day default; `<= 0` expires all). |
| `copy_format_from_trash(book_id, fmt, dest)` | `str` | Copy a trashed format file (stored under its bare extension in `.caltrash/f/<id>/`) out to `dest` (since 1.24.0); returns the absolute path. Pure filesystem; raises when the entry or file is missing. |
| `copy_book_from_trash(book_id, dest)` | `str` | Copy a trashed book directory (OPF sidecar, covers, format files) out to `dest` (since 1.24.0); returns the absolute path. Pure filesystem; raises when the entry is missing. |
| `move_format_from_trash(book_id, fmt)` | `bool` | Undelete one trashed format into its live book (since 1.24.0): re-registers the `data` row under the book's shared filename stem, places the file after the commit, queues FTS re-extraction, and removes the trash entry once only `metadata.json` remains. |
| `move_book_from_trash(book_id)` | `None` | Undelete a whole trashed book (since 1.24.0): rebuilds the row from the entry's sidecar `metadata.opf` (title/title sort, authors with the OPF's `author_sort` verbatim, tags, identifiers, comments, publisher, languages, pubdate, timestamp, series, rating, preserved uuid), re-registers every other extension-bearing file, sets `has_cover` from a cover file, queues OPF resync + FTS re-extraction, and moves the directory back after the rows commit. The id must NOT exist; custom columns/annotations/plugin data are not restored (the OPF carries none of them). |
| `delete_trash_entry(book_id, category)` | `bool` | Remove one trash entry outright (since 1.24.0; `category` is `'b'` or `'f'`). Irreversible; honest `False` when absent. |
| `set_series(book_id, name, index=None)` | `bool` | Assign/clear series + `series_index` (defaults 1.0 fresh, preserves on reassign). Clearing (`name=None`) deletes the link and resets `series_index` to 1.0 -- the column is NOT NULL in real libraries (`REAL NOT NULL DEFAULT 1.0`), so Calibre's no-series state is index 1.0 with no link row, never NULL. |
| `set_series_index(book_id, index)` | `bool` | Set the series index verbatim (since 1.23.0): updates `books.series_index` in place, the link row untouched -- no delete-and-reinsert. The book must already belong to a series (`set_series` assigns one); `None` raises. Honest no-op on an equal value. |
| `set_pages(book_id, pages, *, algorithm=0, format="", format_size=0)` | `bool` | Set the page-count row (since 1.25.0, upstream `set_pages`): a frontend that computes page counts itself records the value and clears `needs_scan` -- a real value is what the pending rescan waited for. The `book` column is the table's PRIMARY KEY, so an existing row is replaced; the book queues OPF resync. Honest no-op on an identical row with a clean flag (a pending scan always rewrites). Raises `ValueError` for negative `pages`, an unknown book, or a schema predating the native table. |
| `add_data_file(book_id, relpath, data, *, replace=False, auto_rename=False)` | `str \| None` | Write one extra file into the book's `data/` directory (since 1.25.0, upstream `add_extra_files`): `data` is bytes or a source path, parents created, the relpath actually written returned. Existing target: `replace` overwrites, `auto_rename` writes beside it under `merge conflict[N]/` (upstream's layout), neither answers None. Pure filesystem -- extra files have no database rows. Unsafe relpaths and pathless books raise. |
| `rename_data_file(book_id, relpath, new_relpath, *, replace=False)` | `bool` | Move one extra file within `data/` (since 1.25.0): False when the source is absent or the target exists without `replace`. |
| `remove_data_files(book_id, relpaths)` | `dict[str, Exception \| None]` | Delete extra files from `data/` permanently (since 1.25.0; the recycle-bin mode is a GUI concept with no library-side form): `{relpath: None}` on success, the `OSError` per failure. |
| `copy_book_from_library(src_db, book_id, *, preserve_timestamp=True)` | `int` | The blessed cross-library copy (since 1.25.0, upstream `copy_one_book` composed from sanctioned reads + writes): core fields, format FILES (through `add_book`'s seed path), cover bytes, per-author sort keys then the book-level `author_sort` verbatim, the timestamp unless `preserve_timestamp=False`, and conversion options (the blob verbatim; plugin data stays put like upstream's copy). One `batch()` on the destination -- any failure rolls everything back. Not carried, each for a recorded reason: annotations (the postprocess decline), `data/` extras, custom columns, and the uuid (always fresh, upstream's default). Duplicate policy stays with the frontend; byte-identical re-imports raise. |
| `set_plugin_data(book_id, name, val)` | `bool` | Upsert one `books_plugin_data` row (since 1.25.0, upstream `add_custom_book_data` per book); `None` deletes. `str` stores verbatim, other JSON-serializable payloads go through `json.dumps` (upstream's serialization). Not OPF-visible, so nothing queues. |
| `set_conversion_options(book_id, data, *, fmt="PIPE")` | `bool` | Upsert one `conversion_options` blob (since 1.25.0, upstream `set_conversion_options`); `None` deletes. Passthrough by recorded scope: bytes verbatim, `str` UTF-8 encoded -- upstream pickles recipe payloads in-process, and reproducing that serialization is the caller's boundary. |
| `set_book_storage(book_id, fmt, data, *, user_type="local", user="viewer")` | `bool` | Upsert one `book_storage` entry (since 1.25.0, upstream `update_book_storage_for_book`, the viewers' per-book localStorage); `None` deletes. The `data` column stores ONLY the str->str payload map, JSON-encoded with `ensure_ascii=False` exactly like upstream (the timestamp is its own column) -- the 1.25.0 wrapped-entry shape was a bug that made every row unreadable by Calibre's viewer. Upstream's guards reproduced: non-str keys/values raise, the 1 MiB UTF-16 cap raises, and an older entry never overwrites a newer one (False). Schemas predating the table raise `ValueError`. |
| `get_book_storage(book_id, fmt, *, user_type="local", user="viewer")` | `dict \| None` | The read half (since 1.26.0, upstream `Cache.book_storage_for_book`): `{'timestamp', 'data'}` reassembled from the row's columns like upstream, or None when absent, the schema predates the table, or the stored payload fails upstream's str->str validation. |
| `set_publisher(book_id, name)` | `bool` | Replace/clear publisher; case-insensitive match; orphans pruned. |
| `set_rating(book_id, stars)` | `bool` | 0-5 stars stored as x2; UNIQUE(rating) rows deduplicated via find-or-create. |
| `clear_rating(book_id)` | `bool` | Self-documenting alias of `set_rating(book_id, None)` (since 1.13.0); the orphaned rating row prunes with it. False when the book was already unrated. |
| `set_languages(book_id, codes)` | `bool` | Replace languages (supports `list[str]` or comma-separated `str`); English names canonicalized to ISO 639-2 via the search engine's map. |
| `set_comments(book_id, text)` | `bool` | 1:1 upsert/clear of the comments HTML row. |
| `set_pubdate(book_id, value)` | `bool` | Publication-date setter accepting `str` / `date` / `datetime` / `None` (sentinel); stored as Calibre TEXT in UTC. |
| `set_custom_column(book_id, label, value)` | `bool` | Generic custom-column writer: storage layout auto-detected (link-table vs direct) with a datatype dispatch; unknown datatypes (or a datatype on the wrong layout) raise instead of being stringified. Enumerations validate against `display.enum_values` (an empty `enum_values` rejects every value), tristate bools accepted, rating-typed columns take 0-5 stars (stored x2 on Calibre's internal 0-10 scale like `set_rating`; 0 stars clears), datetime-typed columns normalize like `set_pubdate` (ISO text in UTC), non-editable/composite columns raise. |
| `add_custom_column_values(book_id, label, values)` | `int` | Append semantics for multi-valued (Pattern A) columns (since 1.13.0): dedupes against the book's existing values (the link table is `UNIQUE(book, value)`) and within the input, inserts only the new links, returns the honest count; a no-op bumps nothing. Single-valued and direct-storage columns raise toward `set_custom_column`; a bare string raises `TypeError` rather than being comma-split; `None` entries inside `values` are skipped (never stringified into `'None'`); enumeration-typed columns validate each appended value. |
| `add_format(book_id, fmt, name, size)` / `remove_format(book_id, fmt)` | `bool` | Register/drop `data` rows (the file itself is the caller's responsibility). |
| `set_format(book_id, fmt, name, size)` | `bool` | Sanctioned replace of one format row, remove+add in one transaction (since 1.17.0). `True` when a row was written, `False` when the identical row already exists; the file swap itself stays the caller's atomic-replace job. Since 1.18.0 the replacement is queued for FTS re-extraction and a pages rescan in the sidecar when it exists. |
| `set_has_cover(book_id, has_cover)` | `bool` | Toggle the catalogued flag. |
| `add_book(title, authors, *, formats=None, cover=None, identifiers=None, language=None, pubdate=None, publisher=None, dry_run=False)` | `int \| dict` | The creation path (since 1.14.0): inserts `(title, series_index, author_sort)` first and lets `books_insert_trg` fill `sort`/`uuid`, then links authors/identifiers/language/pubdate/publisher, copies format files and an optional cover (JPEG/PNG sniff-or-raise) into the `Author/Title (id)` directory with truthful `data` rows, and queues OPF resync. Since 1.20.1 every seeded format is also queued for FTS re-extraction and a pages rescan (upstream's add path queues every `data` INSERT). ONE `batch()`: any failure rolls the SQL back and removes the created directory; inside a caller's shared `batch()`, a failure ANYWHERE in the pass also removes the directories of books added earlier in that same batch (batch-scoped filesystem compensation), while books added outside the failing batch are never touched. A format seed byte-identical to an already-catalogued file raises `ValueError` before anything is written (dry runs included; the 'never imported twice' invariant since 1.15.0; metadata-based duplicate screening stays a frontend concern). Empty title becomes `Unknown` (Calibre parity); an empty author list is legal (no links, what `find_authorless` expects). `dry_run=True` writes nothing and returns the plan (predicted id from `sqlite_sequence`, resolved authors, format filenames, the row diff). Copy only: sources are never moved or deleted. |
| `remove_book(book_id, delete_files=None)` | `None` | Full book removal: custom columns (both patterns) + all dirtied queues cleaned (metadata, annotations, and the FTS sidecar's `dirtied_formats` for the book's formats when the sidecar exists; since 1.20.1), cascade trigger fires, orphaned entities pruned. Irreversible (the rows). `delete_files` extends the removal to the book's on-disk directory (since 1.17.0): `"trash"` moves it into the library-local `.caltrash/b/<id>/` (upstream's own layout, recoverable by hand), `"permanent"` deletes it outright, `None` leaves the files for the caller. File removal lands only after the rows COMMIT -- inside a `batch()` it defers to the outermost commit, so a rollback never leaves resurrected rows fileless. |
| `batch()` | `ContextManager` | Defer commits across a multi-book, multi-field pass. Explicit context manager that batches writes into one transaction. Since 1.20.1 the batch state (tracked directories, poison flag) resets even when a post-commit flush raises, so a flush failure never leaves a later failed exit deleting committed books' directories; a queued removal retried by a later flush skips directories that are already gone. |
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
| `find_missing_format_files(db)` | `list[int]` | Catalogued format rows whose file is absent on disk (since 1.23.0; rides `get_format_path`'s verified resolution, the same way the cover check rides `get_cover_path`; empty `books.path` skipped by the same rule). Closes the family's last disk hole beside `find_formatless` (no rows) and `find_missing_cover_files` (the cover). |
| `check_library_disk(db, *, name_ignores=(), extension_ignores=())` | `dict[str, list[dict[str, Any]]]` | The extra-side disk checks of upstream's `check_library` (since 1.24.0): one walk over the library tree, categorized findings (`extra_titles`, `extra_authors`, `malformed_paths`, `malformed_formats`, `extra_formats`, `extra_files`, `extra_covers`, `failed_folders`), each entry `{"book_id", "title_dir", "path"}` (plus `"error"` on failed folders; `book_id` 0 when the finding belongs to no catalogued book). The missing side is deliberately not re-answered (`find_missing_format_files`/`find_missing_cover_files` own it). POSIX case sensitivity assumed; the format-vs-unknown-file classifier is structural (a token extension minus the image/OPF/junk set), not upstream's curated book-extension list. |
| `find_deprecated_formats(db, formats)` | `list[int]` | Books whose whole format set sits inside the caller's deprecated set (case-insensitive). cquarry owns the subset mechanism; what counts as deprecated is a curation opinion. Formatless books excluded. |
| `find_low_res_covers(db, min_dimension=500)` | `dict[int, tuple[int, int]]` | `{id: (w, h)}` for resolvable, parseable covers under the dimension floor. Missing files are `find_missing_cover_files`' answer; unreadable images are skipped. |
| `find_duplicate_books(db)` | `dict[tuple[str, str], list[int]]` | `(title.lower(), primary_author.lower())` groups with more than one member. |
| `find_series_gaps(db)` | `dict[str, list[int]]` | `{series: [missing indices]}` composing `get_all_series()` + `detect_series_gaps()`. |
| `find_identifierless(db)` | `list[int]` | Books carrying no identifiers at all (since 1.17.0; promoted from Hermitage's inline Insights predicate). |
| `find_invalid_uuids(db)` | `list[int]` | Books whose `uuid` is empty or does not parse as a UUID (since 1.21.0; the reader degrades pre-`uuid`-column schemas to `""`, which reports honestly). |
| `find_sentinel_pubdates(db)` | `list[int]` | Books whose `pubdate` is the undefined-date sentinel `0101-01-01` (or the `0100-01-01` ancestor -- the same pair the search engine treats as dateless; since 1.21.0). |
| `find_bad_language_codes(db)` | `list[int]` | Books linked to a language code that is not an ISO 639-2 code (not exactly three lowercase ASCII letters: bare names like `English`, two-letter codes, empty strings; since 1.21.0 -- shape check only, so a valid rare code never false-positives). |

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

*(Note: presence vocabulary is split by datatype (since 1.18.0, upstream parity): numeric, rating, and date locations take exactly `true`/`false` as presence/absence words, and any other word must parse as a number or date or the query raises `ParseException`. Boolean locations are the ones that accept the full tristate set: `true`/`yes`/`checked`, `false`/`no`/`unchecked`/`blank`/`empty`, and the `_`-prefixed variant of each; anything else on a boolean location raises. An empty query after ANY location matches nothing, never everything (since 1.16.0). Dates accept both `-` and `/` separators, and undefined date sentinels (`0101-01-01`, `0100-01-01`) evaluate as `None`. Multi-token queries in `languages:` split on commas and canonicalize each independently; two-letter ISO 639-1 codes canonicalize too (`languages:ja` matches `jpn`).)*


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
| `all` | *(bare terms)* | | Searches title, authors, author_sort, series, publisher, tags, comments, formats, languages (canonicalized) + custom text columns; a bare term never text-matches identifier keys or values (the identifiers store joins only the `true`/`false` presence test); numeric fields are probed by exact equality (dates match nothing) |

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
