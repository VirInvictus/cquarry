## v1.8.0 (2026-08-30)

### Phase 9: the approved full mine (composed reads, integrity, analytics)

- **`get_book_dossier(book_id, *, include_comments=False)`.** The composed
  deep fetch detail views hand-assembled from ~10 read calls: the standard
  row, `cover_path`, per-format detail, `custom_columns` keyed `#label` as
  `{name, datatype, value}` (values exactly as `field()` yields),
  annotations, reading positions, plugin data, conversion overrides, and ; 
  only when flagged; `comments` as `{html, plain}`. `None` for unknown
  books. The frontend keeps rendering; cquarry owns the assembly.
- **`format_path_index()` + `find_book_by_path()`.** Every catalogued
  format path → book id in one `data ⋈ books` query, built exactly like
  `get_format_path()` and keyed `normcase(normpath())`, cached. Bindery's
  `CalibreIdResolver` was the seed consumer.
- **`cquarry.integrity`.** The mechanical "incomplete" predicates promoted
  from CalibreQuarry's `--audit` frontend: untagged, unrated, authorless,
  formatless, coverless, missing cover files, deprecated formats (caller
  supplies the set), low-res covers (`{id: (w, h)}`), duplicates
  (`(title, primary author)` groups), series gaps. Pure over the cached
  rows; every id list sorted; the two cover-file checks are the only
  functions that touch the disk.
- **`cquarry.analytics`.** `addition_timeline` (month/year), `author_stats`
  (count-desc then name, star-scale averages, unrated excluded),
  `rating_distribution` (half-step stars, `"unrated"` last), `vl_overlap`
  (multi-wing combos only, unknown wings raise through `resolve_vl`).
- **helpers, ISBN family.** `isbn_normalize`, `isbn_check_digit_is_valid`
  (ISBN-10 mod-11 / ISBN-13 EAN), and `to_isbn13` (978-prefix conversion;
  13-digit inputs pass through; deliberately NO source check-digit
  validation, matching the LibraryThing exporter's contract this replaces).
- **helpers, `tag_rollup(counts)`.** Subtree totals for dot-path counts:
  every node carries its own count plus everything below it; the rule
  Hermitage's `_total_count` and Carrel's category union already render,
  so adopting it is output-identical. The Phase 9 roadmap's example showed
  the keyed node keeping its bare count (`Fic.Fantasy: 3` where the
  subtree rule gives 5); the example was inconsistent with the render
  parity it was designed for and is corrected in the roadmap tick.
- **Docs split: `API.md` + README unbusy.** The full per-method reference
  moved from README's Public API section into `API.md` (with every new API
  above); the README keeps the hero, quick-starts (a dossier example joined
  the batch one), install, a one-line-per-module API-at-a-glance linking to
  `API.md`, the full search-grammar section, and the back matter.
  Spec gained §3.7 (integrity) and §3.8 (analytics); §6's consumer table
  refreshed for the sync releases below.

### Internal
- Test suite 209 → 241: `test_integrity.py`, `test_analytics.py`, plus
  `TestDossierAndPathIndex` and the ISBN/rollup batteries in
  `test_helpers.py`.

## v1.7.1 (2026-08-30)

### Bug fixes

- **`WritableCalibreDB.transaction()` restored as an exact alias of
  `batch()`.** The 2026-08-29 phase-3 import called `with db.transaction():`
  and hit `AttributeError`; 1.7.0 had shipped the deferred-commit context as
  `batch()` only, and the session fell back to raw `sqlite3`. The alias keeps
  the pre-1.7 call shape working (same `BEGIN IMMEDIATE` at entry, one commit
  at clean exit, full rollback on failure); the phase-3-import skill moved to
  `batch()` in the same pass.

### Internal
- Test suite 207 → 209: alias commit-at-exit and mid-block rollback cases
  mirroring the batch pair.

## v1.7.0 (2026-08-28)

### Phase 8: write-path completeness

