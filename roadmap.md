# cquarry Roadmap

**Status: REOPENED 2026-09-29 as the spine of the ecosystem Calibre-parity program.** The
2026-09-28 closure (and the FINAL BLITZ closure before it, recorded in `project.done`) is
superseded by Brandon's 2026-09-29 decision to scope every roadmap in the ecosystem toward
full 1:1 parity with the Calibre application, so that Calibre work can be completely
automated. The verified 2026-09-28 utility survey becomes committed phases (14-18 below);
every recorded decline stands unless a phase names its reversal; the reopen conditions in
`project.done` are either answered by this decision or stay gated below.

The original roadmap carried the full phase-by-phase build history (Phases 1-13 with
per-item ship notes, upstream-sync checkboxes, cascade notes, and dated audit blocks).
That history was minimized into the ledger below on 2026-09-28; the complete text is
preserved in git history (the tree of commit 2593086) and the release-by-release record is
`patchnotes.md`. The 2026-09-29 parity restructure kept the ledger intact and converted the
open-work inventory into the program and phases below.

Standing rules for any lane, unchanged by the reopen:

- **Cross-Repo Implementation Rule.** A cquarry feature is done only when every affected
  consumer repo is synced or explicitly waived: CalibreQuarry, bindery-cli, Hermitage,
  Carrel-calibre-web. Each consumer roadmap now carries its side of the program.
- **Parity claims name their lanes.** No roadmap in the ecosystem claims bare "1:1 parity";
  claims count native (N), owned native gaps (G), and orchestrated (O) coverage, and name
  the excluded process-bound (P) and declined (D) surface. Definitions below.
- **Version sync is guarded** (all eight carriers) by `tests/test_version_sync.py`;
  releases follow the release procedure, including the verbatim patchnotes tag.
- **Hermitage-touching releases bump the Flatpak manifest pin in the same release**;
  the pin is Hermitage's floor mechanism. Its cquarry pin currently rides the
  `compat-py3.13` branch (v1.18.0+py313.1), not main.

## Completed phases (ledger)

