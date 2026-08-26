# cquarry specification

The contract. Read this before changing semantics.

**Project:** `cquarry`  
**Version:** 1.4.0  
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

**Caching.** `get_all_books()` executes an 8-JOIN query and caches the result list. `get_virtual_libraries()` reads the `preferences` table once and caches the dict. `count_books()` uses the books cache or the all-IDs cache if either is populated, falling back to a raw `COUNT(*)`. All caches are populated lazily on first access and are never invalidated (the database is read-only and the connection is short-lived). To prevent memory exhaustion on large libraries, massive text blocks like `comments` and custom columns of type `comments` are strictly lazy-loaded on-demand per book ID rather than eager-loaded during search view construction.

**Custom column dispatch.** `load_custom_column()` checks `sqlite_master` for the existence of `books_custom_column_N_link` to decide between the normalized path (text, enumeration, series: value table joined through a link table) and the direct path (int, float, bool, datetime, comments: value table with a `book` column). This is safer than keying off `is_multiple`, because a single-valued enumeration is normalized but not multi-valued.

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

**Numeric fields.** `rating`, `id`, `series_index`, `size`, `pages`, and numeric custom columns support relational operators (`=`, `>`, `<`, `>=`, `<=`, `!=`) and presence/absence keywords: `true`/`false` plus the tristate vocabulary `checked`, `unchecked`, `blank`, `empty` and `_`-prefixed variants. Size suffixes (`k`, `m`, `g`) are supported on all float/int locations, so `size:>10m` works. Rating `false`/`blank` matches `None` or `0`; rating `true`/`checked` matches any positive value.

**Date fields.** `pubdate`, `timestamp`, `last_modified`, and date custom columns support the same relational operators, plus the keywords `today`, `yesterday`, `thismonth`, and `N daysago`. Dates can be specified at year (`2024`), month (`2024-06`), or day (`2024-06-15`) precision, with either `-` or `/` separators; the comparison respects the precision level. Calibre's undefined-date sentinels (`0101-01-01`, `0100-01-01`) are treated as `None`.

**Identifiers.** The `identifiers` location supports keypair search: `identifiers:isbn:VALUE` matches the `isbn` key with a value match, `identifiers:true` tests for any identifier, and `isbn:VALUE` is shorthand for `identifiers:=isbn:VALUE`. Match kinds apply independently to both the key and value halves.

**Virtual library cross-references.** `vl:"Name"` resolves the named virtual library's search expression and evaluates it recursively, intersecting the result with the current candidate set. Circular references raise `ParseException`; unknown names also raise `ParseException` (no silent empty sets). Name resolution is case-insensitive at both engine and DB layers.

**Saved-search interpolation.** `search:"Name"` behaves identically to `vl:` but resolves against the provider's saved searches (Calibre's `preferences.saved_searches`). Nested references compose; cycles and unknown names raise `ParseException`.

**Grouped search terms.** Calibre's `preferences.grouped_search_terms` maps a group name to member search locations; the engine resolves `GroupName:query` (and `@GroupName:query`) as the union over members, each evaluated without further group recursion — nesting is a `ParseException`. `GroupName:false` matches books where no member matches (upstream's inversion). Real field names always win over same-named groups.

**Annotation search.** The `annotations` location exposes each book's concatenated annotation `searchable_text`; presence keywords (`true`/`false`) and all text match kinds work against it. Bare terms (`all`) never sweep annotation text, mirroring upstream.

**The `all` pseudo-location.** Bare terms search `title`, `authors`, `author_sort`, `series`, `publisher`, `tags`, `comments`, **plus** any custom column whose engine datatype is text-like (text, text_multi, hier). Numeric, date, bool, and identifier custom columns are excluded, mirroring Calibre.

### 3.3 Helpers (`helpers.py`)

Domain-specific utilities shared across the ecosystem. These are public API; downstream consumers import them.

