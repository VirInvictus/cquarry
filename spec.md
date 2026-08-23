# cquarry specification

The contract. Read this before changing semantics.

**Version:** 1.0.0
**Language:** Python 3.14+
**Dependencies:** None (pure stdlib)
**License:** MIT

## 1. Mission

Provide a single, canonical, read-only Calibre database and search evaluation engine for the Python ecosystem. By centralizing the parsing of Calibre's search grammar, cquarry ensures that all consumers (CLI, web, GTK4) agree exactly on which books match a given query or virtual library definition.

## 2. Hard constraints

These are invariants. Violating any of them is a spec breach.

- **No external dependencies.** The package runs on the Python 3.14+ standard library alone: `sqlite3`, `re`, `json`, `unicodedata`, `struct`, `datetime`, `os`, `sys`, `shutil`, `tempfile`, `urllib.parse`. No PyPI packages, no optional extras.
- **Read-only by design.** cquarry will never write to `metadata.db`. It opens the database with `?mode=ro` and issues only `SELECT` statements. The lock-escape path copies the database to a temp file rather than attempting a write lock. If a write API is ever added (see roadmap Phase 3), it will be a separate, explicitly opt-in module with its own safety contract; it will never be reachable through the read-only `CalibreDB` class.
- **Search parity.** `SearchEngine` must behave like Calibre's native search bar. This includes implicit AND evaluation, hierarchical tag anchoring, identifier keypair routing, date math with precision levels, custom column type dispatch, and virtual library cross-reference recursion detection. Documented deviations (see §5) are acceptable only when they are dependency-bound (ICU, the `regex` module) and do not change the result set for queries a user would plausibly write.
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

**Stage 1: Lexer.** A `re.Scanner` tokenizes the input into opcodes (`(`/`)`), words, and quoted words. Backslash escapes (`\\`, `\"`, `\(`, `\)`) are handled by a replacement/unreplacement cycle using low control characters as sentinels.

**Stage 2: Parser.** A recursive-descent parser (`_Parser`) builds an AST of `["and", lhs, rhs]`, `["or", lhs, rhs]`, `["not", child]`, and `["token", location, query]` nodes. Operator precedence: `not` binds tightest, then `and` (including implicit AND between adjacent terms), then `or`. Parentheses override precedence. A bare term (no `location:` prefix) is assigned location `"all"`. Raises `ParseException` on malformed input.