| Phase | Scope | Closed | Releases |
|---|---|---|---|
| 1 | Read-only enhancements: `get_book`/`search_books`/`get_format_path`, saved-search interpolation, VL UI state, hierarchical tags, safe custom-column reads, rating normalization | 2026-08-23..25 | v1.0.0-v1.2.0 |
| 2 | Metadata portability: annotations, reading progress, plugin data, comment stripping, conversion profiles | 2026-08-23..25 | v1.0.0-v1.2.0 |
| 3 | Write capabilities: `register_udfs`, `update_title`, tag and identifier writes | 2026-08-23..25 | v1.0.0-v1.2.0 |
| 4 | Search parity wave 1: author comma splitting, missing locations, count operator, language canonicalization, strict VL errors, boolean vocabulary, `.`/`..` component matching | 2026-08-23..25 | v1.0.0-v1.2.0 |
| 5 | Sweep hardening: JPEG-sniffer hang, AST corruption on quoted colons, N+1 comment queries, `re.Scanner` removal, version sync | 2026-08-23 | v1.0.1 |
| 6 | Full database coverage: `metadata_dirtied`, native `pages`, library UUID, entity sort/link columns, `get_formats`, cover helpers, display config + `get_field_metadata`, preferences + grouped search, annotation search, dirtied visibility; write-side expansion (entity setters, `set_comments`, custom-column writers, book lifecycle, format management) | 2026-08-25..26 | v1.2.0-v1.6.0 |
| 7 | Carrel-calibre-web data-layer extraction: the fork's reads onto cquarry, `list_books` (+ multi-key sorts), cover routing, attribution and rebrand decisions | 2026-09-03..04 | v1.10.0-v1.11.1 (Carrel 0.6.30-0.6.39) |
| 8 | Write-path completeness: `set_pubdate`, `batch()` transactions, comments read surface, `transaction()` alias, import-skill syncs | 2026-08-28..30 | v1.7.0-v1.7.1 |
| 9 | Frontend mine: `get_book_dossier`, `cquarry.integrity`, `cquarry.analytics`, the ISBN family, `tag_rollup`, `format_path_index`/`find_book_by_path`, `genre_distribution`, the README-to-`API.md` split | 2026-08-30 (+2026-09-06) | v1.8.0, v1.12.0 |
| 10 | `add_book` creation path: dry-run plan, sniff-or-raise covers, byte-identical re-import refusal, batch-scoped directory compensation | 2026-09-06 | v1.14.0 |
| 11 | Set-write conveniences: `clear_tags`, `add_custom_column_values`, `clear_rating` | 2026-09-06 | v1.13.0 |
| 12 | Audit hardening: BaseException rollback, batch filesystem compensation, datatype dispatch + star-rating columns, nested-batch poisoning, native-list custom columns, `refresh()`, search parity wave 2 + the spec §5 honesty pass, `set_format`, `find_candidate_duplicates`, `export_rows`, `find_identifierless`, test-suite deflation | 2026-09-09..10 | v1.15.0-v1.17.0, v1.16.1 |
| 13 | Upstream comparison wave: FTS sidecar reads, page provenance, `#label_index`, super-quotes, §5 dispositions, path re-laying on title/author writes, `rename_entity`/`remove_entity_everywhere`, `set_cover`/`remove_cover`, the trash lifecycle, custom-column DDL, original-format save/restore, passthrough sort setters, identifier cleaning, the four-program consumer wave | 2026-09-12..13 | v1.18.0-v1.21.0 |
| Final audit + blitz | GitHub storefront and publish-workflow hardening (SHA pins, ruleset, Releases backfill), docs-truth batches, the LOW bug tail, `find_missing_format_files`, `external_changes_detected`, the backup-API snapshot (`SNAPSHOT_LEASH`/`backup_to`), annotations bulk read + `get_annotations_decoded`, `set_series_index`, the Hermitage py3.13 compat branch | 2026-09-15 | v1.22.0-v1.23.0 |
| Field fixes | `set_series` NOT NULL crash (clears reset to 1.0); version-sync guard converted to `unittest` after CI's discovery skipped it; the 2026-09-26 `set_comments` report investigated and CLOSED as a caller defect (2,914-scenario in-batch fuzz, five regression pins, no library change) | 2026-09-16 / 2026-09-26 | v1.23.1-v1.23.2 / no release |
| Upstream watch | Calibre 9.15 audit: zero schema/search/annotations diff; one hardening candidate logged (see Logged hardening candidates) | 2026-09-18 | no release |

## The parity program (defined 2026-09-29)

The ecosystem's working answer to "fully 1:1 parity with the Calibre application": every
capability Calibre has is covered natively, or by scripted orchestration of Calibre's own
headless tools, so any Calibre work can be automated without opening the GUI. Lanes:

- **N (native, shipped).** Covered natively by cquarry or a consumer repo.
- **G (native, gap).** Coverable inside the ecosystem's constraints (stdlib-only,
  read-only-by-default); a named phase or repo owns it.
- **O (orchestrated).** Automated by driving Calibre's own headless binaries (calibredb,
  ebook-convert, ebook-polish, ebook-meta, calibre-debug, ebook-device) through a consumer
  verb; the established pattern (`run convert`, `run flush` in CalibreQuarry).
- **P (process-bound).** Needs the running Calibre process; excluded from the denominator:
  FTS5 MATCH through the custom tokenizer, the `meta` and `tag_browser_filtered_*` views,
  GUI-state fields, the OPF backup daemon thread, in-process plugin execution, the
  MTP/wireless device stack, the GUI-only editors/viewers (ebook-edit, ebook-viewer),
  annotation-content writes, composite-column computation (rides the GPM boundary below).
- **D (declined).** Excluded by a recorded decline (the Declined section below and the
  per-repo ledgers); each carries its reason, reversal needs a new decision.

