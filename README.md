<div align="center">
  <img src="logo.svg" width="96" height="96" alt="cquarry logo"/>
  <h1>cquarry</h1>
  <p>Canonical Calibre database layer and search grammar engine for Calibre libraries.</p>
</div>

This library powers [CalibreQuarry](https://github.com/VirInvictus/CalibreQuarry) (CLI/TUI), [Hermitage](https://github.com/VirInvictus/Hermitage) (GTK4 gallery), [Carrel-calibre-web](https://github.com/VirInvictus/Carrel-calibre-web) (web reader), and [Bindery](https://github.com/VirInvictus/Bindery) (EPUB repair & audit). By centralizing the search grammar parser and metadata access, cquarry ensures that virtual library definitions and search queries evaluate identically across every frontend in the ecosystem.

## Features

- **Direct SQLite access.** No `calibredb` binary required, no Calibre Python initialization overhead.
- **Lock-safe snapshots.** Automatically detects if Calibre holds an exclusive write lock on `metadata.db` and routes queries through a temporary WAL-consistent copy.
- **Full search grammar parity.** A recursive-descent parser faithfully porting Calibre's native search capabilities: boolean logic, field prefixes, date math (hyphen *and* slash separators), hierarchical tags with `.`/`..` component modifiers on every text field, custom columns, identifiers, saved-search interpolation (`search:"Name"`), multi-valued count operators (`tags:#>3`), language canonicalization (`languages:English` → `eng`), and nested virtual library cross-references.
- **Native page counts.** The `pages:` location reads Calibre's own `books_pages_link` table first (maintained by upstream's CountPages integration) and falls back to an int custom column labelled `pages`; counts also ride along in every book row.
- **Entity secondary columns & display config.** Book rows carry `author_sorts`/`author_links` parallel to `authors`; `get_entities(kind)` exposes `{id, name, sort, link, count}` for authors/series/publishers/tags/languages; custom columns report `editable`, `normalized` and their decoded `display` JSON (`enum_values`, `enum_colors`, …); and a typed preferences accessor covers everything else (`get_preference`, `get_field_metadata`, `get_user_categories`, `get_tag_browser_state`).
- **Metadata portability.** Read e-reader annotations, per-device reading progress, third-party plugin data, and conversion profiles; sanitize comments HTML for display.
- **Opt-in write path.** `cquarry.write.WritableCalibreDB` offers trigger-safe mutations in a separate module the read-only API can never touch: title, authors (with `author_sort` recomputation), series (+index), publisher, rating (UNIQUE-deduped), languages (canonicalized to ISO codes), tags, identifiers, comments, generic custom-column writes (layout auto-detected, enumerations validated against `display.enum_values`, non-editable columns refused), format registration/removal, `has_cover`, and full book removal with orphan pruning — every mutation queued in `metadata_dirtied` for OPF resync.
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

## Search Grammar

cquarry implements a three-stage pipeline (lexer, recursive-descent parser, candidate-set evaluator) ported from Calibre's `search_query_parser.py` and `calibre/db/search.py`.

### Operators

| Syntax | Meaning |
|--------|---------|
| `and` | Logical AND (also implicit: `title:foo author:bar` is `title:foo and author:bar`) |
| `or` | Logical OR |
| `not` | Logical NOT |
| `( )` | Grouping |

### Match prefixes

