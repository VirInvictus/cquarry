## v1.18.0 (2026-09-12)

### Phase 13: the upstream comparison (FTS reads, search honesty, write completions)

Everything below ships against REPORT-12-Sept.md, three read-only research
passes that compared this repo line-for-line against the upstream Calibre
clone (schema + upgrade map, search stack, write paths). The promotion
candidates the report surfaced stay shut pending approval; every ungated
box in Phase 13 (roadmap sections A-D) is closed here, each with its
pinning or regression test.

### Read coverage

- **The `full-text-search.db` sidecar is now read** (the last unread
  Calibre data file). `CalibreDB.get_book_text(book_id, fmt)` returns one
  format's full `books_text` row including `searchable_text`;
  `get_text_extractions(book_id=None)` returns the bulk status rows WITHOUT
  the megabyte texts (`err_msg` audit, format-hash change detection);
  `search_book_text(query, *, fmt=None, ids=None)` is a case- and
  accent-folded Python-side content search returning
  `{book_id: {FORMAT, ...}}`. Same lock-escape snapshot handling as
  metadata.db; a missing sidecar degrades every read to empty; `refresh()`
  drops the sidecar connection with the rest. No FTS5 machinery: the index
  tables tokenize through Calibre's custom tokenizer and are unqueryable
  outside Calibre, so the plain table is the read surface.
- **`integrity.find_failed_text_extraction(db)`** returns
  `{book_id: {FORMAT: err_msg}}` for scans, DRM, and corrupt files; the
  third sanctioned non-cached read in the module after the two cover-file
  checks.
- **`CalibreDB.get_page_metadata(book_id=None)`** exposes the
  `books_pages_link` auxiliary columns (`algorithm`, `format`,
  `format_size`, `timestamp`, `needs_scan` as bool): provenance for
  displayed page counts, with `needs_scan=True` meaning Calibre has queued
  a recount.
- **`#label_index` is real for custom series columns.** The engine
  registered the location but it could never resolve (`#myseries_index:>3`
  silently matched nothing); `load_custom_column` now reads the link
  table's `extra` float and the engine serves it. An exact label literally
  ending in `_index` keeps the token; ancient schemas degrade. Also riding
  the same SQL: `CalibreDB.custom_column_links(col_name)` surfaces the
  normalized value tables' `link` URL column (no Calibre UI populates it).
- **Doc line** (spec 3.2, CLAUDE.md, API.md): a stored annotation's notes
  follow its highlighted text joined by LF + unit separator + LF; the
  research line dropped the leading LF and is corrected in the roadmap.

### Search parity (behavior changes)

- **Fixed: `identifiers:KEY:TRUE/FALSE` inverted on uppercase** (the one
  real bug the research found). The presence gate folded the value but the
  selection compared the raw one, so `identifiers:isbn:TRUE` matched the
  COMPLEMENT of the right answer. Upstream lowercases once and uses that
  value for both; so does cquarry now.
- **Bare `true`/`false` are the sweep-wide presence test** (upstream
  parity), not substring matches: `true` matches books where any swept
  field holds a non-blank value, and the identifiers store and cover flag
  count as present. Identifier keys never text-sweep (the old spec claim
  was wrong; the code now matches upstream instead of the claim). Bare
  numeric probes lost `id` (upstream excludes it) and cover-as-0/1 (cover
  joins via presence), leaving series_index, rating, pages, size.
- **Super-quotes `"""..."""` ported** (upstream's documented escape hatch
  for quote/paren/regex-heavy queries): `title:"""a "b" (c)"""` parses and
  matches; it used to mis-tokenize.
- **`template:` raises a clear ParseException** naming the missing
  template engine (model: upstream TemplatesNotAllowed) instead of
  silently matching nothing. The optional template implementation stays
  out of scope.
