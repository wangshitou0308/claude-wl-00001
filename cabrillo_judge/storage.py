"""SQLite persistence for batches, logs, findings, decisions and versions.

All data lives in a single file (default ``cabrillo_judge.db``).  The module
uses only :mod:`sqlite3` from the standard library.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    rules_json  TEXT NOT NULL,
    locked      INTEGER NOT NULL DEFAULT 0,
    created_ts  INTEGER NOT NULL,
    updated_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    station_call  TEXT,
    raw_text      TEXT NOT NULL,
    parsed_json   TEXT NOT NULL,
    upload_ts     INTEGER NOT NULL,
    UNIQUE(batch_id, filename)
);

CREATE TABLE IF NOT EXISTS findings (
    batch_id   TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL,
    data_json  TEXT NOT NULL,
    PRIMARY KEY (batch_id, finding_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    batch_id      TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    finding_id    TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    fault_station TEXT,
    penalty_code  TEXT,
    reason        TEXT NOT NULL,
    judge         TEXT,
    updated_ts    INTEGER NOT NULL,
    PRIMARY KEY (batch_id, finding_id)
);

CREATE TABLE IF NOT EXISTS versions (
    batch_id     TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    version_no   INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    note         TEXT,
    snapshot_json TEXT NOT NULL,
    created_ts   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, version_no)
);

CREATE TABLE IF NOT EXISTS api_keys (
    -- placeholder table for future offline tokens; unused today
    key TEXT PRIMARY KEY
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> int:
    return int(time.time())


class Storage:
    def __init__(self, path: str = "cabrillo_judge.db"):
        self.path = path
        # ThreadingHTTPServer dispatches each request on its own thread, while
        # all of them share this one connection.  SQLite itself is serialised;
        # the write lock makes multi-statement write transactions atomic from
        # Python's side too.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- batches ----------------------------------------------------------
    def create_batch(self, batch_id: str, name: str,
                     rules: dict[str, Any]) -> dict[str, Any]:
        ts = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO batches (id, name, rules_json, locked, created_ts, "
                "updated_ts) VALUES (?, ?, ?, 0, ?, ?)",
                (batch_id, name, json.dumps(rules, ensure_ascii=False), ts, ts))
            self.conn.commit()
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return self._batch_row(row) if row else None

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM batches ORDER BY created_ts").fetchall()
        return [self._batch_row(r) for r in rows]

    def update_rules(self, batch_id: str, rules: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE batches SET rules_json = ?, updated_ts = ? WHERE id = ?",
                (json.dumps(rules, ensure_ascii=False), _now(), batch_id))
            self.conn.commit()

    def set_locked(self, batch_id: str, locked: bool) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE batches SET locked = ?, updated_ts = ? WHERE id = ?",
                (1 if locked else 0, _now(), batch_id))
            self.conn.commit()

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"],
                "rules": json.loads(row["rules_json"]),
                "locked": bool(row["locked"]),
                "created_ts": row["created_ts"],
                "updated_ts": row["updated_ts"]}

    def require_open(self, batch_id: str) -> None:
        b = self.get_batch(batch_id)
        if b is None:
            raise KeyError(f"批次 {batch_id} 不存在")
        if b["locked"]:
            raise PermissionError(f"批次 {batch_id} 已锁定，"
                                  f"不能修改日志、规则或裁决")

    # -- logs -------------------------------------------------------------
    def add_log(self, log_id: str, batch_id: str, filename: str,
                raw_text: str, parsed: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO logs (id, batch_id, filename, station_call, "
                "raw_text, parsed_json, upload_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (log_id, batch_id, filename,
                 parsed.get("station_call"), raw_text,
                 json.dumps(parsed, ensure_ascii=False), _now()))
            self.conn.commit()

    def list_logs(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, filename, station_call, upload_ts FROM logs "
            "WHERE batch_id = ? ORDER BY upload_ts, filename",
            (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_log(self, log_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM logs WHERE id = ?", (log_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "batch_id": row["batch_id"],
                "filename": row["filename"],
                "station_call": row["station_call"],
                "raw_text": row["raw_text"],
                "parsed": json.loads(row["parsed_json"]),
                "upload_ts": row["upload_ts"]}

    def get_submissions(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM logs WHERE batch_id = ? ORDER BY upload_ts, "
            "filename", (batch_id,)).fetchall()
        return [{"log_id": r["id"], "filename": r["filename"],
                 "station_call": r["station_call"],
                 "parsed": json.loads(r["parsed_json"])} for r in rows]

    # -- findings ---------------------------------------------------------
    def replace_findings(self, batch_id: str,
                         findings: list[dict[str, Any]]) -> None:
        with self._lock:
            with self.conn:
                self.conn.execute("DELETE FROM findings WHERE batch_id = ?",
                                  (batch_id,))
                self.conn.executemany(
                    "INSERT INTO findings (batch_id, finding_id, data_json) "
                    "VALUES (?, ?, ?)",
                    [(batch_id, f["id"], json.dumps(f, ensure_ascii=False))
                     for f in findings])

    def list_findings(self, batch_id: str,
                      status: str | None = None,
                      pending: bool | None = None,
                      station: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT data_json FROM findings WHERE batch_id = ?",
            (batch_id,)).fetchall()
        out = []
        for r in rows:
            f = json.loads(r["data_json"])
            if status and f["status"] != status:
                continue
            if pending is not None and bool(f["pending"]) != pending:
                continue
            if station and station not in f.get("stations", []):
                # one-sided findings list the owner station too
                if not f.get("refs") or \
                        f["refs"][0].get("station") != station:
                    continue
            out.append(f)
        out.sort(key=lambda f: (f["ts_hint"], f["status"], f["id"]))
        return out

    def get_finding(self, batch_id: str, finding_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT data_json FROM findings WHERE batch_id = ? AND "
            "finding_id = ?", (batch_id, finding_id)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def findings_count(self, batch_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM findings WHERE batch_id = ?",
            (batch_id,)).fetchone()["c"]

    # -- decisions --------------------------------------------------------
    def upsert_decision(self, batch_id: str, finding_id: str,
                        resolution: str, reason: str,
                        fault_station: str | None = None,
                        penalty_code: str | None = None,
                        judge: str | None = None) -> dict[str, Any]:
        with self._lock:
            if not reason or not reason.strip():
                raise ValueError("裁决必须填写理由（reason）")
            dec = {"resolution": resolution, "fault_station": fault_station,
                   "penalty_code": penalty_code, "reason": reason.strip(),
                   "judge": judge, "updated_ts": _now()}
            self.conn.execute(
                "INSERT INTO decisions (batch_id, finding_id, resolution, "
                "fault_station, penalty_code, reason, judge, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_id, finding_id) DO UPDATE SET "
                "resolution=excluded.resolution, "
                "fault_station=excluded.fault_station, "
                "penalty_code=excluded.penalty_code, reason=excluded.reason, "
                "judge=excluded.judge, updated_ts=excluded.updated_ts",
                (batch_id, finding_id, resolution, fault_station, penalty_code,
                 reason.strip(), judge, _now()))
            self.conn.commit()
            return dec

    def delete_decision(self, batch_id: str, finding_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM decisions WHERE batch_id = ? AND finding_id = ?",
                (batch_id, finding_id))
            self.conn.commit()

    def list_decisions(self, batch_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE batch_id = ?",
            (batch_id,)).fetchall()
        return {r["finding_id"]:
                {"resolution": r["resolution"],
                 "fault_station": r["fault_station"],
                 "penalty_code": r["penalty_code"],
                 "reason": r["reason"], "judge": r["judge"],
                 "updated_ts": r["updated_ts"]}
                for r in rows}

    # -- versions ---------------------------------------------------------
    def next_version_no(self, batch_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 n FROM versions "
            "WHERE batch_id = ?", (batch_id,)).fetchone()
        return int(row["n"])

    def save_version(self, batch_id: str, version_no: int,
                     content_hash: str, note: str | None,
                     snapshot: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO versions (batch_id, version_no, content_hash, note, "
                "snapshot_json, created_ts) VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, version_no, content_hash, note,
                 json.dumps(snapshot, ensure_ascii=False), _now()))
            self.conn.commit()

    def list_versions(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version_no, content_hash, note, created_ts FROM versions "
            "WHERE batch_id = ? ORDER BY version_no", (batch_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, batch_id: str, version_no: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM versions WHERE batch_id = ? AND version_no = ?",
            (batch_id, version_no)).fetchone()
        if not row:
            return None
        return {"version_no": row["version_no"],
                "content_hash": row["content_hash"], "note": row["note"],
                "created_ts": row["created_ts"],
                "snapshot": json.loads(row["snapshot_json"])}

    def touch_batch(self, batch_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE batches SET updated_ts = ? WHERE id = ?",
                (_now(), batch_id))
            self.conn.commit()
