"""
Initialize the SQLite database and required directories.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.sqlite_store import SQLiteStore
from src.utils.config import Config


REQUIRED_CONFIG_KEYS: Iterable[str] = (
    "storage.vault_dir",
    "storage.db_path",
    "storage.vector_index_dir",
)
REQUIRED_TABLES: Iterable[str] = (
    "knowledge_items",
    "content_chunks",
    "tags",
    "knowledge_tags",
    "video_timestamps",
    "knowledge_items_fts",
)


def validate_config(config: Config) -> None:
    """Validate required config keys.

    Args:
        config: Loaded configuration instance.
    """
    missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise ValueError(f"Missing required config keys: {', '.join(missing)}")


def ensure_directories(config: Config) -> Dict[str, Path]:
    """Create required directories and return key paths.

    Args:
        config: Loaded configuration instance.

    Returns:
        Mapping of directory labels to absolute paths.
    """
    config.ensure_dirs()
    return {
        "db_dir": config.db_path.parent,
        "vault_dir": config.vault_dir,
        "vector_dir": config.vector_index_dir,
    }


def initialize_database(config: Config) -> Dict[str, bool]:
    """Initialize SQLite schema and return table existence checks.

    Args:
        config: Loaded configuration instance.

    Returns:
        Mapping of table names to existence flags.
    """
    store = SQLiteStore(db_path=config.db_path)
    store.initialize()
    return {name: store.table_exists(name) for name in REQUIRED_TABLES}


def main() -> int:
    """Run database initialization.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    print("=" * 60)
    print("Personal Knowledge Vault - Init DB")
    print("=" * 60)

    try:
        config = Config()
        validate_config(config)

        dirs = ensure_directories(config)
        table_status = initialize_database(config)
        missing_tables = [name for name, ok in table_status.items() if not ok]
        if missing_tables:
            raise RuntimeError(f"Database missing tables: {', '.join(missing_tables)}")

        print("\nInitialization results:")
        print(f"  Config: {PROJECT_ROOT / 'config' / 'config.yaml'}")
        print(f"  Database: {config.db_path}")
        for key, path in dirs.items():
            print(f"  {key}: {path}")
        print(f"  Tables: {', '.join(REQUIRED_TABLES)}")
        print("  Status: OK")
    except Exception as exc:
        print(f"\nInitialization failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