| Prefix | Meaning |
|--------|---------|
| *(none)* | Substring match (case- and accent-folded) |
| `=` | Exact match (case- and accent-folded) |
| `~` | Regular expression (stdlib `re`, case-insensitive) |
| `^` | Accent-folded substring |
| `\` | Escape the next character (treat literally) |

### Field locations

| Location | Aliases | Datatype | Notes |
|----------|---------|----------|-------|
| `title` | | text | |
| `title_sort` | | text | |
| `authors` | `author` | text_multi | |
| `author_sort` | | text | |
| `series` | | text | |
| `series_sort` | | text | `"Series [index]"` |
| `publisher` | | text | |
| `tags` | `tag` | hierarchical | Anchored prefix: `Foo` matches `Foo` and `Foo.*` |
| `comments` | `comment` | text | |
| `annotations` | | text | Book's concatenated annotation text; `true`/`false` test presence |
| `rating` | | rating | Numeric; `true`/`false` for presence |
| `series_index` | | float | |
| `formats` | `format` | text_multi | |
| `languages` | `language`, `lang` | text_multi | English names canonicalized to ISO codes |
| `size` | | float (bytes) | Total across formats; `k`/`m`/`g` suffixes |
| `pages` | | int | Native `books_pages_link` first; `#pages` custom column fallback |
| `pubdate` | | date | |
| `timestamp` | `date` | date | |
| `last_modified` | | date | |
| `identifiers` | `identifier`, `ids` | identifiers | Keypair search; see below |
| `isbn` | | identifiers | Shorthand for `identifiers:=isbn:<value>` |
| `cover` | | bool | |
| `id` | | int | |
| `uuid` | | text | |
| `#<label>` | | *(per column)* | Custom columns by label |
| `vl` | | virtual library | Cross-reference: `vl:"Wing Name"` |
| `search` | | saved search | Cross-reference: `search:"Saved Name"` |
| `@Name` | | user category | Books holding any member value: `@Favorites:true`; leading `.` includes subcategories, `false` inverts |
| `all` | *(bare terms)* | | Searches title, authors, author_sort, series, publisher, tags, comments + custom text columns |

Multi-valued locations additionally accept the count operator: `tags:#>3`, `identifiers:#=0`, `formats:#<5`.

### Date queries

```
pubdate:>30daysago
timestamp:<2024-06
last_modified:=today
pubdate:>=yesterday
timestamp:thismonth
pubdate:2024          # matches any date in 2024
pubdate:2024-06       # matches any date in June 2024
pubdate:2024-06-15    # matches that exact day
```

### Identifier queries

```
identifiers:isbn:true          # has any ISBN
identifiers:amazon:B0...       # specific Amazon ASIN
isbn:9780123456789             # shorthand for identifiers:=isbn:9780123456789
identifiers:true               # has any identifier at all
```

### Grouped search terms

Calibre lets users define groups (`preferences.grouped_search_terms`: group name -> member
locations). cquarry resolves them with upstream's semantics:

```
People:leckie        # union over the group's member locations
People:false         # books where NO member matches
```

Real field names always win over same-named groups, and nesting a group inside a group is a
parse error.

### User categories

Calibre lets users define tag-browser pseudo-categories (`preferences.user_categories`).
cquarry searches them with upstream's exact semantics:

```
@Favorites:true      # books holding any member value (exact match per member location)
@Favorites:false     # the inverse
@Favorites:.true     # include subcategories (category names starting with "Favorites.")
```

As in Calibre, any query text other than `false`/a leading `.` is ignored (the GUI always
writes `@Name:true`); groups and real fields win over same-named categories; unknown
`@Names` match nothing.

### Documented deviations from Calibre

- **Regex engine.** `~` uses stdlib `re`, not Calibre's third-party `regex` module (`VERSION1`/`\X` are unavailable; otherwise compatible).
- **Accent folding.** Uses `unicodedata` NFKD decomposition rather than ICU collation, so punctuation-insensitivity is not reproduced.
- **GPM templates.** `@...:` template expressions tokenize for parse parity but are not evaluated.
- **GUI-state locations.** `marked`, `ondevice`, and `in_tag_browser` exist only inside Calibre's own UI session and are not implemented.
- **Hierarchical tag matching.** `tags:` uses cquarry's anchored match (`Foo` matches `Foo` and `Foo.*`) rather than Calibre's raw substring default. This is a long-standing project invariant.
- **`annotations:` matching.** Calibre searches annotations through its FTS tables (with stemming and rank ordering); cquarry matches the concatenated `searchable_text` with ordinary text semantics — same result set for typical queries, no stemming or ranking.
- **`series_sort` format.** Computed as `"Series [index]"`.

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
