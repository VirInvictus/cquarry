# CLAUDE.md (cquarry)

Per-project guidance. Overrides the global file where they conflict.

## What this is
The canonical read-only SQLite database layer and search grammar engine for Calibre. Extracted from CalibreQuarry to serve as a shared backend for Hermitage, Wings (Carrel), and the CalibreQuarry CLI.

## Hard constraints
- **No external dependencies.** Must run on pure Python 3.14+ standard library (`sqlite3`, `re`, `json`).
- **Read-only by design.** `CalibreDB` NEVER writes to `metadata.db`. The only sanctioned mutation path is the separate opt-in module `cquarry.write.WritableCalibreDB` — never add write methods to `CalibreDB`, and never import `cquarry.write` from read-only code.
- **Perfect Parity.** `cquarry.search.SearchEngine` must behave exactly like Calibre's native search bar. This includes edge cases like implicit AND evaluation, dot-delimited hierarchical tag search, identifier routing, exact match prefixing (`=`) with `.`/`..` component modifiers on every text field, multi-valued count operators (`tags:#>3`), language canonicalization, and strict errors for unknown virtual libraries / saved searches. Documented deviations live in spec.md §5; do not silently add more.
- **Performance.** `get_all_books()` executes an optimized 8-JOIN query and caches it. `SearchEngine` queries the cache, rarely the disk.

## Programmer-facing contract notes
- **List-typed fields.** `get_all_books()` / `get_book()` expose `authors`, `tags`, `languages`, `formats` as native `list[str]` (never comma-joined strings). Downstream code must NOT `.split(",")` them.
- **Rows include `size`.** Every book row carries `size` = SUM(data.uncompressed_size); may be None on odd schemas.
- **Strict VL/saved-search errors.** `search("vl:X")` raises `ParseException` when X is unknown; only `resolve_vl()`/`resolve_saved_search()` raise `ValueError` with an available-names message.
- **Raw comments are HTML.** Anything rendered must pass through `helpers.strip_html()` first.
- **Writes must queue OPF resync.** Every `WritableCalibreDB` mutation inserts the book id into `metadata_dirtied` (Calibre regenerates sidecar `.opf`s only for queued ids). New write APIs MUST route through `_touch_book()`/`_mark_dirty()` — never bump `last_modified` alone.
- **Dirtied queue is read-only on the read side.** `CalibreDB.get_dirtied_books()` only observes; clearing entries is Calibre's job (`mark_book_as_clean()`). Never DELETE from it in cquarry code.
- **Custom-column writers follow physical layout, not flags.** Detect Pattern A (value table + link) vs B (direct `book` column) by link-table existence — the same rule as the reader. Enumerations validate against `display.enum_values`; non-editable columns raise; composite columns have no storage and raise.
- **Entity writes prune orphans AFTER links go.** The fkc_delete_on_* triggers abort while references remain; clean links first, then delete unreferenced entity rows.

## Layout
- `src/cquarry/db.py`: `CalibreDB` connection management, snapshot fallback for locked databases, schema mapping, Phase-2 extractors (`get_annotations`, `get_last_read_positions`, `get_plugin_data`, `get_conversion_profiles`), single-entity APIs (`get_book`, `search_books`, `get_format_path`), read-side coverage (`get_dirtied_books`, `get_formats`, `get_cover_path`, `get_library_uuid`, native `pages` via `books_pages_link`, entity secondary columns via `get_entities`, typed preferences via `get_preference`/`get_field_metadata`/`get_user_categories`/`get_tag_browser_state`).
- `src/cquarry/search.py`: Lexer, AST Parser, and Evaluator for Calibre's search grammar (incl. saved-search interpolation, count operator, lang canonicalization).
- `src/cquarry/helpers.py`: Common domain-specific logic (star ratings, JPEG/PNG header sniffing, author normalization, `strip_html`, `tags_to_tree`).
- `src/cquarry/write.py`: Opt-in `WritableCalibreDB` + `register_udfs()` for trigger-safe writes. Separate module by design (spec §3.6).
- `src/cquarry/config.py`: Default path configuration.
- `tests/`: Extensive unit tests imported from the original CalibreQuarry repository.

## Testing
Run with the source tree on the path so you exercise this repo, not a stale installed copy:
```sh
PYTHONPATH=src python -m pytest tests/ -q
```
(The bare `python -m pytest` picks up whatever cquarry is pip-installed in the environment — check its version if results look stale.)

## Cross-Repo Implementation Rule
If any major update or new feature is added to `cquarry`, you MUST immediately assess and implement it throughout `~/.gitrepos/CalibreQuarry`, `~/.gitrepos/Bindery`, and `~/.gitrepos/Hermitage` if the update logically fits their respective domains. Keep the entire Calibre ecosystem synchronized with `cquarry`'s latest capabilities.
