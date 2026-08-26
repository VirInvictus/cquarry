# cquarry Roadmap

This roadmap outlines the planned evolution of `cquarry` from a read-only metadata extractor to a full-featured Calibre ecosystem bridge, utilizing the structural discoveries documented in `database_report.md`.

> **Status (v1.4.0, 2026-08-26):** Phases 1–5 are implemented and covered by tests.
> Phase 6: write-path correctness + dirtied visibility (v1.2.0), read-side batch 1
> (v1.3.0) and read-side batch 2 — entity columns, display config, preferences,
> grouped-search parity, annotation search (v1.4.0) — are shipped and synced
> across all four consumers. Remaining: write-side expansion, then Phase 7
> (Carrel data-layer extraction).

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

## Phase 6: Full Database Coverage (researched 2026-08-25)
*Context: exhaustive audit of `metadata.db` (user_version 27, 48 tables, 60 triggers), cross-checked against Calibre master (`db/backend.py`, `db/cache.py`, `db/page_count.py`). Findings from the schema archaeology sweep; nothing below is implemented yet.*

> **Upstream-sync policy** (per .clinerules Cross-Repo Implementation Rule): a phase item
> counts as done only when the cquarry change lands AND every affected consumer repo is
> synced (or explicitly waived here). Consumers: *CalibreQuarry* (CLI/TUI),
> *Hermitage* (GTK4 gallery), *Carrel-calibre-web* (web reader; ships its own older
> uv-25 library copy — use it as the low-schema test fixture for every read item),
> *Bindery* (EPUB repair; live `WritableCalibreDB.add_tag()` caller).

### Correctness first (write path)
- [x] **Mark books dirty in `metadata_dirtied`:** Upstream regenerates a book's sidecar `.opf` ONLY for ids present in `metadata_dirtied` (backend.py `dirty_books()`/`dirtied_books()`; consumed at startup then cleared). `WritableCalibreDB` currently bumps `last_modified` only — external edits never reach OPF/wireless sync. Every mutation must also `INSERT OR IGNORE INTO metadata_dirtied(book)`.
  - Pure behavior fix, no API change; consumers inherit it on version bump. *(Shipped in v1.2.0 — every mutation routes through `_touch_book()`/`_mark_dirty()`, guarded by a cached existence check for old schemas.)*
  - Upstream sync:
    - [x] *Bindery*: re-run audit flag-tagging (`src/bindery/audit.py` `add_tag` path); confirm tagged books' `.opf` regenerate on next Calibre start. *(Verified against the real user_version-27 schema: tagged ids land in `metadata_dirtied`, which is exactly the queue backend.py consumes for OPF regeneration; see Bindery patchnotes v0.18.1.)*
    - [x] *CalibreQuarry*: has zero `WritableCalibreDB` call sites today despite spec §6 listing it — wire its first write flow and verify OPF propagation end-to-end. *(New `--set-title ID TITLE` verb ships in CalibreQuarry v3.16.0 alongside a `--audit` "pending OPF sync" section.)*
    - *Hermitage / Carrel*: read-only, unaffected (no checkbox).
- [ ] **`annotations_dirtied` on annotation writes** (same mechanism; backend copies it into `metadata_dirtied` at startup).
  - Deferred until the ecosystem's first annotation writer lands (Phase 6 write-side expansion); the mechanism will ride along with it.
  - Upstream sync: none today — no ecosystem repo writes annotations yet; revisit when the annotations writer lands.

