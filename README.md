# cquarry

A lightweight, canonical Python package providing read-only access to Calibre's `metadata.db` and a full parser for Calibre's search expression grammar.

This library powers [CalibreQuarry](https://github.com/VirInvictus/CalibreQuarry) (CLI/TUI), [Hermitage](https://github.com/VirInvictus/Hermitage) (GTK4 Desktop Gallery), and [Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) (Web Reader) within the ecosystem, ensuring that Virtual Library definitions and search queries evaluate identically across all frontends.

## Features
- **Direct SQLite Access:** No `calibredb` binary required, avoiding Calibre's heavy Python initialization overhead.
- **Lock-safe Snapshots:** Automatically detects if Calibre has an exclusive write-lock on `metadata.db` and safely routes queries through a temporary WAL-consistent snapshot.
- **Full Search Grammar Parity:** A recursive-descent parser that perfectly matches Calibre's native search capabilities (exact matches, substring, boolean logic, date math, custom columns, identifiers, and nested virtual libraries).

## Usage
```python
from cquarry.db import CalibreDB

# Initialize the database (creates snapshot if locked)
db = CalibreDB("~/Calibre Library/metadata.db")

# Fetch all books with pre-joined metadata
books = db.get_all_books()

# Search using native Calibre grammar
matching_ids = db.search("tags:Fic.SciFi and rating:>=4")
print(f"Found {len(matching_ids)} highly rated Sci-Fi books.")
```

## Installation
```sh
pip install git+https://github.com/VirInvictus/cquarry.git
```

## Public API

`CalibreDB(db_path: str)`
*   `get_all_books() -> list[dict[str, Any]]`: Returns a list of all books in the library, pre-hydrated with `authors`, `tags`, `series`, `rating`, `publisher`, and `languages`.
*   `get_custom_columns() -> dict[str, dict[str, Any]]`: Returns a dictionary of all user-defined custom columns in the library.
*   `load_custom_column(col_name: str) -> dict[int, Any]`: Returns a dictionary mapping `book_id` to the value of the specified custom column.
*   `get_identifiers(book_id: int) -> dict[str, str]`: Returns all identifiers (e.g. `isbn`, `amazon`) for a specific book.
*   `get_virtual_libraries() -> dict[str, str]`: Returns a dictionary mapping Virtual Library names to their Calibre search expression.
*   `search(query: str) -> set[int]`: Parses a Calibre search expression and returns a set of matching `book_id`s.
*   `resolve_vl(vl_name: str) -> set[int]`: Returns a set of `book_id`s that belong to the specified Virtual Library.

## Search Grammar Support

`cquarry` implements a recursive-descent parser that is built to match Calibre's native search engine. It fully supports:
*   **Logical operators**: `and`, `or`, `not`.
*   **Relational operators**: `=`, `:`, `>`, `>=`, `<`, `<=`.
*   **Exact and Substring Matches**: `=Term` (exact), `Term` (substring), `~Term` (regex).
*   **Hierarchical Fields**: Tag searches (e.g., `tags:Fiction.SciFi`) correctly respect hierarchy.
*   **Date Math**: `pubdate:>30daysago`.
*   **Custom Columns**: Evaluated using the `#column_name:` prefix.
*   **Virtual Library Cross-References**: `vl:"Library Name"` to nest queries.

