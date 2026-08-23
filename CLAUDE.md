# CLAUDE.md (cquarry)

Per-project guidance. Overrides the global file where they conflict.

## What this is
The canonical read-only SQLite database layer and search grammar engine for Calibre. Extracted from CalibreQuarry to serve as a shared backend for Hermitage, Wings (Carrel), and the CalibreQuarry CLI.

## Hard constraints
- **No external dependencies.** Must run on pure Python 3.14+ standard library (`sqlite3`, `re`, `json`).
- **Read-only by design.** This library will NEVER write to `metadata.db`. It is explicitly a query and evaluation engine.
- **Perfect Parity.** `cquarry.search.SearchEngine` must behave exactly like Calibre's native search bar. This includes edge cases like implicit AND evaluation, dot-delimited hierarchical tag search, identifier routing, and exact match prefixing (`=`).
- **Performance.** `get_all_books()` executes an optimized 8-JOIN query and caches it. `SearchEngine` queries the cache, rarely the disk.

## Layout
- `src/cquarry/db.py`: `CalibreDB` connection management, snapshot fallback for locked databases, schema mapping.
- `src/cquarry/search.py`: Lexer, AST Parser, and Evaluator for Calibre's search grammar.
- `src/cquarry/helpers.py`: Common domain-specific logic (star ratings, JPEG/PNG header sniffing, author normalization).
- `src/cquarry/config.py`: Default path configuration .
- `tests/`: Extensive unit tests imported from the original CalibreQuarry repository.
