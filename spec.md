# cquarry specification

The contract. Read this before changing semantics.

**Project:** `cquarry`  
**Version:** 1.18.0
**Role:** Headless Engine (Standalone Library)
**Language:** Python 3.14+
**Dependencies:** None (pure stdlib)
**License:** MIT

## 1. Mission

Provide a single, canonical Calibre database and search evaluation engine for the Python ecosystem: every read path is strictly read-only, and the only sanctioned write path lives in an explicitly opt-in module (§3.6). By centralizing the parsing of Calibre's search grammar, cquarry ensures that all consumers (CLI, web, GTK4) agree exactly on which books match a given query or virtual library definition.

## 2. Hard constraints

These are invariants. Violating any of them is a spec breach.

- **No external dependencies.** The package runs on the Python 3.14+ standard library alone: `sqlite3`, `re`, `json`, `unicodedata`, `struct`, `datetime`, `os`, `sys`, `shutil`, `tempfile`, `urllib.parse`. No PyPI packages, no optional extras.
- **Read-only by design.** The read path (`CalibreDB`) never writes to `metadata.db`: it opens the database with `?mode=ro` and issues only `SELECT` statements. The lock-escape path copies the database to a temp file rather than attempting a write lock. Write access exists only in the separate, explicitly opt-in `cquarry.write` module (§3.6), which is never reachable through the read-only `CalibreDB` class and must be imported on purpose.
- **Search parity.** `SearchEngine` must behave like Calibre's native search bar. This includes implicit AND evaluation, hierarchical tag anchoring, identifier keypair routing, date math with precision levels, custom column type dispatch, saved-search interpolation, multi-valued count operators, language canonicalization, and virtual library cross-reference recursion detection. Documented deviations (see §5) are acceptable only when they are dependency-bound (ICU, the `regex` module) or GUI-state-bound, and do not change the result set for queries a user would plausibly write.
- **No silent data loss.** The lock-escape snapshot copies `-wal` and `-shm` alongside the main database file. A snapshot that omits these can silently read stale data. The temp files are cleaned up on `close()` and on context manager exit.

## 3. Architecture

### 3.1 The database layer (`db.py`)

`CalibreDB` is the primary public interface.

**Connection management.** The constructor takes a path to `metadata.db`, opens it read-only via a percent-encoded `file:` URI (§3.4), and issues a `SELECT 1 FROM books LIMIT 1` probe. If the probe raises `OperationalError` with "locked" in the message, the constructor copies the database (and its `-wal`/`-shm` sidecars) to a temp file and opens the copy instead, printing a notice to stderr. The temp path is stored and cleaned up by `close()`.

**Caching.** `get_all_books()` executes a 6-JOIN query over `books` (series, ratings, publishers) and hydrates the list-type fields in Python from per-table reads; the result is cached. `get_virtual_libraries()` reads the `preferences` table once and caches the dict. `count_books()` uses the books cache or the all-IDs cache if either is populated, falling back to a raw `COUNT(*)`. All caches are populated lazily on first access and are otherwise never invalidated (the database is read-only and the connection is short-lived); `refresh()` (cquarry >= 1.16) clears them all in one call, giving long-lived holders a coherence boundary after an external write. To prevent memory exhaustion on large libraries, massive text blocks like `comments` and custom columns of type `comments` are strictly lazy-loaded on-demand per book ID rather than eager-loaded during search view construction.

**Custom column dispatch.** `load_custom_column()` checks `sqlite_master` for the existence of `books_custom_column_N_link` to decide between the normalized path (text, enumeration, series, rating: value table joined through a link table) and the direct path (int, float, bool, datetime, comments: value table with a `book` column). This is safer than keying off `is_multiple`, because a single-valued enumeration is normalized but not multi-valued. Multi-valued columns yield native `list[str]` values end to end (cquarry >= 1.16): the old comma-join-on-load, re-split-on-use round-trip turned stored values like `Doe, John` into phantom values.

**Paginated listing (cquarry ≥ 1.10).** `list_books(ids=, sort=, descending=, offset=, limit=)` pages the cached rows for frontends that resolve an id set through the search engine and then paginate. `sort` takes one key or a sequence (primary first, one direction for all — the author-sort/series-name/series-index shape). Pure over the cache (no SQL of its own); None-valued sort keys sort last regardless of direction; unknown sort keys and negative offsets/limits raise.