- **`set_pubdate(book_id, value)`.** Accepts `str` (`YYYY-MM-DD` or a full ISO
  datetime), `date`, `datetime`, or `None`; naive datetimes are taken as UTC and
  the value is stored as `datetime.isoformat(' ')` in UTC, which reproduces
  Calibre's TEXT rows byte-for-byte (`'1991-10-01 07:00:00+00:00'`). `None`
  writes the `0101-01-01 00:00:00+00:00` undefined-date sentinel, which the
  search engine already treats as absent. No-op honest: an equal instant
  returns `False` without bumping `last_modified` or queuing OPF resync. This
  retires the raw-SQL pubdate workaround that put unix integers in the TEXT
  column and cost 8 linter errors on 2026-08-27.
- **`with wdb.batch():`** moves the commit boundary to the end of the block:
  `BEGIN IMMEDIATE` at outermost entry (the write lock held across the pass),
  nested batches join the one transaction, and a fault-injected mid-batch
  failure rolls back everything, including the `metadata_dirtied` queue. Every
  setter keeps its signature and per-call return semantics; only the commit
  boundary moves (`_begin`/`_commit`/`_rollback` guard on `_batch_depth`).
- **Comments read surface.** `get_book(book_id, include_comments=True)` adds
  the raw stored HTML under a `comments` key; rows otherwise keep omitting
  comment text (documented in docstrings at last). New bulk
  `get_comments(book_id=None) -> {book: html}` is the sanctioned bulk read,
  replacing consumers' reach-ins to `db.conn` (Hermitage's next sync adopts it).
- Docs: spec §3.6 documents the setter and batch semantics; CLAUDE.md gains the
  batch commit-boundary, pubdate-TEXT, and comments-omission contract notes;
  README's write example shows `batch()`.

### Internal
- Test suite 163 → 207 pytest-green runs: 17 new tests (batch
  atomicity/nesting/fault injection; pubdate round-trips
  str/date/datetime/aware-tz/sentinel/no-op/unknown-book; comments access
  including absent-table degradation) plus the parent cases the new fixture
  subclasses (TestBatchContext, TestSetPubdate, TestCommentsAccess) re-run by
  inheritance.

## v1.6.1 (2026-08-28)

### Bug sweep & hardening

- **Search fix (upstream parity).** An empty numeric query; `rating:`, `size:`, `pages:`,
  `series_index:`, `id:` with no value; now matches nothing (upstream `NumericSearch`'s
  `if not query: return matches`) instead of raising `ParseException`. Regression-tested.
- **Refactor: shared `_BOOK_SELECT`.** `get_all_books()` and `get_book()` now build their rows
  from one SQL constant. They drifted once (v1.6.0's `size` finding was the second instance of
  that class); the duplicate is what made it possible.
- **Transaction control pinned deliberately.** `WritableCalibreDB` now sets
  `autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL` explicitly with the rationale in code:
  the write path's `BEGIN IMMEDIATE` (take-the-write-lock-upfront, `busy_timeout` on
  acquisition) is impossible under PEP 249 `autocommit=False`, which holds a transaction open
  from the first statement; verified empirically before choosing.
- **Lint hardening.** Adopted `contextlib.suppress` (5 sites), comprehensions over
  append-loops (3 sites), removed an unnecessary lambda wrapper, renamed an ambiguous `l`,
  fixed an ambiguous `×` in a docstring. Swept with strict rule families
  (F/E7/B/SIM/PERF/C4/RET/PLW/RUF); zero functional findings beyond the fixes above.
- **Deliberate non-change:** `os.path` is kept over `pathlib`; `Path.resolve()` resolves
  symlinks where `os.path.abspath` doesn't, which would silently change `get_format_path()` /
  snapshot paths for symlinked libraries.

### Consumers swept
Hermitage, CalibreQuarry and Bindery swept with the same strict rule families: no functional
findings (global-statement caches, Pillow `with`-rebinds and script-level `subprocess.run`
calls are deliberate patterns). The `.split(",")` contract on native list fields: zero
violations across all three.

## v1.6.0 (2026-08-26)

### Completeness mining: every table, view, and query shape (Phase 6+)

Driven by a full audit against the 7,631-book testing-facility library, cross-checked
against upstream Calibre source (`calibre/db/search.py`):