The honest parity claim is therefore "N + G + O complete", never "equals Calibre". The
full-surface classification against the v9.15.0+103 reference clone (2026-09-29): the
database layer's gap menu is the survey, now Phases 14-18; the CLI surface is
CalibreQuarry's lane (calibredb 23-command parity is already met or exceeded, plus ~10
headless upstream verbs with no ecosystem wiring yet: its Phase 20); conversion is O-lane
(`run convert` around ebook-convert, ~45 input / ~20 output formats); polish is O-lane
plus bindery-cli's native repair domain, which exceeds ebook-polish on acceptance (the
epubcheck gate, byte determinism, atomic library replacement); the browse GUI is
Hermitage's lane; the web reading room is Carrel-calibre-web's lane.

**The one large native gap with no owner is the GPM template engine**
(`src/calibre/utils/formatter.py` + 127 builtin functions): composite columns, `template:`
searches, and save-to-disk filename templates are computed in-process and invisible to
every ecosystem tool. spec §7 keeps it a permanent boundary for cquarry search; the parity
ledger records it as the boundary's standing cost, revisitable only by a new decision.

**Open unowned automation surface (recorded 2026-09-29, deliberately not declined):**

- **News fetching**: 1,094 upstream recipes (`recipes/`; engine
  `src/calibre/web/feeds/news.py`, anti-bot infra `src/calibre/web/automate/`). Headless
  today via `ebook-convert <recipe> out.epub`; the natural owner is a CalibreQuarry
  `run news` verb. Unowned until a lane claims it.
- **Device sync**: the USBMS subset is headless today (`ebook-device`,
  `src/calibre/devices/cli.py:247-390`); MTP/wireless (~65 drivers) is P. Natural owner:
  a CalibreQuarry `run device` verb for the USBMS subset. Unowned until claimed.

These rows stay visible here so nothing is silently out of scope; claiming one is a
normal lane decision, not a reversal.

## Parity phases (committed 2026-09-29)

Sequenced, undated; ship order within a phase is free. Upstream evidence cites the
reference clone's `src/calibre/...` lines. The Cross-Repo Implementation Rule applies to
every item: a cquarry ship is not done until the named consumers adopt or waive.

### Phase 14: Completion wave (finishes contracts cquarry owns)

- [x] **Restore-from-trash verbs** (committed 2026-09-29 under the automation-set
  decision; was survey-logged). `copy_format_from_trash`/`move_format_from_trash`,
  `copy_book_from_trash`/`move_book_from_trash`, `delete_trash_entry`
  (`cache.py:3503-3568`): cquarry's own `remove_book(delete_files="trash")` is write-only
  today. Size S for the format half; the book half needs sidecar-OPF parsing (adjacent to,
  but distinct from, the declined OPF-generation family: this reads Calibre's own stored
  OPF, it does not generate one). **Shipped 1.24**: the book half parses the entry's
  sidecar `metadata.opf` (stdlib ElementTree) to rebuild the core row; custom-column
  values, annotations, and plugin data are not restored (the OPF carries none of them).
- [x] **`get_dirtied_formats()` read** beside `get_dirtied_books()` and
  `get_annotations_dirtied_books()`. Retires CalibreQuarry's raw sidecar read
  (`modes/fts.py:91`) and unblocks its `fts-index` verb. Size XS.
  **Shipped 1.24**: `list[tuple[int, str]]`, sorted, formats uppercased as stored.
- [x] **`set_custom_column_metadata`** (`backend.py:1407`, `cache.py:3233`): modify an
  existing column's name/editable/display JSON, notably `enum_values`, without Calibre
  open; today nothing can populate that list. Size S. **Shipped 1.24**: honest no-op on
  equal values; a change sets `update_all_last_mod_dates_on_start` like upstream's
  wrapper; datatype and label changes are refused by omission (schema compatibility).