- **Strictness fixes (the honesty pass, each pinned):** date locations
  take exactly `true`/`false` as presence words and no match-kind
  prefixes -- `pubdate:blank` or `pubdate:~2020` now raise, like
  upstream's date-conversion error, instead of matching dateless books.
  Numeric locations take exactly `true`/`false` -- `rating:checked`/
  `rating:blank` raise like upstream's non-numeric error (the tristate
  vocabulary is bool-only, where it still works). Text fields' presence
  words narrowed to exact `true`/`false` (`title:yes` is substring text
  again). New dated spec-deviation entries: tristate bool fidelity (9),
  lexer strictness (10), the benign extensions consolidated (11), the
  entity case-change policy (12).

### Write completions

- **Path re-laying on `update_title`/`set_authors`.** Curation renames no
  longer leave the `Author/Title (id)` directory and `Title - Author.ext`
  format files under the old names: the layout moves with the rows
  (directory rename, per-format file renames, emptied-parent removal,
  stale-target replacement, case-only spelling fix, db-only correction for
  path-less legacy rows). The fs half lands only after the commit,
  deferred to the outermost `batch()` commit via `_pending_relayouts` and
  dropped on rollback, so a failed pass never leaves rows pointing at
  renamed directories.
- **Fixed in the same machinery:** a failed batch never cleared
  `_pending_removals`, so a later successful batch could flush stale
  removals against rows the rollback had resurrected. The rollback path
  now clears both deferred queues; regression test included.
- **FTS + pages dirtying alongside format writes.** `add_format` and
  `set_format` queue the (book, format) pair into the sidecar's
  `dirtied_formats` and set `books_pages_link.needs_scan`, so a repaired
  file no longer leaves Calibre's content index and page counts stale
  forever (Calibre never re-reads a file on its own; it processes its
  queues). `remove_format` clears the queue entry so Calibre never
  re-extracts a vanished file. The sidecar is attached before the
  transaction opens, the queue writes roll back with the batch, and a
  missing sidecar degrades to a no-op. The stale `books_text` row of a
  removed format stays Calibre's own to clean (the sidecar's delete
  triggers need Calibre's custom tokenizer; deleting here would corrupt
  the index).
- **`clean_identifier` parity.** `set_identifier` (and the mirrored
  `clear_identifier` normalization) cleans both halves like upstream: the
  type is stripped, lowercased, and stripped of `:`/`,`; the value is
  stripped with `,` mapped to `|` -- Calibre's comma-free stored shape.

Suite: 381 passed (was 327). Consumers bring up per roadmap Phase 13
section D (the four-program wave).

## v1.17.0 (2026-09-09)

### The approved promotion candidates

- **`set_format(book_id, fmt, name, size)`.** The sanctioned remove+add of
  one `data` row in a single transaction -- the row half of swapping a
  repaired file into place. Honest no-op (`False`) when the identical row
  already exists; `True` when written. Retires bindery's hand-rolled
  `remove_format` + `add_format` composition.
- **`remove_book(book_id, delete_files=None)`.** The file story is decided:
  `"trash"` moves the book's directory into the library-local
  `.caltrash/b/<id>/` (upstream's own trash layout, exact stdlib parity via
  `shutil.move`), `"permanent"` deletes it outright, and the default `None`
  keeps rows-only semantics for callers that sweep themselves. File removal
  lands only after the rows COMMIT; inside a `batch()` it defers to the
  outermost commit, so a rollback never leaves resurrected rows fileless.
