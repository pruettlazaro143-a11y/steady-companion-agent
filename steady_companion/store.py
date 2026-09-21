"""Local, explicit memories and opt-in reminders; no transcript storage.

The SQLite file is for one local user. It is not an encrypted vault or a
multi-user service. Reminders are delivered only when the CLI polls them.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
from typing import Iterator


MEMORY_KINDS = frozenset({"fact", "preference", "boundary", "goal"})
NOTE_KINDS = frozenset({"moment", "thread", "style"})
NOTE_SOURCES = frozenset({"user_command", "approved_learning"})


class StaleRevisionError(ValueError):
    """A proposed note refers to state that has since changed."""


def _note_text(value: str) -> str:
    text = _text(value)
    if len(text) > 1200:
        raise ValueError("相处记录不能超过 1200 个字符。")
    return text


def _note_field(value: str, limit: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name}必须是长度不超过 {limit} 个字符的文本。")
    # Preserve evidence verbatim; its exact original excerpt is approved by
    # the caller, and is not an automatically inferred psychological label.
    return value


def _timestamp(value: str | None = None) -> str:
    if value is None:
        moment = datetime.now(timezone.utc)
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("时间必须是包含时区的 ISO 8601 字符串。")
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("时间格式无效，例如 2030-01-02T18:00:00+08:00。") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("时间必须包含时区，例如 +08:00 或 Z。")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("编号必须是正整数。")
    return value


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("内容不能为空。")
    return value.strip()


class Store:
    """SQLite-backed state; each operation sees changes made by other processes."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser()
        if self.data_dir.is_symlink():
            raise ValueError("数据目录不能是符号链接。")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / "companion.sqlite3"
        self._restrict(self.data_dir, 0o700)
        # Do not follow an existing DB symlink to another person's data.
        if self.db_path.is_symlink():
            raise ValueError("数据库路径不能是符号链接。")
        if not self.db_path.exists():
            try:
                descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                # Another local process may have created it after our check.
                if self.db_path.is_symlink():
                    raise ValueError("数据库路径不能是符号链接。")
            else:
                os.close(descriptor)
        self._restrict(self.db_path, 0o600)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value) VALUES ('revision', 0);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('fact', 'preference', 'boundary', 'goal')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'delivered', 'cancelled')),
                    memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, at);
                CREATE INDEX IF NOT EXISTS reminders_memory ON reminders(memory_id);
                CREATE TABLE IF NOT EXISTS companion_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('moment', 'thread', 'style')),
                    text TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL
                        CHECK(source IN ('user_command', 'approved_learning')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            # Additive migration: old rows and IDs stay intact; no auto opt-in.
            for table, fields in {
                'memories': {'source': 'user_command', 'scope': '仅记录原文指定范围', 'status': 'active'},
                'companion_notes': {'status': 'active'},
            }.items():
                existing = {row[1] for row in conn.execute('PRAGMA table_info('+table+')')}
                for name, default in fields.items():
                    if name not in existing:
                        conn.execute("ALTER TABLE "+table+" ADD COLUMN "+name+" TEXT NOT NULL DEFAULT '"+default+"'")

    def close(self) -> None:
        """Compatibility helper; connections are already closed per operation."""

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @staticmethod
    def _restrict(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            # Some filesystems/Windows do not implement POSIX permissions.
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("PRAGMA journal_mode=DELETE")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _bump(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")

    @staticmethod
    def _redact_linked(conn: sqlite3.Connection, memory_id: int | None, now: str) -> None:
        # Keep status bookkeeping without retaining information that was corrected
        # or forgotten. A pending reminder must never deliver stale information.
        where = "memory_id IS NOT NULL" if memory_id is None else "memory_id=?"
        params = (now, now) if memory_id is None else (now, now, memory_id)
        conn.execute(
            "UPDATE reminders SET text='', "
            "cancelled_at=CASE WHEN status='pending' THEN ? ELSE cancelled_at END, "
            "status=CASE WHEN status='pending' THEN 'cancelled' ELSE status END, "
            "updated_at=? WHERE " + where,
            params,
        )

    def revision(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])

    def list_memories(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM memories ORDER BY id")]

    def remember(self, text: str, kind: str = "fact") -> dict:
        text = _text(text)
        if not isinstance(kind, str) or kind not in MEMORY_KINDS:
            raise ValueError("记忆类型必须是 fact、preference、boundary 或 goal。")
        now = _timestamp()
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO memories(kind,text,created_at,updated_at) VALUES (?,?,?,?)",
                (kind, text, now, now),
            )
            self._bump(conn)
            return dict(conn.execute("SELECT * FROM memories WHERE id=?", (cursor.lastrowid,)).fetchone())

    def correct(self, id: int, text: str) -> dict:
        id, text, now = _identifier(id), _text(text), _timestamp()
        with self._transaction() as conn:
            cursor = conn.execute("UPDATE memories SET text=?,updated_at=? WHERE id=?", (text, now, id))
            if not cursor.rowcount:
                raise ValueError("找不到这个记忆编号。")
            self._redact_linked(conn, id, now)
            self._bump(conn)
            return dict(conn.execute("SELECT * FROM memories WHERE id=?", (id,)).fetchone())

    def forget(self, id: int) -> bool:
        id, now = _identifier(id), _timestamp()
        with self._transaction() as conn:
            if conn.execute("SELECT id FROM memories WHERE id=?", (id,)).fetchone() is None:
                return False
            self._redact_linked(conn, id, now)
            conn.execute("DELETE FROM memories WHERE id=?", (id,))
            self._bump(conn)
            return True

    def forget_all(self) -> int:
        with self._transaction() as conn:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count:
                self._redact_linked(conn, None, _timestamp())
                conn.execute("DELETE FROM memories")
                self._bump(conn)
            return int(count)

    def list_notes(self) -> list[dict]:
        """Return approved shared moments, unfinished topics, and style notes."""
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM companion_notes ORDER BY id")]

    def remember_note(
        self,
        text: str,
        kind: str,
        *,
        scope: str = "",
        evidence: str = "",
        source: str = "user_command",
        expected_revision: int | None = None,
    ) -> dict:
        """Save one explicitly approved note, never an unapproved proposal.

        The CLI or host owns the approval step. When a proposal was prepared
        from earlier context, expected_revision guards against later changes
        under the same write lock as the insertion.
        """
        text = _note_text(text)
        if not isinstance(kind, str) or kind not in NOTE_KINDS:
            raise ValueError("相处记录类型必须是 moment、thread 或 style。")
        scope = _note_field(scope, 240, "适用范围")
        evidence = _note_field(evidence, 300, "依据原话")
        if not isinstance(source, str) or source not in NOTE_SOURCES:
            raise ValueError("记录来源必须是 user_command 或 approved_learning。")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("预期状态版本必须是非负整数。")
        now = _timestamp()
        with self._transaction() as conn:
            revision = int(conn.execute(
                "SELECT value FROM metadata WHERE key='revision'"
            ).fetchone()[0])
            if expected_revision is not None and revision != expected_revision:
                raise StaleRevisionError("记录依据的状态已变化，请重新查看后再确认保存。")
            cursor = conn.execute(
                "INSERT INTO companion_notes(kind,text,scope,evidence,source,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind, text, scope, evidence, source, now, now),
            )
            self._bump(conn)
            return dict(conn.execute(
                "SELECT * FROM companion_notes WHERE id=?", (cursor.lastrowid,)
            ).fetchone())

    def correct_note(self, id: int, text: str, *, scope: str | None = None) -> dict:
        """Replace a note and remove its outdated source excerpt."""
        id, text, now = _identifier(id), _note_text(text), _timestamp()
        if scope is not None:
            scope = _note_field(scope, 240, "适用范围")
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE companion_notes SET text=?,scope=COALESCE(?,scope),evidence='',"
                "source='user_command',updated_at=? WHERE id=?",
                (text, scope, now, id),
            )
            if not cursor.rowcount:
                raise ValueError("找不到这个相处记录编号。")
            self._bump(conn)
            return dict(conn.execute("SELECT * FROM companion_notes WHERE id=?", (id,)).fetchone())

    def forget_note(self, id: int) -> bool:
        id = _identifier(id)
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM companion_notes WHERE id=?", (id,))
            if not cursor.rowcount:
                return False
            self._bump(conn)
            return True

    def schedule(self, text: str, at_iso: str, memory_id: int | None = None) -> dict:
        text, at, now = _text(text), _timestamp(at_iso), _timestamp()
        if at <= now:
            raise ValueError("提醒时间必须在未来。")
        if memory_id is not None:
            memory_id = _identifier(memory_id)
        with self._transaction() as conn:
            if memory_id is not None and conn.execute(
                "SELECT id FROM memories WHERE id=?", (memory_id,)
            ).fetchone() is None:
                raise ValueError("找不到要关联的记忆编号。")
            cursor = conn.execute(
                "INSERT INTO reminders(text,at,memory_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                (text, at, memory_id, now, now),
            )
            self._bump(conn)
            return dict(conn.execute("SELECT * FROM reminders WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_reminders(self) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM reminders ORDER BY at,id")]

    def cancel_reminder(self, id: int) -> bool:
        id, now = _identifier(id), _timestamp()
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET status='cancelled',cancelled_at=?,updated_at=? "
                "WHERE id=? AND status='pending'", (now, now, id),
            )
            if cursor.rowcount:
                self._bump(conn)
                return True
            return False

    def due_reminders(self, now_iso: str | None = None) -> list[dict]:
        now = _timestamp(now_iso)
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM reminders WHERE status='pending' AND at<=? ORDER BY at,id", (now,)
            )]

    def deliver_reminder(self, id: int, now_iso: str | None = None) -> dict | None:
        """Atomically claim one due reminder; cancellation is checked again.

        The caller may print the returned value once. This is an at-most-once
        claim, not guaranteed delivery: a crash before printing can lose it.
        """
        id, now = _identifier(id), _timestamp(now_iso)
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET status='delivered',delivered_at=?,updated_at=? "
                "WHERE id=? AND status='pending' AND at<=?", (now, now, id, now),
            )
            if not cursor.rowcount:
                return None
            self._bump(conn)
            return dict(conn.execute("SELECT * FROM reminders WHERE id=?", (id,)).fetchone())

    def wipe_all(self) -> dict:
        """Delete all local remembered content, companion notes, and reminders."""
        with self._transaction() as conn:
            counts = {
                "memories": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
            }
            note_count = conn.execute("SELECT COUNT(*) FROM companion_notes").fetchone()[0]
            if note_count:
                counts["companion_notes"] = note_count
            conn.execute("DELETE FROM reminders")
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM companion_notes")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='p1_memories'").fetchone():
                conn.execute("DELETE FROM p1_memories")
                conn.execute("UPDATE p1_settings SET value='manual' WHERE key='memory_mode'")
                conn.execute("UPDATE p1_settings SET value='off' WHERE key IN ('memory_assist','context_summary')")
            self._bump(conn)
            return counts
