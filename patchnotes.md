## v1.0.0 (2026-08-23)

### Extract & Launch
- **Initial Extraction:** Graduated `cquarry` into a standalone shared library.
- **Database Engine (`cquarry.db`):** Features the `CalibreDB` wrapper, which intelligently manages `metadata.db` access, falling back to a WAL-consistent snapshot if the Calibre desktop application holds an exclusive write-lock. Exposes `get_all_books()`, tags, series, and identifiers with performant SQLite JOINs and internal memory caching.
- **Search Grammar Engine (`cquarry.search`):** A full recursive descent parser implementing Calibre's search expression logic. Provides boolean logic (`AND`, `OR`, `NOT`), exact matching (`=value`), hierarchical tag prefix matching (`tags:Fic` matches `Fic.Fantasy`), date math (`date:>14daysago`), and nested Virtual Library resolution (`vl:"My Books"`).
- **Helpers:** Inherits standard Calibre domain formatters from CalibreQuarry (star rating converters, deterministic missing series gap detection, and binary image dimension sniffing).
