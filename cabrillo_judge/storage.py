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

from . import appeals as _appeals
from . import feedback as _feedback
from .engine import content_hash

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

CREATE TABLE IF NOT EXISTS clock_schemes (
    id                 TEXT PRIMARY KEY,
    batch_id           TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    reference_log_id   TEXT NOT NULL,
    max_window_seconds INTEGER NOT NULL,
    offsets_json       TEXT NOT NULL,
    analysis_json      TEXT,
    active             INTEGER NOT NULL DEFAULT 0,
    created_ts         INTEGER NOT NULL,
    updated_ts         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_archive (
    id             TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    finding_id     TEXT NOT NULL,
    decision_json  TEXT NOT NULL,
    archive_reason TEXT NOT NULL,
    scheme_id      TEXT,
    archived_ts    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_reports (
    -- 站级赛后反馈包：每次生成都是不可变快照；发布状态与替代关系在此记录
    id                  TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    station_call        TEXT NOT NULL,
    version_no          INTEGER NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'normal',
    status              TEXT NOT NULL DEFAULT 'draft',
    content_hash        TEXT NOT NULL,
    corrects_report_id  TEXT,
    corrects_version_no INTEGER,
    superseded_by       TEXT,
    report_json         TEXT NOT NULL,
    external_json       TEXT,
    previewed_ts        INTEGER,
    published_ts        INTEGER,
    created_ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    -- placeholder table for future offline tokens; unused today
    key TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS appeal_cases (
    -- 赛后复议案件：对已发布反馈包的异议；绑定反馈包/计分版本与内容哈希
    id                       TEXT PRIMARY KEY,
    batch_id                 TEXT NOT NULL REFERENCES batches(id)
                             ON DELETE CASCADE,
    station_call             TEXT NOT NULL,
    report_id                TEXT NOT NULL,
    version_no               INTEGER NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'submitted',
    applicant                TEXT,
    judge                    TEXT,
    version_content_hash     TEXT NOT NULL,
    report_content_hash      TEXT NOT NULL,
    binding_content_hash     TEXT NOT NULL,
    -- 确认改判后一次性冻结的新版本（无改判/无分差时为 NULL）
    resolved_version_no      INTEGER,
    correction_report_id     TEXT,
    score_before             INTEGER,
    score_after              INTEGER,
    received_ts              INTEGER NOT NULL,
    accepted_ts              INTEGER,
    withdrawn_ts             INTEGER,
    closed_ts                INTEGER
);

CREATE TABLE IF NOT EXISTS appeal_claims (
    id             TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES appeal_cases(id)
                   ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    subject        TEXT NOT NULL,
    summary        TEXT NOT NULL,
    finding_id     TEXT,
    log_refs_json  TEXT NOT NULL DEFAULT '[]',
    -- 受理后逐项填写的结论
    conclusion     TEXT,
    resolution     TEXT,
    fault_station  TEXT,
    penalty_code   TEXT,
    rationale      TEXT,
    judge          TEXT,
    UNIQUE(case_id, seq)
);

CREATE TABLE IF NOT EXISTS appeal_events (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES appeal_cases(id)
                ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    actor       TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_ts  INTEGER NOT NULL,
    UNIQUE(case_id, seq)
);

CREATE TABLE IF NOT EXISTS appeal_previews (
    -- 服务端为案件实际生成的最新一次处理预览（单案一行）。
    -- 确认时只认可本服务端落库的预览，客户端不得自行提交 digest 绕过。
    case_id              TEXT PRIMARY KEY REFERENCES appeal_cases(id)
                         ON DELETE CASCADE,
    rulings_json         TEXT NOT NULL,
    ruling_digest        TEXT NOT NULL,
    binding_fingerprint  TEXT NOT NULL,
    version_no           INTEGER NOT NULL,
    version_content_hash TEXT NOT NULL,
    report_content_hash  TEXT NOT NULL,
    score_before         INTEGER NOT NULL,
    score_after          INTEGER NOT NULL,
    created_ts           INTEGER NOT NULL
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
                                  f"不能修改日志、规则、裁决或校正方案")

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
        # upload_ts 相同（同一秒上传）时按插入顺序（rowid）排列，
        # 保证"最近上传"语义稳定
        rows = self.conn.execute(
            "SELECT * FROM logs WHERE batch_id = ? ORDER BY upload_ts, "
            "rowid", (batch_id,)).fetchall()
        return [{"log_id": r["id"], "filename": r["filename"],
                 "station_call": r["station_call"],
                 "upload_ts": r["upload_ts"],
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

    # -- clock-skew correction schemes --------------------------------------
    def create_clock_scheme(self, scheme_id: str, batch_id: str, name: str,
                            reference_log_id: str, max_window_seconds: int,
                            offsets: dict[str, int],
                            analysis: dict[str, Any] | None
                            ) -> dict[str, Any]:
        ts = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO clock_schemes (id, batch_id, name, "
                "reference_log_id, max_window_seconds, offsets_json, "
                "analysis_json, active, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (scheme_id, batch_id, name, reference_log_id,
                 int(max_window_seconds),
                 json.dumps(offsets, ensure_ascii=False),
                 json.dumps(analysis, ensure_ascii=False)
                 if analysis is not None else None, ts, ts))
            self.conn.commit()
        return self.get_clock_scheme(scheme_id)

    @staticmethod
    def _scheme_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "batch_id": row["batch_id"],
                "name": row["name"],
                "reference_log_id": row["reference_log_id"],
                "max_window_seconds": row["max_window_seconds"],
                "offsets": json.loads(row["offsets_json"]),
                "analysis": (json.loads(row["analysis_json"])
                             if row["analysis_json"] else None),
                "active": bool(row["active"]),
                "created_ts": row["created_ts"],
                "updated_ts": row["updated_ts"]}

    def get_clock_scheme(self, scheme_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM clock_schemes WHERE id = ?",
            (scheme_id,)).fetchone()
        return self._scheme_row(row) if row else None

    def list_clock_schemes(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM clock_schemes WHERE batch_id = ? "
            "ORDER BY created_ts, id", (batch_id,)).fetchall()
        return [self._scheme_row(r) for r in rows]

    def get_active_clock_scheme(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM clock_schemes WHERE batch_id = ? AND active = 1",
            (batch_id,)).fetchone()
        return self._scheme_row(row) if row else None

    def update_clock_scheme(self, scheme_id: str,
                            name: str | None = None,
                            offsets: dict[str, int] | None = None,
                            analysis: dict[str, Any] | None = None) -> None:
        sets = ["updated_ts = ?"]
        args: list[Any] = [_now()]
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        if offsets is not None:
            sets.append("offsets_json = ?")
            args.append(json.dumps(offsets, ensure_ascii=False))
        if analysis is not None:
            sets.append("analysis_json = ?")
            args.append(json.dumps(analysis, ensure_ascii=False))
        args.append(scheme_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE clock_schemes SET {', '.join(sets)} WHERE id = ?",
                args)
            self.conn.commit()

    def set_active_clock_scheme(self, batch_id: str,
                                scheme_id: str | None) -> None:
        """启用一个方案（同时停用本批次其他方案）；None 表示全部停用。"""
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE clock_schemes SET active = 0, updated_ts = ? "
                    "WHERE batch_id = ?", (_now(), batch_id))
                if scheme_id is not None:
                    self.conn.execute(
                        "UPDATE clock_schemes SET active = 1, updated_ts = ? "
                        "WHERE id = ? AND batch_id = ?",
                        (_now(), scheme_id, batch_id))

    def delete_clock_scheme(self, scheme_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM clock_schemes WHERE id = ?",
                              (scheme_id,))
            self.conn.commit()

    # -- archived decisions (pending re-review) ------------------------------
    def archive_decision(self, archive_id: str, batch_id: str,
                         finding_id: str, decision: dict[str, Any],
                         reason: str, scheme_id: str | None
                         ) -> dict[str, Any]:
        """把失去依据的裁决从 decisions 移入 decision_archive（同一事务）。"""
        ts = _now()
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO decision_archive (id, batch_id, finding_id, "
                    "decision_json, archive_reason, scheme_id, archived_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (archive_id, batch_id, finding_id,
                     json.dumps(decision, ensure_ascii=False),
                     reason, scheme_id, ts))
                self.conn.execute(
                    "DELETE FROM decisions WHERE batch_id = ? AND "
                    "finding_id = ?", (batch_id, finding_id))
        return {"id": archive_id, "finding_id": finding_id,
                "decision": decision, "archive_reason": reason,
                "scheme_id": scheme_id, "archived_ts": ts}

    def list_archived_decisions(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decision_archive WHERE batch_id = ? "
            "ORDER BY archived_ts, id", (batch_id,)).fetchall()
        return [{"id": r["id"], "finding_id": r["finding_id"],
                 "decision": json.loads(r["decision_json"]),
                 "archive_reason": r["archive_reason"],
                 "scheme_id": r["scheme_id"],
                 "archived_ts": r["archived_ts"]} for r in rows]

    def delete_archived_decision(self, batch_id: str, archive_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM decision_archive WHERE batch_id = ? AND id = ?",
                (batch_id, archive_id))
            self.conn.commit()
            return cur.rowcount > 0

    # -- feedback reports (站级赛后反馈包) ----------------------------------
    def create_feedback_report(self, report_id: str, batch_id: str,
                               station: str, version_no: int, kind: str,
                               corrects_report_id: str | None,
                               corrects_version_no: int | None,
                               content_hash: str,
                               report: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.conn.execute(
                "INSERT INTO feedback_reports (id, batch_id, station_call, "
                "version_no, kind, status, content_hash, corrects_report_id, "
                "corrects_version_no, report_json, created_ts) "
                "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
                (report_id, batch_id, station, int(version_no), kind,
                 content_hash, corrects_report_id, corrects_version_no,
                 json.dumps(report, ensure_ascii=False), _now()))
            self.conn.commit()
        return self.get_feedback_report(report_id)

    @staticmethod
    def _feedback_row(row: sqlite3.Row,
                      with_payload: bool = True) -> dict[str, Any]:
        out = {"id": row["id"], "batch_id": row["batch_id"],
               "station": row["station_call"],
               "version_no": row["version_no"], "kind": row["kind"],
               "status": row["status"], "content_hash": row["content_hash"],
               "corrects_report_id": row["corrects_report_id"],
               "corrects_version_no": row["corrects_version_no"],
               "superseded_by": row["superseded_by"],
               "previewed_ts": row["previewed_ts"],
               "published_ts": row["published_ts"],
               "created_ts": row["created_ts"]}
        if with_payload:
            out["report"] = json.loads(row["report_json"])
            out["external"] = (json.loads(row["external_json"])
                               if row["external_json"] else None)
        return out

    def get_feedback_report(self, report_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM feedback_reports WHERE id = ?",
            (report_id,)).fetchone()
        return self._feedback_row(row) if row else None

    def list_feedback_reports(self, batch_id: str,
                              station: str | None = None,
                              status: str | None = None,
                              version_no: int | None = None,
                              kind: str | None = None
                              ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM feedback_reports WHERE batch_id = ?"
        args: list[Any] = [batch_id]
        if station:
            sql += " AND station_call = ?"
            args.append(station)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if version_no is not None:
            sql += " AND version_no = ?"
            args.append(int(version_no))
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY created_ts, id"
        rows = self.conn.execute(sql, args).fetchall()
        return [self._feedback_row(r, with_payload=False) for r in rows]

    def find_feedback_by_hash(self, batch_id: str, station: str,
                              content_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM feedback_reports WHERE batch_id = ? AND "
            "station_call = ? AND content_hash = ? ORDER BY created_ts",
            (batch_id, station, content_hash)).fetchone()
        return self._feedback_row(row) if row else None

    def latest_published_feedback(self, batch_id: str,
                                  station: str) -> dict[str, Any] | None:
        # 同秒连续发布多个包时 published_ts/created_ts 都可能并列；
        # 用版本号与 id 做确定性决胜，保证始终选中更正链最末端的已发布包
        row = self.conn.execute(
            "SELECT * FROM feedback_reports WHERE batch_id = ? AND "
            "station_call = ? AND status = 'published' "
            "ORDER BY version_no DESC, published_ts DESC, created_ts DESC, "
            "id DESC LIMIT 1",
            (batch_id, station)).fetchone()
        return self._feedback_row(row) if row else None

    def published_feedback_for_version(self, batch_id: str, station: str,
                                       version_no: int
                                       ) -> dict[str, Any] | None:
        """该台站在指定冻结版本上已发布的反馈包（含更正包）。

        同一冻结版本上的已发布包内容哈希固定，重复生成时必须幂等复用，
        不得因 latest_published 在同秒并列时选错链头而另建 normal 草稿。
        优先取更新链最末端（superseded_by 为空）者。
        """
        rows = self.conn.execute(
            "SELECT * FROM feedback_reports WHERE batch_id = ? AND "
            "station_call = ? AND version_no = ? AND status = 'published' "
            "ORDER BY (superseded_by IS NULL) DESC, published_ts DESC, "
            "created_ts DESC, id DESC",
            (batch_id, station, int(version_no))).fetchall()
        return self._feedback_row(rows[0]) if rows else None

    def set_feedback_external(self, report_id: str,
                              external: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE feedback_reports SET external_json = ?, "
                "previewed_ts = ? WHERE id = ?",
                (json.dumps(external, ensure_ascii=False), _now(),
                 report_id))
            self.conn.commit()

    def set_feedback_published(self, report_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE feedback_reports SET status = 'published', "
                "published_ts = ? WHERE id = ?", (_now(), report_id))
            self.conn.commit()

    def set_feedback_superseded(self, old_id: str, new_id_: str) -> None:
        """记录替代关系（只写元数据指针，不改写旧报告内容）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE feedback_reports SET superseded_by = ? WHERE id = ?",
                (new_id_, old_id))
            self.conn.commit()

    # -- 赛后复议案件 --------------------------------------------------------
    def create_appeal_case(self, case_id: str, batch_id: str, station: str,
                           report_id: str, version_no: int,
                           version_content_hash: str,
                           report_content_hash: str,
                           binding_hash: str,
                           claims: list[dict[str, Any]],
                           applicant: str | None) -> dict[str, Any]:
        ts = _now()
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO appeal_cases (id, batch_id, station_call, "
                    "report_id, version_no, status, applicant, "
                    "version_content_hash, report_content_hash, "
                    "binding_content_hash, received_ts) "
                    "VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?)",
                    (case_id, batch_id, station, report_id, int(version_no),
                     applicant, version_content_hash, report_content_hash,
                     binding_hash, ts))
                for i, c in enumerate(claims, start=1):
                    self.conn.execute(
                        "INSERT INTO appeal_claims (id, case_id, seq, subject, "
                        "summary, finding_id, log_refs_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (c["id"], case_id, i, c["subject"],
                         c["summary"].strip(), c.get("finding_id"),
                         json.dumps(c.get("log_refs") or [],
                                    ensure_ascii=False)))
                self._append_event_unlocked(
                    case_id, "created", applicant,
                    {"report_id": report_id, "version_no": int(version_no),
                     "claim_count": len(claims)})
        return self.get_appeal_case(case_id)

    @staticmethod
    def _appeal_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "batch_id": row["batch_id"],
                "station": row["station_call"],
                "report_id": row["report_id"],
                "version_no": row["version_no"],
                "status": row["status"], "applicant": row["applicant"],
                "judge": row["judge"],
                "version_content_hash": row["version_content_hash"],
                "report_content_hash": row["report_content_hash"],
                "binding_content_hash": row["binding_content_hash"],
                "resolved_version_no": row["resolved_version_no"],
                "correction_report_id": row["correction_report_id"],
                "score_before": row["score_before"],
                "score_after": row["score_after"],
                "received_ts": row["received_ts"],
                "accepted_ts": row["accepted_ts"],
                "withdrawn_ts": row["withdrawn_ts"],
                "closed_ts": row["closed_ts"]}

    @staticmethod
    def _claim_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "case_id": row["case_id"],
                "seq": row["seq"], "subject": row["subject"],
                "summary": row["summary"], "finding_id": row["finding_id"],
                "log_refs": json.loads(row["log_refs_json"]),
                "conclusion": row["conclusion"],
                "resolution": row["resolution"],
                "fault_station": row["fault_station"],
                "penalty_code": row["penalty_code"],
                "rationale": row["rationale"], "judge": row["judge"]}

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "case_id": row["case_id"],
                "seq": row["seq"], "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json.loads(row["detail_json"]),
                "created_ts": row["created_ts"]}

    def get_appeal_case(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM appeal_cases WHERE id = ?",
            (case_id,)).fetchone()
        return self._appeal_row(row) if row else None

    def list_appeal_claims(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM appeal_claims WHERE case_id = ? ORDER BY seq",
            (case_id,)).fetchall()
        return [self._claim_row(r) for r in rows]

    def list_appeal_events(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM appeal_events WHERE case_id = ? ORDER BY seq",
            (case_id,)).fetchall()
        return [self._event_row(r) for r in rows]

    def list_appeal_cases(self, batch_id: str, *,
                          status: str | None = None,
                          station: str | None = None,
                          report_id: str | None = None
                          ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM appeal_cases WHERE batch_id = ?"
        args: list[Any] = [batch_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if station:
            sql += " AND station_call = ?"
            args.append(station)
        if report_id:
            sql += " AND report_id = ?"
            args.append(report_id)
        sql += " ORDER BY received_ts, id"
        rows = self.conn.execute(sql, args).fetchall()
        return [self._appeal_row(r) for r in rows]

    def _append_event_unlocked(self, case_id: str, event_type: str,
                               actor: str | None,
                               detail: dict[str, Any]) -> None:
        n = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 n FROM appeal_events "
            "WHERE case_id = ?", (case_id,)).fetchone()["n"]
        self.conn.execute(
            "INSERT INTO appeal_events (id, case_id, seq, event_type, actor, "
            "detail_json, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("E"), case_id, n, event_type, actor,
             json.dumps(detail or {}, ensure_ascii=False), _now()))

    def add_appeal_event(self, case_id: str, event_type: str,
                         actor: str | None, detail: dict[str, Any]) -> None:
        with self._lock:
            with self.conn:
                self._append_event_unlocked(case_id, event_type, actor, detail)

    # -- 赛后复议：服务端处理预览（确认门控依据） ---------------------------
    def save_appeal_preview(self, case_id: str, rulings: list[dict[str, Any]],
                            ruling_digest: str, binding_fingerprint: str,
                            *, version_no: int, version_content_hash: str,
                            report_content_hash: str,
                            score_before: int, score_after: int) -> None:
        """记录服务端为该案件实际生成的最新一次处理预览（覆盖旧预览）。"""
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO appeal_previews (case_id, rulings_json, "
                    "ruling_digest, binding_fingerprint, version_no, "
                    "version_content_hash, report_content_hash, "
                    "score_before, score_after, created_ts) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(case_id) DO UPDATE SET "
                    "rulings_json=excluded.rulings_json, "
                    "ruling_digest=excluded.ruling_digest, "
                    "binding_fingerprint=excluded.binding_fingerprint, "
                    "version_no=excluded.version_no, "
                    "version_content_hash=excluded.version_content_hash, "
                    "report_content_hash=excluded.report_content_hash, "
                    "score_before=excluded.score_before, "
                    "score_after=excluded.score_after, "
                    "created_ts=excluded.created_ts",
                    (case_id, json.dumps(rulings, ensure_ascii=False),
                     ruling_digest, binding_fingerprint, int(version_no),
                     version_content_hash, report_content_hash,
                     int(score_before), int(score_after), _now()))

    def get_appeal_preview(self, case_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM appeal_previews WHERE case_id = ?",
            (case_id,)).fetchone()
        if not row:
            return None
        return {"case_id": row["case_id"],
                "rulings": json.loads(row["rulings_json"]),
                "ruling_digest": row["ruling_digest"],
                "binding_fingerprint": row["binding_fingerprint"],
                "version_no": row["version_no"],
                "version_content_hash": row["version_content_hash"],
                "report_content_hash": row["report_content_hash"],
                "score_before": row["score_before"],
                "score_after": row["score_after"],
                "created_ts": row["created_ts"]}

    def delete_appeal_preview(self, case_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM appeal_previews WHERE case_id = ?", (case_id,))
            self.conn.commit()

    def live_binding_fingerprint(self, batch_id: str,
                                 bound_finding_ids: set[str]) -> str:
        """当前配对证据集与现行裁决（限绑定证据）的绑定指纹。

        证据集必须恰好等于绑定快照；任一证据缺失/新增，或某条现行裁决
        与创建预览时不同，确认时据此判定预览失效。
        """
        rows = self.conn.execute(
            "SELECT finding_id, data_json FROM findings WHERE batch_id = ?",
            (batch_id,)).fetchall()
        live_ids = {r["finding_id"] for r in rows}
        decisions = self.list_decisions(batch_id)
        from .appeals import binding_fingerprint
        if live_ids != set(bound_finding_ids):
            # 证据集已变：给出与任何快照都不同的指纹
            missing = sorted(set(bound_finding_ids) - live_ids)
            added = sorted(live_ids - set(bound_finding_ids))
            return binding_fingerprint(
                [f"__missing__:{x}" for x in missing]
                + [f"__added__:{x}" for x in added]
                + sorted(live_ids), decisions)
        return binding_fingerprint(sorted(live_ids), decisions)

    def set_appeal_status(self, case_id: str, status: str,
                          *, judge: str | None = None,
                          actor: str | None = None,
                          detail: dict[str, Any] | None = None) -> None:
        ts_col = {"in_review": "accepted_ts",
                  "withdrawn": "withdrawn_ts",
                  "closed": "closed_ts"}.get(status)
        event = {"in_review": "accepted", "withdrawn": "withdrawn",
                 "closed": "closed"}.get(status, f"status_{status}")
        with self._lock:
            with self.conn:
                sets = ["status = ?"]
                args: list[Any] = [status]
                if ts_col:
                    sets.append(f"{ts_col} = ?")
                    args.append(_now())
                if judge is not None:
                    sets.append("judge = ?")
                    args.append(judge)
                args.append(case_id)
                self.conn.execute(
                    f"UPDATE appeal_cases SET {', '.join(sets)} WHERE id = ?",
                    args)
                self._append_event_unlocked(case_id, event, actor,
                                             detail or {})

    def commit_appeal_rulings(self, case_id: str, *,
                              rulings: list[dict[str, Any]],
                              annotated_findings: list[dict[str, Any]],
                              results: dict[str, Any],
                              rules: dict[str, Any],
                              version_digest: str,
                              clock_scheme_slim: dict[str, Any] | None,
                              score_before: int, score_after: int,
                              judge: str | None,
                              note: str | None,
                              build_correction) -> dict[str, Any]:
        """确认改判：二次核对全部绑定后一次性写入并冻结新计分版本。

        *build_correction(new_version, snapshot)* 由接口层提供，基于
        :mod:`cabrillo_judge.feedback` 构建更正包；返回 None 表示无需更正包
        （得分未变）。任何一项核对失败抛 :class:`AppealStale`，
        所有写入回滚（整案不写入）。
        """
        def stale(code: str, message: str,
                  bucket: list[dict[str, str]]) -> None:
            bucket.append({"code": code, "message": message})

        with self._lock:
            with self.conn:
                case = self.get_appeal_case(case_id)
                problems: list[dict[str, str]] = []
                if case is None:
                    raise AppealStale([{"code": "CASE_NOT_FOUND",
                                       "message": f"案件 {case_id} 不存在"}])
                if case["status"] != "in_review":
                    stale("CASE_NOT_REVIEWABLE",
                          f"案件状态为 {case['status']}，仅 in_review 可确认",
                          problems)
                report = self.get_feedback_report(case["report_id"])
                if report is None or report["batch_id"] != case["batch_id"]:
                    stale("REPORT_NOT_FOUND",
                          f"绑定反馈包 {case['report_id']} 已不存在", problems)
                else:
                    if report["status"] != "published":
                        stale("REPORT_NOT_PUBLISHED",
                              f"绑定反馈包 {report['id']} 当前为 "
                              f"{report['status']}（创建案件时已发布）",
                              problems)
                    if report["content_hash"] != case["report_content_hash"]:
                        stale("REPORT_HASH_MISMATCH",
                              f"反馈包 {report['id']} 的内容哈希与案件绑定"
                              f"快照不一致", problems)
                    if report.get("superseded_by"):
                        stale("REPORT_SUPERSEDED",
                              f"反馈包 {report['id']} 已有后续更正包 "
                              f"{report['superseded_by']}，不自动改绑；"
                              f"请告知申请方针对最新包重新提案", problems)
                version = self.get_version(case["batch_id"],
                                           case["version_no"])
                if version is None:
                    stale("VERSION_NOT_FOUND",
                          f"绑定计分版本 v{case['version_no']} 已不存在",
                          problems)
                elif version["content_hash"] != case["version_content_hash"]:
                    stale("VERSION_HASH_MISMATCH",
                          f"计分版本 v{case['version_no']} 的内容哈希与案件"
                          f"绑定快照不一致", problems)
                latest_no = self.next_version_no(case["batch_id"]) - 1
                if version is not None and latest_no > case["version_no"]:
                    stale("VERSION_NOT_LATEST",
                          f"案件绑定 v{case['version_no']}，但批次已冻结至 "
                          f"v{latest_no}；绑定快照不再是最新依据，"
                          f"整案不写入", problems)

                bound_findings = (version["snapshot"].get("findings")
                                  if version else []) or []
                bound_index = {f["id"]: f for f in bound_findings}
                bound_decisions = (version["snapshot"].get("decisions")
                                   if version else {}) or {}

                # 实时配对与裁决不得相对绑定快照漂移
                live_rows = self.conn.execute(
                    "SELECT finding_id, data_json FROM findings "
                    "WHERE batch_id = ?",
                    (case["batch_id"],)).fetchall()
                live_ids = {r["finding_id"] for r in live_rows}
                bound_ids = set(bound_index)
                if live_ids != bound_ids:
                    gone = sorted(bound_ids - live_ids)
                    added = sorted(live_ids - bound_ids)
                    stale("PAIRING_STALE",
                          f"当前配对证据集与绑定快照不一致"
                          f"（消失 {len(gone)} 条、新增 {len(added)} 条），"
                          f"绑定依据已失效，整案不写入", problems)
                live_decisions = self.list_decisions(case["batch_id"])
                _DEC_KEYS = ("resolution", "fault_station", "penalty_code",
                             "reason", "judge")
                for fid in sorted(bound_ids):
                    a = {k: (live_decisions.get(fid) or {}).get(k)
                         for k in _DEC_KEYS}
                    b = {k: (bound_decisions.get(fid) or {}).get(k)
                         for k in _DEC_KEYS}
                    if a != b:
                        stale("DECISION_STALE",
                              f"证据 {fid} 的现行裁决与绑定快照不一致，"
                              f"整案不写入", problems)

                # 逐项引用复核：finding 存在/属本台/在报告中；日志行同检
                claims = self.list_appeal_claims(case_id)
                if report is not None:
                    report_data = report.get("report") or {}
                    entry_lines = {(e.get("filename"), e.get("line"))
                                   for e in report_data.get("entries", [])}
                    own_files = {l.get("filename")
                                 for l in report_data.get("logs", [])}
                    findings_in_report = {
                        e.get("finding_id")
                        for e in report_data.get("entries", [])
                        if e.get("finding_id")}
                    findings_in_report |= {
                        u.get("finding_id")
                        for u in report_data.get(
                            "unassociated_evidence", [])
                        if u.get("finding_id")}
                    for c in claims:
                        fid = c.get("finding_id")
                        if fid:
                            f = bound_index.get(fid)
                            if f is None:
                                stale("FINDING_NOT_IN_BINDING",
                                      f"争议项 {c['seq']} 引用的证据 {fid} "
                                      f"不在绑定版本快照中", problems)
                            else:
                                if not _appeals.finding_touches_station(
                                        f, case["station"]):
                                    stale("CLAIM_STATION_MISMATCH",
                                          f"争议项 {c['seq']} 引用的证据 "
                                          f"{fid} 不属于台站 "
                                          f"{case['station_call']}", problems)
                                if fid not in findings_in_report:
                                    stale("TARGET_NOT_IN_REPORT",
                                          f"争议项 {c['seq']} 的目标证据 "
                                          f"{fid} 未出现在反馈包中", problems)
                        for ref in c.get("log_refs") or []:
                            if ref.get("filename") not in own_files:
                                stale("CLAIM_STATION_MISMATCH",
                                      f"争议项 {c['seq']} 引用的日志 "
                                      f"{ref.get('filename')!r} 不属于台站 "
                                      f"{case['station_call']}", problems)
                            elif (ref.get("filename"), ref.get("line")) \
                                    not in entry_lines:
                                stale("TARGET_NOT_IN_REPORT",
                                      f"争议项 {c['seq']} 的目标行 "
                                      f"{ref.get('filename')}:"
                                      f"{ref.get('line')} 未出现在反馈包中",
                                      problems)

                # 服务端处理预览门控：只认可本服务端为该案件实际生成、
                # 与最新绑定快照和当前处理意见一致的预览；客户端不得自行
                # 提交 digest 绕过预览步骤
                preview = self.get_appeal_preview(case_id)
                if preview is None:
                    stale("PREVIEW_REQUIRED",
                          f"案件 {case_id} 没有服务端生成的处理预览；"
                          f"请先 POST .../preview 生成并核对后再确认",
                          problems)
                else:
                    from .appeals import preview_digest
                    if preview["ruling_digest"] != preview_digest(rulings):
                        stale("PREVIEW_MISMATCH",
                              "确认提交的处理意见与最近一次服务端预览不"
                              "一致；请重新生成预览并核对后再确认", problems)
                    if preview["version_no"] != case["version_no"] or \
                            preview["version_content_hash"] != \
                            case["version_content_hash"] or \
                            preview["report_content_hash"] != \
                            case["report_content_hash"]:
                        stale("PREVIEW_STALE",
                              "生成预览所依据的版本/反馈包绑定已变化，"
                              "预览失效；请重新预览", problems)
                    current_fingerprint = self.live_binding_fingerprint(
                        case["batch_id"], bound_ids)
                    if preview["binding_fingerprint"] != current_fingerprint:
                        stale("PREVIEW_STALE",
                              "生成预览后配对证据集或现行裁决已变化，"
                              "预览失效（不会按陈旧预览写入）；"
                              "请重新预览", problems)
                    if version is not None:
                        snapshot_fingerprint = \
                            _appeals.binding_fingerprint(
                                sorted(bound_ids), bound_decisions)
                        if preview["binding_fingerprint"] != \
                                snapshot_fingerprint:
                            stale("PREVIEW_STALE",
                                  "预览生成时的绑定依据与案件绑定快照不"
                                  "一致；请重新预览", problems)

                if problems:
                    raise AppealStale(problems)

                # ---- 全部核对通过：一次性写入 ---------------------------
                rulings_by_id = {r["claim_id"]: r for r in rulings}
                changed_fids: set[str] = set()
                for c in claims:
                    r = rulings_by_id.get(c["id"])
                    if not r:
                        continue
                    fault = r.get("fault_station")
                    fault = str(fault).upper() if fault else None
                    self.conn.execute(
                        "UPDATE appeal_claims SET conclusion = ?, "
                        "resolution = ?, fault_station = ?, penalty_code = ?, "
                        "rationale = ?, judge = ? WHERE id = ?",
                        (r.get("conclusion"),
                         (str(r.get("resolution") or "").upper()
                          if r.get("resolution") else None),
                         fault, r.get("penalty_code"),
                         str(r.get("rationale") or "").strip() or None,
                         r.get("judge"), c["id"]))
                    if r.get("conclusion") == "revised" and c["finding_id"]:
                        changed_fids.add(c["finding_id"])
                        self.conn.execute(
                            "INSERT INTO decisions (batch_id, finding_id, "
                            "resolution, fault_station, penalty_code, reason, "
                            "judge, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(batch_id, finding_id) DO UPDATE SET "
                            "resolution=excluded.resolution, "
                            "fault_station=excluded.fault_station, "
                            "penalty_code=excluded.penalty_code, "
                            "reason=excluded.reason, judge=excluded.judge, "
                            "updated_ts=excluded.updated_ts",
                            (case["batch_id"], c["finding_id"],
                             str(r.get("resolution") or "").upper(),
                             fault, r.get("penalty_code"),
                             str(r.get("rationale") or "").strip(),
                             r.get("judge"), _now()))

                new_no = None
                digest = None
                correction_id = None
                # 有改判项时才重写证据/冻结新计分版本；纯维持/证据不足案件
                # 不改批次数据，直接结案
                if changed_fids:
                    self.conn.execute(
                        "DELETE FROM findings WHERE batch_id = ?",
                        (case["batch_id"],))
                    self.conn.executemany(
                        "INSERT INTO findings (batch_id, finding_id, data_json) "
                        "VALUES (?, ?, ?)",
                        [(case["batch_id"], f["id"],
                          json.dumps(f, ensure_ascii=False))
                         for f in annotated_findings])

                    new_no = self.next_version_no(case["batch_id"])
                    submissions = self.get_submissions(case["batch_id"])
                    log_manifest = [{
                        "log_id": s["log_id"], "filename": s["filename"],
                        "station_call": s["station_call"],
                        "upload_ts": s["upload_ts"]} for s in submissions]
                    active_scheme = self.get_active_clock_scheme(
                        case["batch_id"])
                    scheme_for_hash = clock_scheme_slim
                    if active_scheme is not None and clock_scheme_slim is None:
                        scheme_for_hash = {
                            "reference_log_id":
                            active_scheme["reference_log_id"],
                            "max_window_seconds":
                            active_scheme["max_window_seconds"],
                            "offsets": active_scheme["offsets"]}
                    digest = content_hash(
                        rules, annotated_findings,
                        self.list_decisions(case["batch_id"]),
                        results, clock_scheme=scheme_for_hash)
                    snapshot = {
                        "batch_id": case["batch_id"], "version_no": new_no,
                        "content_hash": digest,
                        "batch_name":
                        self.get_batch(case["batch_id"])["name"],
                        "rules": rules,
                        "decisions": self.list_decisions(case["batch_id"]),
                        "findings": annotated_findings, "results": results,
                        "logs": log_manifest,
                        "clock_scheme": ({**scheme_for_hash,
                                          "id": active_scheme["id"],
                                          "name": active_scheme["name"]}
                                         if active_scheme else None),
                        "created_by_appeal": case_id,
                        "created_ts": _now()}
                    self.conn.execute(
                        "INSERT INTO versions (batch_id, version_no, "
                        "content_hash, note, snapshot_json, created_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (case["batch_id"], new_no, digest, note,
                         json.dumps(snapshot, ensure_ascii=False), _now()))

                    # 得分变化时生成沿用既有替代关系的更正包
                    if build_correction is not None \
                            and score_after != score_before:
                        new_version = {"version_no": new_no,
                                       "content_hash": digest,
                                       "note": note,
                                       "created_ts": snapshot["created_ts"],
                                       "snapshot": snapshot}
                        corr = build_correction(new_version, snapshot)
                        if corr is not None:
                            corr_digest = _feedback.report_content_hash(corr)
                            existing = self.find_feedback_by_hash(
                                case["batch_id"], case["station"],
                                corr_digest)
                            if existing:
                                correction_id = existing["id"]
                            else:
                                correction_id = new_id("R")
                                self.conn.execute(
                                    "INSERT INTO feedback_reports (id, "
                                    "batch_id, station_call, version_no, kind, "
                                    "status, content_hash, corrects_report_id, "
                                    "corrects_version_no, report_json, "
                                    "external_json, previewed_ts, "
                                    "published_ts, created_ts) VALUES "
                                    "(?, ?, ?, ?, 'correction', 'published', "
                                    "?, ?, ?, ?, ?, ?, ?, ?)",
                                    (correction_id, case["batch_id"],
                                     case["station"], new_no, corr_digest,
                                     report["id"], report["version_no"],
                                     json.dumps(corr, ensure_ascii=False),
                                     json.dumps(
                                         _feedback.build_external_report(corr),
                                         ensure_ascii=False),
                                     _now(), _now(), _now()))
                                self.conn.execute(
                                    "UPDATE feedback_reports SET "
                                    "superseded_by = ? WHERE id = ?",
                                    (correction_id, report["id"]))
                            self._append_event_unlocked(
                                case_id, "correction_published", judge,
                                {"report_id": correction_id,
                                 "supersedes": report["id"],
                                 "version_no": new_no})

                self.conn.execute(
                    "UPDATE appeal_cases SET status = 'closed', judge = ?, "
                    "resolved_version_no = ?, correction_report_id = ?, "
                    "score_before = ?, score_after = ?, closed_ts = ? "
                    "WHERE id = ?",
                    (judge, new_no, correction_id, score_before, score_after,
                     _now(), case_id))
                self._append_event_unlocked(
                    case_id, "ruled", judge,
                    {"revised": sorted(changed_fids),
                     "revision_count": len(changed_fids),
                     "score_before": score_before,
                     "score_after": score_after})
                if new_no is not None:
                    self._append_event_unlocked(
                        case_id, "version_frozen", judge,
                        {"version_no": new_no, "content_hash": digest})
                self._append_event_unlocked(case_id, "closed", judge, {})
                # 处理意见一次性消费：确认成功后删除预览，防止重放
                self.conn.execute(
                    "DELETE FROM appeal_previews WHERE case_id = ?",
                    (case_id,))
                return {"version_no": new_no, "content_hash": digest,
                        "frozen": new_no is not None,
                        "correction_report_id": correction_id,
                        "score_before": score_before,
                        "score_after": score_after}


class AppealStale(Exception):
    """确认时绑定快照已失效（引用/哈希对不上）；整案不写入。"""

    def __init__(self, problems: list[dict[str, str]]):
        self.problems = problems
        super().__init__("；".join(p["message"] for p in problems))
