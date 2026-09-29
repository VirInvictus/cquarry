# cquarry Roadmap

**Status: CLOSED.** cquarry finished with v1.23.0 ("THE FINAL BLITZ", 2026-09-15; the
closure record and its reopen conditions live in `project.done`). Per the 2026-09-28
review, no further work is scheduled and every recorded decline stands. This file now
does one job: it is the complete inventory of everything left OPEN, each item with the
trigger that would reopen it: the gated items from `project.done`, logged hardening
candidates, on-demand deferrals, the verified 2026-09-28 utility survey, consumer-side
debts, and the declines with their reasons. Nothing here is committed work; a box is
ticked only when its trigger fires, Brandon reopens the lane, and the work ships.

The original roadmap carried the full phase-by-phase build history (Phases 1-13 with
per-item ship notes, upstream-sync checkboxes, cascade notes, and dated audit blocks).
That history was minimized into the ledger below on 2026-09-28; the complete text is
preserved in git history (the tree of commit 2593086, the last commit before this
minimization) and the release-by-release record is `patchnotes.md`.

Standing rules for any future lane, unchanged by the closure:

- **Cross-Repo Implementation Rule.** A cquarry feature is done only when every affected
  consumer repo is synced or explicitly waived: CalibreQuarry, bindery-cli, Hermitage,
  Carrel-calibre-web.
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
| Upstream watch | Calibre 9.15 audit: zero schema/search/annotations diff; one hardening candidate logged (see Open work) | 2026-09-18 | no release |

## Open work

Nothing below is scheduled. Each item names the trigger that would reopen it.

### Gated items (recorded in `project.done`)

- [ ] **`merge_books(book, duplicate, policy)`**: waits for the acquisition-importer lane
  to pull it (standing gate). Size M.
- [ ] **`facet_counts`**: browse facets over a search result; waits for Carrel's lane.
  Would ship together with the restricted tag browser candidate below if that lane asks.
  Size M.
- [ ] **The Carrel residue trio**: `get_book_by_uuid` (the Calibre-Companion endpoint
  dependency), the entity-to-ids resolver, and a bulk formats map. Waits for Carrel's
  lane; interim compositions exist (`uuid:` search, `get_entities` + `get_all_series`,
  per-book `get_formats`). (The originating box's other items are closed: the ids-order
  mode shipped 1.21.0 as `sort="ids"`, and the cc-adapter `.extra` gap closed with
  1.18.0's `#label_index`.) Size S each.
- [ ] **`schema_version()`**: requester-less; only on a consumer ask. Size XS.
- [ ] **pypi environment tag-deployment policy**: UI-only on GitHub today (Settings ->
  Environments -> pypi -> Deployment branches and tags); the REST API creates
  branch-type policies only, and any branch policy rejects tag deployments outright
  (the v1.22.0 erratum). Apply if and when GitHub ships REST/UI parity.
- [ ] **Hermitage's Flatpak side** (that repo's gate, recorded here for the trigger map):
  the pip-floor policy decision (`requires-python >=3.14` vs the 3.13-capable compat
  branch) and the forward-only tag the manifest's `hermitage` module pins. cquarry's
  half is verified end to end.
- [ ] **Dependabot's two open major-bump PRs** (actions/checkout 4 -> 7,
  actions/setup-python 5 -> 7): Brandon's merge call; the current pins are correct and
  green either way.

### Logged hardening candidates

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

### Deferred on consumer demand

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

### 2026-09-28 utility survey (verified; logged, not scheduled)

Four research agents compared this repo against the upstream Calibre clone
(v9.15.0+103; read, write/maintenance, and CLI surfaces) and swept the four consumers
for adoption debts; an independent verifier re-checked all 22 load-bearing claims
against the sources (22 confirmed, 0 refuted). Verdict: cquarry is not at full
utility, but it is deliberately closed. Roughly three-quarters of Calibre's read
surface is mirrored and the per-book write-setter surface is complete; what remains is
a thin conveniences layer over data already fetched, a maintenance/lifecycle ring
around the write module, and machinery that is genuinely process-bound. Per Brandon's
2026-09-28 decision the project stays closed and this menu is logged so the triggers
are explicit. Upstream evidence cites the reference clone's `src/calibre/...` lines.

