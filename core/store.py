from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class DotaStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_bindings (
                    sender_id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    persona_name TEXT NOT NULL DEFAULT '',
                    bound_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bind_account(self, sender_id: str, account_id: int, persona_name: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO user_bindings (sender_id, account_id, persona_name)
                   VALUES (?, ?, ?)
                   ON CONFLICT(sender_id) DO UPDATE SET
                       account_id = excluded.account_id,
                       persona_name = excluded.persona_name,
                       bound_at = datetime('now')""",
                (sender_id, account_id, persona_name),
            )

    def unbind_account(self, sender_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM user_bindings WHERE sender_id = ?", (sender_id,)
            )
            return cursor.rowcount > 0

    def get_bound_account(self, sender_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account_id FROM user_bindings WHERE sender_id = ?", (sender_id,)
            ).fetchone()
            return row["account_id"] if row else None

    def get_binding_info(self, sender_id: str) -> tuple[int, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account_id, persona_name FROM user_bindings WHERE sender_id = ?",
                (sender_id,),
            ).fetchone()
            return (row["account_id"], row["persona_name"]) if row else None