**Custom column lookup parity (cquarry ≥ 1.9).** Columns are addressable by `#label`, bare label, or display name through `find_custom_column()` / `load_custom_column()`: a leading `#` matches the label only (labels are unique, never ambiguous), otherwise an exact display-name match wins (the historical key, so existing callers keep working) and a bare label is the graceful fallback; label matching is case-insensitive, mirroring the write module's `_custom_column_meta`. `get_custom_columns()` stays keyed by display name.

**MetadataProvider implementation.** `CalibreDB` implements the `MetadataProvider` protocol (§3.2) so that `SearchEngine` can be constructed with it directly. The provider methods (`all_ids`, `field`, `vl_expression`, `custom_locations`) are public but are primarily the search engine's interface; most consumers use `search()` and `resolve_vl()` instead.

### 3.2 The search engine (`search.py`)

A three-stage pipeline.

**Stage 1: Lexer.** A `re.finditer` scanner over a single documented pattern tokenizes the input into opcodes (`(`/`)`), words (including `@...:` template tokens), and quoted words. Backslash escapes (`\\`, `\"`, `\(`, `\)`) are handled by a replacement/unreplacement cycle using low control characters as sentinels. Unmatched characters raise `ParseException`. (v1.1: replaced the undocumented `re.Scanner`.)

**Stage 2: Parser.** A recursive-descent parser (`_Parser`) builds an AST of `["and", lhs, rhs]`, `["or", lhs, rhs]`, `["not", child]`, and `["token", location, query]` nodes. Operator precedence: `not` binds tightest, then `and` (including implicit AND between adjacent terms), then `or`. Parentheses override precedence. A bare term (no `location:` prefix) is assigned location `"all"`. Raises `ParseException` on malformed input.

**Stage 3: Evaluator.** `SearchEngine._evaluate()` traverses the AST with candidate-set semantics (matching Calibre's `and`/`or`/`not` behavior):
- `and`: evaluate the left side, then evaluate the right side against only the left's matches.
- `or`: evaluate the left side, then evaluate the right side against the candidates minus the left's matches, and union the results.
- `not`: subtract the child's matches from the candidates.

For `token` nodes, the evaluator dispatches to a type-specific matcher based on the field's datatype.

**Match kinds.** The query string prefix determines the match semantics:
- No prefix: substring match (case- and accent-folded via NFKD decomposition).
- `=`: exact match (folded). Leading-dot modifiers apply to every text field: `.foo` matches the subtree rooted at `foo`, `..foo` matches a single dot-delimited component exactly.
- `~`: regex match (stdlib `re`, case-insensitive).
- `^`: accent-folded substring (same as default; exists for Calibre grammar compatibility).
- `\`: escape (the next character is literal).

**Hierarchical tags.** The `tags` location uses anchored prefix matching: query `Foo` matches tag `Foo` and any tag starting with `Foo.` (e.g. `Foo.Bar`, `Foo.Bar.Baz`), but not `Foobar`. This is a cquarry invariant, not a Calibre port; Calibre uses raw substring matching by default.

**Multi-valued count operator.** Any multi-valued location (`authors`, `formats`, `languages`, `tags`, `identifiers`) accepts `#<relop><n>` comparing the value count: `tags:#>3`, `identifiers:#=0`. Malformed counts raise `ParseException`.

**Language canonicalization.** The `languages` location canonicalizes English names to ISO 639-2 codes before matching (`English` → `eng`); unknown tokens pass through as raw text. Multi-token queries split on commas and canonicalize each token independently.

**Numeric fields.** `rating`, `id`, `series_index`, `size`, `pages`, and numeric custom columns support relational operators (`=`, `>`, `<`, `>=`, `<=`, `!=`) and exactly two presence/absence keywords, `true`/`false` (cquarry ≥ 1.18, upstream parity: the tristate vocabulary is bool-only, and any other non-numeric word raises). Size suffixes (`k`, `m`, `g`) are supported on all float/int locations, so `size:>10m` works. Rating `false` matches `None` (and a stored `0`, which Calibre never writes); rating `true` matches any positive value. Rating-typed custom columns share the builtin rating's scale end to end: the writer takes 0-5 stars, Calibre's stored 0-10 internal value is surfaced as stars, so `#myrat:4` means 4 stars exactly like `rating:4`. An empty numeric query (`rating:` with no value) matches nothing rather than erroring; upstream `NumericSearch` parity, extended (cquarry >= 1.16) to every location: an empty query after ANY location matches nothing, never everything.

**Date fields.** `pubdate`, `timestamp`, `last_modified`, and date custom columns support the same relational operators, plus the keywords `today`, `yesterday`, `thismonth`, and `N daysago`. Presence is the exact word `true`/`false` (cquarry ≥ 1.18, upstream parity): any other word must parse as a date or the query raises, and match-kind prefixes (`~`, `^`) are not date vocabulary. Dates can be specified at year (`2024`), month (`2024-06`), or day (`2024-06-15`) precision, with either `-` or `/` separators; the comparison respects the precision level. Calibre's undefined-date sentinels (`0101-01-01`, `0100-01-01`) are treated as `None`.

**Identifiers.** The `identifiers` location supports keypair search: `identifiers:isbn:VALUE` matches the `isbn` key with a value match, `identifiers:true` tests for any identifier, and `isbn:VALUE` is shorthand for `identifiers:=isbn:VALUE`. Match kinds apply independently to both the key and value halves.

**Virtual library cross-references.** `vl:"Name"` resolves the named virtual library's search expression and evaluates it recursively, intersecting the result with the current candidate set. Circular references raise `ParseException`; unknown names also raise `ParseException` (no silent empty sets). Name resolution is case-insensitive at both engine and DB layers.

**Saved-search interpolation.** `search:"Name"` behaves identically to `vl:` but resolves against the provider's saved searches (Calibre's `preferences.saved_searches`). Nested references compose; cycles and unknown names raise `ParseException`.