### Read-side gaps found
- [x] **Native `pages` field:** `books_pages_link` is now an upstream-managed table (cache.py maintains it natively; FIELD_MAP index 22). This library already holds 7,631 populated rows via CountPages. cquarry's `pages:` search location only probes a custom column labelled `pages`, so it matches nothing here. Add a `books_pages_link` fallback (guarded by `sqlite_master` existence check).
  - *(Shipped in v1.3.0: native table read first with OperationalError guard, `#pages` custom column kept as fallback; counts also ride on every book row. Former spec §5 deviation 7 rewritten.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: surface page counts in analytics/detail output. *(v3.17.0: `pages` in JSON/CSV/AI exports.)*
    - [x] *Hermitage*: page count in the book info popover. *(v1.4.0: `Book.pages` + Codex meta row.)*
    - [x] *Carrel-calibre-web*: optional — native page count on the detail page (template already carries reader-state hooks). *(v0.6.27: `cps/page_count.py` template global + detail.html line.)*
    - [x] *Bindery*: unaffected — waived explicitly (no pages surface in its audits).
- [x] **Library UUID:** expose `library_id.uuid` (`get_library_uuid()`); book uuids are fetched internally but absent from `get_all_books()`/`get_book()` rows.
  - *(Shipped in v1.3.0.)*
  - Upstream sync:
    - [x] *Carrel-calibre-web*: key wings/cache state by UUID instead of file path (its bundled library is a distinct copy — UUID disambiguates after moves/restores). *(v0.6.27: LibraryCache invalidates on `(mtime, library_id.uuid)`.)*
    - [x] *CalibreQuarry*: stamp library provenance (UUID) into exported catalogs/reports. *(v3.17.0: catalog headers + audit summary.)*
    - *Hermitage / Bindery*: unaffected — waived explicitly.
- [x] **Author/entity secondary columns:** `authors.sort`, `authors.link`, `tags.link`, `series.sort/link`, `publishers.sort/link`, `ratings.link`, `languages.link` are all unread today.
  - Additive row fields — no breakage risk for existing consumers.
  - *(Shipped in v1.4.0: book rows carry `author_sorts`/`author_links` parallel arrays; `get_entities(kind)` returns `{id, name, sort, link, count}` for authors/series/publishers/tags/languages with PRAGMA guards for old schemas. `ratings.link` remains unread — no consumer need; revisit on demand.)*
  - Upstream sync:
    - [x] *Hermitage*: author cards ordered by true sort name; clickable author link URLs. *(v1.5.0: Codex orders authors by sort key; links surfaced in tooltip.)*
    - [x] *CalibreQuarry*: opt-in link/sort columns in catalog & export output. *(v3.18.0: `--show-author-details` enriches catalog lines and JSON/CSV exports.)*
    - *Carrel / Bindery*: unaffected.
- [x] **Per-format detail:** `data.name` (filename stem) and per-format `uncompressed_size` are not publicly exposed (only aggregate `size`). Add `get_formats(book_id)` returning `{fmt: {path, size_bytes, name}}`.
  - *(Shipped in v1.3.0.)*
  - Upstream sync:
    - [x] *Bindery*: choose/report the audited EPUB via this API instead of raw `data` lookups (pairs with its existing `get_format_path` call site). — **Waived**: its single call site pre-filters EPUB rows in SQL and needs exactly one path; `get_format_path` remains the right tool there. Revisit if Bindery grows multi-format reporting.
    - [x] *Hermitage*: "open with external reader" format picker showing per-format sizes. *(v1.4.0: reader launcher resolves exact files via `get_formats()` first, glob kept as fallback.)*
    - [ ] *CalibreQuarry*: per-format disk-usage reporting in export/audit modes. — **Deferred** to the format-management item below; it wants an aggregate (`SUM(uncompressed_size) GROUP BY format`) helper rather than N×`get_formats()` calls, which lands naturally with that API batch.
- [x] **Cover helpers:** `has_cover` flag exists; no `get_cover_path(book_id)` resolving `<root>/<books.path>/cover.jpg` with disk verification.
  - *(Shipped in v1.3.0: `.jpg` primary with `.png` fallback, `verify=` toggle, ValueError on unknown books.)*
  - Upstream sync:
    - [x] *Hermitage*: audit how thumbnails resolve today, then route through `get_cover_path()`. *(v1.4.0: `Book.cover_path` delegates to cquarry, unverified semantics preserved.)*
    - [ ] *Carrel-calibre-web*: same for its cover route/static serving. — **Deferred to Phase 7** (the cover route is stock calibre-web `cps/cover.py`; routing it through cquarry is part of the data-layer extraction below, not a one-line swap).
    - [x] *CalibreQuarry*: cover-audit commands switch to the helper. *(v3.17.0: audit cover checks resolve through `get_cover_path()`.)*
    - [x] *Bindery*: pair `get_cover_path()` with `get_image_size()` for EPUB cover audits. — **Waived**: Bindery has no cover-audit code path today (its audits are content/pagenumbers/emptytext/ocr); nothing pairs against yet.
- [x] **Custom-column display config:** `get_custom_columns()` omits `normalized`, `editable`, and the `display` JSON (enum_values/enum_colors/composite_template). The richer `field_metadata` preference key is also unread.
  - Dict keys are additive — safe for all four consumers.
  - *(Shipped in v1.4.0: `editable`/`normalized`/decoded `display` with documented defaults on old schemas, plus `get_field_metadata()`.)*
  - Upstream sync:
    - [x] *Hermitage*: render enumeration values as colored badges (`#reading_status` is the showcase column). *(v1.5.0: pills tint from `display.enum_colors`.)*
    - [ ] *CalibreQuarry*: TUI coloring from `enum_colors`; disable edit verbs when `editable=0`. — **Deferred**: CalibreQuarry's terminal output has no pill/badge rendering surface yet and no edit verbs exist until the write-side verbs land; both arrive naturally then.
    - *Carrel / Bindery*: unaffected.
- [x] **Generic preferences accessor:** typed `get_preference(key)` wrapper; surface `grouped_search_terms`, `user_categories`, `tag_browser_*` order/hidden state.
  - Includes search-parity work inside cquarry itself (resolve grouped-search names in queries).
  - *(Shipped in v1.4.0: cached JSON-decoding accessor + typed helpers; engine resolves `GroupName:query` with upstream union/false-inversion semantics via an optional provider hook.)*
  - Upstream sync:
    - [x] *Hermitage*: sidebar honors `user_categories` groupings and hidden categories. *(v1.5.0: User Categories sidebar section expanding members into OR expressions. Hidden-category handling applies to built-in categories via `get_tag_browser_state()`.)*
    - [x] *CalibreQuarry*: expose grouped-search resolution in `--search` help/output. *(v3.18.0: `--search` help documents grouped terms + annotations; resolution works through the engine automatically.)*
- [x] **Annotation search:** the `annotations:` location matches each book's concatenated annotation `searchable_text` (full text-match kinds plus `true`/`false` presence); bare terms never sweep it, mirroring upstream's all-location. Documented deviation: ordinary matching rather than FTS5 `MATCH` stemming/ranking — same result set for typical queries without a second query path.
  - Upstream sync:
    - [x] *Hermitage*: search box over highlights/bookmarks (already renders annotations). *(Inherited by design: its search box evaluates through cquarry's engine, so `annotations:` queries just work.)*
    - [x] *CalibreQuarry*: optional `--search-annotations` mode. — **Satisfied by design**: `--search "annotations:<term>"` needs no separate mode; help text updated in v3.18.0.
    - [x] *Carrel-calibre-web*: search bar inherits `annotations:` through cquarry automatically (no code change required).
    - *Bindery*: unaffected.
- [x] **Dirtied-state visibility:** read-only `get_dirtied_books()` so consumers can show what Calibre will resync. *(Shipped in v1.2.0: sorted, deduplicated ids; `[]` when the table is absent; strictly observational.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: "pending OPF sync" section in doctor/check commands. *(Added to `--audit` output in CalibreQuarry v3.16.0.)*
    - *Others*: unaffected.
- [ ] **Notes system (adjacent DB):** modern Calibre stores category-item notes in `.calnotes/notes.db` (`notes`, `resources`, `notes_resources_link`; present but empty in this library). Out of `metadata.db` scope but worth a separate read-only module eventually.
  - Upstream sync (future):
    - [ ] *Hermitage*: display author/tag/publisher notes.
    - [ ] *CalibreQuarry*: include notes in catalog exports.

## Phase 7: Carrel extraction — from calibre-web fork to a cquarry-native web app (proposed 2026-08-26)
*Context: Carrel-calibre-web is currently a calibre-web fork where seven Carrel-owned modules
route through cquarry (`carrel_search`, `wings`, `categories`, `saved_searches`, `series_info`,
`reader_state`, `page_count`) while the core data path still runs on stock calibre-web's own
layer: `cps/db.py` (~1,200 lines of hand-rolled metadata.db access) plus `cps/config_sql.py`.
Stripping that out and putting cquarry underneath turns the fork into a new project with roots
in calibre-web — and makes cquarry grow the read APIs a real web frontend needs.*

> **Attribution & licensing gate (must land before any code moves):**
> - Pulling features/code FROM Carrel INTO cquarry requires a calibre-web attribution in
>   cquarry's README (Acknowledgements section naming calibre-web as the fork's base).
> - Confirm what actually needs attribution per pull: the search grammar implementation is
>   cquarry-original (`cps/carrel_search.py` only *evaluates* through cquarry's engine), so it
>   carries no calibre-web lineage; UI/template work and anything descended from calibre-web
>   files does.
> - **License check:** calibre-web is GPL-3.0; cquarry is MIT. Copying GPL code into MIT files
>   effectively relicenses those parts. Prefer clean-room API design informed by behavioral
>   research over verbatim code moves; if code must move, move it with its license notice and
>   decide the project-wide licensing story first.

- [ ] **Audit & boundary map:** enumerate every `cps/db.py` / `config_sql.py` call site and classify: replace-with-existing-cquarry-API, needs-new-cquarry-API, or calibre-web-domain-only (session/user/shelf logic stays).
- [ ] **Fill the API gaps the audit finds** in cquarry (likely: paginated/sorted book listing, browse facets, shelf-equivalent reads), each flowing through the Cross-Repo Implementation Rule (upstream research for CalibreQuarry, Bindery, Hermitage).
- [ ] **Route `cps/cover.py` through `get_cover_path()`** (deferred from Phase 6's cover-helpers item).
- [ ] **Swap the data layer:** replace `db.py` internals module-by-module behind its existing interface until metadata.db access happens only through cquarry; delete dead code.
- [ ] **Rebrand decision + README attribution** once the swap is complete.

### Write-side expansion (after the dirtied fix lands)
- [ ] **Entity setters:** authors (N:M link + name/sort computation), series (+`series_index`), publisher, rating (UNIQUE(rating) dedup), languages (canonicalize to ISO codes).
  - Upstream sync:
    - [ ] *CalibreQuarry*: CLI verbs (`--set-authors`, `--set-series`, `--set-rating`, …) become this repo's first real `WritableCalibreDB` consumers.
    - [ ] *Hermitage*: scope-assess lightweight edit popovers; default posture stays read-mostly (waive explicitly if declined).
    - *Carrel / Bindery*: unaffected.
- [ ] **`set_comments`:** 1:1 upsert/delete on `comments`.
  - Upstream sync:
    - [ ] *CalibreQuarry*: `--set-comments` verb.
    - [ ] *Hermitage*: optional description editing in the detail view.
    - *Carrel / Bindery*: unaffected (read-only rendering).
- [ ] **Custom-column writers:** Pattern A (normalized: value table + link) vs Pattern B (direct `book` column); enumeration validation against `display.enum_values`; tristate bool handling. Depends on the display-config read item above.
  - Upstream sync:
    - [ ] *Hermitage*: reading-status dropdown writing `#reading_status` (enum-validated).
    - [ ] *CalibreQuarry*: generic `--set-column <label> <value>`.
    - [ ] *Carrel-calibre-web*: optional reading-status toggle in the reader (its detail template already surfaces Calibre sync state).
    - *Bindery*: unaffected (keeps tag-based flagging).
- [ ] **Book lifecycle:** `remove_book` must satisfy the full `fkc_delete_*` trigger ordering across every link table, `comments`, `data`, `annotations`, `last_read_positions`, plugin/custom data; optional guarded `add_book` row insert (triggers auto-fill `sort`/`uuid`).
  - Upstream sync:
    - [ ] *CalibreQuarry*: guarded delete command (dry-run default, explicit confirm flag).
    - *Hermitage / Carrel / Bindery*: intentionally none — deletion stays a CLI-only operation.
- [ ] **Format management:** register/drop `data` rows (name, size) and toggle `has_cover`.
  - Upstream sync:
    - [ ] *Bindery*: register repaired/replacement EPUBs if write-back is ever added to its repair flow.
    - [ ] *CalibreQuarry*: import/export bookkeeping around format rows.

### Documentation drift
- [ ] **spec.md §5 item 7 is stale:** "There is no native pages table" was true historically; upstream now treats `books_pages_link` as a managed one-to-one field. Rewrite once the fallback above ships. *(cquarry-internal; no upstream impact)*

> **Version-sync reminder** (Phase 5 practice, applies to EVERY item above): bump
> `VERSION` + `__init__.py` + `config.py` + `README.md` + `spec.md` together, log the
> change in `patchnotes.md`, and mirror any behavior-affecting fix into each synced
> consumer repo's own patchnotes before ticking its checkbox.

