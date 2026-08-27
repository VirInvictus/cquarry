# Calibre SQLite Database Analysis Report

This report documents the internal mechanics, schema anomalies, and safe access patterns of Calibre's `metadata.db`, mapped out during independent agent exploration of the core tables, taxonomy, custom columns, application state, and write-path integrity requirements.

## 1. Core Metadata Anomalies (`books`, `authors`, etc.)
- **Denormalized Book Sorting**: `series_index` and `author_sort` live directly on the `books` table, meaning unlinked series keep their float index on the book row, and `author_sort` is a concatenated string representing the combined sort of all authors on a book.
- **Asymmetric Link Tables**: Link tables uniformly use `id`, `book`, and the target foreign key (e.g., `author`, `series`). However, `books_authors_link` supports N:M relationships with `UNIQUE(book, author)`, while `books_publishers_link` and `books_series_link` are strictly 1:N via `UNIQUE(book)`.
- **Order Preservation**: Author ordering is determined by `books_authors_link.id`.
- **Custom Rating Scale**: Ratings are stored as integers from 1-10 on `ratings` (mapping to 0.5-5.0 stars). Unrated is `NULL` or 0.

## 2. Taxonomy and Files (`tags`, `data`, `identifiers`)
- **Hierarchical Tags**: Calibre implements hierarchical trees entirely via dot-delimited strings in `tags.name` (e.g., `Fiction.Science Fiction`).
- **File Asset Mapping**: The `data` table represents files (EPUB, PDF, etc.) mapping `(book, format)`. `data` does not store the absolute path; paths are dynamically constructed using `books.path` and `data.name + '.' + lower(data.format)`.
- **Identifiers limit**: `identifiers` is an Entity-Attribute-Value store limited to one value per type per book (`UNIQUE(book, type)`). This forces plugins to create pseudo-types like `isbn13` when a book has multiple ISBNs.
- **Comments as 1:1 Isolation**: `comments` is technically a 1:1 offshoot of `books` but is isolated to prevent large HTML payloads from destroying the cache locality of `books` scans.

## 3. Custom Column Storage Patterns
Custom columns registered in `custom_columns` use ID-based dynamic tables (`custom_column_N`). Storage is determined by the `normalized` boolean flag.
- **Pattern A (Normalized = 1)**: (text, enumeration, series). Deduplicated strings stored in `custom_column_N` joined via `books_custom_column_N_link`.
- **Pattern B (Normalized = 0)**: (int, float, bool, datetime, composite). Values mapped directly to `books(id)` on `custom_column_N` with no link table.
- **Multiple Values**: `is_multiple=1` doesn't alter SQL structures; it changes ingestion logic to split strings into discrete tokens across multiple rows in the link table.
- **Safe Reading**: Read-only queries must check `sqlite_master` for the existence of `books_custom_column_N_link` to decide which query strategy to use, instead of blindly trusting `is_multiple`.

## 4. Internal State, UI, and Virtual Libraries
The `preferences` table is a JSON key-value store for application state.
- **Virtual Libraries**: Found under `virtual_libraries`, stored as a dict mapping names to Calibre search grammar strings.
- **Saved Searches**: Found under `saved_searches`, which can be nested via `search:"<name>"` in other queries.
- **Annotations**: Bookmarks and highlights are stored in `annotations` with a robust set of FTS5 virtual tables (`annotations_fts`, `annotations_fts_stemmed`).
- **Reading Progress**: `last_read_positions` tracks location per user, device, and book.

## 5. Safely Executing Write Operations
Writing directly to `metadata.db` via Python's `sqlite3` driver requires extreme care due to Calibre's heavy reliance on SQL triggers for foreign key cascading and sort-key generation.
- **Trigger Hazards**: `books_update_trg` and `books_insert_trg` call custom SQLite Python UDFs like `title_sort(1)` and `uuid4(0)`. If a Python client connects and executes an `UPDATE books SET title = ...` without registering `title_sort`, SQLite will throw an `OperationalError` and abort the transaction.
- **UDF Registration Requirement**: Writing requires registering custom Python functions (e.g. `conn.create_function("title_sort", 1, title_sort)`) and collations (`PYNOCASE`).
- **Tag Write Sequence**: Adding a tag involves: (1) `INSERT OR IGNORE` into `tags`, (2) Selecting the new tag ID, (3) `INSERT OR IGNORE` into `books_tags_link`, and (4) Updating `books.last_modified`. Deleting tags requires cleaning the link table before the main `tags` table to satisfy `fkc_delete_on_tags`.

## 6. In-Process Landmines (from the testing-facility audit, 2026-08-26)
Objects in the schema that reference SQL functions Calibre only registers inside its own
process — they raise `OperationalError` when read by external clients:
- **`meta` view**: calls `sortconcat(bal.id, name)` (an aggregate) and `concat(...)`.
  `SELECT * FROM meta` fails outside Calibre. cquarry's `get_all_books()` supersedes the
  view; it is deliberately not read.
- **`tag_browser_filtered_*` views**: every count/avg subquery guards on
  `books_list_filter(book)` — the GUI's live search-restriction function. GUI state, not
  data; cquarry skips these variants and reads the pure-SQL `tag_browser_*` views instead.
- **`tag_browser_series` view**: sorts via `title_sort(name)`. cquarry supplies the stdlib
  `helpers.title_sort` implementation on its connection for the duration of the
  `get_tag_browser_counts()` read, then removes it.
- **Column-spelling drift across the views**: entity views expose `name`, custom-column
  views expose `value`, and the ratings view exposes `rating` — readers must fall through
  the three spellings (cquarry's `get_tag_browser_counts()` does).
- **`books_languages_link.item_order`** (schema ≥ user_version 23-ish): Calibre orders a
  book's languages by it, not by link id. Pre-column schemas fall back to link-id order.
- **`annotations_fts` / `annotations_fts_stemmed`** (FTS5): indexed derivatives of
  `annotations.searchable_text`. cquarry reads `searchable_text` directly — identical
  result sets for text matching, no stemming/ranking dependency on FTS tokenizer details.
