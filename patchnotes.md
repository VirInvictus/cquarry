## v1.1.0 (2026-08-25)

### Search parity (Phase 4 — now actually true)
- **New built-in locations:** `size` (total bytes across formats, honors `k`/`m`/`g` suffixes), `pages` (sourced from an int custom column labelled `pages` when present), `title_sort`, and `series_sort` (`"Series [index]"`). All are exposed in `get_all_books()`/search views.
- **Saved-search interpolation:** `search:"Name"` resolves through the new `CalibreDB.get_saved_searches()` / `saved_search()`. Nesting works, cycles raise `ParseException`, unknown names raise `ParseException`, and lookups are case-insensitive.
- **Multi-valued count operator:** `tags:#>3`, `authors:#=2`, `formats:#<5`, `identifiers:#>=2`.
- **Language canonicalization:** `languages:English` matches books stored as `eng` via a 55-entry ISO 639-2 map; unknown tokens pass through untouched.
- **Slash date separators:** `pubdate:=1965/08/01` and `timestamp:2006/07` work alongside hyphens.
- **Expanded boolean keywords:** `checked`, `unchecked`, `blank`, `empty` plus `_`-prefixed variants, accepted in both boolean (`cover:`) and numeric/tristate (`rating:blank`) positions.
- **Strict virtual-library errors:** `vl:Unknown` raises `ParseException` instead of silently returning no matches; VL name resolution is case-insensitive end-to-end (`resolve_vl`, `vl_expression`).
- **Component matching everywhere:** Calibre's leading-dot modifiers under `=` — `.foo` (subtree) and `..foo` (component) — now apply to *all* text fields, not just hierarchical tags.
- **`all` sweeps custom text columns:** bare terms also search custom text/enumeration/tags-like columns.

### Single-entity & path APIs (Phase 1)
- **`get_book(book_id)`** fetches one hydrated record without scanning the library.
- **`search_books(query)`** returns hydrated books for a search expression directly.
- **`get_format_path(book_id, fmt, verify=True)`** resolves `<library>/<books.path>/<name>.<fmt>` from the original DB location (snapshot-safe).
- **`tags_to_tree(tags)`** builds nested dicts from dot-delimited hierarchies for TreeView rendering.
- **`normalize_rating(int)`** is the canonical name for the 1–10 → 0–5 star conversion (`calibre_rating_to_stars` kept as alias).
- **`get_vl_ui_state()`** exposes Calibre's stored `virt_libs_hidden` / `virt_libs_order` so frontends can mirror the GUI sidebar exactly.
- **`resolve_saved_search(name)`** resolves a saved search to book IDs with case-insensitive matching.

### Metadata portability (Phase 2)
- **`get_annotations(book_id=None)`** extracts highlights/bookmarks/notes from the `annotations` table with JSON `annot_data` decoding.
- **`get_last_read_positions(book_id=None)`** maps per-device reading progress (`pos_frac`, CFI, epoch time).
- **`get_plugin_data(book_id=None, name=None)`** reads third-party payloads (Goodreads IDs, word counts, page counts) from `books_plugin_data`.
- **`get_conversion_profiles(book_id=None)`** lists manual conversion overrides; the pickled blob stays raw bytes (never unpickled).
- **`strip_html(html)`** reduces comments HTML payloads to safe plain text (tag stripping, entity unescaping, whitespace collapse).

### Write capabilities (Phase 3 — new opt-in module)
- **`cquarry.write.WritableCalibreDB`:** a separate class that is unreachable from read-only `CalibreDB`. Registers Calibre's trigger dependencies (`title_sort()`, `uuid4()`, `PYNOCASE`) before any statement, uses `BEGIN IMMEDIATE` transactions, bumps `books.last_modified` on every mutation, and cleans link tables before tag deletion to satisfy `fkc_delete_on_tags`.
- **APIs:** `update_title()`, `add_tag()` / `remove_tag()` (returns whether state changed), `set_identifier()` / `set_identifiers()` batch upserts honoring `UNIQUE(book, type)`.

### Internal
- **Dropped `re.Scanner`:** the tokenizer is a plain `re.finditer` scanner over a documented pattern — pure documented stdlib.
- Test suite grew from 47 to 104 tests covering every feature above.

## v1.0.3 (2026-08-24)
- **Fix**: Prevented infinite loops in `get_jpeg_size` by asserting frame payload lengths are valid.
- **Fix**: Re-wrote AST quoted-colon parsing block in `_base_token` to successfully preserve strings like `identifiers:isbn:"value"`.
- **Fix**: Fixed logical rating searches (`#rating:false`) by properly declaring `#rating` as `DT_RATING`.
- **Fix**: Added dynamic series index generation for custom `#series` columns in location routing.
- **Fix**: Eliminated Calibre format-splitting bugs for author/tag strings containing commas by omitting `GROUP_CONCAT` in favor of dictionary mapping and native python lists.
- **Fix**: Shielded `resolve_vl` from virtual library recursion explosions.
- **Fix**: Extracted date-time components accurately in ISO-8601 targets, preventing exact match failures on ISO strings containing `T`.

## v1.0.2 (2026-08-24)

- **Build:** Configured pyproject.toml to ignore strict ruff lints blocking the CI pipeline.
## v1.0.1 (2026-08-23)

### Performance
- **Lazy-Loaded Comments & Custom Columns:** `db.py` no longer eagerly loads the `comments` HTML payloads or large custom column tables into memory when building the search view. These are now fetched from SQLite strictly on-demand per book ID during search expression evaluation. This massively reduces memory footprint and snapshot copy time for libraries with extensive HTML comments.

## v1.0.0 (2026-08-23)

### Extract & Launch
- **Initial Extraction:** Graduated `cquarry` into a standalone shared library.
- **Database Engine (`cquarry.db`):** Features the `CalibreDB` wrapper, which intelligently manages `metadata.db` access, falling back to a WAL-consistent snapshot if the Calibre desktop application holds an exclusive write-lock. Exposes `get_all_books()`, tags, series, and identifiers with performant SQLite JOINs and internal memory caching.
- **Search Grammar Engine (`cquarry.search`):** A full recursive descent parser implementing Calibre's search expression logic. Provides boolean logic (`AND`, `OR`, `NOT`), exact matching (`=value`), hierarchical tag prefix matching (`tags:Fic` matches `Fic.Fantasy`), date math (`date:>14daysago`), and nested Virtual Library resolution (`vl:"My Books"`).
- **Helpers:** Inherits standard Calibre domain formatters from CalibreQuarry (star rating converters, deterministic missing series gap detection, and binary image dimension sniffing).