#### Completion wave (finishes contracts cquarry itself owns)

- [ ] **Restore-from-trash verbs.** cquarry's own `remove_book(delete_files="trash")`
  is write-only today: `.caltrash` can be listed and emptied, but nothing can be
  un-trashed. Upstream: `copy_format_from_trash`/`move_format_from_trash` (re-register
  the `data` row plus file move), `copy_book_from_trash`/`move_book_from_trash`
  (recreate rows from the trashed OPF), `delete_trash_entry`
  (`cache.py:3503-3568`). Trigger: any field incident where a trash deletion was a
  mistake. Size S for the format half; the book half needs sidecar-OPF parsing
  (adjacent to the declined clone/restore family).
- [ ] **`get_dirtied_formats()` read** beside `get_dirtied_books()` and
  `get_annotations_dirtied_books()`. CalibreQuarry's FTS staleness panel reads the
  sidecar with raw SQL today (`modes/fts.py:91`) and its own guidance records the
  promotion wish. Size XS. Trigger: the next CalibreQuarry lane touching `--fts`.
- [ ] **`set_custom_column_metadata`**: modify an existing column's name/editable/
  display JSON, notably `enum_values` (upstream `backend.py:1407`, `cache.py:3233`).
  cquarry can create and delete columns and validates enum writes against
  `display.enum_values`, but nothing can populate that list without Calibre open.
  Size S. Trigger: any consumer curating enum columns without Calibre.
- [ ] The Carrel residue trio (above) also belongs to this wave by size; it stays
  gated on Carrel's lane.

#### Maintenance ring (the lifecycle layer around the write module)

- [ ] **`vacuum` / `analyze` / `integrity_check` verb.** Absent from the library
  entirely; upstream `backend.py:1638` vacuums the main DB and the attached FTS
  sidecar (the notes-DB portion stays out of scope). Size XS-S. Trigger: any field
  corruption scare or a consumer maintenance lane.
- [ ] **check_library extra-side disk checks** for the integrity family: extra format
  files, extra covers, malformed paths, extra files in book dirs, failed folders
  (upstream `src/calibre/library/check_library.py:50`). cquarry ships the missing-side
  checks only. Size M. Trigger: CalibreQuarry's `--health` lane.
- [ ] **FTS queue management verbs** (`fts_unindex`, per-book reindex, full reset):
  pure sidecar SQL against `books_text`/`dirtied_formats` (upstream
  `cache.py:603-698`, `fts/connect.py:75-92`); extraction itself stays Calibre's.
  Size S. Trigger: a consumer needing to force re-extraction without Calibre.
- [ ] **A typed `set_preference` writer.** Saved-search add/delete/rename
  (`cache.py:3379-3399`), virtual libraries, user categories, grouped search terms,
  and the FTS enable flag (`cache.py:588` + `fts/connect.py:60`) are all plain
  preference rows; cquarry reads all of them and writes none. One schema-faithful JSON
  upsert unlocks the set. NOTE: writing GUI-adjacent state is a posture shift for a
  library that currently only reads it; this one needs a recorded decision, not just
  demand. Size S.

#### Read-surface wave (conveniences over data already fetched)

- [ ] **Cover bytes and freshness**: `Cache.cover()` / `cover_last_modified()`
  (`cache.py:1426`, `:1481`). cquarry stops at path + dimensions; web frontends need
  bytes and conditional-GET mtimes. Size XS.
- [ ] **Inverse virtual-library map**: `virtual_libraries_for_books`
  (`cache.py:3600`), "which wings is this book in"; computable from cquarry's own
  search engine. Size S.
- [ ] **Inverse user-category map**: `user_categories_for_books` (`cache.py:3645`),
  the `@Name` twin of the above. Size S.
- [ ] **`format_hash` / `format_metadata`** (`cache.py:1255`, `:1268`): SHA-256 plus
  on-disk mtime, the "has the file changed under Calibre" detector the FTS sidecar's
  own hash columns compare against. Size XS.