- [x] **The Carrel residue trio, ungated**: `get_book_by_uuid` (the Calibre-Companion
  endpoint dependency), the entity-to-ids resolver, and a bulk formats map. The standing
  gate ("waits for Carrel's lane") is answered: the fork's roadmap (created 2026-09-29)
  pulls them. Size S each. **Shipped 1.24** as `get_book_by_uuid` (NOCASE, None on miss),
  `get_entity_book_ids(kind, name)` (authors/series/publishers/tags/languages; tags are
  the engine's anchored-subtree rule; ratings out, a rating slice is a search), and
  `get_all_formats()` (cached `{book_id: [FMT]}`). Carrel's adoption stays waived this
  round per the decision record.
- [x] **`facet_counts`**: browse facets over a search result; ungated with Carrel's lane,
  ships together with Phase 18's restricted tag browser. Size M. **Shipped 1.24** as
  `facet_counts(query)` + the Phase 18 seam `facet_counts_for_ids(ids)`: per-value counts
  over the restricted set, builtin facet fields plus custom columns by `#label`, ratings
  in stars, count-desc then value-asc.

### Phase 15: Maintenance ring (the lifecycle layer around the write module)

- [x] **`vacuum` / `analyze` / `integrity_check` verb** (upstream `backend.py:1638`
  vacuums the main DB and the attached FTS sidecar; the notes DB stays out of scope).
  Size XS-S. **Shipped 1.24** as `WritableCalibreDB.maintain(*, vacuum, analyze,
  integrity_check, include_fts)`: refuses to run inside `batch()` or mid-transaction,
  reports the check rows, and names the sidecar's attach state.
- [x] **check_library extra-side disk checks** for the integrity family: extra format
  files, extra covers, malformed paths, extra files in book dirs, failed folders
  (upstream `src/calibre/library/check_library.py:50`); cquarry ships the missing-side
  checks only. Size M. Consumer: CalibreQuarry's `--health` lane. **Shipped 1.24** as
  `integrity.check_library_disk(db, *, name_ignores, extension_ignores)`: one walk,
  categorized findings (extra_titles/extra_authors/malformed_paths/malformed_formats/
  extra_formats/extra_files/extra_covers/failed_folders); the missing side is not
  re-answered (find_missing_format_files/find_missing_cover_files own it); the format
  classifier is structural (token extension minus the image/OPF/junk set), not
  upstream's curated BOOK_EXTENSIONS, and POSIX case sensitivity is assumed.
- [x] **FTS queue management verbs** (`fts_unindex`, per-book reindex, full reset): pure
  sidecar SQL against `books_text`/`dirtied_formats` (`cache.py:603-698`,
  `fts/connect.py:75-92`); extraction itself stays Calibre's. Size S. **Shipped 1.24**
  with one boundary named: `fts_reindex_book`/`fts_reindex_all`/`fts_queue_clear` are the
  queue half (upstream `dirty_book`/`dirty_existing`/`remove_dirty`/`clear_all_dirty`);
  upstream `fts_unindex`'s index-row deletion is process-bound -- the `books_text`
  delete triggers tokenize through Calibre's custom FTS5 tokenizer (the schema fact the
  CLAUDE.md NEVER-delete rule is built on), so removing indexed text stays Calibre's
  (an O-lane `calibredb fts_unindex` wrap if a consumer asks).
