"""Persistent execution records; knowledge nodes remain in StateStore."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class TaskStoreError(RuntimeError):
    pass


class RequestConflict(TaskStoreError):
    pass


class TaskStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.vault_root = self.state_dir.parents[2]
        self.path = self.state_dir / "tasks.sqlite3"
        try:
            with self._connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY, payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS requests (
                        owner TEXT NOT NULL, request_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
                        PRIMARY KEY(owner, request_id)
                    );
                    CREATE TABLE IF NOT EXISTS controls (
                        owner TEXT NOT NULL, request_id TEXT NOT NULL,
                        task_id TEXT NOT NULL, action TEXT NOT NULL,
                        PRIMARY KEY(owner, request_id)
                    );
                """)
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError("任务账本损坏或无法打开，已停止写入") from exc

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError) as exc:
            raise TaskStoreError("任务记录损坏，不能按空记录继续") from exc

    def get(self, task_id: str) -> dict | None:
        try:
            with self._connect() as db:
                return self._decode(db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone())
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError("任务账本读取失败") from exc

    def all(self) -> list[dict]:
        try:
            with self._connect() as db:
                return [self._decode(row) for row in db.execute("SELECT payload FROM tasks ORDER BY rowid")]
        except sqlite3.DatabaseError as exc:
            raise TaskStoreError("任务账本读取失败") from exc

    def request(self, owner: str, request_id: str, fingerprint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT fingerprint, task_id FROM requests WHERE owner=? AND request_id=?",
                             (owner, request_id)).fetchone()
            if not row:
                return None
            if row[0] != fingerprint:
                raise RequestConflict("相同请求标识对应不同内容或配置")
            return self.get(row[1])

    def create(self, task: dict, fingerprint: str) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT fingerprint, task_id FROM requests WHERE owner=? AND request_id=?",
                                 (task["owner"], task["request_id"])).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise RequestConflict("相同请求标识对应不同内容或配置")
                    existing = self._decode(db.execute("SELECT payload FROM tasks WHERE id=?", (row[1],)).fetchone())
                    db.commit()
                    return existing
                db.execute("INSERT INTO tasks VALUES (?, ?)",
                           (task["task_id"], json.dumps(task, ensure_ascii=False)))
                db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                           (task["owner"], task["request_id"], fingerprint, task["task_id"]))
                db.commit()
                return task
            except Exception:
                db.rollback()
                raise

    def update(self, task_id: str, mutate) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                task = self._decode(db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone())
                if task is None:
                    raise TaskStoreError("任务不存在")
                mutate(task)
                db.execute("UPDATE tasks SET payload=? WHERE id=?",
                           (json.dumps(task, ensure_ascii=False), task_id))
                db.commit()
                return task
            except Exception:
                db.rollback()
                raise

    def control(self, task_id: str, owner: str, request_id: str, action: str, mutate) -> dict:
        """Idempotent cancel/retry and its state change share a transaction."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT task_id, action FROM controls WHERE owner=? AND request_id=?",
                                   (owner, request_id)).fetchone()
                if prior and prior != (task_id, action):
                    raise RequestConflict("控制请求标识已用于另一操作")
                task = self._decode(db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone())
                if task is None:
                    raise TaskStoreError("任务不存在")
                if not prior:
                    mutate(task)
                    db.execute("INSERT INTO controls VALUES (?, ?, ?, ?)",
                               (owner, request_id, task_id, action))
                    db.execute("UPDATE tasks SET payload=? WHERE id=?",
                               (json.dumps(task, ensure_ascii=False), task_id))
                db.commit()
                return task
            except Exception:
                db.rollback()
                raise
