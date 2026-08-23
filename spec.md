# cquarry — Library Specification

**Version:** 1.0.0  
**Language:** Python 3.14+  
**Dependencies:** None (Pure stdlib)  
**License:** MIT

---

## 1. Mission Statement
Provide a unified, read-only Calibre database and search evaluation engine for the Python ecosystem. By centralizing the parsing of Calibre's complex search grammar, `cquarry` ensures that all external tools (CLI, Web, GTK4) agree exactly on which books match a given query or Virtual Library.

## 2. Architecture

### 2.1 The Database Layer (`db.py`)
- **Connection Management:** Uses `sqlite3` with `mode=ro` URIs.
- **Lock Escapes:** If `SELECT 1 FROM books` raises an OperationalError due to a lock, `_make_snapshot()` creates a tempfile copy of `metadata.db` (including `-wal` and `-shm`) and routes queries to the snapshot.
- **Data Caching:** The result of `get_all_books()` is heavily cached in-memory.

### 2.2 The Search Engine (`search.py`)
A three-stage pipeline (Lexer -> Parser -> Evaluator).
1. **Lexer:** Tokenizes quoted strings, boolean keywords, and relational operators.
2. **Parser:** Builds an AST respecting precedence (implicit AND, explicit AND, OR, NOT, Parentheses).
3. **Evaluator:** Traverses the AST against the cached dictionary of books. It implements Calibre-specific type coercion:
   - `DT_DATE`: parses `>7daysago`, `<today`, `2024-05`.
   - `DT_BOOL`: true/false/yes/no mapping.
   - `DT_TEXT_MULTI`: hierarchy rules for tags vs substring rules for authors.