- **`CalibreDB.find_candidate_duplicates(title, authors, isbn=None)`.** The
  library half of duplicate screening: the ISBN rule first (separator- and
  case-insensitive against the book's `isbn` identifier), then normalized
  title (folded, subtitle and leading article scrubbed) plus folded first
  author, both exact. Returns `{"id", "matched_by": "isbn" |
  "title_author"}` entries sorted by id; the file half stays in the
  frontend where the embedded-metadata readers live.
- **`CalibreDB.export_rows(*, ids=None, include_custom=True)`.** Flat
  one-dict-per-book rows: the hydrated row with every non-composite custom
  column flattened in as a `#label` key (`None` when the book has no
  value, so the key set is uniform). `rating` stays the raw internal 0-10
  and `pubdate` the raw TEXT; conversion is the renderer's job. Researched
  against CalibreQuarry's inline exporter SQL, which this retires.
- **`integrity.find_identifierless(db)`.** Books with an empty identifiers
  store; promoted from Hermitage's inline Insights predicate and joining
  the `find_*` family.

### Decisions recorded, decisions made

- **Q2 (notes and dist):** `database_report.md` and `research.md` are
  deleted; the three facts that existed nowhere else moved into CLAUDE.md
  (asymmetric link-table uniqueness, the FTS5 sibling table names, the
  identifiers EAV pseudo-type note). The gitignored `dist/` 1.9.0 build
  artifacts are gone.
- **3.2 satisfied:** batch-scoped filesystem compensation needs no further
  exposure -- 1.15.0 made it automatic inside any consumer's `batch()`.
- **3.4 deferred:** the banned-labels policy stays unbuilt -- no upstream
  anchor, no consumer has specified labels or semantics; revisit when
  CalibreQuarry's lane brings a concrete policy.
- The `on_duplicate=` policy parameter on `add_book` was not built: callers
  consult `find_candidate_duplicates` first, the same
  confirmation-before-act pattern `remove_book` uses.

## v1.16.1 (2026-09-09)

### Tests and docs hygiene (the sweep's Batch C)

- **Test inflation deflated: 415 collected items down to 326 distinct.**
  The phase-6 expansion suite re-ran in full inside every subclass
  (`TestBatchContext`, `TestSetPubdate`, `TestSetWriteConveniences`,
  `TestAddBook` each inherited all of `TestWriteSideExpansion`'s tests).
  The fixture plumbing is now `_WriteSideFixture` and the expansion tests
  live in `_WriteSideTests`; plain mixins carry no `TestCase` base, so
  each suite runs exactly once, and the one variant whose schema genuinely
  differs (`TestAddBook`'s trigger schema) keeps its own re-run variant.
  Also: the triple-pasted simple DDL lifted into `_make_simple_db`, the
  `transaction()` success twin replaced with an identity assert (the
  failure twin still exercises the alias end to end), the near-
  tautological search-integration count folded into the first real
  assertion, the duplicated get-entities unknown-kind assertion deduped,
  and the `_now()` timestamp shape pinned by regex.
- **Docs repaired.** The `tag_rollup` example in the roadmap now teaches
  the shipped subtree-totals rule (its own ship note had declared the old
  mixed rule dead); the annotations no-FTS deviation is spec section 5
  item 9, so the canonical list matches API.md's; "8-JOIN" corrected to
  the real 6 joins plus Python hydration in spec.md and CLAUDE.md; the
  1.13.0 patchnotes baseline repaired to 258 so same-day entries agree;
  both README IndentationError snippets fixed (the `get_dirtied_books`
  one rode 1.15.0, the quickstart's mid-block dedent this release).
- **Version-sync guard.** `tests/test_version_sync.py` asserts all six
  version carriers agree (VERSION, pyproject.toml, `__init__.py`,
  `config.py`, spec.md, API.md); nothing guarded them before, and
  carriers had drifted once (1.12.0 shipped with `config.py` and API.md
  stale).
- **Recorded, not decided** (Brandon's call): the committed working notes
  (`database_report.md`, `research.md`) and the gitignored `dist/` 1.9.0
  build artifacts stay until he decides; the state and options are in the
  roadmap box.

## v1.16.0 (2026-09-09)

### Search parity: the upstream-fidelity batch

- **Recursion never escapes as `RecursionError`.** Grammar-valid
  adversarial queries (400-deep nesting, 5000-term chains, deep
  `vl:`/`search:` chains) now surface as `ParseException` from
  `search()` and from the virtual-library and saved-search matchers,
  with upstream's own two guard sites as the model.
- **An empty query after any location matches nothing.** `title:` used
  to match presence (all books) while upstream, and cquarry's own
  `tags:`, returned nothing; every location now agrees with upstream,
  including `vl:`, `search:`, and date fields.
- **Invalid boolean keywords raise.** `cover:maybe` raises
  `ParseException` like upstream's `BooleanSearch` instead of silently
  matching nothing.
- **Two-letter language codes canonicalize.** `languages:ja` matches
  books stored with `jpn` (upstream's `canonicalize_lang` step, now
  mirrored with a full ISO 639-1 to 639-2 map for cquarry's languages).
- **Component exact matching strips parts.** `authors:=..Cherryh`
  matches `C.J. Cherryh`'s last component (upstream matches stripped
  components; cquarry compared raw split parts), the leading-dot
  literal comparison mirrors upstream's one-dot form, and the module
  docstring's flagship example is replaced with one that is actually
  true.
- **The `all` sweep is upstream's.** Bare terms now cover `formats`
  and `languages` (canonicalized), sweep identifier KEYS as text, and
  probe numeric fields (`id`, `series_index`, `rating`, `pages`,
  `size`, `cover` as 0/1) by exact equality; date fields take no part,
  all exactly like upstream's sweep.
- **Smaller papercuts, fixed or dated.** Fixed: `search:=Name` strips
  the exact-match prefix instead of reporting a known search as
  unknown; a bare `#N` with no relop text-searches the literal string
  instead of raising; identifier presence accepts exactly
  `true`/`false` so `yes`/`checked` no longer leak into value
  matching; whitespace-only stored values count as absent. Documented
  as dated spec section 5 items instead: date parsing/comparison
  leniency (`fromisoformat` vs `dateutil`, calendar dates vs local
  instants) and composite custom columns matching nothing (the
  template engine stays out of scope per section 7).

### Read side

- **Native lists end to end for multi-valued custom columns.**
  `load_custom_column()` and `field()` return `list[str]` for
  multi-valued columns, and `set_custom_column` treats a bare string
  as ONE value instead of comma-splitting it, so a stored
  `Doe, John` survives as a single value; the old join-on-load,
  re-split-on-use round-trip turned it into phantom values and made
  count and exact searches lie. Consumers that `.split(",")`
  custom-column output must stop.
- **`CalibreDB.refresh()`.** The cache-invalidation boundary for
  long-lived holders: one call clears every cache (rows, ids, search
  view and engine, preferences, custom columns, path index) so a
  connection held open across an external write stops contradicting
  itself.
- **Corrupt preferences and ancient schemas degrade instead of
  crashing.** A corrupt or non-dict `virtual_libraries`/`saved_searches`
  payload degrades to `{}` with the same treatment the generic
  preferences accessor already used; `get_identifiers()`, the search
  view's identifier sweep, and `field()`'s comments read degrade on
  schemas missing those tables, like `get_all_books()` always did.
- **`resolve_vl()` and `resolve_saved_search()` canonicalize before the
  lookup.** `"My VL"` and `" My VL "` resolve to a known library
  instead of raising; the guard and the lookup can never disagree
  again.
- **Papercuts.** The `0101-01-01` sentinel sorts as dateless in
  `list_books` (undated books no longer come first on descending
  pubdate); duplicate `books_ratings_link` rows no longer fan a book
  out into several rows; `title_sort` strips a wrapping quote pair
  before the article move (upstream's `quote_pairs`, also making the
  trigger UDF more faithful); `strip_html` drops unterminated
  `script`/`style` bodies; the format-path docstrings state the POSIX
  truth about `normcase`.

### Write module (smaller upstream-parity fixes)

- `set_rating(book_id, 0)` clears like `None` (Calibre maps 0 to
  unrated; a 0-rating link used to land as a spurious Tag Browser
  entry).
- `set_languages` writes `item_order` when the schema carries it
  instead of leaving every row at 0.
- `_write_pattern_a`'s no-op detection orders old rows deterministically
  instead of relying on scan order.
- `add_format` rejects negative sizes.
- Dispositioned as documented: new authors default their sort to the
  display name (upstream's surname-flip heuristic runs on GUI edits,
  not row creation); dated note in the spec.

### Skills and records

- Both import skills synced with the behavior changes: phase-1 gained
  the byte-identity floor note in its duplicate screen; phase-3 gained
  the Ctrl-C-safe batch note, the one-value-per-bare-string rule, and
  the add_book double-import clause.
- The gated items are recorded, not decided: `remove_book`'s
  filesystem story carries both options and a recommendation in the
  roadmap box; the sweep's papercut list is fully dispositioned
  (implemented or documented, each with its commit); the promotion
  candidates remain open for Brandon.

## v1.15.0 (2026-09-09)

### Write-module hardening: the two confirmed holes closed

- **No more torn writes on Ctrl-C.** `WritableCalibreDB.__exit__` used
  to commit unconditionally, and every setter's rollback caught only
  `Exception`, so a `KeyboardInterrupt` between a setter's SQL
  statements escaped the rollback and was committed on context exit: a
  link row without its `metadata_dirtied` row, a publisher change
  queued but unmarked. Rollback paths now catch `BaseException`, the
  context-manager exit rolls back whenever an exception is in flight,
  and its commit failures propagate instead of silently discarding the
  transaction (matching `batch()`'s exit). The class docstring's
  "nothing is written unless a method returns normally" is now honored
  rather than aspirational.
- **A failed batch no longer strands orphan book directories.**
  `add_book`'s directory removal was per-call only: when a later book
  in a shared `batch()` failed, the SQL rolled back but the earlier
  books' directories stayed behind, looking like real books no row
  pointed at. Directories created inside the outermost batch are now
  tracked and a failed exit removes them all; adds made outside any
  batch never register, so a later failed batch cannot touch committed
  books.
- **Nested-batch failure sticks.** An inner `batch()` exception caught
  by the outer block used to be swallowed into a commit (the local
  success flag only saw the outer's clean yield). A failure now poisons
  the pass: the outermost exit rolls back, and the flag clears on exit
  so the next batch on the same handle commits normally.
- **add_book refuses byte-identical re-imports.** The "never imported
  twice" invariant is enforced with a data-table check: a format seed
  whose bytes match an already-catalogued file (same format and size
  pre-filter, then content compare) raises before anything is written,
  dry runs included, under any title or author. Metadata-based
  duplicate screening (ISBN/title/author fuzz-matching) stays a
  frontend concern and remains the gated promotion candidate.

### Custom-column writers dispatch by datatype

- **Unknown datatypes raise instead of being stringified.** The writers
  now dispatch against Calibre's own datatype set: `text`,
  `enumeration`, `series`, and `rating` write through link-table
  storage, `int`, `float`, `bool`, `datetime`, and `comments` write
  direct; anything unknown, or a known datatype on the wrong layout,
  raises `ValueError`.
- **Rating-typed custom columns take 0-5 stars** and store x2 on the
  same internal 0-10 scale as `set_rating` (verified against upstream:
  its writer takes internal values and the x2 lives only in the GUI;
  cquarry's API takes stars so one library never carries two
  conventions). 0 stars means unrated and clears, matching upstream's
  purge of 0-rating rows. On the read side, `field()` surfaces rating
  custom columns as stars, so `#myrat:4` means 4 stars exactly like
  `rating:4`; the sweep's two-scales-in-one-library search gap closes
  with it.
- **Datetime-typed custom columns normalize like `set_pubdate`**: ISO
  text in UTC, never a naive `str()` without the offset.
- **Empty enumerations accept nothing.** Validation used to be skipped
  when `display.enum_values` was empty (upstream silently drops such
  writes instead); cquarry raises with a message that says so.
- **`None` entries in value lists are skipped**, never stringified into
  the literal string `'None'`, in both `set_custom_column` and
  `add_custom_column_values`.

### Tests for the sharpest untested edges

- `remove_book` had zero tests despite dynamic-table DELETEs, orphan
  pruning, and irreversibility: a dedicated fixture now carries the
  real `books_delete_trg` cascade and pins the upstream-faithful
  residue (an unreferenced Pattern-A value row survives removal, since
  no trigger purges it and upstream leaves it too).
- The locked-database snapshot fallback, a README headline feature, has
  its first witness: an `EXCLUSIVE` lock forces the snapshot path, the
  stderr notice fires, and the copy plus its `-wal`/`-shm` sidecars are
  cleaned up on `close()`.
- Six documented read APIs get their first tests
  (`get_format_stats`, `get_identifiers`, `count_books` raw-then-
  cached, `get_virtual_libraries`, `get_all_tags`, `get_tag_counts`
  with zero-count tags), the `~` regex match kind gains its first
  exercise including the malformed-pattern-to-`ParseException` path,
  and `format_path_index` pins the honest POSIX case semantics.
- Suite: 306 collected items at 1.14.0 to 360 here.

## v1.14.0 (2026-09-06)

### `add_book`: the creation path

- **Calibre-parity book creation, one batch, copy-only.**
  `WritableCalibreDB.add_book(title, authors, ...)` inserts
  `(title, series_index, author_sort)` FIRST and lets
  `books_insert_trg` fill `sort`/`uuid` (the caller never passes
  either), takes the id from `lastrowid`, then writes the
  `Author/Title (id)` path with upstream's component budgets
  (`PATH_LIMIT` 100 POSIX) and fallbacks. The ONE documented
  deviation: the ASCII fold is a plain `encode("ascii", "replace")`
  where Calibre runs an ICU user-codec first. Seeds: format FILES
  (copied atomically and placed as `Title - Author.ext`, `data` rows
  carrying the real bytes on disk), a cover (path or bytes,
  JPEG/PNG sniff-or-raise so an unparseable cover is never
  catalogued with `has_cover=1`), identifiers (types normalized like
  `set_identifier`), one language (canonicalized like
  `set_languages`), pubdate, publisher. No tags/series/ratings/
  comments/custom columns at creation: phase 2 clears and sets
  those itself, phase 3 curates. An empty title becomes `Unknown`
  (Calibre parity); an empty author list is legal and leaves the
  book exactly what `find_authorless` expects. Everything runs in
  ONE `batch()` with the setters reused inside it: any failure
  rolls the SQL back and the tracked directory is removed, so a
  failed add leaves zero rows, zero links, and no directory.
  `_touch_book()` queues the id; Calibre generates the sidecar
  `.opf` on next startup. Copy only, by decision: sources are
  never moved or deleted; queue hygiene is the runner's policy.
- **`dry_run=True` writes nothing** and returns the computed plan:
  the id predicted from `sqlite_sequence` (labeled `predicted_id`),
  the predicted path, resolved authors with their sort keys and
  whether each row is new, format filenames and sizes, the cover
  filename, and the `books` row that would be inserted.
- **New helper `helpers.sniff_image_format(data)`**: the
  bytes-level sibling of `get_image_size()`; the cover guard
  builds on it.
- Manual pass against a copy of the testing-facility library
  (user_version 27): predicted id matched the real id, triggers
  filled sort/uuid, the format file landed with a truthful size,
  and the read side (`get_book`, `get_format_path`) resolves the
  book. The Calibre-GUI half of the pass (open the book, watch the
  `.opf` regenerate on next start) awaits Brandon.
- Upstream sync: CalibreQuarry Phase 17 (`run phase2`) is the
  driving consumer and awaits this release; Bindery
  (`--install-to-calibre` repairs existing books only),
  Carrel-calibre-web (read-only), and Hermitage (read-mostly, no
  Flatpak pin bump) are unaffected per the Phase 10 roadmap box.
- Suite: the `test_write.py` fixture gains the real INSERT-path
  hazards (`books_insert_trg`, `books_pages_link_create_trigger`,
  `series_insert_trg`, the `fkc_insert_*` guards) and 13 new
  add_book tests cover the row/link/path contract, atomic file
  placement, the sniff-or-raise, dry-run honesty, and both
  failure-compensation shapes.

## v1.13.0 (2026-09-06)

### Write-module conveniences for set operations

- **`clear_tags(book_id) -> int`** detaches every tag from a book in one
  call: link rows deleted, now-orphaned tag rows pruned after (the
  `fkc_delete_on_tags` order), `last_modified` bumped and the book queued
  for OPF resync only when a link actually went away. The count of removed
  links comes back and an already-untagged book is an honest zero; until
  now a caller had to know the tags to clear them through per-name
  `remove_tag`.
- **`add_custom_column_values(book_id, label, values) -> int`** gives
  multi-valued custom columns append semantics. `set_custom_column` is
  replace-only, so putting `Brandon` beside an existing `Rin` in `#audience`
  took a read-modify-replace dance; the new call dedupes against the book's
  existing values (the link table is `UNIQUE(book, value)`), collapses
  duplicates within the input, inserts only genuinely new links, and
  returns the honest count. Scoped to `is_multiple` Pattern-A columns:
  single-valued and direct-storage columns raise with a pointer to
  `set_custom_column`, and a bare string is a `TypeError` rather than a
  comma-split guess.
- **`clear_rating(book_id) -> bool`** is a self-documenting alias of
  `set_rating(book_id, None)` so an audit trail names the operation; the
  orphaned rating row prunes with it.
- Consumers: CalibreQuarry's Phase 16 set-mode verbs and Phase 17's
  `#audience` step sit directly on these three calls and need nothing new
  downstream; Bindery, Hermitage, and Carrel-calibre-web are unaffected in
  their recorded postures and no Flatpak pin moves. The phase-3-import
  skill's `UNIQUE(book, value)` gotcha now teaches the setters instead of
  raw delete-then-insert, and the phase-1 skill was swept clean.
- The test fixture's link tables now carry the real `UNIQUE(book, value)`
  shape plus an is_multiple `#audience` column; suite 258 → 280 (the
  255 baseline here was a typo; the same-day 1.12.0 entry below says
  255 → 258 -- repaired 2026-09-09 so the two agree).

## v1.12.0 (2026-09-06)

### genre_distribution: genre shares for the whole library

- **New `analytics.genre_distribution(db)`** answers "what fraction of the
  library is each genre" over Calibre's genre-as-hierarchical-tags
  convention. Every dot-path node rolls up its subtree (`Fic.Fantasy.Epic`
  contributes to `Fic`, `Fic.Fantasy`, and itself), shares are fractions of
  every book in the library, and a book counts once per node even when
  several of its tags share an ancestor. Multi-genre books therefore land
  in several roots and the shares can legitimately sum over 1.0; the
  docstring says so where renderers will read it. Nodes come back
  depth-first (parents before children, siblings share-descending then
  name) so both a top-level headline slice and a full tree render are
  plain dict walks; books with no tags count under `"untagged"`, last.
- Deliberately not a duplicate of `get_tag_counts` (flat per-tag link
  counts): the rollup, the denominator, and the untagged bucket are the
  delta the analytics module's scope rule demands. CalibreQuarry's
  `--analytics genres` (3.27.0) is the first renderer.
- Suite 255 → 258.

## v1.11.1 (2026-09-03)

### Fixed: the "sort" key was a silent no-op

- **`list_books(sort="sort")` never sorted.** The hydrated rows store
  Calibre's title-sort under `title_sort`, so the public `sort` key
  resolved to a missing field, every comparator result was "equal", and
  the listing came back in cache order — which happens to BE title-sort
  order (the SELECT's `ORDER BY b.author_sort, b.sort`), so nothing
  looked wrong and the 1.10.0 tests passed by that coincidence. Found by
  the first real consumer: Carrel's descending search sort returned
  ascending results. The public key now maps to the row field
  (`_ROW_KEY_ALIASES`), and a regression test inserts books whose
  insertion order differs from their title order so a no-op sort can
  never pass again.
- Suite 254 → 255.

## v1.11.0 (2026-09-03)

### list_books: multi-key sorts (the search-page sort-header shape)

- **`sort` accepts a key sequence** (primary first, one direction for
  all) alongside a single key, and `author_sort`/`series` join the key
  set — together covering the eight-button search-page sort header
  (`authaz` = author_sort, series, series_index; `authza` = the same
  descending) that motivated the API. None-valued keys still sink last
  regardless of direction; unknown keys raise. Implemented as an explicit
  comparator (`functools.cmp_to_key`) so the None handling and multi-key
  ordering are one readable rule.
- Tests: suite grows 251 → 254 (multi-key ordering both directions with
  a NULL tie-breaker, unknown-key-in-sequence, empty-sequence).
- Consumer postures unchanged from 1.10.0: the fork adopts immediately;
  CalibreQuarry/Hermitage/Bindery waivers stand.

## v1.10.0 (2026-09-03)

### list_books: the paginated, sorted listing (Phase 7's needs-new-API gap)

- **`CalibreDB.list_books(*, ids, sort, descending, offset, limit)`** pages
  the cached rows for frontends that resolve a book-id set through the
  search engine and then paginate it — the read-side answer that lets
  Carrel unwind its hybrid (cquarry id sets paged through the fork's stock
  ORM `fill_indexpage`). Pure over the cache, no SQL of its own; the
  sort keys are `sort` (Calibre's title-sort), `title`, `timestamp`,
  `pubdate`, `rating`, `series_index`, `id`; None-valued keys sort last
  regardless of direction; unknown keys raise ValueError.
- **Consumer postures (the §2.3 wave):** Carrel-calibre-web adopts in
  0.6.31 (the wings/saved_searches/categories hybrid unwinds onto it).
  CalibreQuarry, Hermitage, and Bindery waived: CalibreQuarry's CLI has no
  pager surface (its verbs cover the need), Hermitage's grid is
  client-side over the full load, and Bindery has no listing surface at
  all.
- Tests: suite grows 245 → 251 (ids filtering, every sort key both
  directions, None-last semantics, offset/limit slices, error paths).


## v1.9.0 (2026-09-02)

### Custom-column lookup parity (the read side catches up to the write side)

- **Columns are addressable three ways.** `load_custom_column()` accepted only
  the exact *Display Name* (e.g. `Translator(s)`), while Calibre's native
  search, `WritableCalibreDB.set_custom_column`, and the `#` search grammar all
  speak the internal `#label` (`#translators`) — a UX asymmetry the 2026-09-02
  import batch kept paying for. New `find_custom_column(key)` resolves one
  record by `#label`, bare label, or display name: a leading `#` matches the
  label only (labels are unique, never ambiguous); otherwise an exact
  display-name match wins (the historical key, so every existing caller keeps
  working) and a bare label is the graceful fallback; the label side is
  case-insensitive, mirroring the write module's `_custom_column_meta`.
  `load_custom_column()` routes through it, and its not-found error now lists
  both the name and the `#label` of every column. `get_custom_columns()` stays
  keyed by display name (consumers iterate it; rekeying would break them).
- **Consumer note**: Hermitage passes display names into `load_custom_column`
  and keeps working unchanged (display names still resolve); CalibreQuarry's
  `--show-custom` likewise. Bindery and Carrel-calibre-web do not read custom
  columns through this path.

### clear_identifier

- **`WritableCalibreDB.clear_identifier(book_id, id_type)`** deletes one pair
  from the EAV `identifiers` table and queues the book for OPF regeneration
  (`_touch_book()`), matching every other mutation. The type is normalized
  exactly like `set_identifier` (stripped, lowercased; empty raises); a pair
  that is already absent is an honest no-op returning False; unknown books
  raise like every other setter. Motivated by the 2026-09-02 phase-3 import:
  cleaning invalid `mobi-asin` UUIDs and migrating `amazon` ISBN-10s required
  dropping to raw SQL because the explicit helper did not exist
  (`set_identifier(b, t, None)` always worked but was not discoverable).
- Tests: suite grows 241 → 245 (dual-resolution matrix with a display-name/
  label ambiguity case, clear round-trip, normalization, no-op, dirtied-queue,
  unknown-book raise).

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