**Stage 3: Evaluator.** `SearchEngine._evaluate()` traverses the AST with candidate-set semantics (matching Calibre's `and`/`or`/`not` behavior):
- `and`: evaluate the left side, then evaluate the right side against only the left's matches.
- `or`: evaluate the left side, then evaluate the right side against the candidates minus the left's matches, and union the results.
- `not`: subtract the child's matches from the candidates.

For `token` nodes, the evaluator dispatches to a type-specific matcher based on the field's datatype.

**Match kinds.** The query string prefix determines the match semantics:
- No prefix: substring match (case- and accent-folded via NFKD decomposition).
- `=`: exact match (folded).
- `~`: regex match (stdlib `re`, case-insensitive).
- `^`: accent-folded substring (same as default; exists for Calibre grammar compatibility).
- `\`: escape (the next character is literal).

**Hierarchical tags.** The `tags` location uses anchored prefix matching: query `Foo` matches tag `Foo` and any tag starting with `Foo.` (e.g. `Foo.Bar`, `Foo.Bar.Baz`), but not `Foobar`. This is a cquarry invariant, not a Calibre port; Calibre uses raw substring matching by default. The exact-match prefix (`=`) supports Calibre's leading-`.` and `..` modifiers for subtree and component matching.

**Numeric fields.** `rating`, `id`, `series_index`, and numeric custom columns support relational operators (`=`, `>`, `<`, `>=`, `<=`, `!=`) and the keywords `true`/`false` for presence/absence. Size suffixes (`k`, `m`, `g`) are supported. Rating `false` matches `None` or `0`; rating `true` matches any positive value.

**Date fields.** `pubdate`, `timestamp`, `last_modified`, and date custom columns support the same relational operators, plus the keywords `today`, `yesterday`, `thismonth`, and `N daysago`. Dates can be specified at year (`2024`), month (`2024-06`), or day (`2024-06-15`) precision; the comparison respects the precision level. Calibre's undefined-date sentinels (`0101-01-01`, `0100-01-01`) are treated as `None`.

**Identifiers.** The `identifiers` location supports keypair search: `identifiers:isbn:VALUE` matches the `isbn` key with a value match, `identifiers:true` tests for any identifier, and `isbn:VALUE` is shorthand for `identifiers:=isbn:VALUE`. Match kinds apply independently to both the key and value halves.

**Virtual library cross-references.** `vl:"Name"` resolves the named virtual library's search expression and evaluates it recursively, intersecting the result with the current candidate set. Circular references (`vl:A` where A's expression contains `vl:A`) raise `ParseException`.

### 3.3 Helpers (`helpers.py`)

Domain-specific utilities shared across the ecosystem. These are public API; downstream consumers import them.

- **Database discovery** (`find_db`): a four-stage resolution chain (explicit arg, saved config, default paths, interactive prompt).
- **Rating conversion** (`calibre_rating_to_stars`, `format_stars`): Calibre stores ratings on a 0-10 scale; the portfolio displays them on 0.0-5.0 with Unicode star glyphs.
- **Author formatting** (`normalize_author_display`, `author_sort_key`): comma-separated to ampersand-joined display, with a `primary_only` mode.
- **Series analysis** (`detect_series_gaps`): given a series' known indices, return the missing integers.
- **Image dimensions** (`get_image_size`, `get_jpeg_size`, `get_png_size`): header-only dimension reads for cover quality auditing. The JPEG reader seeks through segment markers rather than reading a fixed buffer, so large EXIF/ICC blocks do not hide the SOF.
- **Terminal color** (`color`): TTY-aware ANSI wrapping with predefined codes.

### 3.4 URI encoding (`db_uri_ro`)

SQLite's `file:` URI mode parses `?` as query-string and `#` as fragment. A library path like `Books #2/metadata.db` must be percent-encoded or it opens a different file and fails with "no such table: books". `db_uri_ro()` uses `urllib.parse.quote()` (which leaves `/` alone) and appends `?mode=ro`.

### 3.5 Config (`config.py`)

A JSON file at `~/.config/cquarry/config.json` persists the database path across sessions. `get_db_path()` and `set_db_path()` are the read/write interface; `load_config()` and `save_config()` are the underlying I/O. The config is user-facing (the TUI and `find_db()` interactive prompt write to it), not an internal cache.

## 4. Field location table

Canonical locations, their datatypes, and recognized aliases. Custom columns are registered dynamically from the `custom_columns` table and use `#label` as their location token.

| Canonical | Datatype | Aliases |
|-----------|----------|---------|
| `title` | text | |
| `author_sort` | text | |
| `series` | text | |
| `publisher` | text | |
| `comments` | text | `comment` |
| `uuid` | text | |
| `authors` | text_multi | `author` |
| `formats` | text_multi | `format` |
| `languages` | text_multi | `language`, `lang` |
| `tags` | hier | `tag` |
| `rating` | rating | |
| `series_index` | float | |
| `id` | int | |
| `pubdate` | date | |
| `timestamp` | date | `date` |
| `last_modified` | date | |
| `identifiers` | identifiers | `identifier`, `ids`, `isbn` |
| `cover` | bool | |

The `all` pseudo-location (used for bare terms) searches: `title`, `authors`, `author_sort`, `series`, `publisher`, `tags`, `comments`.

## 5. Documented deviations from Calibre

These are permanent, dependency-bound limitations, not bugs.

1. **Regex engine.** `~` uses stdlib `re`, not the third-party `regex` module. `VERSION1` mode and `\X` (extended grapheme cluster) are unavailable. For the query patterns users actually write, this is transparent.
2. **Accent/contains folding.** Uses `unicodedata.normalize("NFKD")` with combining-character stripping, not ICU collation. Punctuation-insensitivity (e.g. treating `'` and `'` as equivalent) is not reproduced.
3. **GPM templates.** `@...:` template expressions are not evaluated. These are a power-user feature that requires Calibre's template engine.
4. **Saved searches.** `search:"name"` interpolation from the `preferences` table is not yet implemented (roadmap Phase 1).
5. **Tag matching default.** `tags:Foo` uses anchored prefix matching (matches `Foo` and `Foo.*`), not Calibre's raw substring matching (which would also match `BarFoo`). This is a deliberate project invariant, not a porting gap; it matches how every consumer in the ecosystem has always treated tags.

## 6. Downstream consumers

cquarry is the shared foundation. Changes to its behavior affect all of these:

| Consumer | What it uses |
|----------|-------------|
| **CalibreQuarry** (CLI/TUI) | `CalibreDB`, `search()`, `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `get_all_series()`, `get_tag_counts()`, `find_db()`, `format_stars()`, `normalize_author_display()`, `detect_series_gaps()`, `get_image_size()`, `color()` |
| **Hermitage** (GTK4 gallery) | `CalibreDB`, `search()`, `get_all_books()`, `get_custom_columns()`, `load_custom_column()`, `get_virtual_libraries()`, `calibre_rating_to_stars()` |
| **Carrel-calibre-web** (web reader) | `CalibreDB`, `search()`, `get_virtual_libraries()` |
| **Bindery** (EPUB repair) | `get_image_size()` (cover audit) |

## 7. Out of scope (non-goals)

- Writing to `metadata.db` from the read-only `CalibreDB` class. Ever.
- Running `calibredb` or shelling out to Calibre.
- Evaluating GPM templates or Calibre's template language.
- Thread safety. `CalibreDB` is designed for single-threaded, short-lived use. Concurrent access from multiple threads is not supported and not tested.
- Async I/O. All database access is synchronous `sqlite3`.
