"""放行状态的 SQLite 存储。"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    project_id    TEXT NOT NULL,
    stage         TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    sealed_at     TEXT NOT NULL,
    PRIMARY KEY (project_id, stage)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_releases_request
    ON releases (project_id, request_id);
"""

_COLUMNS = "project_id, stage, request_id, content_hash, manifest_path, sealed_at"


class ReleaseStore:
    """保存每个项目各阶段的放行记录（每阶段最多一条）。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        return {
            "project_id": row["project_id"],
            "stage": row["stage"],
            "request_id": row["request_id"],
            "content_hash": row["content_hash"],
            "manifest_path": row["manifest_path"],
            "sealed_at": row["sealed_at"],
        }

    def get_release(self, project_id: str, stage: str) -> Optional[dict]:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM releases WHERE project_id=? AND stage=?",
            (project_id, stage),
        ).fetchone()
        return self._to_dict(row) if row else None

    def find_by_request_id(self, project_id: str, request_id: str) -> Optional[dict]:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM releases WHERE project_id=? AND request_id=?",
            (project_id, request_id),
        ).fetchone()
        return self._to_dict(row) if row else None

    def list_releases(self, project_id: str) -> List[dict]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM releases WHERE project_id=?",
            (project_id,),
        ).fetchall()
        return [self._to_dict(r) for r in rows]

    def insert_release(
        self,
        project_id: str,
        stage: str,
        request_id: str,
        content_hash: str,
        manifest_path: str,
        sealed_at: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO releases (project_id, stage, request_id, content_hash,"
                " manifest_path, sealed_at) VALUES (?,?,?,?,?,?)",
                (project_id, stage, request_id, content_hash, manifest_path, sealed_at),
            )
