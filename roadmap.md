# cquarry Roadmap

## Open Items

- [ ] **Single-Entity Fetching**
  Add `get_book(book_id: int) -> dict[str, Any]` to fetch a single book by ID without eagerly loading the entire library via `get_all_books()`. Useful when `cquarry` is embedded in a context where full library iteration isn't necessary.

- [ ] **Combined Search & Fetch**
  Add `search_books(query: str) -> list[dict[str, Any]]` to yield hydrated book dictionaries directly, saving consumers from cross-referencing `db.search(query)` results with `db.get_all_books()`.

- [ ] **Filesystem Path Resolution**
  Add `get_cover_path(book_id: int) -> Path | None` and `get_format_path(book_id: int, fmt: str) -> Path | None`. Consumers currently have to prepend the library root to `book["path"]` manually. This would centralize safe path resolution.

- [ ] **Saved Searches Support**
  Calibre supports user-defined Saved Searches alongside Virtual Libraries.
  Add `get_saved_searches() -> dict[str, str]` and wire the search engine to resolve them (similar to `resolve_vl()`), allowing consumers to query `search("search:my_saved_search")` natively.

- [ ] **Hierarchical Taxonomy Helpers**
  Calibre allows hierarchical tags and custom columns (e.g., `Fiction.SciFi`).
  Add helper methods to extract the taxonomy tree, converting a flat list of dot-separated string tags into a nested tree structure for UI consumers (like Hermitage's category browser).

