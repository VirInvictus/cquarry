# cquarry Roadmap

This roadmap outlines the planned evolution of `cquarry` from a read-only metadata extractor to a full-featured Calibre ecosystem bridge, utilizing the structural discoveries documented in `database_report.md`.

## Phase 1: Read-Only Enhancements (Current & Near-Term)
*Context: Improving our query capabilities using existing read-only mechanics (ref: database_report.md Sections 1-4).*

- [x] **Single-Entity Fetching**: Add `get_book(book_id: int) -> dict` to fetch single records without full-library scanning.
  - **Downstream Upgrades**: 
    - *CalibreQuarry*: Use this in `reconcile_file_metadata.py --id X` to avoid loading the whole DB.
    - *Bindery*: Use when auditing a single isolated file.
    - *Hermitage*: Use when a user clicks a book to fetch deep metadata just-in-time rather than storing everything in RAM.

- [x] **Combined Search & Fetch**: Add `search_books(query) -> list[dict]` to immediately yield hydrated metadata from search sets.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Simplify `cquarry_cli --search` pipeline (skip the lookup cross-reference).
    - *Hermitage*: Use this to power the GTK UI search bar directly.

- [x] **Format Path Resolution**: Implement `get_format_path(book_id, fmt)`. Resolve `(library_root / books.path / data.name + format)` dynamically (ref: Report Sec 2).
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Replace manual path joins in `catalog.py` and `export.py`.
    - *Bindery*: Replace manual string concat when analyzing target files.
    - *Hermitage*: Use when launching a book in an external reader.

- [x] **Saved Search Resolution**: Parse the JSON `preferences` table to support `search:"<name>"` interpolation in `SearchEngine` queries (ref: Report Sec 4).
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Allow users to run `cquarry_cli --search 'search:"My Search"'`.
    - *Hermitage*: Automatically populate the sidebar with Saved Searches alongside Virtual Libraries.

- [x] **Virtual Library UI Metadata**: Expose hidden/ordering JSON lists from `preferences` (`virt_libs_hidden`, `virt_libs_order`) so consumers can match Calibre's exact tab layout.
  - **Downstream Upgrades**:
    - *Hermitage*: Update the GTK sidebar to hide and order tabs exactly as the user's Calibre GUI does.

- [x] **Hierarchical Taxonomy Parsing**: Provide a helper to convert dot-delimited flat `tags` (e.g., `Fiction.Science Fiction`) into native Python nested dictionaries.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Enhance `analytics tags` command to print actual tree structures.
    - *Hermitage*: Render a collapsible TreeView for tags in the GTK sidebar.

- [x] **Safe Custom Column Reads**: Transition `load_custom_columns` to check `sqlite_master` for the explicit existence of `books_custom_column_N_link`.
  - **Downstream Upgrades**: No major changes required in consumers, this is an internal stability fix to prevent SQLite OperationalErrors.

- [x] **Star Rating Conversion**: Expose a standard `normalize_rating(int)` method converting internal 1-10 scales to 0.0-5.0 float stars.
  - **Downstream Upgrades**:
    - *Hermitage*: Replace local `rating / 2.0` logic with the upstream API.
    - *CalibreQuarry*: Standardize output rendering of ratings.

## Phase 2: Metadata Portability & Export
*Context: Enabling users and agents to safely extract more than just book catalog metadata.*

- [x] **Extract Annotations**: Query the `annotations` table to extract e-reader highlights, bookmarks, and user notes as JSON payload.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Add a `--export-annotations` command to dump highlights.
    - *Hermitage*: Add an "Annotations" tab to the book details view to display highlights.

- [x] **Extract Reading Progress**: Map the `last_read_positions` table to track reading velocity/progress fractions per device.
  - **Downstream Upgrades**:
    - *Hermitage*: Show a progress bar indicating how far the user is in a book based on the last device sync.

- [x] **Plugin Data Bridges**: Expose the `books_plugin_data` table to enable `cquarry` to read third-party Goodreads sync, WordCount, or ISBN metadata.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Allow `catalog.py` to optionally print Goodreads IDs or fetched word counts.

- [x] **Comments Parsing Utilities**: Add utilities to safely strip or sanitize the raw HTML payloads found in the `comments` table.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Prevent raw HTML tags from leaking into terminal outputs during spot checks.
    - *Hermitage*: Sanitize text before feeding it to GTK Label rendering to prevent markup injection.

- [x] **Extract Conversion Profiles**: Query the `conversion_options` table to back up specific book conversion pipeline recipes.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Add an audit script to find books with manual conversion overrides.

## Phase 3: Write Capabilities (Long-Term)
*Context: Establishing safe write paths for agents and scripts without destroying database integrity.*

- [x] **UDF Registration Framework**: Implement a standard `register_udfs(conn)` method injecting `title_sort()`, `uuid4()`, etc.
  - **Downstream Upgrades**: Internal prep for write-capable tools. 

