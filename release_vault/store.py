"""SQLite 放行状态持久层。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    request_id TEXT NOT NULL,
    business_hash TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    evidence_dir TEXT NOT NULL,
    PRIMARY KEY (project_id, stage)
);
"""


@dataclass(frozen=True)
class ReleaseRow:
    """一条放行记录。"""

    project_id: str
    stage: str
    request_id: str
    business_hash: str
    sealed_at: str
    manifest_path: str
    evidence_dir: str


class ReleaseStore:
    """放行记录的 SQLite 存取。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def get_release(self, project_id: str, stage: str) -> Optional[ReleaseRow]:
        cur = self._conn.execute(
            "SELECT project_id, stage, request_id, business_hash, sealed_at,"
            " manifest_path, evidence_dir FROM releases"
            " WHERE project_id = ? AND stage = ?",
            (project_id, stage),
        )
        row = cur.fetchone()
        return ReleaseRow(*row) if row else None

    def list_releases(self, project_id: str) -> List[ReleaseRow]:
        cur = self._conn.execute(
            "SELECT project_id, stage, request_id, business_hash, sealed_at,"
            " manifest_path, evidence_dir FROM releases"
            " WHERE project_id = ? ORDER BY sealed_at, stage",
            (project_id,),
        )
        return [ReleaseRow(*r) for r in cur.fetchall()]

    def count_releases(self, project_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM releases WHERE project_id = ?", (project_id,)
        )
        return int(cur.fetchone()[0])

    def insert_release(self, row: ReleaseRow) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO releases (project_id, stage, request_id,"
                " business_hash, sealed_at, manifest_path, evidence_dir)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.project_id,
                    row.stage,
                    row.request_id,
                    row.business_hash,
                    row.sealed_at,
                    row.manifest_path,
                    row.evidence_dir,
                ),
            )

    def close(self) -> None:
        self._conn.close()
