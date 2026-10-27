from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vless_reality.home import restrict_dir, restrict_file


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class User:
    id: int
    name: str
    uuid: str
    enabled: bool
    created_at: str


@dataclass(frozen=True)
class OutboxItem:
    id: int
    body: str


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        restrict_dir(path.parent)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._init()
        restrict_file(path)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _init(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                uuid TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def bump_reload(self) -> int:
        with self._lock:
            current = self._db.execute(
                "SELECT value FROM meta WHERE key = 'reload_generation'"
            ).fetchone()
            generation = int(current["value"]) + 1 if current else 1
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES('reload_generation', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(generation),),
            )
            self._db.commit()
        return generation

    def generation(self) -> int:
        raw = self.get("reload_generation")
        return int(raw) if raw else 0

    def add_user(self, name: str, user_uuid: str) -> User:
        with self._lock:
            try:
                cursor = self._db.execute(
                    "INSERT INTO users(name, uuid, enabled, created_at) VALUES(?, ?, 1, ?)",
                    (name, user_uuid, utc_now()),
                )
                self._db.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(name) from exc
            row = self._db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _user(row)

    def remove_user(self, name: str) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM users WHERE name = ?", (name,))
            self._db.commit()
        return cursor.rowcount > 0

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE users SET enabled = ? WHERE name = ?",
                (1 if enabled else 0, name),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def user(self, name: str) -> User | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        return None if row is None else _user(row)

    def users(self) -> list[User]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY name").fetchall()
        return [_user(row) for row in rows]

    def enqueue(self, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO outbox(body, created_at) VALUES(?, ?)",
                (body, utc_now()),
            )
            self._db.commit()

    def outbox(self) -> list[OutboxItem]:
        with self._lock:
            rows = self._db.execute("SELECT id, body FROM outbox ORDER BY id").fetchall()
        return [OutboxItem(id=int(row["id"]), body=str(row["body"])) for row in rows]

    def drop_outbox(self, item_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))
            self._db.commit()

    def admin_ids(self) -> tuple[int, ...]:
        raw = self.get("admin_ids")
        if not raw:
            return ()
        data = json.loads(raw)
        return tuple(int(item) for item in data)

    def set_admin_ids(self, ids: list[int]) -> None:
        unique = sorted(set(ids))
        self.set("admin_ids", json.dumps(unique))


def _user(row: sqlite3.Row) -> User:
    return User(
        id=int(row["id"]),
        name=str(row["name"]),
        uuid=str(row["uuid"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
    )
