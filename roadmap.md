# cquarry Roadmap

This roadmap outlines the planned evolution of `cquarry` from a read-only metadata extractor to a full-featured Calibre ecosystem bridge, utilizing the structural discoveries documented in `database_report.md`.

> **Status (v1.7.0, 2026-08-28):** Phases 1–5 are implemented and covered by tests.
> Phase 6 is complete: write-path correctness + dirtied visibility (v1.2.0),
> read-side batches 1–2 (v1.3.0/v1.4.0), the write-side expansion (v1.5.0), and the
> v1.6.0 completeness-mining pass (user-category search, row-shape parity, language
> `item_order`, feeds / annotations-dirtied / tag-browser views, ratings entities).
> Phase 8 shipped in v1.7.0 (set_pubdate, batch() context, comments read surface)
> with its CalibreQuarry sync (3.23.0: --set-pubdate/--clear-pubdate, TUI pubdate
> ops, multi-verb batch mode). Phase 9 is DESIGNED AND APPROVED but not started:
> the full-mine scope agreed on 2026-08-28 is written into the Phase 9 section
> below, the implementation plan lives in session memory
> (`~/.claude/projects/-home-bdkl--gitrepos-cquarry/memory/project_phase89_extraction.md`),
> and the next session should resume there. Phase 7 (Carrel data-layer extraction)
> remains the follow-on project after Phase 9, with its license/attribution gate.

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
> uv-25 library copy; use it as the low-schema test fixture for every read item),
> *Bindery* (EPUB repair; live `WritableCalibreDB.add_tag()` caller).

### Correctness first (write path)
- [x] **Mark books dirty in `metadata_dirtied`:** Upstream regenerates a book's sidecar `.opf` ONLY for ids present in `metadata_dirtied` (backend.py `dirty_books()`/`dirtied_books()`; consumed at startup then cleared). `WritableCalibreDB` currently bumps `last_modified` only; external edits never reach OPF/wireless sync. Every mutation must also `INSERT OR IGNORE INTO metadata_dirtied(book)`.
  - Pure behavior fix, no API change; consumers inherit it on version bump. *(Shipped in v1.2.0; every mutation routes through `_touch_book()`/`_mark_dirty()`, guarded by a cached existence check for old schemas.)*
  - Upstream sync:
    - [x] *Bindery*: re-run audit flag-tagging (`src/bindery/audit.py` `add_tag` path); confirm tagged books' `.opf` regenerate on next Calibre start. *(Verified against the real user_version-27 schema: tagged ids land in `metadata_dirtied`, which is exactly the queue backend.py consumes for OPF regeneration; see Bindery patchnotes v0.18.1.)*
    - [x] *CalibreQuarry*: has zero `WritableCalibreDB` call sites today despite spec §6 listing it; wire its first write flow and verify OPF propagation end-to-end. *(New `--set-title ID TITLE` verb ships in CalibreQuarry v3.16.0 alongside a `--audit` "pending OPF sync" section.)*
    - *Hermitage / Carrel*: read-only, unaffected (no checkbox).
- [ ] **`annotations_dirtied` on annotation writes** (same mechanism; backend copies it into `metadata_dirtied` at startup).
  - Deferred until the ecosystem's first annotation writer lands (Phase 6 write-side expansion); the mechanism will ride along with it.
  - Upstream sync: none today; no ecosystem repo writes annotations yet; revisit when the annotations writer lands.

