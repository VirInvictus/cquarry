# cquarry Research Report: Calibre Architecture and Search Parity

This document synthesizes findings from an extensive research sweep of the `calibre` upstream repository, the `testing_facility/metadata.db` schema, and the `cquarry` search engine architecture.

## 1. Database Architecture & Interaction (`metadata.db`)

Calibre's interaction with `metadata.db` relies on a highly specialized architecture:

- **SQLite Driver**: Calibre uses `apsw` (Another Python SQLite Wrapper) combined with a custom C++ extension (`sqlite_extension.cpp`) to implement ICU tokenizers, Snowball stemmers, and custom collations (`PYNOCASE`, `icucollate`) directly in SQLite.
- **Hybrid In-Memory Caching**: SQLite serves strictly as a persistence layer. Upon startup, Calibre reads all core tables into a normalized in-memory Python cache. All searches, category tree builds, and complex queries run in-memory, avoiding continuous SQLite read locks.
- **Locking Model**: Calibre utilizes a Multiple-Readers Single-Writer (MRSW) lock internally in Python. Database writes synchronize in-memory caches, perform batched SQLite link table updates, and then log the book IDs to `metadata_dirtied` for asynchronous filesystem `.opf` generation.
- **Dynamic Schema for Custom Columns**: Defined in `custom_columns`, user fields are either normalized (creating a `custom_column_N` table and `books_custom_column_N_link` table) or non-normalized (a direct `custom_column_N` table with a `book` foreign key). 
- **Commas in Author Names**: Calibre stores the raw display name (e.g. `Cay S. Horstmann`) in `authors.name` and the inverted name (`Horstmann, Cay S.`) in `authors.sort`. To avoid comma-splitting bugs, Calibre uses relational link tables (`books_authors_link`) rather than parsing comma-separated strings.
- **Size and Pages**: The core schema tracks `uncompressed_size` within the `data` table. There is no native `pages` table; page counts are managed via custom columns or third-party plugin data (`books_plugin_data`).
- **Comments**: Stored in a distinct `comments` table mapped 1:1 to books containing raw HTML payloads, isolating them from core table scans for performance.

## 2. Search Engine Parity Analysis

Calibre evaluates searches by generating inverted-index field iterators across candidate book subsets. `cquarry` ports Calibre's recursive-descent grammar and boolean pruning to pure stdlib Python, but evaluates linearly per-book.

While `cquarry` successfully reproduces implicit ANDs, nested virtual libraries, and general match kinds (`=`, `~`), several significant gaps exist:

### 2.1 Grammar and Syntax Gaps
- **Docstring Literals**: Calibre allows triple-quoted strings (`"""..."""`) to escape quotes and parens inside templates. `cquarry` fails to parse these.
- **Multi-Valued Count Operator**: Calibre supports `#<relop><count>` (e.g. `tags:#>3`) to find books with more than 3 tags. `cquarry` has no evaluator for this.
- **Exact Component Matching**: Calibre's exact match (`=`) supports leading `.` (subtree) and `..` (component) matching on *all* text fields. `cquarry` only supports this in the `tags` hierarchy (`_match_hier`).
- **Date Separators**: `cquarry` requires hyphens (`YYYY-MM-DD`). Calibre also allows slashes (`YYYY/MM/DD`).
- **Boolean Keywords**: Calibre recognizes tristate bools and extensive localized keywords (`checked`, `blank`, `empty`, `_yes`). `cquarry` only supports strict `true`/`false`/`yes`/`no`.

### 2.2 Missing Locations and Field Coverage
- **Missing Locations**: `size`, `pages`, `title_sort`, `series_sort`, `marked`, `in_tag_browser`, `ondevice`.
- **Saved Searches (`search:`)**: Calibre allows interpolating named queries from the `preferences` table. `cquarry` lacks this resolution logic.
- **The "All" Field**: Calibre's bare term search dynamically queries all text columns, comments, and numeric fallback columns. `cquarry` hardcodes 7 specific fields.
- **Language Canonicalization**: Calibre converts `languages:English` to `eng` via an internal map. `cquarry` treats it as a raw string match.

### 2.3 Implementation Bugs Discovered
- **Author Splitting Bug**: Because `CalibreDB.get_all_books()` uses `GROUP_CONCAT(name, ', ')`, author names containing literal commas are mangled in `cquarry`'s search view string splitter.
- **Custom Rating Datatype**: In `_CUSTOM_DT_MAP`, custom rating columns are mistakenly mapped to `DT_FLOAT` instead of `DT_RATING`.
- **Custom Series Indices**: `cquarry` fails to automatically register the float `#label_index` column for custom series.
- **Virtual Library Error Handling**: `cquarry` silently returns empty sets for unknown Virtual Libraries (`vl:Unknown`) instead of raising a `ParseException`.

---

This research directly informs the roadmap phase targeted at achieving 1:1 search parity with Calibre's native engine.
