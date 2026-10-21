"""生成结果缓存 (SQLite)。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("data") / "generation_cache.db"


class GenerationCache:
    def __init__(self, path: str | Path | None = DEFAULT_PATH) -> None:
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generation_cache (
                key         TEXT PRIMARY KEY,
                model       TEXT NOT NULL,
                response    TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT response FROM generation_cache WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def put(self, key: str, response: str, model: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO generation_cache (key, model, response, created_at)"
            " VALUES (?, ?, ?, ?)",
            (key, model, response, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def clear(self) -> None:
        self._conn.execute("DELETE FROM generation_cache")
        self._conn.commit()

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM generation_cache").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()