**Grouped search terms.** Calibre's `preferences.grouped_search_terms` maps a group name to member search locations; the engine resolves `GroupName:query` (and `@GroupName:query`) as the union over members, each evaluated without further group recursion; nesting is a `ParseException`. `GroupName:false` matches books where no member matches (upstream's inversion). Real field names always win over same-named groups.

**User categories.** Calibre's `preferences.user_categories` defines tag-browser pseudo-categories; `@Name:query` (cquarry ≥ 1.6) mirrors upstream's `get_user_category_matches`: a category matches the books holding any member value, probed as an exact (`=`) match on the member's own location (e.g. `tags`, `authors`, `publisher`, `#label`). A leading `.` (`@Name:.query`) includes subcategories (`Name.`-prefixed); `false` inverts; any other query text is ignored exactly as upstream (the GUI always writes `@Name:true`); queries shorter than two characters match nothing. Groups and real fields win over same-named categories; unknown `@Names` match nothing (never an `all:` text sweep).

**Annotation search.** The `annotations` location exposes each book's concatenated annotation `searchable_text`; presence keywords (`true`/`false`) and all text match kinds work against it. Bare terms (`all`) never sweep annotation text, mirroring upstream. Within a stored `searchable_text`, an annotation's notes follow its highlighted text joined by `\n\x1f\n` (LF, ASCII unit separator, LF; upstream `annot_db_data`), so consumers splitting highlights from notes split on that byte sequence.

**The `all` pseudo-location.** Bare terms search `title`, `authors`, `author_sort`, `series`, `publisher`, `tags`, `comments`, `formats`, and `languages` (canonicalized), **plus** any custom column whose engine datatype is text-like (text, text_multi, hier). With contains semantics, the exact words `true`/`false` are the presence test across the whole sweep (upstream parity, cquarry ≥ 1.18): `true` matches books where any swept field holds a non-blank value, with the identifiers store and the cover flag counting as present when non-empty or set; a bare term never text-matches identifier keys or values (the pre-1.18 claim that keys sweep as text was wrong, and the code now matches upstream instead of the claim). Numeric probes are exact equality on `series_index`, `rating`, `pages`, and `size`; `id` takes no part (upstream excludes it from the sweep) and dates match nothing in the sweep, so a bare year matches nothing by itself.

### 3.3 Helpers (`helpers.py`)

Domain-specific utilities shared across the ecosystem. These are public API; downstream consumers import them.

- **Database discovery** (`find_db`): a four-stage resolution chain (explicit arg, saved config, default paths, interactive prompt).
- **Rating conversion** (`normalize_rating`, alias `calibre_rating_to_stars`, `format_stars`): Calibre stores ratings on a 0-10 scale; the portfolio displays them on 0.0-5.0 with Unicode star glyphs.
- **Author formatting** (`normalize_author_display`, `author_sort_key`): comma-separated to ampersand-joined display, with a `primary_only` mode.
- **Series analysis** (`detect_series_gaps`): given a series' known indices, return the missing integers.
- **Image dimensions** (`get_image_size`, `get_jpeg_size`, `get_png_size`): header-only dimension reads for cover quality auditing. The JPEG reader seeks through segment markers rather than reading a fixed buffer, so large EXIF/ICC blocks do not hide the SOF.
- **Comments sanitization** (`strip_html`): reduces comments HTML payloads to plain text; drops tags (including `script`/`style` bodies), unescapes entities, collapses whitespace, converts block boundaries to newlines. Consumers must run raw HTML through this before terminal or GTK label rendering.
- **Taxonomy parsing** (`tags_to_tree`): builds nested dictionaries from dot-delimited hierarchical tags for tree rendering.
- **Terminal color** (`color`): TTY-aware ANSI wrapping with predefined codes.