### Read-side gaps found
- [x] **Native `pages` field:** `books_pages_link` is now an upstream-managed table (cache.py maintains it natively; FIELD_MAP index 22). This library already holds 7,631 populated rows via CountPages. cquarry's `pages:` search location only probes a custom column labelled `pages`, so it matches nothing here. Add a `books_pages_link` fallback (guarded by `sqlite_master` existence check).
  - *(Shipped in v1.3.0: native table read first with OperationalError guard, `#pages` custom column kept as fallback; counts also ride on every book row. Former spec §5 deviation 7 rewritten.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: surface page counts in analytics/detail output. *(v3.17.0: `pages` in JSON/CSV/AI exports.)*
    - [x] *Hermitage*: page count in the book info popover. *(v1.4.0: `Book.pages` + Codex meta row.)*
    - [x] *Carrel-calibre-web*: optional; native page count on the detail page (template already carries reader-state hooks). *(v0.6.27: `cps/page_count.py` template global + detail.html line.)*
    - [x] *Bindery*: unaffected; waived explicitly (no pages surface in its audits).
- [x] **Library UUID:** expose `library_id.uuid` (`get_library_uuid()`); book uuids are fetched internally but absent from `get_all_books()`/`get_book()` rows.
  - *(Shipped in v1.3.0.)*
  - Upstream sync:
    - [x] *Carrel-calibre-web*: key wings/cache state by UUID instead of file path (its bundled library is a distinct copy; UUID disambiguates after moves/restores). *(v0.6.27: LibraryCache invalidates on `(mtime, library_id.uuid)`.)*
    - [x] *CalibreQuarry*: stamp library provenance (UUID) into exported catalogs/reports. *(v3.17.0: catalog headers + audit summary.)*
    - *Hermitage / Bindery*: unaffected; waived explicitly.
- [x] **Author/entity secondary columns:** `authors.sort`, `authors.link`, `tags.link`, `series.sort/link`, `publishers.sort/link`, `ratings.link`, `languages.link` are all unread today.
  - Additive row fields; no breakage risk for existing consumers.
  - *(Shipped in v1.4.0: book rows carry `author_sorts`/`author_links` parallel arrays; `get_entities(kind)` returns `{id, name, sort, link, count}` for authors/series/publishers/tags/languages with PRAGMA guards for old schemas. `ratings.link` remains unread; no consumer need; revisit on demand.)*
  - Upstream sync:
    - [x] *Hermitage*: author cards ordered by true sort name; clickable author link URLs. *(v1.5.0: Codex orders authors by sort key; links surfaced in tooltip.)*
    - [x] *CalibreQuarry*: opt-in link/sort columns in catalog & export output. *(v3.18.0: `--show-author-details` enriches catalog lines and JSON/CSV exports.)*
    - *Carrel / Bindery*: unaffected.
- [x] **Per-format detail:** `data.name` (filename stem) and per-format `uncompressed_size` are not publicly exposed (only aggregate `size`). Add `get_formats(book_id)` returning `{fmt: {path, size_bytes, name}}`.
  - *(Shipped in v1.3.0.)*
  - Upstream sync:
    - [x] *Bindery*: choose/report the audited EPUB via this API instead of raw `data` lookups (pairs with its existing `get_format_path` call site).; **Waived**: its single call site pre-filters EPUB rows in SQL and needs exactly one path; `get_format_path` remains the right tool there. Revisit if Bindery grows multi-format reporting.
    - [x] *Hermitage*: "open with external reader" format picker showing per-format sizes. *(v1.4.0: reader launcher resolves exact files via `get_formats()` first, glob kept as fallback.)*
    - [x] *CalibreQuarry*: per-format disk-usage reporting in export/audit modes. *(Done in v3.19.0 via `--format-stats`, powered by `get_format_stats()`.)*
- [x] **Cover helpers:** `has_cover` flag exists; no `get_cover_path(book_id)` resolving `<root>/<books.path>/cover.jpg` with disk verification.
  - *(Shipped in v1.3.0: `.jpg` primary with `.png` fallback, `verify=` toggle, ValueError on unknown books.)*
  - Upstream sync:
    - [x] *Hermitage*: audit how thumbnails resolve today, then route through `get_cover_path()`. *(v1.4.0: `Book.cover_path` delegates to cquarry, unverified semantics preserved.)*
    - [ ] *Carrel-calibre-web*: same for its cover route/static serving.; **Deferred to Phase 7** (the cover route is stock calibre-web `cps/cover.py`; routing it through cquarry is part of the data-layer extraction below, not a one-line swap).
    - [x] *CalibreQuarry*: cover-audit commands switch to the helper. *(v3.17.0: audit cover checks resolve through `get_cover_path()`.)*
    - [x] *Bindery*: pair `get_cover_path()` with `get_image_size()` for EPUB cover audits.; **Waived**: Bindery has no cover-audit code path today (its audits are content/pagenumbers/emptytext/ocr); nothing pairs against yet.
- [x] **Custom-column display config:** `get_custom_columns()` omits `normalized`, `editable`, and the `display` JSON (enum_values/enum_colors/composite_template). The richer `field_metadata` preference key is also unread.
  - Dict keys are additive; safe for all four consumers.
  - *(Shipped in v1.4.0: `editable`/`normalized`/decoded `display` with documented defaults on old schemas, plus `get_field_metadata()`.)*
  - Upstream sync:
    - [x] *Hermitage*: render enumeration values as colored badges (`#reading_status` is the showcase column). *(v1.5.0: pills tint from `display.enum_colors`.)*
    - [ ] *CalibreQuarry*: TUI coloring from `enum_colors`; disable edit verbs when `editable=0`.; **Deferred**: CalibreQuarry's terminal output has no pill/badge rendering surface yet and no edit verbs exist until the write-side verbs land; both arrive naturally then.
    - *Carrel / Bindery*: unaffected.
- [x] **Generic preferences accessor:** typed `get_preference(key)` wrapper; surface `grouped_search_terms`, `user_categories`, `tag_browser_*` order/hidden state.
  - Includes search-parity work inside cquarry itself (resolve grouped-search names in queries).
  - *(Shipped in v1.4.0: cached JSON-decoding accessor + typed helpers; engine resolves `GroupName:query` with upstream union/false-inversion semantics via an optional provider hook.)*
  - Upstream sync:
    - [x] *Hermitage*: sidebar honors `user_categories` groupings and hidden categories. *(v1.5.0: User Categories sidebar section expanding members into OR expressions. Hidden-category handling applies to built-in categories via `get_tag_browser_state()`.)*
    - [x] *CalibreQuarry*: expose grouped-search resolution in `--search` help/output. *(v3.18.0: `--search` help documents grouped terms + annotations; resolution works through the engine automatically.)*
- [x] **Annotation search:** the `annotations:` location matches each book's concatenated annotation `searchable_text` (full text-match kinds plus `true`/`false` presence); bare terms never sweep it, mirroring upstream's all-location. Documented deviation: ordinary matching rather than FTS5 `MATCH` stemming/ranking; same result set for typical queries without a second query path.
  - Upstream sync:
    - [x] *Hermitage*: search box over highlights/bookmarks (already renders annotations). *(Inherited by design: its search box evaluates through cquarry's engine, so `annotations:` queries just work.)*
    - [x] *CalibreQuarry*: optional `--search-annotations` mode.; **Satisfied by design**: `--search "annotations:<term>"` needs no separate mode; help text updated in v3.18.0.
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

## Phase 7: Carrel extraction; from calibre-web fork to a cquarry-native web app (proposed 2026-08-26)
*Context: Carrel-calibre-web is currently a calibre-web fork where seven Carrel-owned modules
route through cquarry (`carrel_search`, `wings`, `categories`, `saved_searches`, `series_info`,
`reader_state`, `page_count`) while the core data path still runs on stock calibre-web's own
layer: `cps/db.py` (~1,200 lines of hand-rolled metadata.db access) plus `cps/config_sql.py`.
Stripping that out and putting cquarry underneath turns the fork into a new project with roots
in calibre-web; and makes cquarry grow the read APIs a real web frontend needs.*

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
- [x] **Entity setters:** authors (N:M link + name/sort computation), series (+`series_index`), publisher, rating (UNIQUE(rating) dedup), languages (canonicalize to ISO codes).
  - *(Shipped in v1.5.0: NOCASE find-or-create, author_sort recomputation from per-author sort keys joined " & ", series_index defaults/preservation, ratings stored ×2 with dedup, language canonicalization via the new public `search.canonical_language`.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: CLI verbs (`--set-authors`, `--set-rating`, …) become this repo's first real `WritableCalibreDB` consumers. *(v3.18.0 `--set-title` was first; v3.19.0 adds the full verb surface through a shared `_run_write` dispatcher.)*
    - [x] *Hermitage*: scope-assess lightweight edit popovers; default posture stays read-mostly (waive explicitly if declined).; **Waived**: Hermitage's spec pins a read-mostly posture; no edit UI is planned, and cquarry's write module remains available if that ever changes.
    - *Carrel / Bindery*: unaffected.
- [x] **`set_comments`:** 1:1 upsert/delete on `comments`.
  - Upstream sync:
    - [x] *CalibreQuarry*: `--set-comments` / `--clear-comments` verbs. *(v3.19.0.)*
    - [x] *Hermitage*: optional description editing in the detail view.; **Waived** with the read-mostly posture above.
    - *Carrel / Bindery*: unaffected (read-only rendering).
- [x] **Custom-column writers:** Pattern A (normalized: value table + link) vs Pattern B (direct `book` column); enumeration validation against `display.enum_values`; tristate bool handling. Depends on the display-config read item above.
  - *(Shipped in v1.5.0: layout auto-detected by link-table existence; enum validation; tristate bools; non-editable/composite columns raise.)*
  - Upstream sync:
    - [ ] *Hermitage*: reading-status dropdown writing `#reading_status` (enum-validated).; **Waived** with the read-mostly posture above.
    - [x] *CalibreQuarry*: generic `--set-column <label> <value>`. *(v3.19.0, plus `--clear-column`; non-editable columns surface as clean errors.)*
    - [ ] *Carrel-calibre-web*: optional reading-status toggle in the reader (its detail template already surfaces Calibre sync state).; **Deferred to Phase 7**: writing belongs to the data-layer extraction, not a pre-extraction patch.
    - *Bindery*: unaffected (keeps tag-based flagging).
- [x] **Book lifecycle:** `remove_book` must satisfy the full `fkc_delete_*` trigger ordering across every link table, `comments`, `data`, `annotations`, `last_read_positions`, plugin/custom data; optional guarded `add_book` row insert (triggers auto-fill `sort`/`uuid`).
  - *(Shipped in v1.5.0 minus `add_book`: custom columns in both PRAGMA-detected patterns + both dirtied queues cleaned before the cascade trigger fires, orphaned entities pruned after. `add_book` stays open; creation flows want more design (path layout, cover handling) than a bare row insert.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: guarded delete command (dry-run default, explicit confirm flag). *(v3.19.0: `--remove-book ID [--confirm-remove]`, dry-run prints title+formats.)*
    - *Hermitage / Carrel / Bindery*: intentionally none; deletion stays a CLI-only operation.
- [x] **Format management:** register/drop `data` rows (name, size) and toggle `has_cover`.
  - *(Shipped in v1.5.0: `add_format` rejects case-insensitive duplicates, `remove_format`, `set_has_cover`; files stay the caller's responsibility by design.)*
  - Upstream sync:
    - [ ] *Bindery*: register repaired/replacement EPUBs if write-back is ever added to its repair flow.; **Conditional-future waiver**: its repair flow currently writes files via atomic replace without touching metadata.db rows; revisit only if that changes.
    - [x] *CalibreQuarry*: import/export bookkeeping around format rows. *(v3.19.0: `--format-stats` covers the reporting side via `get_format_stats()`.)*

### Documentation drift
- [x] **spec.md §5 item 7 is stale:** "There is no native pages table" was true historically; upstream now treats `books_pages_link` as a managed one-to-one field. Rewrite once the fallback above ships. *(cquarry-internal; no upstream impact)*; **Done in v1.3.0**: §5 item 7 was removed and replaced with a resolution note; the `pages:` row in §4 documents native-first sourcing.

## Phase 8: Phase-3 write-path completeness (proposed 2026-08-28, from the live gamut batch)

*Context: the 2026-08-27 acquisition batch exercised the write module end to
end on real books for the first time. Two gaps surfaced: there is no pubdate
setter (the batch had to write raw SQL and got the column's TEXT convention
wrong, costing 8 linter errors), and the phase-3 skill's "fix everything in one
transaction" is actually N separate `BEGIN IMMEDIATE` commits; each setter is
individually atomic, but a multi-book, multi-field curation pass is not.*

- [x] **`set_pubdate(book_id, value)`** on `WritableCalibreDB`: accept `str`
  (`'YYYY-MM-DD'` or a full datetime), `date`, or `datetime`; normalize to
  Calibre's stored TEXT form `'YYYY-MM-DD 00:00:00+00:00'`; route through
  `_touch_book()` + `_mark_dirty()` like every setter. The column is TEXT in
  metadata.db; writing a raw unix integer produces `'sentinel pubdate'` AND
  `'unparseable pubdate'` linter errors downstream (8 errors from 4 books on
  2026-08-27).
  - *(Shipped in v1.7.0: accepts `str | date | datetime | None`; naive datetimes
  are taken as UTC and the value stored via `datetime.isoformat(' ')` in UTC,
  which matches real Calibre TEXT rows byte-for-byte; `None` writes the
  `0101-01-01 00:00:00+00:00` undefined-date sentinel, the value the search
  engine already treats as absent; no-op honest; equal instants don't bump
  `last_modified` or queue OPF resync.)*
  - Upstream sync:
    - [x] *CalibreQuarry*: `--set-pubdate ID DATE` write verb through
      `run_write()`. (CalibreQuarry roadmap Phase 15.) *(Shipped in
      CalibreQuarry 3.23.0 with `--clear-pubdate`, TUI pubdate ops, and a
      multi-verb batch mode that runs several write flags in one invocation
      through a single `batch()` transaction; the driving consumer of this
      item's batch context.)*
- [x] **Batch-transaction context** (`with wdb.batch():` or equivalent): defer
  commits so a multi-book, multi-field pass commits exactly once; a crash
  mid-pass currently leaves a half-curated batch. Keep every existing method's
  signature and per-call semantics; only the commit boundary moves.
  - *(Shipped in v1.7.0: `BEGIN IMMEDIATE` at outermost entry, nested batches
  join the one transaction, and a fault-injected mid-batch failure rolls back
  everything including the dirtied queue; `_begin()`/`_commit()`/`_rollback()`
  guard on `_batch_depth`, so bare calls behave exactly as they did before.)*
- [x] **Tests**: `set_pubdate` round-trip (str / date / datetime inputs, TEXT
  normalization); batch-context atomicity (fault-inject a failure mid-batch →
  nothing written). *(v1.7.0: 17 new tests; batch/pubdate plus the comments
  battery; suite runs 163 → 207 green, the total including parent cases the
  new fixture subclasses re-run.)*
- [x] **Skill sync**: the phase-3-import skill in Brandon's library
  (`~/docs/Calibre Library/.claude/skills/`) currently documents the raw-SQL
  pubdate workaround this setter retires, including a gotcha entry explaining
  the TEXT column convention; update its transaction guidance to use
  `set_pubdate` / `--set-pubdate` when this ships. **This item is a floor,
  not a ceiling**: any behavior-affecting discovery made while building ; 
  formats, defaults, failure modes the tests surface; gets documented in the
  affected skills in the same release, even when this phase didn't predict it.
  *(Done in v1.7.1's pass, 2026-08-30: step 6 teaches `batch()`, the
  setter/verb surface is current, and both pubdate gotchas are
  setter-first.)*
- [x] **Document (or opt-in hydrate) `comments` on `get_book()` rows**: rows
  deliberately omit comment text (it can be huge), but nothing documents that ; 
  the 2026-08-27 batch read descriptions via raw SQL before noticing. Either
  note the omission in the README/docstrings or add an opt-in
  `include_comments` flag; silence is the only wrong answer.
  - *(Shipped in v1.7.0: both halves; `get_book(include_comments=True)` adds
  the raw HTML under a `comments` key, and the new bulk `get_comments()` gives
  the {book: html} map Hermitage was approximating by reaching into `db.conn`;
  the omission is documented in docstrings, CLAUDE.md, and the API reference.
  The bulk accessor rides into Phase 9's dossier work: the same release that
  documents the omission ships the composed read that carries comments.)*

Non-goals: no new read APIs; no CLI work here (verbs live in CalibreQuarry per
the frontend-only split).

> Batch-context upstream sync: CalibreQuarry's `_run_write()` dispatcher can
> grow a multi-verb mode over this, and the phase-3 skill's "fix it all in one
> transaction" standard is the driving consumer (its 2026-08-27 run committed
> ~45 mutations as 45 separate transactions).

## Phase 9: Mine the frontend; composed reads & library predicates from CalibreQuarry (proposed 2026-08-28)

*Context: the 2026-08-27 phase-3 batch curating 8 books surfaced logic that
lives in the CalibreQuarry *application* but is pure database derivation ; 
exactly the kind the frontend-only split says belongs here. Three seams, in
descending order of value. Each item names its consumers; each consumer's
implementation today is the mine site.*

- [x] **`get_book_dossier(book_id)`; the composed deep fetch.** *(cquarry side shipped in v1.8.0: signature as frozen; `cover_path` via `get_cover_path()` defaults so the row's `has_cover` distinguishes catalogued-but-missing; custom columns keyed `#label` from the value's `label` field.)* CalibreQuarry's
  `--book` dossier (`modes/detail.py show_book`) hand-composes ~10 read calls:
  `get_book`, `get_cover_path`, formats via `get_format_path`, custom columns
  via the `field()` hook, comments (through `strip_html`), `get_annotations`,
  `get_last_read_positions`, `get_plugin_data`, and conversion overrides.
  Hermitage and Carrel will reimplement every line of that for their detail
  views. Move the composition here as one opt-in-heavy call; pairs with
  Phase 8's comments-omission item (`include_comments`). The frontend keeps
  rendering; cquarry owns the assembly.
  - Upstream sync:
    - [x] *CalibreQuarry*: `show_book` becomes a renderer over the dossier dict
      (CalibreQuarry roadmap Phase 15). *(Shipped in CalibreQuarry 3.24.0;
      --audit/--analytics/--stats also consume the integrity and analytics
      modules, verified byte-identical on the real library.)*
    - [x] *Skill sync*: phase-3-import's "read EVERY field" step names the
      dossier call once shipped. **Floor, not ceiling**; same rule as Phase 8.
      *(Done 2026-08-30: the step names `get_book_dossier` + `--book`.)*
- [x] **Library integrity predicates.** *(cquarry side shipped in v1.8.0 as `cquarry.integrity` with the ten finders; rules match the frontend's exactly so the CSV is byte-identical.)* `modes/audit.py` implements untagged,
  unrated, coverless, and series-gap checks as frontend SQL, and
  `scripts/validate_metadata.py` reimplements overlapping rules; while
  `helpers.detect_series_gaps()` already lives here, proving the precedent.
  Promote the predicates to a read-side module (e.g. `cquarry.integrity`:
  `find_untagged()`, `find_unrated()`, `find_coverless()`, series gaps via the
  existing helper) so every consumer shares one definition of "incomplete".
  The taxonomy-driven opinionated layer stays in Brandon's library linter; only
  the mechanical predicates move.
  - Upstream sync:
    - [ ] *CalibreQuarry*: `--audit` renders the module's results (roadmap
      Phase 15).
    - [ ] *Hermitage*: health/dashboard views get the same predicates for free.
- [x] **Analytics derivations.** *(cquarry side shipped in v1.8.0 as `cquarry.analytics`; timeline/author stats/rating distribution/vl overlap.)* `modes/analytics.py` derives reading pace
  (books added per month from `timestamp`), per-author stats (count, ratings,
  formats), and wing overlap (books matching multiple resolved virtual
  libraries) from pure database data. The derivations move here
  (`get_addition_timeline()`, `get_author_stats()`; overlap composes from the
  existing VL resolution); the frontend keeps formatting.
  - Upstream sync:
    - [ ] *CalibreQuarry*: `--analytics` renders the module's results (roadmap
      Phase 15).
    - [ ] *Hermitage*: stats views; *Carrel*: collection-pace views.
- [x] **`to_isbn13()` into `helpers`** *(v1.8.0, as part of the full ISBN family below.)*: generic ISBN-10→13 conversion currently
  lives in `modes/librarything.py`; identifier work elsewhere (including the
  write module's `set_identifier`) will want it.

Non-goals: no network code (the LoC SRU client stays in CalibreQuarry's
scripts); no export-format rendering (LibraryThing/CSV/AI shapes are frontend);
no taxonomy-opinionated rules (they stay in the library linter).

### Approved full-mine expansion (design signed off 2026-08-28, not started)

The 2026-08-28 survey of all four consumer repos approved a wider mine than the
four items above. Everything here was designed section-by-section with Brandon;
signatures are frozen as written. License rule for the whole phase: CalibreQuarry
and Bindery are MIT (logic may move freely); Hermitage and Carrel are GPL-3.0,
so everything taken from them is behavioral (clean-room, rewritten against
cquarry's caches/row shapes), never a verbatim code move.

- [x] **helpers; ISBN family** *(v1.8.0: `isbn_normalize`, `isbn_check_digit_is_valid`, `to_isbn13`; the exporter's None→"" mapping lands with CalibreQuarry 3.24.0.)* (replaces the two divergent frontend copies in
  `modes/librarything.py` and `scripts/audit_isbns.py`): `isbn_normalize(raw)
  -> str` (strip separators, uppercase, keep X); `isbn_check_digit_is_valid(
  isbn) -> bool` (validates ISBN-10 mod-11 and ISBN-13 EAN); `to_isbn13(raw)
  -> str | None` (10→13 via 978-prefix + recomputed check digit, valid 13 passes
  through, else None; NO source check-digit validation, documented, matching the
  LibraryThing exporter; callers pair it with the validity helper for
  strictness). CalibreQuarry's exporter maps None back to its current "" output
  so the CSV stays byte-identical.
- [x] **helpers; `tag_rollup(counts: dict[str, int]) -> dict[str, int]`**: *(v1.8.0. EXAMPLE CORRECTION: the frozen example showed the keyed `Fic.Fantasy` keeping its bare 3 while implied `Fic` got 5; a mixed rule no consumer renders. The shipped rule is subtree totals (own + descendants: `Fic.Fantasy` becomes 5), which is what Hermitage's `_total_count` and Carrel's union already display, so adoption is render-identical. Flagged to Brandon in the 2026-08-30 session.)*
  leaf/partial dot-path counts in, every node including implied ancestors
  rolled up (`{"Fic.Fantasy": 3, "Fic.Fantasy.Epic": 2}` → `{"Fic": 5,
  "Fic.Fantasy": 3, "Fic.Fantasy.Epic": 2}`). Hermitage's `genres.py` and
  Carrel's `cps/categories.py` independently built this; the tree itself stays
  `tags_to_tree`.
- [x] **db; Bindery's gap**: `format_path_index() -> dict[str, int]` *(v1.8.0, plus `find_book_by_path()`.)* (every
  catalogued format path → book id, built with one `data ⋈ books` query using
  exactly `get_format_path`'s construction, keys `normcase(normpath())`,
  cached) and `find_book_by_path(path) -> int | None`. Bindery's
  `CalibreIdResolver` rebuilds on this; its `(123)` regex stays as the
  documented legacy fallback.
- [x] **db; `get_book_dossier(book_id, *, include_comments=False) ->
  dict | None`**: *(v1.8.0.)* composed deep fetch. Keys: `book` (the standard row),
  `cover_path` (`get_cover_path` defaults; row's `has_cover` distinguishes
  catalogued-but-missing), `formats` (`get_formats`), `custom_columns`
  (`{"#label": {name, datatype, value}}`, values exactly as `field()` yields ; 
  comments-typed columns are raw HTML), `annotations`, `reading_positions`,
  `plugin_data`, `conversion_overrides`, plus `comments` ({html, plain via
  strip_html}) only when flagged. Returns None for unknown books.
- [x] **`cquarry.integrity` module**; pure functions over the cached rows *(v1.8.0; signatures as frozen.)* (no
  SQL of their own; the two cover-file checks ride `get_cover_path` +
  `get_image_size`): `find_untagged(db)`, `find_unrated(db)`,
  `find_authorless(db)` (empty or ["Unknown"]), `find_formatless(db)`,
  `find_coverless(db)` (catalogued flag), `find_missing_cover_files(db)` (flag
  set, file absent), `find_deprecated_formats(db, formats)` (caller supplies
  the set; cquarry owns only the subset-of mechanism),
  `find_low_res_covers(db, min_dimension=500) -> {id: (w, h)}` (missing files
  excluded; that is find_missing_cover_files' answer), `find_duplicate_books(
  db) -> {(title-lower, primary-author-lower): [ids]}` (multi-member groups
  only), `find_series_gaps(db) -> {name: [missing]}` composing
  `get_all_series()` + `detect_series_gaps()`. All id lists sorted.
- [x] **`cquarry.analytics` module**; same shape, skipping anything existing *(v1.8.0.)*
  APIs already serve (`get_format_stats`, `get_entities`, `get_tag_counts`):
  `addition_timeline(db, granularity="month") -> {"YYYY-MM": n}` ("year" also
  supported, chronological), `author_stats(db) -> [{author, book_count,
  avg_rating, formats}]` (star-scale averages, unrated excluded, count-desc
  then name), `rating_distribution(db) -> {stars | "unrated": n}`,
  `vl_overlap(db, names=None) -> {(wing, ...): [ids]}` (multi-wing combos only;
  unknown wing raises via `resolve_vl`).
- [x] **Docs: `API.md` + README unbusy.** *(v1.8.0: API.md carries the full reference plus every new API; README 444 → 255 lines with a dossier quick-start and a module-at-a-glance table.)* New `API.md` at repo root carries the
  full per-method reference (moved from README's Public API section) plus every
  new API above; README keeps hero, features, quick-starts (dossier + batch),
  install, a one-line-per-module "API at a glance" linking to API.md, the full
  Search Grammar section, Acknowledgements (calibre-web attribution already
  covers the GPL-sourcing rule), Support, License. Target ~444 → ~250 lines.
- [x] **Tests**: `test_integrity.py`, `test_analytics.py`, plus extensions to
  test_helpers/test_db; suite should clear 220. *(v1.8.0: 209 → 241 green.)*
- [x] **Skill sync**: phase-3-import's "read EVERY field" step names
  `get_book_dossier` once shipped. **Floor, not ceiling**; same rule as
  Phase 8. *(Done 2026-08-30.)*

Upstream syncs for this expansion (each repo bumps and ticks its own roadmap):

- [x] *CalibreQuarry 3.24.0*: `show_book` renders over the dossier (+ prints
  pubdate); `modes/audit.py` predicates become integrity calls (CSV shape
  byte-identical); `modes/analytics.py` + `modes/stats.py` consume the
  analytics module and existing APIs (rendering only); ISBN family imported in
  `modes/librarything.py` + `scripts/audit_isbns.py`.
- [x] *Hermitage 1.7.0* (clean-room; GPL): `insights.py` predicates/analytics
  recompute through the modules (rendered output identical); `database.py`
  drops both `db.conn` reach-ins (`get_comments()` + rows' `identifiers`);
  `genres.py` via `tag_rollup`; `verify.py:33` hardcoded cover.jpg through
  `get_cover_path()`; `codex.py` `_clean_html`/star glyphs adopt cquarry's
  ONLY where a render-parity check proves identical output, else keep local
  and note why. This release also reconciles `__init__.py` 1.5.0 vs patchnotes
  v1.6.0 drift. *(Shipped 2026-08-30 (reconciliation landed in its 1.6.1 the
  same day). Waivers recorded in Hermitage's roadmap: `codex` keeps local
  under the parity gate; the Insights cover row merges cquarry's split
  predicates and thereby adopts canonical resolution (png-only covers count
  as covered); the identifiers row stays local; no upstream predicate.
  Flatpak pin bumped to this release.)*
- [x] *Bindery 0.20.0*: `CalibreIdResolver._load` over `format_path_index()`.
  WAIVED deliberately: swapping `audit.py`'s three targeted raw joins for
  whole-library hydration (the joins are cheaper and Bindery wants per-book
  maps, not row dicts). *(Shipped 2026-08-30; keys keep the resolver's
  historical resolve().lower() normalization. The uv.lock bumps to current
  main once cquarry's 1.8.0 commits are pushed; noted in Bindery's
  patchnotes.)*
- [x] *Carrel-calibre-web 0.6.28*: deployment venv
  (`~/.local/share/carrel/venv`, currently a stale non-editable cquarry 1.1.1
  copy) reinstalled EDITABLE from this repo per Carrel spec §8.2's documented
  contract; `cps/library_cache.py`'s own-SQL UUID read becomes
  `get_library_uuid()` (its "deliberately does NOT go through cquarry" comment
  was a stale-install artifact, not architecture). Analytics adoption DEFERRED
  to Phase 7 (the fork is about to be reworked; don't patch it twice).
  *(Shipped 2026-08-30 with one recorded deviation: the UUID read keeps its
  one-query shape; a full CalibreDB lifecycle would regress the per-cache-hit
  cost the module exists to keep cheap; but adopts `db_uri_ro()`'s contract,
  fixing a latent break on `?`/`#` library paths. Companion Carrel patchnote
  0.9.5; version leapfrogs upstream's in-flight 0.6.27b.)*

Waivers/deferrals recorded at design time: no `find_orphan_custom_column_links`
(schema-probing, deserves its own fixture work; candidate for a later pass);
no multi-verb CLI grammar beyond what 3.23.0 shipped; no LibraryCache-style
memoization in cquarry (contradicts the documented short-lived single-threaded
design); CalibreQuarry's `scripts/db_util.py` connect-ro triplication stays
(scripts deliberately keep raw connections outside the package contract).

> **Version-sync reminder** (Phase 5 practice, applies to EVERY item above): bump
> `VERSION` + `__init__.py` + `config.py` + `README.md` + `spec.md` together, log the
> change in `patchnotes.md`, and mirror any behavior-affecting fix into each synced
> consumer repo's own patchnotes before ticking its checkbox.

- [x] **Bug/API Drift (2026-08-29)**: `WritableCalibreDB` is missing the `transaction()` context manager in `cquarry 1.7.0`.
  - **Context**: During a Phase 3 import, calling `with db.transaction():` (the established pattern) threw an `AttributeError: 'WritableCalibreDB' object has no attribute 'transaction'`.
  - **Cause**: The 1.7.0 patchnotes introduce a new `batch()` transaction context. It appears `transaction()` was replaced by `batch()`.
  - **Workaround used**: Bypassed `cquarry.write` entirely and fell back to raw `sqlite3` `BEGIN IMMEDIATE` for the batch.
  - **Required Fix**: Either restore `transaction()` as an alias to `batch()` in `cquarry.write.WritableCalibreDB` for backwards compatibility, or officially update the downstream `phase-3-import` skill to use `batch()` instead.
  - *(Shipped in v1.7.1: both halves; `transaction()` is back as an exact
  `batch()` alias with commit/rollback tests, and the phase-3-import skill's
  "One transaction" step now teaches `batch()` + `set_pubdate` instead of raw
  SQL.)*

> **Standing note (2026-08-30, Hermitage Flatpak pin):** Hermitage's Flatpak
> manifest sources cquarry at a pinned commit (added in Hermitage 1.6.1;
> reproducible builds were chosen over `@main` tracking). Any item in this
> roadmap that touches Hermitage; a new consumer-facing API, a behavior
> fix, or a sync; must bump that pinned commit in the same release as the
> consumer sync, or Hermitage's packaged build silently lags the ecosystem.

- [ ] **Search/API (cquarry 1.9.0 candidate)**: Normalize custom column lookups. `db.load_custom_column()` currently expects the exact *Display Name* (e.g., `Translator(s)`), which propagates to `CalibreQuarry`'s `--show-custom` flag and the `#` search grammar. This creates a UX asymmetry, as Calibre's native search and `cquarry`'s own `WritableCalibreDB.set_custom_column` use the internal `#label` (e.g., `#translators`). `load_custom_column` and `get_custom_columns` should resolve columns via the `#label` (or gracefully fallback to display name) to unify the API. **Upon completion, update the `phase-3-import` skill to remove the Custom Column gotcha so the next session is in the loop.**
