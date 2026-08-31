<div align="center">
  <img src="logo.svg" width="96" height="96" alt="cquarry logo"/>
  <h1>cquarry</h1>
  <p>Canonical Calibre database layer and search grammar engine for Calibre libraries.</p>
</div>

This library powers [CalibreQuarry](https://github.com/VirInvictus/CalibreQuarry) (CLI/TUI), [Hermitage](https://github.com/VirInvictus/Hermitage) (GTK4 gallery), [Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) (web reader), and [Bindery](https://github.com/VirInvictus/Bindery) (EPUB repair & audit). By centralizing the search grammar parser and metadata access, cquarry evaluates virtual library definitions and search queries consistently across frontends.

## Features

- **Direct SQLite access.** No `calibredb` binary required, no Calibre Python initialization overhead.
- **Lock-safe snapshots.** Automatically detects if Calibre holds an exclusive write lock on `metadata.db` and routes queries through a temporary WAL-consistent copy.
- **Full search grammar parity.** A recursive-descent parser implementing Calibre's native search capabilities: boolean logic, field prefixes, date math (hyphen *and* slash separators), hierarchical tags with `.`/`..` component modifiers on every text field, custom columns, identifiers, saved-search interpolation (`search:"Name"`), multi-valued count operators (`tags:#>3`), language canonicalization (`languages:English` → `eng`), and nested virtual library cross-references.
- **Native page counts.** The `pages:` location reads Calibre's own `books_pages_link` table first (maintained by upstream's CountPages integration) and falls back to an int custom column labelled `pages`; counts also ride along in every book row.
- **Entity secondary columns & display config.** Book rows carry `author_sorts`/`author_links` parallel to `authors`; `get_entities(kind)` exposes `{id, name, sort, link, count}` for authors/series/publishers/tags/languages; custom columns report `editable`, `normalized` and their decoded `display` JSON (`enum_values`, `enum_colors`, …); and a typed preferences accessor covers everything else (`get_preference`, `get_field_metadata`, `get_user_categories`, `get_tag_browser_state`).
- **Metadata portability.** Read e-reader annotations, per-device reading progress, third-party plugin data, and conversion profiles; sanitize comments HTML for display.
- **Opt-in write path.** `cquarry.write.WritableCalibreDB` offers trigger-safe mutations in a separate module the read-only API can never touch: title, authors (with `author_sort` recomputation), series (+index), publisher, rating (UNIQUE-deduped), languages (canonicalized to ISO codes), tags, identifiers, comments, generic custom-column writes (layout auto-detected, enumerations validated against `display.enum_values`, non-editable columns refused), format registration/removal, `has_cover`, and full book removal with orphan pruning; every mutation queued in `metadata_dirtied` for OPF resync.
- **Context manager.** `CalibreDB` supports `with` statements for automatic cleanup of snapshot files.
- **Zero dependencies.** Pure Python 3.14+ stdlib (`sqlite3`, `re`, `json`, `unicodedata`).

## Usage

```python
from cquarry.db import CalibreDB

# Open a library (creates a snapshot if Calibre has the lock)
with CalibreDB("~/Calibre Library/metadata.db") as db:
    # Fetch all books with pre-joined metadata
    books = db.get_all_books()

    # Search using Calibre's native grammar
    sci_fi = db.search("tags:Fic.SciFi and rating:>=4")
    print(f"Found {len(sci_fi)} highly rated Sci-Fi books.")

    # Resolve a virtual library to a set of book IDs
    wing = db.resolve_vl("To Read")

    # Interpolate a saved search straight from Calibre's preferences
    award_winners = db.search('search:"Award Winners"')

    # Inspect custom columns
    cols = db.get_custom_columns()
    status = db.load_custom_column("Reading Status")

    # Single-entity helpers (no whole-library scan)
    book = db.get_book(42)
    epub = db.get_format_path(42, "EPUB")

    # Metadata portability
    highlights = db.get_annotations(42)
    progress = db.get_last_read_positions(42)
    wordcounts = db.get_plugin_data(name="wordcount")
```

The composed deep fetch (what detail views used to hand-assemble from ten
read calls):

```python
    dossier = db.get_book_dossier(42, include_comments=True)
    print(dossier["formats"], dossier["custom_columns"])
    print(dossier["comments"]["plain"])  # comments HTML, already stripped
```

Writes live behind an explicit opt-in import:

```python
from cquarry.write import WritableCalibreDB

with WritableCalibreDB("~/Calibre Library/metadata.db") as wdb:
    wdb.add_tag(42, "Audited")
    wdb.set_identifier(42, "isbn", "9780123456789")

# A multi-book curation pass commits exactly once:
with WritableCalibreDB("~/Calibre Library/metadata.db") as wdb:
    with wdb.batch():
        wdb.set_pubdate(42, "1991-10-01")
        wdb.add_tag(43, "Audited")

# Every mutation queues an OPF regeneration; check what Calibre will resync:
with CalibreDB("~/Calibre Library/metadata.db") as db:
    print(db.get_dirtied_books())  # e.g. [42, 43]
```

## Installation

```sh
pip install git+https://github.com/VirInvictus/cquarry.git
```

## API at a glance

The full per-method reference lives in [API.md](API.md). One line per module:

| Module | What it is |
|--------|------------|
| `cquarry.db.CalibreDB` | The read-only database layer: hydrated rows, single-entity fetches, format/cover path resolution, custom columns, preferences, annotations and progress extractors, VL/saved-search resolution, and the composed `get_book_dossier()` deep fetch. |
| `cquarry.search` | The lexer/parser/evaluator porting Calibre's search grammar; usable standalone behind the `MetadataProvider` protocol. |
| `cquarry.helpers` | Domain utilities: rating conversion, comment sanitization, author display, series gaps, image dimension sniffing, the ISBN family (`isbn_normalize`, `isbn_check_digit_is_valid`, `to_isbn13`), and `tag_rollup`. |
| `cquarry.integrity` | The shared library-integrity predicates: untagged, unrated, authorless, formatless, coverless, missing cover files, deprecated formats, low-res covers, duplicates, series gaps. |
| `cquarry.analytics` | Shared derivations: addition timeline, per-author stats, rating distribution, wing overlap. |
| `cquarry.write` | The opt-in mutation path (`WritableCalibreDB`): trigger-safe setters, `batch()` transactions, `remove_book`. Every mutation queues OPF resync. |
| `cquarry.config` | Saved database-path configuration (`~/.config/cquarry/config.json`). |

## Development

```sh
python -m pytest tests/           # full suite
python -m pytest tests/ -v        # verbose
```

Run with `PYTHONPATH=src` to exercise this checkout rather than any installed copy.

Four test modules: `test_db.py` (CalibreDB against fixture databases), `test_helpers.py` (utility functions), `test_search.py` (parser, matcher, and integration tests), `test_write.py` (opt-in write module with trigger-hazard fixtures).

See [spec.md](spec.md) for the full contract and [roadmap.md](roadmap.md) for planned work.

## Acknowledgements

[Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) is a fork of
[calibre-web](https://github.com/janeczku/calibre-web) that uses cquarry as its search and
virtual-library engine. Features proven there flow back into cquarry's roadmap (see Phase 7);
calibre-web's original authors deserve the credit for the web experience that fork builds on.

## Support

If cquarry's useful to you and you'd like to chip in:

- liberapay · [liberapay.com/bdkl](https://liberapay.com/bdkl/)
- bitcoin
  ```
  bc1qkge6zr45tzqfwfmvma2ylumt6mg7wlwmhr05yv
  ```

## License

MIT. See [LICENSE](LICENSE).