### 3.4 URI encoding (`db_uri_ro`)

SQLite's `file:` URI mode parses `?` as query-string and `#` as fragment. A library path like `Books #2/metadata.db` must be percent-encoded or it opens a different file and fails with "no such table: books". `db_uri_ro()` uses `urllib.parse.quote()` (which leaves `/` alone) and appends `?mode=ro`.

### 3.5 Config (`config.py`)

A JSON file at `~/.config/cquarry/config.json` persists the database path across sessions. `get_db_path()` and `set_db_path()` are the read/write interface; `load_config()` and `save_config()` are the underlying I/O. The config is user-facing (the TUI and `find_db()` interactive prompt write to it), not an internal cache.

### 3.6 Write access (`write.py`); opt-in

`WritableCalibreDB` is the only sanctioned mutation path. It is a distinct class precisely so no read-only code path can reach it.

**Safety contract:**
- Registers Calibre's trigger dependencies before any statement runs: `title_sort()`, `uuid4()`, and the `PYNOCASE` collation. Calibre's `books_insert_trg` / `books_update_trg` abort any write when these are missing.
- Opens with a 30 s `busy_timeout` so a running Calibre degrades writes to waiting rather than erroring.
- Mutations run in explicit `BEGIN IMMEDIATE` transactions, bump `books.last_modified`, AND insert the book id into `metadata_dirtied` (`INSERT OR IGNORE`). Calibre regenerates a book's sidecar `.opf`; and re-pushes metadata to wireless readers; only for ids present in that table (backend.py `dirty_books()` / `dirtied_books()`); it consumes and clears the queue at startup. Skipping the insert would leave external edits invisible to OPF/wireless sync forever. The insert is guarded by a cached `sqlite_master` existence check so schemas predating the table keep working.
- Tag removal deletes link-table rows before possibly pruning the orphaned tag; the order `fkc_delete_on_tags` requires.