- [x] **Typed `set_preference` writer** (the 2026-09-29 automation-set reversal; the
  survey's "needs a recorded decision" note is satisfied by that decision): saved-search
  add/delete/rename (`cache.py:3379-3399`), virtual libraries, user categories, grouped
  search terms, and the FTS enable flag (`cache.py:588` + `fts/connect.py:60`) are all
  plain preference rows; cquarry reads all of them and writes none. One schema-faithful
  JSON upsert unlocks the set; this is the calibredb `saved_searches` parity item.
  Size S. **Shipped 1.24** as `set_preference(key, value)` over a five-key whitelist
  (payload validated per key before anything lands) plus `saved_search_add`/
  `saved_search_delete`/`saved_search_rename` (rename resolves NOCASE and REFUSES to
  overwrite an existing name, where upstream silently overwrites -- documented).

### Phase 16: Read-surface wave and shared-helper promotions

- [x] Cover bytes and freshness: `Cache.cover()` / `cover_last_modified()`
  (`cache.py:1426`, `:1481`); web frontends need bytes and conditional-GET mtimes. Size XS.
  **Shipped 1.25** as `get_cover_bytes` / `get_cover_last_modified` (aware-UTC stamp),
  riding `get_cover_path`'s resolution and unknown-book ValueError.
- [x] Inverse virtual-library map: `virtual_libraries_for_books` (`cache.py:3600`).
  Size S. **Shipped 1.25**: `{book: sorted wing-name tuple}`, every requested id
  answered (upstream's shape); a wing that fails to evaluate is skipped with a
  warning where upstream splices an error string into the name tuple.
- [x] Inverse user-category map: `user_categories_for_books` (`cache.py:3645`). Size S.
  **Shipped 1.25**: members probed exactly like the `@Name` location (so the answers
  agree with `@Name:Category` by construction); composite and unknown-location members
  match nothing rather than erroring.
- [x] `format_hash` / `format_metadata` (`cache.py:1255`, `:1268`): the file-changed
  detector the FTS sidecar's own hash columns compare against. Size XS.
  **Shipped 1.25**: SHA-256 over `get_format_path`'s verified resolution; metadata is
  `{path, size, mtime}` with an aware-UTC stamp.
- [x] `books_by_year` / `books_by_month` over any date field (`cache.py:1057`, `:1080`);
  `analytics.addition_timeline` buckets only the fixed `timestamp` field into counts.
  Size S. **Shipped 1.25**: builtin date locations plus date-typed custom columns,
  restriction-shaped like facet_counts_for_ids; sentinels/blank land nowhere.
- [x] `get_next_series_num_for` (+ custom-column variant, `cache.py:2567`,
  `legacy.py:873`): the preference-aware next series number. Size XS-S. **Shipped
  1.25** with one boundary named: upstream reads `series_index_auto_increment` from
  its tweaks files, which metadata.db never carries, so cquarry reads the library's
  `preferences` table (fallback: upstream's shipped default `"next"`); a local
  tweaks.py override is invisible to any database-side reader. Custom columns by
  `#label`; `current_indices=True` returns the `{book: index}` map.
- [x] Annotation conveniences: filter/limit/user/type variants over `get_annotations`,
  removed-skeleton handling, style discovery (`cache.py:3896-3924`). Size S.
  **Shipped 1.25** as `get_annotations_filtered` (the decoded view plus
  user_type/user/removed, upstream's exact-dict style match, tombstones hidden by
  default -- upstream's ignore_removed inverted to the renderer-facing default) and
  the discoveries `get_annotation_users`/`get_annotation_types`/`get_annotation_styles`
  (styles are what the library holds; upstream's builtin viewer catalog is GUI
  constants, not data).
- [x] `read_backup` (`cache.py:2212`): read the stored sidecar `metadata.opf` to diff
  Calibre's last write against the rows; reads what Calibre wrote, does not generate.
  Size XS. **Shipped 1.25** as `CalibreDB.read_backup` (bytes; None on no
  directory/backup yet, the empty-path rule included; the OPF-generation decline
  untouched).
- [x] Smaller: `size_stats` (`cache.py:1753`), a per-book all-fields link map
  (`cache.py:3130`), `is_fts_enabled` (`cache.py:551`), last-read-position filters
  (`cache.py:3751`). Size XS each. **Shipped 1.25** in one commit: `get_size_stats`
  (notes always 0 -- the recorded decline -- kept for the three-column shape),
  `get_all_link_maps_for_book` (the four builtin fields plus link-carrying custom
  columns via the custom_column_links seam), `is_fts_enabled` (the preference; the
  in-process pool state is not database-visible), and `get_last_read_positions`
  fmt/user/order_by/limit filters. Rode one fix: the custom-column link map was
  stashed series-only inside load_custom_column, so custom_column_links answered
  empty for text/enumeration/rating despite its documented promise.
- [x] **Ordered-VL-names helper**: promotes the near-identical copies at Carrel
  `cps/wings.py:33-47` and Hermitage `app.py:1605-1616` (Calibre sidebar order:
  stored tab position first, unknown names alphabetical). Size XS. **Shipped 1.25**
  as `CalibreDB.ordered_virtual_library_names()`; per the waiver both consumers
  still carry their private copies, to retire in a future consumer wave.
- [x] **Unpiped-author display helper**: retires the nine `replace("|", ",")` copies
  across Carrel-calibre-web and Hermitage. Size XS. **Shipped 1.25** as
  `helpers.unpipe_author` (render-identical: a bare comma, so switching call sites
  is not a visual change); per the waiver the copies stay until a consumer wave.
- [x] **Tag-membership id-set rollup**: Carrel `cps/categories.py:29-52` builds
  `tag_path -> frozenset(book_ids)` privately because `tag_rollup` returns counts only.
  Size S. **Shipped 1.25** as `CalibreDB.tag_rollup_ids(ids=None)`; the counts-only
  helper stays for count consumers, and Carrel's private copy retires in a consumer
  wave per the waiver.
- [x] **Identifier-link helper, Open Library canonical** (Brandon's 2026-09-29 call):
  ISBN links resolve to `openlibrary.org/isbn/`; Carrel's WorldCat mapping
  (`quarry_grid.py:581`) switches; Hermitage's mapping is already the canonical shape
  (`codex.py:124-138`). Size XS. **Shipped 1.25** as `helpers.IDENTIFIER_LINKS` +
  `identifier_link()` (Hermitage's table verbatim, unknown types None); Carrel's
  WorldCat switch is its consumer wave's business.

### Phase 17: Write-side extras

- [x] Author `sort`/`link` writers (`cache.py:3050`, `:3073`, and the generic
  `set_link_map`, `cache.py:3176`): cquarry reads `author_links` and recomputes author
  sort inside its own setters but cannot edit a link column. Size S. **Shipped 1.25**
  as `set_author_sort_name` (row-level `authors.sort`, book-level `author_sort`
  recomputed " & "-joined) and the generic `set_link_map` over the four entity kinds
  plus custom columns by `#label`; one named deviation from upstream: unknown values
  raise instead of vanishing silently (a link targeted at a misspelled name should
  fail loudly).
- [ ] Pages value writer (`set_pages`, `cache.py:2100`): a frontend that computes page
  counts itself cannot record the value or clear the `needs_scan` flag today. Size XS.
- [ ] Extra-files (`data/` dir) verbs (`cache.py:4099-4181`): cquarry ignores the data
  directory entirely, reads included. Size S-M.
- [ ] A blessed cross-library copy primitive (modeled on `copy_to_library.py:77`):
  composable from existing reads + writes except the declined annotations postprocess
  and data/ extras; duplicate/automerge policy stays frontend. Size M.
- [ ] book_storage / plugin-data / conversion-options writers (`cache.py:4053-4082`,
  `:2913`, `backend.py:2955`): reads exist for all three tables, writes none;
  opaque-blob passthrough. Size XS-S.

### Phase 18: Restricted tag browser (the large candidate)

- [ ] **`get_categories` with restriction and per-node book sets** (`cache.py:1897` ->
  `categories.py:312`; upstream's `Tag` carries `id_set` and `search_expression`, plus
  synthesized `search` and `news` categories). The tag-browser-over-a-search-result story
  `get_tag_browser_counts` (whole-library SQL views, counts only) cannot answer. The
  portable subset is builtin + storage-backed custom columns + restriction + id sets;
  composite-column categories stay gated by the §7 template-engine boundary. Ships
  together with Phase 14's `facet_counts` for Carrel. Size M-L.

## Still gated (reopen conditions unchanged by the program)

- [ ] **`merge_books(book, duplicate, policy)`**: waits for the acquisition-importer lane
  to pull it (standing gate). Size M.
- [ ] **`schema_version()`**: requester-less; only on a consumer ask. Size XS.
- [ ] **pypi environment tag-deployment policy**: UI-only on GitHub today (Settings ->
  Environments -> pypi -> Deployment branches and tags); the REST API creates
  branch-type policies only, and any branch policy rejects tag deployments outright
  (the v1.22.0 erratum). Apply if and when GitHub ships REST/UI parity.
- [ ] **Hermitage's Flatpak side** (that repo's gate, recorded here for the trigger map):
  the pip-floor policy decision (`requires-python >=3.14` vs the 3.13-capable compat
  branch) and the forward-only tag the manifest's `hermitage` module pins. cquarry's
  half is verified end to end. This is the program's first Hermitage item.
- [x] **Dependabot's two open major-bump PRs** (actions/checkout 4 -> 7,
  actions/setup-python 5 -> 7): resolved 2026-09-30, both merged and CI green
  (#3 setup-python 5.6.0 -> 7.0.0, #4 checkout 4.4.0 -> 7.0.1).

## Logged hardening candidates (unchanged; trigger-gated, not program phases)

- [ ] **Snapshot-retry hardening** (logged 2026-09-18, Calibre 9.15 watch). Upstream's
  `_backup_database` retries transient `SQLITE_IOERR`/`SQLITE_IOERR_SHORT_READ` up to
  ten times during SQLite backup-API steps; cquarry's `SNAPSHOT_LEASH` snapshot
  fallback and `backup_to()` use the same API in the same concurrent-write scenario
  with no retry, so a transient step failure surfaces as an error rather than a retry.
  Trigger: the snapshot path failing in the field. Size S.
- [ ] **`find_orphan_custom_column_links`** (design-time waiver, Phase 9 expansion).
  Schema-probing integrity predicate for link rows pointing at purged columns; needs
  its own fixture work. Trigger: an integrity consumer asks (the bindery OPF audits
  are the plausible asker). Size S.
- [ ] **`ratings.link`** read (waived at the v1.4.0 entity-columns ship). No consumer
  need yet. Trigger: on demand. Size XS.

## Deferred on consumer demand (unchanged)

- [ ] **`annotations_dirtied` maintenance on annotation writes** (Phase 6 residue). The
  mechanism rides the ecosystem's first annotation writer; none exists, and annotation
  writers are declined (see below), so this moves only if that decline is reversed.
  Size XS.
- [ ] **Write-side banned-labels policy** (Phase 12 residue; the datatype-dispatch half
  shipped in 1.15.0). A cquarry-original API with no upstream anchor. Trigger: a
  consumer brings a concrete label policy (CalibreQuarry's lane). Size S.
- [ ] **`template:` searches over a minimal template subset** (Phase 13 B residue).
  `search("template:...")` raises a clear ParseException naming the engine; the full
  GPM engine is spec §7 permanent out-of-scope, so even a subset needs a recorded
  decision. Trigger: consumer demand. Size M.
- [ ] **Typeahead prefix queries** (Phase 7 residue from the NEW-AUDIT map). Trigger: a
  consumer grows a typeahead surface. Size S.

## Consumer-side debts (re-homed 2026-09-29)

Each consumer roadmap now carries its own queue; this section is the pointer map, not
the queue. cquarry's half of every item is phased above.

- **CalibreQuarry**: its Phase 20 (opened 2026-09-29) carries the orchestration verbs
  (`backup-metadata`, `restore-database`, `clone`, `fts-index`, catalog plugin builds,
  `customize`, the calibre-debug subset, the ebook-device USBMS wrapper) and adopts
  `get_dirtied_formats()` when Phase 14 ships it.
- **Hermitage**: adopts `get_annotations_decoded()` in `codex.py:_annotation_line`
  (`codex.py:201-226`; cquarry side shipped 1.23.0); optionally grows
  `strip_html(keep_paragraphs=True)` (would ride Phase 16 if Codex wants one HTML
  definition in the family); adopts the ordered-VL-names and unpiped-author helpers in
  Phase 16; its Flatpak pip-floor decision gates the program's install story.
- **Carrel-calibre-web**: its roadmap (created 2026-09-29) carries the `preserve_order`
  retirement via `list_books(sort="ids")` (shipped 1.21.0), the two raw custom-column
  reads to `load_custom_column()`, the ORM residuals, and the Phase 16 helper
  adoptions including the Open Library ISBN switch.
- **Stats-metrics tripwire** (Carrel spec §12.3): unchanged; a FOURTH library-metrics
  consumer triggers the headless metrics-layer promotion. Three lanes exist today
  (CalibreQuarry `--analytics`, Hermitage Insights, Carrel `stats.py`).

## Declined (kept per recorded decisions; reversal needs a new recorded decision)

- **Notes system** (`.calnotes/notes.db`): declined 2026-09-05. No real data to verify
  against; a module that cannot be tested against Brandon's library does not get
  built.
- **`embed_metadata` into format files**: stdlib-violating (needs per-format metadata
  writers); a frontend concern that shells to `ebook-meta`/`calibredb` where needed
  (O-lane).
- **Annotation and reading-position writers**: "consumer, not curation" (the REPORT-12
  extraction, recorded 2026-09-15). The deferred `annotations_dirtied` maintenance is
  linked to this decline.
- **Library clone / `restore_database` / dump-and-restore / `export_library`**: native
  forms out of write scope (create-a-database DDL would be a real scope expansion);
  the O-lane covers them through calibredb `clone`/`restore_database` (CalibreQuarry
  Phase 20).
- **OPF sidecar generation ("OPF-dump-now")**: duplicates Calibre's own backup thread;
  `metadata_dirtied` already hands the job to Calibre, and the O-lane
  (`run backup-metadata`) covers the headless form.
- **Composite/GPM template evaluation**: spec §7 permanent non-goal (search, reads,
  and categories all reference the same boundary); recorded in the parity program as
  the standing cost of that boundary.
- **LibraryCache-style memoization in cquarry**: declined at the Phase 9 design
  (contradicts the documented short-lived single-threaded connection design).
- **CalibreQuarry `enum_colors` TUI rendering**: declined 2026-09-06 (no pill/badge
  render surface); reopen only if the TUI grows colored categories.
- **Carrel reading-status toggle**: WAIVED PERMANENTLY 2026-09-03; the fork is
  read-only by construction and `reading_status` is reserved to Brandon alone.
- **Hermitage reading-status write dropdown**: waived with its recorded read-mostly
  posture.
- **Bindery format write-back**: conditional-future waiver, reworded 2026-09-29.
  The original trigger ("if its repair flow ever writes metadata.db rows") fired long
  ago in the sanctioned sense: bindery has updated `data` rows through
  `WritableCalibreDB.set_format` since v0.24.0. What the waiver actually guards is a
  bindery-side book-metadata write-back convenience (pushing repaired-file metadata
  into title/authors/comments rows); that stays declined unless bindery's repair flow
  grows one.
- **App-experience surfaces, no automation pull (declined 2026-09-29, the parity
  split)**: TTS read-aloud (`gui2/tts/`), store plugins (43 shipped), the LLM suite
  (9 providers, `src/calibre/ai/`), spell dictionaries (`src/calibre/spell/`),
  web2disk website mirroring (`web/fetch/simple.py`), LRF legacy tools
  (`ebooks/lrf/`), email/SMTP delivery (`utils/smtp.py`, `gui2/email.py`). These are
  reader-experience features of the GUI, not library automation; no ecosystem lane
  claims them. Reversal is a normal new decision, and the open-unowned rows above
  (news, devices) show the shape a claim would take.

## Records kept from the minimized blocks

- The v1.16.x/v1.17.0 release tags carry blank-line-stripped messages (historical
  `git tag -F` behavior); the subjects are correct and the tags were deliberately left
  alone rather than force-pushed.
- Upstream parity watch: Calibre release audits land here as dated upstream-watch
  entries (workspace automation plus the `calibre-parity-diff` script, seeded at
  v9.15.0). The 9.15 audit found zero parity diff and one hardening candidate, logged
  above.
- The 2026-09-29 restructure draws its upstream evidence from a full-surface taxonomy
  pass over the reference clone (tool set `src/calibre/linux.py:23-54`, conversion
  `ebooks/conversion/`, devices `devices/`, 1,094 recipes `recipes/`, GPM
  `utils/formatter.py`, content server `srv/`, GUI `gui2/`); the per-repo queues in the
  consumer roadmaps carry the same dating.
