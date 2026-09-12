# REPORT-12-Sept: the upstream comparison (cquarry vs Calibre's own stack)

Research date: 2026-09-10/11. Method: three read-only research passes
against the upstream clone at `~/.gitrepos/calibre` (schema + upgrade
map, search stack, write paths) compared line-for-line against this
repo's read layer, search port, and write module. Full evidence lives
in the agents' findings; this report carries the distilled verdicts and
every actionable gap. Nothing below re-opens shipped work; overlaps
with Phase 12 and the Phase 7 parking lot are marked.

## Verdict

Read coverage of `metadata.db` is **substantially complete**: every
table and nearly every column Calibre writes is read and understood,
including annotations, preferences, custom-column layouts and all
datatypes, dirty queues, and the tag-browser views. Search grammar is
**near-complete (~90-95%)**: core grammar, field table, and the whole
Phase 12 search-parity set verify faithful against current upstream.
Write coverage is **complete for its declared scope** (rows are ours,
files are the caller's): every one-one, many-one, and many-many builtin
field, both custom-column patterns, format rows, creation, removal, and
the dirty queue mirror upstream exactly, including `last_modified`
bumps, `metadata_dirtied` bookkeeping, trigger-filled sort/uuid, and
author_sort recomputation.

The honest gaps are three families: one entire sidecar file unread,
about ten upstream mutation classes not implemented, and a short list
of search-edge divergences (one of them a real bug).

## A. Read-coverage gaps

1. **`full-text-search.db` is entirely unread** (upstream
   `calibre/db/fts/connect.py:35-39` attaches it beside metadata.db;
   `fts_sqlite.sql` defines `books_text` — book, format, format_size,
   format_hash, searchable_text, text_size, text_hash, err_msg,
   timestamp — plus FTS5 index tables with a custom tokenizer). The
   plain `books_text` table is readable with stdlib sqlite and needs no
   FTS5 machinery. This is the single largest missing read surface:
   book-content search and snippets with zero Calibre process, plus
   "which formats failed extraction" (`err_msg`) as an integrity
   signal, and format-hash change detection.
2. **`books_pages_link` auxiliary columns** (`algorithm`, `format`,
   `format_size`, `timestamp`, `needs_scan`; upstream
   `schema_upgrades.py:853-862`) — cquarry reads only `book, pages`
   (db.py:314). Enables a "pages pending rescan" integrity predicate
   and provenance for displayed counts.
3. **Custom-series `extra` (index) resolution** — db.py:1748-1749
   registers `#label_index` as a float location that db.py:1759-1760
   can never resolve because `load_custom_column` never selects the
   link table's `extra` float (db.py:1067-1071). `#myseries_index:>3`
   silently matches nothing. Rides the already-recorded roadmap.md:281
   cc-adapter item; do not re-box, fix under it.
4. **Doc line**: `annotations.searchable_text` joins highlight text and
   notes with `\x1f\n` (upstream `calibre/db/annotations.py:134-145`).
   One sentence in spec/CLAUDE.md stops consumers mishandling notes.
5. **Marginal**: the normalized custom-column value-table `link` column
   (upstream schema_upgrades.py:836) — no Calibre UI populates it; read
   it only if touching item 3's SQL anyway.

## B. Search-parity gaps (spec §5 honesty pass included)

Verified at parity: precedence incl. implicit AND and unary NOT; the
escape cycle; all match kinds with the len>1 guard; `.`/`..` subtree
logic; `#count`; keypair routing with the isbn special case; the full
date vocabulary; numeric relops with k/m/g; bool vocabulary; groups
with false-inversion and nesting ban; user categories with `.` subcats;
saved-search/VL recursion detection; custom `#label`/`#label_index`
addressing; language canonicalization.

1. **Real bug**: `identifiers:KEY:TRUE/FALSE` inverts on uppercase —
   search.py:1058 gates on `valq.lower()` but selects with raw `valq`
   (:1067). Upstream lowercases once and uses it for both (DS:424-430).
2. **Doc/code contradiction**: spec.md:87 and the roadmap ship note say
   identifier KEYS sweep as text in `all`; `_match_all`
   (search.py:1088-1095) omits `identifiers`. Upstream sweeps them
   (DS:808-818). Fix one side and pin with a test.
3. **Super-quotes `"""..."""` unported** (upstream lexer
   SQP:150,191-212; documented user feature, gui.rst:445): the
   sanctioned escape hatch for quote/paren/regex-heavy queries
   mis-tokenizes today.
4. **`template:` location**: upstream registers and evaluates it
   (FM:546; DS:715-801). cquarry has no location, so `template:...`
   silently matches nothing. Minimum: a clear "templates unsupported"
   ParseException (upstream's TemplatesNotAllowed is the model).
   Implementation stays §7-out-of-scope unless a consumer asks.
5. **§5 honesty pass**: date fields over-accept the
   blank/empty/`~`/numeric vocabulary upstream rejects (DS:153-176);
   numeric fields over-accept tristate words upstream rejects
   (DS:245-290); the `all`-sweep probes `id` and numeric-`cover`
   upstream excludes (DS:811) and lacks upstream's bare
   true/false-presence branch (DS:849-855); lexer strictness differs
   (upstream silently truncates unparsable tails and absorbs a quoted
   word after a bare location suffix, SQP:205,279-284; cquarry is
   saner but undocumented); tristate bool fidelity and upstream's
   translated yes/no vocabulary (DS:356-411); benign extensions
   (`lang`, `ids`, timestamp token, case-insensitive VL names, ignored
   prefs). Each gets: fix-to-parity, or a dated §5 entry. Decide
   per-item; never silent.
6. Optional (L, only on consumer demand): implement `template:`
   searches over a minimal template subset.

## C. Write-path gaps (upstream mutation classes not implemented)

Side effects replicated: `last_modified` bumps, `metadata_dirtied`
guards, trigger-filled sort/uuid, author_sort recomputation, languages
item_order, rating 0-mapping, atomic placement + fsync. Skipped:
path re-laying, FTS dirty-marking, pages-recount flags, format-file
trash.

1. **Path re-laying on `update_title`/`set_authors`** (upstream
   `cache.py:1986-1987`, `backend.py:2098-2202`): dir rename, format
   file renames to the new `Title - Author.ext` stem, empty-parent
   removal. Without it, curation renames leave `Author/Title (id)`
   directories lying about their contents — the one gap that silently
   corrupts the human-navigable layout. Internal completion; M.
2. **`rename_entity` / `remove_entity_everywhere`** (upstream
   `cache.py:2758-2862`: author renames recompute sorts and re-lay
   paths, series renumber, case-change merges). The
   fix-misspelled-author-everywhere verb. Promotion candidate; M.
3. **`set_cover` / `remove_cover`** (`cache.py:2237-2253`,
   `backend.py:1962-1992`): closes the shipped
   find-low-res-covers -> replace loop. Promotion candidate; S.
4. **FTS + pages dirtying alongside format writes** (TEMP triggers on
   Calibre's connection, `fts_triggers.sql`; `cache.py:2472,2481`):
   when `full-text-search.db` exists, insert into `dirtied_formats`
   and set `needs_scan` so Calibre's index and page counts stay honest
   after `set_format` repairs. Internal completion; S-M.
5. **Trash lifecycle** (`backend.py:2386-2409`, `cache.py:3557`):
   empty/expire/restore companions to the shipped
   `remove_book(delete_files="trash")`. Promotion candidate; S.
6. **`set_author_sort` / `set_title_sort` / `set_timestamp`
   passthrough setters** (`cache.py:2348-2366`): caller-supplied
   corrections for mangled sorts. Distinct from the documented
   deviation on the surname-flip heuristic. Promotion candidate; S.
7. **`create_custom_column` / `delete_custom_column`** (`backend.py:
   1384,1536`): library bootstrap for the acquisition importer
   (`#audience`/`#reading_status` before phase-2 bulk writes).
   Promotion candidate; M.
8. **`save_original_format` / `restore_original_format`**
   (`cache.py:1581-1614`): undo-able format repair; binds directly to
   bindery-cli's install lane. Promotion candidate; S-M.
9. **`clean_identifier` parity** (`db/write.py:118-121`): value
   `,`->`|`, type strips `:`/`,`. Internal; S.
10. **Docs**: record the case-change policy (cquarry never re-cases
    existing entity rows; upstream has `allow_case_change`,
    db/write.py:299-315) in spec §5. Docs-only.

Deliberately not recommended: embed_metadata into format files (L,
stdlib-violating), annotation/reading-position writers (consumer, not
curation), library clone/restore/export (out of write scope),
OPF-dump-now (decided), notes (declined by recorded decision).

## D. What this repo already leads at

Read-side: annotations with searchable_text, dirty queues, full custom
column layout handling, single-entity fetches, the entire guard/trigger
census — none of it present in calibredb's surface. Search-side: the
port is saner than upstream at the edges (raises on garbage instead of
silently truncating). Write-side: batch sessions with filesystem
compensation and the `set_format` atomic path have no upstream
equivalent as a single transaction.

## E. Phase 13 routing

Everything above is boxed in roadmap.md **Phase 13** with fresh-agent
context. Effort split: A1+A2+B1-B3+C4+C9 fit one release lane; the
promotion candidates (C2, C3, C5-C8) each need the recorded approval
per the standing rule before their boxes open; the §5 honesty pass and
the docs items ride any commit.