**API:** `update_title(book_id, title)` (refreshes `sort` via `title_sort`), `add_tag` / `remove_tag` (link-table sequence), `set_identifier` / `set_identifiers` (EAV upserts; `None` deletes; both halves cleaned like upstream's `clean_identifier` since 1.18: type stripped/lowercased/`:`+`,`-stripped, value ``,`->`|`), `clear_identifier` (cquarry ≥ 1.9: deletes one pair by type, normalized like `set_identifier`; honest no-op when absent; queues OPF resync), plus the write-side expansion (cquarry ≥ 1.5): `set_authors` (relinks + recomputes `books.author_sort` from per-author sort keys joined " & "), `set_series` (+`series_index`, default 1.0 fresh / preserved on reassign), `set_publisher`, `set_rating` (0–5 stars stored ×2; UNIQUE(rating) rows found-or-created), `set_languages` (English names canonicalized through the engine's lang map), `set_comments` (1:1 upsert/clear on the UNIQUE(book) row), `set_custom_column` (storage layout auto-detected via link-table existence; Pattern A value+link vs Pattern B direct; datatypes dispatched against Calibre's set so unknown types and layout mismatches raise instead of stringifying; enumerations validated against `display.enum_values` with an empty set rejecting every value; rating-typed columns take 0-5 stars stored x2 on the shared internal 0-10 scale exactly like `set_rating`, 0 stars clearing; datetime-typed columns normalized like `set_pubdate`; tristate bools accepted; non-editable and composite columns raise), the set-write conveniences (**cquarry ≥ 1.13**: `clear_tags` deletes the book's tag links and prunes orphaned tag rows for an honest count; `clear_rating` is the audit-trail alias of `set_rating(book_id, None)`; `add_custom_column_values` appends to `is_multiple` Pattern-A columns with the `UNIQUE(book, value)` dedupe, single-valued and direct-storage columns raising toward `set_custom_column`), `add_format` / `remove_format` (data-row registration; files are the caller's responsibility), `set_has_cover`, and `remove_book` (custom columns in both patterns + dirtied queues cleaned first, cascade trigger fires, orphaned entities pruned after). Entity relinks prune now-orphaned entity rows once their links are gone. **`set_author_sort`/`set_title_sort`/`set_timestamp` (cquarry ≥ 1.19)** are verbatim passthrough corrections for mangled sorts and the addition timestamp; recomputing setters (`set_authors`, `update_title`, renames) overwrite an override by design. **`set_pubdate` (cquarry ≥ 1.7)** accepts `str | date | datetime | None`, stores `datetime.isoformat(' ')` in UTC (the column is TEXT; naive values are taken as UTC), writes the `0101-01-01` sentinel for `None`, and is no-op honest: an equal instant does not bump `last_modified` or queue OPF resync. **`batch()` (cquarry ≥ 1.7)** is a context manager moving the commit boundary to the end of the block: `BEGIN IMMEDIATE` at outermost entry, nested batches join the one transaction, and any exception rolls the whole pass back while every setter keeps its signature and per-call semantics.

The read side exposes the same queue for observability: `CalibreDB.get_dirtied_books()` returns the sorted, deduplicated ids awaiting resync (empty list when the table is absent). It never clears the queue; that remains Calibre's job (`mark_book_as_clean()`). The annotations sibling queue gets the same treatment: `get_annotations_dirtied_books()` observes `annotations_dirtied`, the ids Calibre pushes highlights/bookmarks from.

**Book creation (`add_book`, cquarry ≥ 1.14).** The sanctioned insertion path: one `batch()` covering the `books` row (inserted first so `books_insert_trg` fills `sort`/`uuid`), the entity links, atomic format-file placement into the Calibre-authored `Author/Title (id)` layout, an optional JPEG/PNG cover (sniff-or-raise, verbatim bytes), and the OPF-resync queue entry. Copy only; sources are never moved. Batch-scoped filesystem compensation removes every directory the pass created when the outermost batch exits failed. A format seed byte-identical to an already-catalogued file raises before anything is written (the 'never imported twice' invariant); content-agnostic, metadata-based duplicate screening stays a frontend concern; its library side is the `find_candidate_duplicates` screening API. `dry_run=True` returns the computed plan without writing. **`set_format` and `remove_book`'s file story (cquarry ≥ 1.17).** `set_format` replaces one format row (remove+add in one transaction; honest no-op when identical). `remove_book` gained `delete_files`: `"trash"` moves the book directory into the library-local `.caltrash/b/<id>/` (upstream's own trash layout, reproducible with `shutil.move`), `"permanent"` deletes it, and the default `None` keeps rows-only semantics; the fs step lands only after the rows COMMIT, deferred to the outermost batch commit inside a `batch()`. New authors default their `sort` to the display name -- upstream's surname-flip heuristic (`author_to_author_sort`) runs on GUI metadata edits, not row creation, and is not reproduced here (dated 2026-09-09). **Cover management (cquarry ≥ 1.19).** `set_cover(book_id, data)` keeps the decided sniff-or-raise rule (JPEG→cover.jpg, PNG→cover.png, unparseable raises; no stdlib transcoding) and removes a stale cover under the other extension; `remove_cover` sweeps both. Cover files land only after the commit (the `_pending_fs_ops` queue), so a failed pass never catalogues a cover it did not keep. **Entity-wide renames and removals (cquarry ≥ 1.19).** `rename_entity(kind, old, new)` and `remove_entity_everywhere(kind, name)` address authors/series/publishers/tags; name resolution is exact-spelling-first with a lowest-id NOCASE fallback; merges drop colliding links against the survivor's UNIQUE(book, fk), author merges recompute `author_sort` and re-lay paths, and series merges renumber incoming books to max+1 over the survivor's other books. **Path re-laying (cquarry ≥ 1.18).** `update_title` and `set_authors` re-lay the on-disk layout to match the rename: the `Author/Title (id)` directory moves, every format file renames to the new `Title - Author.ext` stem, an emptied parent is removed, and `books.path`/`data.name` are updated in the same transaction; the filesystem half defers to the outermost `batch()` commit and is dropped on rollback, so a failed pass never leaves rows pointing at renamed directories.