- [x] **Title Update API**: Build `update_title(book_id, new_title)` that handles `last_modified` timestamp updates.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Allow `reconcile_file_metadata.py` to optionally fix titles directly in the DB instead of just the files.

- [x] **Safe Tag Application / Removal**: Build `add_tag(book_id, tag_string)` and `remove_tag(book_id, tag_string)`.
  - **Downstream Upgrades**:
    - *Hermitage*: Add a context menu option to "Mark as Read" (applying a specific tag).
    - *Bindery*: Automatically tag books as "Audited" or "Flagged" when issues are found.

- [x] **Identifier Batch Updater**: Build a safe write pipeline to append EAV records into the `identifiers` table.
  - **Downstream Upgrades**:
    - *CalibreQuarry*: Allow `fetch_library_codes.py` to inject ISBNs/LCCs directly into the DB safely without requiring Calibre to be closed, if we implement safe SQLite locking.

## Phase 4: Full Search Parity & Stability (Identified via Research)
*Context: Closing the feature gaps between `cquarry`'s search engine and Calibre's native search query parser. **Before beginning work on this phase, agents MUST read `research.md` for the full technical breakdown of Calibre's schema and grammar.** *

- [x] **Fix Author Comma Splitting**: Remove `GROUP_CONCAT` in `get_all_books()` for authors, tags, and formats, and instead fetch their discrete items directly from the normalized link tables to prevent names like "Strunk, Jr." from splitting incorrectly.
- [x] **Add Missing Built-in Locations**: Implement `size` (DT_FLOAT), `pages` (DT_INT), `title_sort`, and `series_sort` to the search engine aliases.
- [x] **Saved Search Resolution (`search:`)**: Implement interpolation of `saved_searches` from the `preferences` table.
- [x] **Multi-Valued Count Operator (`#`)**: Add support for `#>X` and `#=X` syntax across all multi-valued locations (tags, authors, formats, identifiers).
- [x] **Fix Custom Column Mappings**: Fix the `DT_RATING` mapping bug for custom rating columns and dynamically register `#{label}_index` for custom series columns.
- [x] **Language Canonicalization**: Implement a `lang_map` to canonicalize language queries (e.g. `languages:English` -> `eng`).
- [x] **Advanced Date Separators**: Support slash (`/`) date separators (`YYYY/MM/DD`) alongside hyphens.
- [x] **Expand Boolean Keywords**: Add support for `checked`, `unchecked`, `blank`, `empty`, and `_`-prefixed variants for tristate boolean logic parity.
- [x] **Strict Error Handling**: Raise `ParseException` for unknown Virtual Libraries (`vl:Unknown`) instead of failing silently.
- [x] **Expand the `all` Field**: Dynamically scan custom text columns when resolving bare search terms.
- [x] **Component Matching (`=..`)**: Expand `_match_text` to support Calibre's `.` (subtree) and `..` (component) exact match modifiers across all text fields.

## Phase 5: Sweep & Data Integrity Hardening (2026-08-23)
*Context: Found critical evaluation bugs and data corruption risks during a codebase sweep.*

### Bugs to Fix
- [x] **JPEG Dimension Sniffer Hang:** Add a sanity check (`length < 2`) in `helpers.py:get_jpeg_size()` to prevent infinite loops on corrupt images.
- [x] **AST Corruption on Quoted Colons:** Fix `_base_token` to properly append quoted string values (e.g. `identifiers:isbn:"..."`) instead of misinterpreting them.
- [x] **Custom Rating Column Datatype:** Change custom rating mappings from `DT_FLOAT` to `DT_RATING` to restore 0-rating `false` semantics.
- [x] **Missing Series Index Registration:** Dynamically register `#{label}_index` for custom series columns to enable position filtering.
- [x] **Author/Tag Comma Splitting:** Stop using `GROUP_CONCAT` for authors/tags. Build string lists directly to prevent names with literal commas from splitting in half.
- [x] **Virtual Library Hardening:** Make `vl_expression()` case-insensitive and raise `ParseException` on unknown VL names instead of silently failing.
- [x] **Exact Date Prefix Failure:** Strip match kinds (e.g. `=true`) before evaluating boolean matches in `_match_date()`.
- [x] **Version Sync:** Update `__init__.py`, `config.py`, `README.md`, and `spec.md` to 1.0.1.

### Refactoring & Growth
- [x] **Eliminate N+1 Comment Queries:** Eagerly cache or batch-fetch comments during `_build_search_view()` to prevent thousands of single-row queries.
- [x] **Drop `re.Scanner`:** Replace undocumented `re.Scanner` with standard `re.finditer` for pure stdlib compliance.
- [x] **Saved Searches Interpolation:** Support Calibre's `search:"Name"` by parsing the `preferences` table.
