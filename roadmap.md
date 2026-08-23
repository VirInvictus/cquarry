# cquarry Roadmap

This roadmap outlines the planned evolution of `cquarry` from a read-only metadata extractor to a full-featured Calibre ecosystem bridge, utilizing the structural discoveries documented in `database_report.md`.

## Phase 1: Read-Only Enhancements (Current & Near-Term)
*Context: Improving our query capabilities using existing read-only mechanics (ref: database_report.md Sections 1-4).*

- [ ] **Single-Entity Fetching**: Add `get_book(book_id: int) -> dict` to fetch single records without full-library scanning.
- [ ] **Combined Search & Fetch**: Add `search_books(query) -> list[dict]` to immediately yield hydrated metadata from search sets.
- [ ] **Format Path Resolution**: Implement `get_format_path(book_id, fmt)`. Resolve `(library_root / books.path / data.name + format)` dynamically (ref: Report Sec 2).
- [ ] **Saved Search Resolution**: Parse the JSON `preferences` table to support `search:"<name>"` interpolation in `SearchEngine` queries (ref: Report Sec 4).
- [ ] **Virtual Library UI Metadata**: Expose hidden/ordering JSON lists from `preferences` (`virt_libs_hidden`, `virt_libs_order`) so consumers can match Calibre's exact tab layout.
- [ ] **Hierarchical Taxonomy Parsing**: Provide a helper to convert dot-delimited flat `tags` (e.g., `Fiction.Science Fiction`) into native Python nested dictionaries for TreeView consumers.
- [ ] **Safe Custom Column Reads**: Transition `load_custom_columns` to check `sqlite_master` for the explicit existence of `books_custom_column_N_link` instead of relying on the semantic `is_multiple` flag (ref: Report Sec 3).
- [ ] **Star Rating Conversion**: Expose a standard `normalize_rating(int)` method converting internal 1-10 scales to 0.0-5.0 float stars (ref: Report Sec 1).

## Phase 2: Metadata Portability & Export
*Context: Enabling users and agents to safely extract more than just book catalog metadata.*

- [ ] **Extract Annotations**: Query the `annotations` table to extract e-reader highlights, bookmarks, and user notes as JSON payload.
- [ ] **Extract Reading Progress**: Map the `last_read_positions` table to track reading velocity/progress fractions per device.
- [ ] **Plugin Data Bridges**: Expose the `books_plugin_data` table to enable `cquarry` to read third-party Goodreads sync, WordCount, or ISBN metadata without requiring the plugin itself.
- [ ] **Comments Parsing Utilities**: Add utilities to safely strip or sanitize the raw HTML payloads found in the `comments` table before handing them to CLI/UI consumers.
- [ ] **Extract Conversion Profiles**: Query the `conversion_options` table to back up specific book conversion pipeline recipes.

## Phase 3: Write Capabilities (Long-Term)
*Context: Establishing safe write paths for agents and scripts without destroying database integrity via missing triggers or unhandled UDFs (ref: database_report.md Section 5).*

- [ ] **UDF Registration Framework**: Implement a standard `register_udfs(conn)` method injecting `title_sort()`, `uuid4()`, and `author_to_author_sort()` into the SQLite connection to prevent trigger execution failures.
- [ ] **Collation Injection**: Register `PYNOCASE` collation globally on write connections.
- [ ] **Title Update API**: Build `update_title(book_id, new_title)` that handles `last_modified` timestamp updates and allows `books_update_trg` to naturally rewrite the `sort` field.
- [ ] **Safe Tag Application**: Build `add_tag(book_id, tag_string)` handling the four-step tag creation and link process while preserving comma delimiters.
- [ ] **Safe Tag Removal**: Build `remove_tag(book_id, tag_string)` that aggressively checks for `fkc_delete_on_tags` aborts and prunes orphaned taxonomy categories cleanly.
- [ ] **Identifier Batch Updater**: Build a safe write pipeline to append EAV records into the `identifiers` table without violating `UNIQUE(book, type)`.
