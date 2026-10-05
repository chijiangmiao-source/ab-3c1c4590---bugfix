"""Durable state for the control service (SQLite, WAL, fully synchronous).

The release intent (release_id -> sha256 + bytes) is immutable once inserted;
only the derived state and the repo receipts are ever appended afterwards.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from app.common.receipts import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  release_id TEXT PRIMARY KEY,
  sha256     TEXT NOT NULL,
  size       INTEGER NOT NULL,
  artifact   BLOB NOT NULL,
  state      TEXT NOT NULL,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  release_id TEXT NOT NULL,
  repo       TEXT NOT NULL,
  op         TEXT NOT NULL,
  op_key     TEXT NOT NULL,
  digest     TEXT NOT NULL,
  receipt    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (release_id, repo, op)
);
"""


class Store:
    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(SCHEMA)
            # Removed: receipts are scoped to a release-derived op key and must
            # never be shared across releases even when digests are identical.
            self._db.execute("DROP TABLE IF EXISTS receipt_cache")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- releases ----
    def insert_release(self, release_id: str, sha256: str, artifact: bytes, state: str) -> None:
        now = utcnow()
        with self._lock:
            self._db.execute(
                "INSERT INTO releases(release_id, sha256, size, artifact, state, error,"
                " created_at, updated_at) VALUES(?,?,?,?,?,NULL,?,?)",
                (release_id, sha256, len(artifact), artifact, state, now, now),
            )
            self._db.commit()

    def get_release(self, release_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_releases(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT release_id, sha256, size, state, error, created_at, updated_at"
                " FROM releases ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def release_ids_excluding(self, excluded_states) -> list[str]:
        if not excluded_states:
            marks = ""
            params: tuple = ()
        else:
            marks = " WHERE state NOT IN (" + ",".join("?" for _ in excluded_states) + ")"
            params = tuple(excluded_states)
        with self._lock:
            rows = self._db.execute(
                f"SELECT release_id FROM releases{marks} ORDER BY created_at",
                params,
            ).fetchall()
        return [r[0] for r in rows]

    def update_state(self, release_id: str, state: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE releases SET state=?, error=?, updated_at=? WHERE release_id=?",
                (state, error, utcnow(), release_id),
            )
            self._db.commit()

    # ---- receipts (repo-side evidence) ----
    def put_receipt(self, release_id: str, repo: str, op: str, op_key: str,
                    digest: str, receipt: dict) -> None:
        encoded = json.dumps(receipt, ensure_ascii=False)
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO receipts(release_id, repo, op, op_key, digest,"
                " receipt, created_at) VALUES(?,?,?,?,?,?,?)",
                (release_id, repo, op, op_key, digest, encoded, utcnow()),
            )
            self._db.commit()

    def get_receipt(self, release_id: str, repo: str, op: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT receipt FROM receipts WHERE release_id=? AND repo=? AND op=?",
                (release_id, repo, op),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_receipt(self, release_id: str, repo: str, op: str) -> None:
        """Drop evidence that does not belong to this release's derived op key."""
        with self._lock:
            self._db.execute(
                "DELETE FROM receipts WHERE release_id=? AND repo=? AND op=?",
                (release_id, repo, op),
            )
            self._db.commit()

    def receipts_for(self, release_id: str) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT repo, op, receipt FROM receipts WHERE release_id=?",
                (release_id,),
            ).fetchall()
        out: dict = {}
        for repo, op, receipt in rows:
            out.setdefault(repo, {})[op] = json.loads(receipt)
        return out