- [ ] **`books_by_year` / `books_by_month` over any date field** (`cache.py:1057`,
  `:1080`): `analytics.addition_timeline` buckets the fixed `timestamp` field into
  counts only; no id sets, no pubdate mode. Size S.
- [ ] **`get_next_series_num_for`** (+ custom-column variant, `cache.py:2567`,
  `legacy.py:873`): the preference-aware next series number; useful to every add-book
  flow including cquarry's own. Size XS-S.
- [ ] **Annotation conveniences**: filter/limit/user/type variants over
  `get_annotations`, removed-skeleton handling, style discovery
  (`cache.py:3896-3924`); mostly Python-side derivations over data cquarry already
  reads. Size S. Trigger: an annotations-heavy consumer.
- [ ] **`read_backup`** (`cache.py:2212`): read the stored sidecar `metadata.opf`, to
  diff Calibre's last write against the rows. Distinct from the declined OPF
  generation: this reads what Calibre wrote, it does not generate. Size XS.
- [ ] Smaller: `size_stats` (`cache.py:1753`), a per-book all-fields link map
  (`cache.py:3130`), `is_fts_enabled` (`cache.py:551`), last-read-position filters
  (`cache.py:3751`). Size XS each.

#### Write-side extras (logged for completeness; none bundled)

- [ ] **Author `sort`/`link` writers** (`cache.py:3050`, `:3073`, and the generic
  `set_link_map`, `cache.py:3176`): cquarry reads `author_links` and recomputes author
  sort inside its own setters, but cannot edit a link column. Size S.
- [ ] **Pages value writer** (`set_pages`, `cache.py:2100`): cquarry queues
  `needs_scan` and reads provenance; a frontend that computes page counts itself
  cannot record the value or clear the flag. Size XS.
- [ ] **Extra-files (`data/` dir) verbs** (`cache.py:4099-4181`): cquarry ignores the
  data directory entirely, reads included. Size S-M.
- [ ] **A blessed cross-library copy primitive** (modeled on `copy_to_library.py:77`):
  composable from existing reads + writes except the annotations postprocess
  (declined) and data/ extras; duplicate/automerge policy stays frontend. Size M.
- [ ] **book_storage / plugin-data / conversion-options writers** (`cache.py:4053-4082`,
  `:2913`, `backend.py:2955`): reads exist for all three tables, writes none;
  opaque-blob passthrough. Size XS-S.

#### Restricted tag browser (the large candidate)

- [ ] **`get_categories` with restriction and per-node book sets** (`cache.py:1897` ->
  `categories.py:312`; upstream's `Tag` carries `id_set` and `search_expression`, plus
  synthesized `search` and `news` categories). This is the tag-browser-over-a-search-
  result story `get_tag_browser_counts` (whole-library SQL views, counts only) cannot
  answer. The portable subset is builtin + storage-backed custom columns + restriction
  + id sets; composite-column categories stay gated by the §7 template-engine
  boundary. Adjacent to the gated `facet_counts`; the two would ship together if
  Carrel's lane asks. Size M-L.

#### Confirmed not portable (recorded so nobody re-derives them as gaps)