- **User-category search (parity fix).** `@Name:query` now works exactly as upstream's
  `get_user_category_matches`: books holding any member value (exact match on the member's
  location), `@Name:.query` includes subcategories, `false` inverts, other query text is
  ignored as upstream, <2-char queries match nothing. Groups and real fields win over
  same-named categories; unknown `@Names` match nothing instead of silently degrading to an
  `all:` text sweep (the previous behavior). Providers opt in via an optional
  `user_categories()` hook (`CalibreDB` supplies `preferences.user_categories`).
- **Row-shape parity fix.** `get_book()` now returns the exact `get_all_books()` row shape:
  it was missing `size`. Both rows additionally carry `uuid` and `identifiers` (previously
  only the internal search view had them); enrichment degrades gracefully on ancient schemas.
- **Language ordering (parity fix).** A book's `languages` follow
  `books_languages_link.item_order` (link-id tiebreaker), matching Calibre; schemas
  predating the column keep link-id order.
- **New read APIs.** `get_feeds()` (the `feeds` news-recipe table),
  `get_annotations_dirtied_books()` (the annotations sibling of the OPF dirtied queue), and
  `get_tag_browser_counts()`; Calibre's own `tag_browser_*` sidebar rollups including
  `avg_rating`, with custom columns rekeyed to `#label`. View quirks worked around without
  touching the database: the ratings view's `rating` column aliased to `name`, and the
  series view's `title_sort()` UDF (moved to stdlib `helpers`; registered on the connection
  for the duration of the read only, then removed). The `tag_browser_filtered_*` variants
  are deliberately skipped; they call Calibre's GUI-state `books_list_filter()` function,
  which only exists inside a running Calibre (as does the `meta` view's `sortconcat()`
  aggregate; `meta` stays unread by design).
- **`get_entities("ratings")`** completes entity coverage: `{id, name, sort, link, count}`
  with the half-star integer surfaced as text (resolves the v1.4.0 deferral of
  `ratings.link`).
- **Docs.** `database_report.md` §6 records the in-process-function landmines discovered
  during the audit (`meta`, `tag_browser_filtered_*`, `title_sort` in views).

### Internal
- Test suite grew from 141 to 160 tests: user-category search battery (precedence,
  inversion, subcategories, unknown names, hookless providers), feeds / annotations-dirtied
  / tag-browser reads with old-schema degradation, ratings entities, row-shape parity, and
  language ordering on both current and pre-`item_order` schemas.
- Verified against the real testing-facility library: all 9 tag-browser categories read,
  recursive 31-VL "Unsorted" resolution unchanged, `get_all_books()` still ~0.12 s.

## v1.5.0 (2026-08-26)

