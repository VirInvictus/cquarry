import json
import os

VERSION = "1.4.0"

DEFAULT_DB_PATHS = [
    "metadata.db",
    os.path.expanduser("~/Calibre Library/metadata.db"),
    os.path.expanduser("~/calibre/metadata.db"),
]

CALIBRE_RATING_SCALE = 2  # Calibre stores rating * 2 (so 5 stars = 10)

CONFIG_FILE = os.path.expanduser("~/.config/cquarry/config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def get_db_path() -> str | None:
    return load_config().get("db_path")


def set_db_path(path: str) -> None:
    config = load_config()
    config["db_path"] = os.path.abspath(os.path.expanduser(path))
    save_config(config)
