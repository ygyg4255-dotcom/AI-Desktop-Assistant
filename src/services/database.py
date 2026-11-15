"""SQLite database service for persistent storage (deprecated in favor of src.storage.db.Database)."""
from pathlib import Path
from src.storage.db import Database


class DatabaseService(Database):
    """Legacy database service wrapper around src.storage.db.Database."""

    def __init__(self, db_path: Path):
        super().__init__(db_path)