**Read-side completeness (cquarry ≥ 1.6).** Every remaining table and view of the schema is surfaced: `get_feeds()` lists the news-recipe rows of `feeds`; `get_tag_browser_counts()` reads Calibre's own `tag_browser_*` views (custom columns rekeyed to `#label`, the ratings view's `rating` column aliased, and `tag_browser_series`'s `title_sort()` UDF supplied locally from `helpers` for the duration of the read, then removed) while skipping the `tag_browser_filtered_*` variants, which depend on Calibre's GUI-state `books_list_filter()` function; `get_entities()` gained a `ratings` kind (the `name` column carries the half-star integer as text); and both `get_all_books()` and `get_book()` rows now carry `uuid` and `identifiers` with identical shapes between the two (previously `size` was missing from `get_book` and both ids existed only on the internal search view). Languages honor `books_languages_link.item_order` (link-id order on schemas predating the column), matching Calibre's ordering. The `meta` view is deliberately not read: it requires Calibre's in-process `sortconcat()`/`concat()` aggregates; `get_all_books()` supersedes it.

**Duplicate screening and flat export rows (cquarry ≥ 1.17).** `CalibreDB.find_candidate_duplicates(title, authors, isbn=None)` is the library side of duplicate screening: ISBN rule first (separator/case-insensitive against the book's `isbn` identifier), then normalized title (folded, subtitle and leading article scrubbed) plus folded first author, both exact -- returning `{"id", "matched_by"}` entries sorted by id. `CalibreDB.export_rows(*, ids=None, include_custom=True)` composes the hydrated rows with every non-composite custom column flattened in as a `#label` key (`None` when absent), giving exporters and CSV writers one flat dict per book with no SQL of their own; raw `rating`/`pubdate` values pass through unconverted.

**Composed reads (cquarry ≥ 1.8).** `get_book_dossier(book_id, *, include_comments=False)` is the composed deep fetch detail views previously hand-assembled: the standard `get_book()` row, `cover_path` (via `get_cover_path()` defaults; the row's `has_cover` distinguishes catalogued-but-missing), `formats`, `custom_columns` keyed `#label` as `{name, datatype, value}` (values exactly as the engine's `field()` yields; comments-typed columns stay raw HTML), `annotations`, `reading_positions`, `plugin_data`, `conversion_overrides`, and; only when flagged; `comments` as `{html, plain}` (plain via `strip_html`). `None` for unknown books. `format_path_index()` maps every catalogued format path to its book id in one `data ⋈ books` query built exactly like `get_format_path()` (keys `normcase(normpath())`, cached); `find_book_by_path(path)` reverses it, tolerant of relative spellings and redundant separators.

### 3.7 Integrity predicates (`integrity.py`, cquarry ≥ 1.8)

The mechanical definitions of "incomplete" promoted from CalibreQuarry's frontend so every consumer shares one answer. Pure functions over the cached rows; no SQL of their own; the two cover-file checks ride `get_cover_path()` + `get_image_size()` because a flag cannot see the disk. Every id list is sorted: `find_untagged`, `find_unrated` (`None`/`0`), `find_authorless` (empty or `["Unknown"]`), `find_formatless`, `find_coverless` (catalogued flag), `find_missing_cover_files` (flag set, file absent; empty `books.path` skipped), `find_deprecated_formats(db, formats)` (caller supplies the deprecated set; cquarry owns only the subset-of mechanism), `find_low_res_covers(db, min_dimension=500) → {id: (w, h)}` (missing files excluded; unreadable images skipped), `find_duplicate_books` → `{(title-lower, primary-author-lower): [ids]}` (multi-member only), `find_identifierless` (books with an empty identifiers store; cquarry ≥ 1.17), `find_series_gaps` composing `get_all_series()` + `detect_series_gaps()`.

### 3.8 Analytics derivations (`analytics.py`, cquarry ≥ 1.8)

Derivations promoted from CalibreQuarry's `--analytics` frontend; the frontend keeps formatting, cquarry owns the math. Pure over the cached rows: `addition_timeline(db, granularity="month")` (`"YYYY-MM"` buckets, chronological; `"year"` supported; timestampless books skipped), `author_stats` (per primary author `{author, book_count, avg_rating, rated_count, formats}`; star-scale averages over rated books only, count-desc then name, authorless books skipped; `rated_count` is the renderer-facing additive key the average alone cannot give back), `rating_distribution` (half-step star floats ascending, `"unrated"` last), `genre_distribution(db)` (every dot-path node of the tag hierarchy as a fraction of the whole library: subtree rollup with a book counted once per node even when its tags share an ancestor, so multi-genre books push the sum over 1.0; depth-first, parents before children, siblings share-descending then name, `"untagged"` last; the rollup-plus-denominator delta over `get_tag_counts`), and `vl_overlap(db, names=None)` (books in two or more virtual libraries as `{(wing, ...): [ids]}`, unknown names raising through `resolve_vl`).

## 4. Field location table

Canonical locations, their datatypes, and recognized aliases. Custom columns are registered dynamically from the `custom_columns` table and use `#label` as their location token; series custom columns additionally register `#<label>_index` (cquarry ≥ 1.18), a float fed by the link table's `extra` column (a real column literally labeled `<x>_index` keeps the token).

| Canonical | Datatype | Aliases |
|-----------|----------|---------|
| `title` | text | |
| `title_sort` | text | |
| `author_sort` | text | |
| `series` | text | |
| `series_sort` | text | |
| `publisher` | text | |
| `comments` | text | `comment` |
| `uuid` | text | |
| `authors` | text_multi | `author` |
| `formats` | text_multi | `format` |
| `languages` | text_multi | `language`, `lang` |
| `tags` | hier | `tag` |
| `rating` | rating | |
| `series_index` | float | |
| `size` | float (bytes) | |
| `pages` | int | native `books_pages_link` first, `#pages` custom column fallback |
| `annotations` | text | concatenated `searchable_text`; `all` never sweeps it |
| `id` | int | |
| `pubdate` | date | |
| `timestamp` | date | `date` |
| `last_modified` | date | |
| `identifiers` | identifiers | `identifier`, `ids`, `isbn` |
| `cover` | bool | |

The special locations `vl:"Name"` and `search:"Name"` cross-reference virtual libraries and saved searches. `@Name` searches a user-defined tag-browser category (see §3.2). The `all` pseudo-location (used for bare terms) searches: `title`, `authors`, `author_sort`, `series`, `publisher`, `tags`, `comments`, plus every custom column whose engine datatype is text-like.

## 5. Documented deviations from Calibre

These are permanent, dependency- or GUI-bound limitations, not bugs.

1. **Regex engine.** `~` uses stdlib `re`, not the third-party `regex` module. `VERSION1` mode and `\X` (extended grapheme cluster) are unavailable. For the query patterns users actually write, this is transparent.
2. **Accent/contains folding.** Uses `unicodedata.normalize("NFKD")` with combining-character stripping, not ICU collation. Punctuation-insensitivity (e.g. treating `'` and `'` as equivalent) is not reproduced.
3. **GPM templates.** `@...:` template expressions tokenize for parse parity but are not evaluated, and `template:` searches raise a clear `ParseException` (dated 2026-09-12; upstream evaluates them inside its GUI and raises `TemplatesNotAllowed` where disallowed). Both require Calibre's template engine, which §7 puts out of scope.
4. **GUI-state locations.** `marked`, `ondevice`, and `in_tag_browser` reflect state that only exists inside Calibre's own UI session; they are not implemented.
5. **Tag matching default.** `tags:Foo` uses anchored prefix matching (matches `Foo` and `Foo.*`), not Calibre's raw substring matching (which would also match `BarFoo`). This is a deliberate project invariant, not a porting gap; it matches how every consumer in the ecosystem has always treated tags.
6. **`series_sort` format.** Computed as `"Series [index]"`; Calibre builds an equivalent sort string internally but does not expose its exact formatting contract.
7. **Date parsing and comparison leniency** (dated 2026-09-09). cquarry parses dates with `datetime.fromisoformat` and compares calendar dates at the query's stated precision; upstream parses with `dateutil` (more input shapes accepted) and compares datetime instants against local-time `now`, so `today`/`Ndaysago` boundaries can differ by hours around midnight when timestamps carry times. cquarry also accepts a few query shapes upstream rejects (e.g. `field:="Quoted Value"`). Dependency-bound (no `dateutil` in the stdlib) and result-compatible for the day-precision queries users actually write.

8. **Composite custom columns match nothing in search** (dated 2026-09-09). Upstream computes composite values through its template engine and searches them; implementing that means implementing the GPM template language, which §7 puts out of scope. cquarry registers no location for composite columns, so `#composite:query` is an empty match rather than an error.
9. **Boolean (tristate) fidelity.** (dated 2026-09-12) cquarry bool fields (cover, bool custom columns) accept upstream's full yes/no/checked/unchecked/blank/empty vocabulary plus `_` variants, but treat the field as twostate: `None`/absent and `False` both match the whole false-side vocabulary. Upstream's `bools_are_tristate` pref splits them (`empty`/`blank`/`false` for None; `no`/`unchecked` -- and, a genuine upstream quirk, `true` -- for False) and adds a translated yes/no vocabulary cquarry does not carry (English only). Same results for `true`/`false` queries; the tristate distinctions collapse.
10. **Lexer strictness.** (dated 2026-09-12) upstream's scanner silently truncates unparsable tails (a stray `re.Scanner` artifact) and absorbs a quoted word only after a bare `location:` suffix; cquarry raises `ParseException` on unparsable tails and also absorbs quoted queries after `=`/`:`-suffixed locations (`author:="Asimov"` works here, and mis-lexes upstream). cquarry is stricter and louder by design; queries that relied on upstream's silent truncation error here.
11. **Benign extensions.** (dated 2026-09-12, consolidated) small superset behaviors kept deliberately: the `lang` and `ids` location aliases, the bare `timestamp` token, case-insensitive virtual-library and saved-search name resolution, `search:=Name` prefix tolerance, and tolerance of corrupt/unknown `user_categories`/`grouped_search_terms` preference payloads (degraded to empty rather than raising). None change results for upstream-valid queries.
12. **Entity case-change policy.** (dated 2026-09-12) cquarry never re-cases an existing entity row: `set_authors`/`set_series`/`set_publisher`/`add_tag` resolve entities case-insensitively and reuse the stored spelling as-is. Upstream has the same default (`allow_case_change=False`, db/write.py:299-315); its GUI can opt into rewriting casing, which no cquarry API exposes. A case-different input is treated as the same entity, not an edit.
13. **`annotations:` matching** (dated 2026-09-09, aligning spec with API.md's existing note). Calibre searches annotations through its FTS tables, with stemming and rank ordering; cquarry matches the concatenated `searchable_text` with ordinary text semantics. Same result set for typical queries, no stemming or ranking.

*(Former item 7; "`pages` sourcing"; was resolved in v1.3.0: Calibre now maintains page counts natively in `books_pages_link`, which cquarry reads first with the `#pages` custom column kept as an older-schema fallback. It is no longer a deviation.)*

## 6. Downstream consumers

cquarry is the shared foundation. Changes to its behavior affect all of these:

| Consumer | What it uses |
|----------|-------------|
| **CalibreQuarry** (CLI/TUI) | `CalibreDB`, `search()`, `search_books()`, `get_book()`, `get_book_dossier()` (the `--book` dossier renders over it), `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `get_vl_ui_state()`, `resolve_vl()`, `get_annotations()`, `get_plugin_data()`, `get_dirtied_books()`, `get_formats()`, `get_cover_path()`, `get_library_uuid()`, `get_all_series()`, `get_tag_counts()`, `get_format_path()`, `find_db()`, `format_stars()`, `strip_html()`, `tags_to_tree()`, `normalize_author_display()`, `detect_series_gaps()`, `get_image_size()`, `color()`, `integrity` (the `--audit` predicates), `analytics` (`--analytics`/`--stats`), `helpers.isbn_normalize`/`isbn_check_digit_is_valid`/`to_isbn13` (LibraryThing exporter + ISBN audit), `write.WritableCalibreDB` |
| **Hermitage** (GTK4 gallery) | `CalibreDB`, `search()`, `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `get_saved_searches()`, `get_vl_ui_state()`, `get_annotations()`, `get_last_read_positions()`, `get_comments()` (bulk comments read; no more `db.conn` reach-ins), `normalize_rating()`, `integrity` + `analytics` (Insights/health views), `helpers.tag_rollup` (genre sidebar) |
| **Carrel-calibre-web** (web reader) | `CalibreDB`, `search()`, `get_virtual_libraries()`, `get_vl_ui_state()`, `resolve_vl()`, `field()` (native `pages`), `get_annotations()`, `get_last_read_positions()`, `detect_series_gaps()` |
| **Bindery** (EPUB repair) | `get_image_size()` (cover audit), `get_format_path()` (EPUB resolution; the `CalibreIdResolver` id map rides `format_path_index()`), `get_book()` (single-entity `audit --id` fetch), `get_formats()` (audited-format reporting), `write.WritableCalibreDB` (optional flag tagging) |

## 7. Out of scope (non-goals)

- Writing to `metadata.db` from the read-only `CalibreDB` class. Ever.
- Running `calibredb` or shelling out to Calibre.
- Evaluating GPM templates or Calibre's template language.
- Thread safety. `CalibreDB` is designed for single-threaded, short-lived use. Concurrent access from multiple threads is not supported and not tested.
- Async I/O. All database access is synchronous `sqlite3`.
