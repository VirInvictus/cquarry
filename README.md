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