FTS5 `MATCH` search (Calibre's custom tokenizer); the notes system
(`.calnotes/notes.db`); the `meta` view and `tag_browser_filtered_*` views
(`books_list_filter()` is process-bound); the `marked`/`ondevice`/`in_tag_browser`
GUI-state fields; the GPM template engine; ICU collation (the documented
`unicodedata` deviation); Calibre's format parsers and the FTS/page-count extraction
workers; thread-pool tuning verbs (`set_fts_speed` and siblings).

### Consumer-side debts (those repos' lanes; logged 2026-09-28, none scheduled)

- **Carrel-calibre-web**: retire the `preserve_order` re-sort shim via
  `list_books(sort="ids")` (`cps/quarry_grid.py:604,628-630`; live callers
  `web.py:532,568`; cquarry's `API.md` names the mode as the shim's retirement,
  shipped 1.21.0); swap `reading_shelf.py:30-58` and `stats.py:119-144` to
  `load_custom_column()` (retires the last two fork-owned ORM reads of metadata.db
  and the patchnotes claim those reads contradict).
- **Hermitage**: adopt `get_annotations_decoded()` in `codex.py:_annotation_line`
  (`codex.py:201-226`; shipping since 1.23.0); optionally grow
  `strip_html(keep_paragraphs=True)` if Codex wants one HTML definition in the family.
- **CalibreQuarry**: the raw `dirtied_formats` read in `modes/fts.py:91` pairs with
  the `get_dirtied_formats()` candidate above; the integrity aggregate runner stays
  with its `--health` lane.
- **Ecosystem helper promotions** (each XS, each with waiting consumers, each a
  public-API commit with docs and tests): an ordered-VL-names helper (near-identical
  copies at Carrel `cps/wings.py:33-47` and Hermitage `hermitage/app.py:1605-1616`);
  an unpiped-author display helper (eight `replace("|", ",")` copies across both
  repos); a tag membership rollup returning id sets (Carrel `cps/categories.py:29-47`;
  `tag_rollup` returns counts, Carrel needs the sets).
- **Identifier-link drift** (promotion needs a decision first): Hermitage resolves
  ISBN to Open Library, Carrel to WorldCat, with different label styles
  (`codex.py:124-133` vs `quarry_grid.py:576-595`). Any cquarry identifier-link
  helper needs Brandon's canonical-URL call first.
- **Stats-metrics tripwire** (Carrel `spec.md:573-578`): if a FOURTH library-metrics
  consumer appears, extracting a headless metrics layer becomes the right call. Three
  lanes exist today (CalibreQuarry `--analytics`, Hermitage Insights, Carrel
  `stats.py`). Not currently triggered; the promotion payload would be pubdate
  decades, hour/weekday histograms, the acquisition timeline, and the rating
  histogram.

## Declined (kept per the 2026-09-28 decision; reversal needs a new recorded decision)

- **Notes system** (`.calnotes/notes.db`): declined 2026-09-05. No real data to verify
  against; a module that cannot be tested against Brandon's library does not get
  built.
- **`embed_metadata` into format files**: stdlib-violating (needs per-format metadata
  writers); a frontend concern that would shell to `calibredb` if ever needed.
- **Annotation and reading-position writers**: "consumer, not curation" (the REPORT-12
  extraction, recorded 2026-09-15). The deferred `annotations_dirtied` maintenance is
  linked to this decline.
- **Library clone / `restore_database` / dump-and-restore / `export_library`**: out of
  write scope; the family would require cquarry to gain create-a-database DDL, a real
  scope expansion.
- **OPF sidecar generation ("OPF-dump-now")**: duplicates Calibre's own backup thread;
  `metadata_dirtied` already hands the job to Calibre, which regenerates sidecars at
  its next start.
- **Composite/GPM template evaluation**: spec §7 permanent non-goal (search, reads,
  and categories all reference the same boundary).
- **LibraryCache-style memoization in cquarry**: declined at the Phase 9 design
  (contradicts the documented short-lived single-threaded connection design).
- **CalibreQuarry `enum_colors` TUI rendering**: declined 2026-09-06 (no pill/badge
  render surface); reopen only if the TUI grows colored categories.
- **Carrel reading-status toggle**: WAIVED PERMANENTLY 2026-09-03; the fork is
  read-only by construction and `reading_status` is reserved to Brandon alone.
- **Hermitage reading-status write dropdown**: waived with its recorded read-mostly
  posture.
- **Bindery format write-back**: conditional-future waiver; revisit only if its repair
  flow ever writes metadata.db rows.

## Records kept from the minimized blocks

- The v1.16.x/v1.17.0 release tags carry blank-line-stripped messages (historical
  `git tag -F` behavior); the subjects are correct and the tags were deliberately left
  alone rather than force-pushed.
- Upstream parity watch: Calibre release audits land here as dated upstream-watch
  entries (workspace automation plus the `calibre-parity-diff` script, seeded at
  v9.15.0). The 9.15 audit found zero parity diff and one hardening candidate, logged
  above.