### Write-side expansion (Phase 6)
- **Entity setters:** `set_authors` (relink + `author_sort` recomputation from per-author sort keys joined " & ", orphan pruning), `set_series` (+`series_index`: defaults 1.0 fresh assignment, preserved on reassignment; clearing nulls both), `set_publisher`, `set_rating` (0–5 stars stored ×2 with UNIQUE(rating) find-or-create dedup), `set_languages` (canonicalized to ISO codes via a new public `search.canonical_language`). All NOCASE-matched, transactional, no-op-honest, orphan-pruning.
- **`set_comments`:** 1:1 upsert/clear on the UNIQUE(book) comments row; raw HTML stored verbatim (readers sanitize).
- **Custom-column writers:** `set_custom_column(book_id, label, value)` auto-detects storage pattern by link-table existence (Pattern A value+link vs Pattern B direct), validates enumerations against `display.enum_values`, accepts tristate bools, refuses non-editable columns and composite columns (no storage).
- **Format management:** `add_format` / `remove_format` register/drop `data` rows (duplicate formats rejected case-insensitively); `set_has_cover` toggles the flag; files remain the caller's responsibility by design.
- **Book lifecycle:** `remove_book` cleans custom columns in both storage patterns (PRAGMA-detected) and both dirtied queues before firing the cascade trigger, then prunes orphaned entities; verified against a real user_version-27 library including normalized `custom_column_N` layouts lacking a `book` column.
- **New read API: `get_format_stats()`**; `{fmt: {count, bytes}}` aggregates in one query (unblocks CalibreQuarry's deferred per-format disk-usage report).

### Internal
- Test suite grew from 128 to 141 tests covering every setter's change/no-op/error paths, dedup semantics, validation failures, format round-trips, and removal cascades/pruning.

## v1.4.0 (2026-08-26)

### Read-side coverage (Phase 6, batch 2)
- **Entity secondary columns:** book rows gain `author_sorts` and `author_links`; arrays parallel to `authors` carrying each author's true sort key and link URL (empty strings on ancient schemas). New `get_entities(kind)` returns `{id, name, sort, link, count}` for authors / series / publishers / tags / languages, name-sorted; `PRAGMA` guards keep pre-column schemas working. (`ratings.link` remains unread; no consumer need; revisit on demand.)
- **Custom-column display config:** `get_custom_columns()` values now include `editable`, `normalized`, and the decoded `display` JSON (`enum_values` / `enum_colors` / `composite_template` …), with documented defaults on schemas predating those columns.
- **Generic preferences accessor:** `get_preference(key, default)` reads anything in the `preferences` table (JSON decoded where it parses, cached); typed helpers cover the high-traffic keys: `get_field_metadata()`, `get_grouped_search_terms()`, `get_user_categories()`, `get_tag_browser_state()` (order + hidden).
- **Grouped search terms (search parity):** the engine resolves `GroupName:query` as a union over the group's member locations, `GroupName:false` inverted, real fields winning over same-named groups, nesting raising `ParseException`; upstream semantics from `preferences.grouped_search_terms`. Providers opt in via an optional `grouped_search_terms()` hook.
- **Annotation search:** new `annotations:` location matches each book's concatenated annotation `searchable_text` with full text-match kinds (substring/exact/regex) plus `true`/`false` presence. Bare terms never sweep annotations, mirroring upstream. Documented deviation: ordinary matching instead of FTS stemming/ranking.

### Internal
- Test suite grew from 118 to 128 tests: parallel-array shape, entity shapes/counts/kind errors, display-config decoding, preference typing, grouped expansion/inversion, annotation matching and all-exclusion.

## v1.3.0 (2026-08-26)

### Read-side coverage (Phase 6)
- **Native page counts:** the `pages:` search location now reads Calibre's own `books_pages_link` table first (upstream-managed since the CountPages integration; guarded by an OperationalError catch for older schemas), keeping an int custom column labelled `pages` as fallback. Page counts also surface as a `pages` key on every `get_all_books()`/`get_book()` row. This resolves former documented deviation §5 item 7.
- **New API `get_formats(book_id)`** returns per-format detail `{fmt: {path, size_bytes, name}}` (unverified path from the original DB location; catalogued uncompressed size; filename stem) so consumers can pick/report formats without raw `data` queries.
- **New API `get_cover_path(book_id, verify=True)`** resolves `<library>/<books.path>/cover.jpg` with a `cover.png` fallback and disk verification (None when absent); `verify=False` returns the catalogued path unconditionally. Raises ValueError for unknown books.
- **New API `get_library_uuid()`** exposes the library's identity UUID from `library_id`; stable across moves/restores, unlike per-book uuids; intended as the cache key for per-library state in web/GUI consumers. None on schemas without the table.

### Internal
- Test suite grew from 111 to 118 tests: native-vs-fallback page precedence, end-to-end `pages:` searches, UUID round-trips, format-map shape, and cover-path variants (jpg/png/missing/unverified).

## v1.2.0 (2026-08-25)

### Write-path correctness (Phase 6)
- **OPF sync fix:** every `WritableCalibreDB` mutation now records the book id in Calibre's `metadata_dirtied` queue (`INSERT OR IGNORE`; guarded by a cached `sqlite_master` existence check for pre-existing schemas). Previously writes only bumped `books.last_modified`, but upstream regenerates a book's sidecar `.opf`; and re-pushes metadata to wireless readers; *only* for ids present in `metadata_dirtied` (backend.py `dirty_books()`/`dirtied_books()`), so external edits never reached OPF/wireless sync. No-op mutations still queue nothing.
- **New read API: `CalibreDB.get_dirtied_books()`** returns the sorted, deduplicated ids awaiting resync so consumers can show what Calibre will pick up at its next startup (`[]` when the table is absent). Strictly observational; clearing the queue remains Calibre's job.

### Internal
- Test suite grew from 104 to 111 tests: per-mutation dirtied-queue assertions (including no-op and duplicate-insert semantics against the real `UNIQUE(book)` schema) and reader coverage for missing-table tolerance.

## v1.1.1 (2026-08-25)

- **Fix**: `get_last_read_positions()` now matches Calibre’s real schema; the table has no `user_type` column and its time field is `epoch`, not `epoch_time`. Against a live library the old SELECT silently returned an empty list on OperationalError; rows now surface `id/book/format/user/device/cfi/epoch/pos_frac` exactly as stored. Caught by Carrel-calibre-web’s CI fixture, which uses a schema dumped from a real library.

## v1.1.0 (2026-08-25)

### Search parity (Phase 4; now actually true)
- **New built-in locations:** `size` (total bytes across formats, honors `k`/`m`/`g` suffixes), `pages` (sourced from an int custom column labelled `pages` when present), `title_sort`, and `series_sort` (`"Series [index]"`). All are exposed in `get_all_books()`/search views.
- **Saved-search interpolation:** `search:"Name"` resolves through the new `CalibreDB.get_saved_searches()` / `saved_search()`. Nesting works, cycles raise `ParseException`, unknown names raise `ParseException`, and lookups are case-insensitive.
- **Multi-valued count operator:** `tags:#>3`, `authors:#=2`, `formats:#<5`, `identifiers:#>=2`.
- **Language canonicalization:** `languages:English` matches books stored as `eng` via a 55-entry ISO 639-2 map; unknown tokens pass through untouched.
- **Slash date separators:** `pubdate:=1965/08/01` and `timestamp:2006/07` work alongside hyphens.
- **Expanded boolean keywords:** `checked`, `unchecked`, `blank`, `empty` plus `_`-prefixed variants, accepted in both boolean (`cover:`) and numeric/tristate (`rating:blank`) positions.
- **Strict virtual-library errors:** `vl:Unknown` raises `ParseException` instead of silently returning no matches; VL name resolution is case-insensitive end-to-end (`resolve_vl`, `vl_expression`).
- **Component matching everywhere:** Calibre's leading-dot modifiers under `=`; `.foo` (subtree) and `..foo` (component); now apply to *all* text fields, not just hierarchical tags.
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

### Write capabilities (Phase 3; new opt-in module)
- **`cquarry.write.WritableCalibreDB`:** a separate class that is unreachable from read-only `CalibreDB`. Registers Calibre's trigger dependencies (`title_sort()`, `uuid4()`, `PYNOCASE`) before any statement, uses `BEGIN IMMEDIATE` transactions, bumps `books.last_modified` on every mutation, and cleans link tables before tag deletion to satisfy `fkc_delete_on_tags`.
- **APIs:** `update_title()`, `add_tag()` / `remove_tag()` (returns whether state changed), `set_identifier()` / `set_identifiers()` batch upserts honoring `UNIQUE(book, type)`.

### Internal
- **Dropped `re.Scanner`:** the tokenizer is a plain `re.finditer` scanner over a documented pattern; pure documented stdlib.
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
- **Lazy-Loaded Comments & Custom Columns:** `db.py` no longer eagerly loads the `comments` HTML payloads or large custom column tables into memory when building the search view. These are now fetched from SQLite strictly on-demand per book ID during search expression evaluation. This reduces memory footprint and snapshot copy time for libraries with extensive HTML comments.

## v1.0.0 (2026-08-23)

### Extract & Launch
- **Initial Extraction:** Graduated `cquarry` into a standalone shared library.
- **Database Engine (`cquarry.db`):** Features the `CalibreDB` wrapper, which intelligently manages `metadata.db` access, falling back to a WAL-consistent snapshot if the Calibre desktop application holds an exclusive write-lock. Exposes `get_all_books()`, tags, series, and identifiers with performant SQLite JOINs and internal memory caching.
- **Search Grammar Engine (`cquarry.search`):** A full recursive descent parser implementing Calibre's search expression logic. Provides boolean logic (`AND`, `OR`, `NOT`), exact matching (`=value`), hierarchical tag prefix matching (`tags:Fic` matches `Fic.Fantasy`), date math (`date:>14daysago`), and nested Virtual Library resolution (`vl:"My Books"`).
- **Helpers:** Inherits standard Calibre domain formatters from CalibreQuarry (star rating converters, deterministic missing series gap detection, and binary image dimension sniffing).