- **Database discovery** (`find_db`): a four-stage resolution chain (explicit arg, saved config, default paths, interactive prompt).
- **Rating conversion** (`normalize_rating`, alias `calibre_rating_to_stars`, `format_stars`): Calibre stores ratings on a 0-10 scale; the portfolio displays them on 0.0-5.0 with Unicode star glyphs.
- **Author formatting** (`normalize_author_display`, `author_sort_key`): comma-separated to ampersand-joined display, with a `primary_only` mode.
- **Series analysis** (`detect_series_gaps`): given a series' known indices, return the missing integers.
- **Image dimensions** (`get_image_size`, `get_jpeg_size`, `get_png_size`): header-only dimension reads for cover quality auditing. The JPEG reader seeks through segment markers rather than reading a fixed buffer, so large EXIF/ICC blocks do not hide the SOF.
- **Comments sanitization** (`strip_html`): reduces comments HTML payloads to plain text — drops tags (including `script`/`style` bodies), unescapes entities, collapses whitespace, converts block boundaries to newlines. Consumers must run raw HTML through this before terminal or GTK label rendering.
- **Taxonomy parsing** (`tags_to_tree`): builds nested dictionaries from dot-delimited hierarchical tags for tree rendering.
- **Terminal color** (`color`): TTY-aware ANSI wrapping with predefined codes.

### 3.4 URI encoding (`db_uri_ro`)

SQLite's `file:` URI mode parses `?` as query-string and `#` as fragment. A library path like `Books #2/metadata.db` must be percent-encoded or it opens a different file and fails with "no such table: books". `db_uri_ro()` uses `urllib.parse.quote()` (which leaves `/` alone) and appends `?mode=ro`.

### 3.5 Config (`config.py`)

A JSON file at `~/.config/cquarry/config.json` persists the database path across sessions. `get_db_path()` and `set_db_path()` are the read/write interface; `load_config()` and `save_config()` are the underlying I/O. The config is user-facing (the TUI and `find_db()` interactive prompt write to it), not an internal cache.

### 3.6 Write access (`write.py`) — opt-in

`WritableCalibreDB` is the only sanctioned mutation path. It is a distinct class precisely so no read-only code path can reach it.

**Safety contract:**
- Registers Calibre's trigger dependencies before any statement runs: `title_sort()`, `uuid4()`, and the `PYNOCASE` collation. Calibre's `books_insert_trg` / `books_update_trg` abort any write when these are missing.
- Opens with a 30 s `busy_timeout` so a running Calibre degrades writes to waiting rather than erroring.
- Mutations run in explicit `BEGIN IMMEDIATE` transactions, bump `books.last_modified`, AND insert the book id into `metadata_dirtied` (`INSERT OR IGNORE`). Calibre regenerates a book's sidecar `.opf` — and re-pushes metadata to wireless readers — only for ids present in that table (backend.py `dirty_books()` / `dirtied_books()`); it consumes and clears the queue at startup. Skipping the insert would leave external edits invisible to OPF/wireless sync forever. The insert is guarded by a cached `sqlite_master` existence check so schemas predating the table keep working.
- Tag removal deletes link-table rows before possibly pruning the orphaned tag — the order `fkc_delete_on_tags` requires.

**API:** `update_title(book_id, title)` (refreshes `sort` via `title_sort`), `add_tag` / `remove_tag` (link-table sequence), `set_identifier` / `set_identifiers` (EAV upserts; `None` deletes), plus the write-side expansion (cquarry ≥ 1.5): `set_authors` (relinks + recomputes `books.author_sort` from per-author sort keys joined " & "), `set_series` (+`series_index`, default 1.0 fresh / preserved on reassign), `set_publisher`, `set_rating` (0–5 stars stored ×2; UNIQUE(rating) rows found-or-created), `set_languages` (English names canonicalized through the engine's lang map), `set_comments` (1:1 upsert/clear on the UNIQUE(book) row), `set_custom_column` (storage layout auto-detected via link-table existence — Pattern A value+link vs Pattern B direct; enumerations validated against `display.enum_values`; tristate bools accepted; non-editable and composite columns raise), `add_format` / `remove_format` (data-row registration; files are the caller's responsibility), `set_has_cover`, and `remove_book` (custom columns in both patterns + dirtied queues cleaned first, cascade trigger fires, orphaned entities pruned after). Entity relinks prune now-orphaned entity rows once their links are gone.

