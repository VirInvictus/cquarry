"""Default path configuration and the saved database-path store.

Carries the version constant, the auto-discovery candidate paths, and the
``~/.config/cquarry/config.json`` read/write pair that ``find_db()`'s
resolution chain sits on.
"""

import json
import os
import sys

VERSION = "1.23.2"

DEFAULT_DB_PATHS = [
    "metadata.db",
    os.path.expanduser("~/Calibre Library/metadata.db"),
    os.path.expanduser("~/calibre/metadata.db"),
]

CALIBRE_RATING_SCALE = 2  # Calibre stores rating * 2 (so 5 stars = 10)

CONFIG_FILE = os.path.expanduser("~/.config/cquarry/config.json")


def load_config() -> dict:
    """Load the config file; ``{}`` on a missing or corrupt file (noted)."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # A broken config must not silently look like "no config".
            print(
                f"NOTE: ignoring unreadable config {CONFIG_FILE}: {e}", file=sys.stderr
            )
    return {}


def save_config(config: dict) -> None:
    """Write the config dict to disk, creating parent directories as needed."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def get_db_path() -> str | None:
    """The saved ``db_path``, or None when no config carries one."""
    return load_config().get("db_path")


def set_db_path(path: str) -> None:
    """Persist an absolute, expanded ``db_path`` to the config."""
    config = load_config()
    config["db_path"] = os.path.abspath(os.path.expanduser(path))
    save_config(config)