The read side exposes the same queue for observability: `CalibreDB.get_dirtied_books()` returns the sorted, deduplicated ids awaiting resync (empty list when the table is absent). It never clears the queue — that remains Calibre's job (`mark_book_as_clean()`).

## 4. Field location table

Canonical locations, their datatypes, and recognized aliases. Custom columns are registered dynamically from the `custom_columns` table and use `#label` as their location token.

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

The special locations `vl:"Name"` and `search:"Name"` cross-reference virtual libraries and saved searches. The `all` pseudo-location (used for bare terms) searches: `title`, `authors`, `author_sort`, `series`, `publisher`, `tags`, `comments`, plus every custom column whose engine datatype is text-like.

## 5. Documented deviations from Calibre

These are permanent, dependency- or GUI-bound limitations, not bugs.

1. **Regex engine.** `~` uses stdlib `re`, not the third-party `regex` module. `VERSION1` mode and `\X` (extended grapheme cluster) are unavailable. For the query patterns users actually write, this is transparent.
2. **Accent/contains folding.** Uses `unicodedata.normalize("NFKD")` with combining-character stripping, not ICU collation. Punctuation-insensitivity (e.g. treating `'` and `'` as equivalent) is not reproduced.
3. **GPM templates.** `@...:` template expressions tokenize for parse parity but are not evaluated. These are a power-user feature that requires Calibre's template engine.
4. **GUI-state locations.** `marked`, `ondevice`, and `in_tag_browser` reflect state that only exists inside Calibre's own UI session; they are not implemented.
5. **Tag matching default.** `tags:Foo` uses anchored prefix matching (matches `Foo` and `Foo.*`), not Calibre's raw substring matching (which would also match `BarFoo`). This is a deliberate project invariant, not a porting gap; it matches how every consumer in the ecosystem has always treated tags.
6. **`series_sort` format.** Computed as `"Series [index]"`; Calibre builds an equivalent sort string internally but does not expose its exact formatting contract.

*(Former item 7 — "`pages` sourcing" — was resolved in v1.3.0: Calibre now maintains page counts natively in `books_pages_link`, which cquarry reads first with the `#pages` custom column kept as an older-schema fallback. It is no longer a deviation.)*

## 6. Downstream consumers

cquarry is the shared foundation. Changes to its behavior affect all of these:

| Consumer | What it uses |
|----------|-------------|
| **CalibreQuarry** (CLI/TUI) | `CalibreDB`, `search()`, `search_books()`, `get_book()`, `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `get_vl_ui_state()`, `resolve_vl()`, `get_annotations()`, `get_plugin_data()`, `get_dirtied_books()`, `get_formats()`, `get_cover_path()`, `get_library_uuid()`, `get_all_series()`, `get_tag_counts()`, `get_format_path()`, `find_db()`, `format_stars()`, `strip_html()`, `tags_to_tree()`, `normalize_author_display()`, `detect_series_gaps()`, `get_image_size()`, `color()`, `write.WritableCalibreDB` |
| **Hermitage** (GTK4 gallery) | `CalibreDB`, `search()`, `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `get_saved_searches()`, `get_vl_ui_state()`, `get_annotations()`, `get_last_read_positions()`, `normalize_rating()` |
| **Carrel-calibre-web** (web reader) | `CalibreDB`, `search()`, `get_virtual_libraries()`, `get_vl_ui_state()`, `resolve_vl()`, `field()` (native `pages`), `get_annotations()`, `get_last_read_positions()`, `detect_series_gaps()` |
| **Bindery** (EPUB repair) | `get_image_size()` (cover audit), `get_format_path()` (EPUB resolution), `get_formats()` (audited-format reporting), `write.WritableCalibreDB` (optional flag tagging) |

## 7. Out of scope (non-goals)

- Writing to `metadata.db` from the read-only `CalibreDB` class. Ever.
- Running `calibredb` or shelling out to Calibre.
- Evaluating GPM templates or Calibre's template language.
- Thread safety. `CalibreDB` is designed for single-threaded, short-lived use. Concurrent access from multiple threads is not supported and not tested.
- Async I/O. All database access is synchronous `sqlite3`.
