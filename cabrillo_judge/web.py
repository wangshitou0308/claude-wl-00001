"""Offline JSON HTTP API built on :mod:`http.server`.

Run with::

    python -m cabrillo_judge --host 127.0.0.1 --port 8080 --db judge.db

Every endpoint is described at ``GET /api/docs`` (machine-readable) and
``GET /`` (human-readable Markdown).  No external services are contacted.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse, parse_qs

from . import __version__
from .engine import (
    PENDING_STATUSES,
    RESOLUTIONS,
    adjudicate,
    apply_decisions,
    content_hash,
    score,
)
from .parser import parse_cabrillo
from .rules import default_rules, validate_rules
from .storage import Storage, new_id

DOC_TITLE = "Cabrillo 离线日志裁决 API"

API_SPEC: dict[str, Any] = {
    "title": DOC_TITLE,
    "version": __version__,
    "description": (
        "业余无线电竞赛裁判离线使用。标准库 http.server + sqlite3，"
        "不联网、不查呼号库。所有时间均为 UTC，错误定位到文件名:行号。"),
    "endpoints": [
        {"method": "GET", "path": "/api/health",
         "desc": "健康检查"},
        {"method": "POST", "path": "/api/batches",
         "desc": "创建批次。JSON: {name, rules?}；省略 rules 时用示例规则"},
        {"method": "GET", "path": "/api/batches",
         "desc": "列出全部批次"},
        {"method": "GET", "path": "/api/batches/{id}",
         "desc": "批次详情（含规则、锁定状态、日志数、待裁决数）"},
        {"method": "PUT", "path": "/api/batches/{id}/rules",
         "desc": "更新竞赛规则（未锁定时），随后自动重跑配对"},
        {"method": "POST", "path": "/api/batches/{id}/logs",
         "desc": "上传日志。application/json: {filename, content}；"
                 "或 multipart/form-data 字段 files（可多份）"},
        {"method": "GET", "path": "/api/batches/{id}/logs",
         "desc": "列出批次内日志"},
        {"method": "GET", "path": "/api/batches/{id}/logs/{log_id}",
         "desc": "单份日志：原文、解析结果、按文件:行号定位的错误/警告"},
        {"method": "POST", "path": "/api/batches/{id}/rerun",
         "desc": "重新执行交叉配对（增删日志/改规则后）"},
        {"method": "GET", "path": "/api/batches/{id}/findings",
         "desc": "配对证据列表。查询参数 status/pending/station；"
                 "pending=true 且未裁决的即争议清单"},
        {"method": "GET", "path": "/api/batches/{id}/findings/{fid}",
         "desc": "单条证据（含双方原始行、交换差异、时间差、裁决记录）"},
        {"method": "GET", "path": "/api/batches/{id}/disputes",
         "desc": "争议筛选：所有 pending=true 且尚无裁决的条目"},
        {"method": "POST", "path": "/api/batches/{id}/findings/{fid}/decision",
         "desc": "提交/修改裁决。必填 reason 理由；"
                 "resolution=CONFIRMED|GRANTED|WAIVED|REMOVED，"
                 "可选 fault_station/penalty_code/judge"},
        {"method": "DELETE",
         "path": "/api/batches/{id}/findings/{fid}/decision",
         "desc": "撤销某条裁决"},
        {"method": "GET", "path": "/api/batches/{id}/results",
         "desc": "当前可复现的计分结果（各台站分数、乘数、罚分、证据）"},
        {"method": "POST", "path": "/api/batches/{id}/versions",
         "desc": "生成计分版本快照。JSON: {note?}。"
                 "可加 ?lock=1 在快照后锁定批次"},
        {"method": "GET", "path": "/api/batches/{id}/versions",
         "desc": "版本列表（版本号、内容哈希、时间、备注）"},
        {"method": "GET", "path": "/api/batches/{id}/versions/{no}",
         "desc": "取某一版本完整快照"},
        {"method": "GET", "path": "/api/batches/{id}/versions/diff",
         "desc": "版本比较，查询参数 a/b 为版本号；省略 b 与当前结果比"},
        {"method": "POST", "path": "/api/batches/{id}/lock",
         "desc": "锁定/解锁批次。JSON: {locked: true|false}"},
        {"method": "GET", "path": "/api/batches/{id}/download",
         "desc": "下载完整 JSON（规则、日志原文、证据、裁决、当前结果）"},
        {"method": "GET", "path": "/api/docs",
         "desc": "机器可读 API 说明（本对象）"},
    ],
    "statuses": {
        "MATCH": "双方记录频段/模式/容差时间与交换完全一致",
        "EXCHANGE_DIFF": "配对成立但交换不一致，待裁决（不猜抄错方）",
        "TIME_DRIFT": "互有记录但时间差超容差、在近邻窗口内，待裁决",
        "SUSPECT_CALL": "呼号模糊匹配，疑似抄错，待裁决",
        "NO_PARTNER_LOG": "对方未交日志，无法核实，待裁决",
        "UNIQUE": "对方交了日志但无此记录（单方记录），默认按罚目 NOT_IN_LOG 处理",
        "DUP": "本方日志内重复通联",
    },
    "decisions": {
        "CONFIRMED": "确认通联成立（EXCHANGE_DIFF/TIME_DRIFT/SUSPECT_CALL）",
        "GRANTED": "认定单方/对方未交日志的记录有效计分（NO_PARTNER_LOG）",
        "WAIVED": "豁免：记录不计分且免除自动罚分（UNIQUE/DUP/NO_PARTNER_LOG）",
        "REMOVED": "剔除该记录（不计分）",
    },
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# Multipart (form-data) parsing — just enough for file uploads
# ---------------------------------------------------------------------------

_BOUNDARY_RE = re.compile(rb'boundary="?([^";]+)"?')


def parse_multipart(body: bytes, content_type: str) -> list[dict[str, str]]:
    """Return ``[{name, filename, content}]`` parts."""
    m = _BOUNDARY_RE.search(content_type.encode("utf-8"))
    if not m:
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_MULTIPART",
                       "multipart Content-Type 缺少 boundary")
    boundary = b"--" + m.group(1)
    parts: list[dict[str, str]] = []
    for chunk in body.split(boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        header_blob, _, payload = chunk.partition(b"\r\n\r\n")
        headers = {}
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        fn_m = re.search(r'filename="([^"]*)"', disp)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        if fn_m:
            parts.append({"name": name_m.group(1) if name_m else "files",
                          "filename": fn_m.group(1),
                          "content": payload.decode("utf-8", "replace")})
    return parts


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class JudgeHandler(BaseHTTPRequestHandler):
    server_version = f"CabrilloJudge/{__version__}"
    protocol_version = "HTTP/1.1"

    # Injected by make_server:
    storage: Storage
    demo_seed: bool = False

    # Silence the noisy default logger; emit one concise line instead.
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} "
              f"{fmt % args}")

    # -- low level helpers ------------------------------------------------
    def _send_json(self, obj: Any, status: int = 200,
                   download_name: str | None = None) -> None:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str, content_type: str,
                   status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "EMPTY_BODY",
                           "请求体为空，需要 JSON")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_JSON",
                           f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(obj, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_JSON",
                           "请求体必须是 JSON 对象")
        return obj

    def _query(self) -> dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if method == "GET" and path == "/":
                return self._send_text(_render_markdown(), "text/html")
            if method == "GET" and path == "/api/health":
                return self._send_json({"status": "ok",
                                        "version": __version__,
                                        "service": "cabrillo-judge"})
            if method == "GET" and path == "/api/docs":
                return self._send_json(API_SPEC)
            if method == "POST" and path == "/api/batches":
                return self._create_batch()
            if method == "GET" and path == "/api/batches":
                return self._list_batches()

            m = re.fullmatch(r"/api/batches/([^/]+)", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._batch_detail(bid)
                return None

            m = re.fullmatch(r"/api/batches/([^/]+)/rules", path)
            if m and method == "PUT":
                return self._update_rules(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/logs", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._list_logs(bid)
                if method == "POST":
                    return self._upload_logs(bid)
            m = re.fullmatch(r"/api/batches/([^/]+)/logs/([^/]+)", path)
            if m and method == "GET":
                return self._get_log(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/batches/([^/]+)/rerun", path)
            if m and method == "POST":
                return self._rerun(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/disputes", path)
            if m and method == "GET":
                return self._disputes(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/findings", path)
            if m and method == "GET":
                return self._list_findings(m.group(1))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/findings/([^/]+)", path)
            if m and method == "GET":
                return self._get_finding(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/findings/([^/]+)/decision", path)
            if m:
                bid, fid = m.group(1), m.group(2)
                if method == "POST":
                    return self._put_decision(bid, fid)
                if method == "DELETE":
                    return self._del_decision(bid, fid)
            m = re.fullmatch(r"/api/batches/([^/]+)/results", path)
            if m and method == "GET":
                return self._results(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/versions", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._list_versions(bid)
                if method == "POST":
                    return self._create_version(bid)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/versions/diff", path)
            if m and method == "GET":
                return self._diff_versions(m.group(1))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/versions/(\d+)", path)
            if m and method == "GET":
                return self._get_version(m.group(1), int(m.group(2)))
            m = re.fullmatch(r"/api/batches/([^/]+)/lock", path)
            if m and method == "POST":
                return self._set_lock(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/download", path)
            if m and method == "GET":
                return self._download(m.group(1))

            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND",
                           f"没有 {method} {path} 这个接口")
        except ApiError as exc:
            self._send_json({"error": exc.code, "message": exc.message,
                             "details": exc.details}, exc.status)
        except PermissionError as exc:
            self._send_json({"error": "BATCH_LOCKED",
                             "message": str(exc)}, HTTPStatus.CONFLICT)
        except KeyError as exc:
            self._send_json({"error": "NOT_FOUND",
                             "message": str(exc).strip("'")},
                            HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json(
                {"error": "INTERNAL",
                 "message": f"服务器内部错误：{exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- domain helpers ----------------------------------------------------
    def _get_batch_or_404(self, bid: str) -> dict[str, Any]:
        batch = self.storage.get_batch(bid)
        if not batch:
            raise ApiError(HTTPStatus.NOT_FOUND, "BATCH_NOT_FOUND",
                           f"批次 {bid} 不存在")
        return batch

    def _compute_results(self, batch: dict[str, Any]) -> dict[str, Any]:
        submissions = self.storage.get_submissions(batch["id"])
        raw = adjudicate(batch["rules"], submissions)["findings"]
        decisions = self.storage.list_decisions(batch["id"])
        annotated = apply_decisions(batch["rules"], raw, decisions)
        results = score(batch["rules"], annotated)
        return {"findings": annotated, "results": results}

    def _rerun_store(self, bid: str) -> dict[str, Any]:
        batch = self._get_batch_or_404(bid)
        computed = self._compute_results(batch)
        # Keep finding identity; replace evidence, carry old decisions.
        self.storage.replace_findings(bid, computed["findings"])
        self.storage.touch_batch(bid)
        return computed

    # -- endpoints ---------------------------------------------------------
    def _create_batch(self) -> None:
        body = self._json_body()
        name = str(body.get("name") or "未命名批次").strip()
        rules = body.get("rules")
        if rules is None:
            rules = default_rules()
        elif isinstance(rules, dict):
            # Shallow merge over defaults so partial rule configs still have
            # category defaults etc.
            merged = default_rules()
            merged.update(rules)
            rules = merged
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULES",
                           "rules 必须是对象")
        problems = validate_rules(rules)
        if problems:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULES",
                           "竞赛规则校验失败", problems)
        bid = body.get("id") or new_id("B")
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", bid):
            bid = new_id("B")
        if self.storage.get_batch(bid):
            raise ApiError(HTTPStatus.CONFLICT, "BATCH_EXISTS",
                           f"批次 {bid} 已存在")
        batch = self.storage.create_batch(bid, name, rules)
        self._send_json({"batch": _public_batch(batch)},
                        HTTPStatus.CREATED)

    def _list_batches(self) -> None:
        out = []
        for b in self.storage.list_batches():
            item = _public_batch(b)
            item["log_count"] = len(self.storage.list_logs(b["id"]))
            item["pending_count"] = self._pending_count(b["id"])
            out.append(item)
        self._send_json({"batches": out, "count": len(out)})

    def _batch_detail(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        logs = self.storage.list_logs(bid)
        log_summaries = []
        for lg in logs:
            full = self.storage.get_log(lg["id"])
            assert full is not None
            issues = full["parsed"]["issues"]
            log_summaries.append({
                **lg,
                "qso_lines": len(full["parsed"]["qsos"]),
                "xqso_lines": len(full["parsed"]["xqsos"]),
                "invalid_lines": len(full["parsed"]["invalid_qsos"]),
                "errors": [i for i in issues if i["severity"] == "error"],
                "warnings": [i for i in issues
                             if i["severity"] == "warning"],
            })
        self._send_json({
            "batch": _public_batch(batch),
            "logs": log_summaries,
            "findings_count": self.storage.findings_count(bid),
            "pending_count": self._pending_count(bid),
            "decisions_count": len(self.storage.list_decisions(bid)),
            "versions": self.storage.list_versions(bid),
        })

    def _pending_count(self, bid: str) -> int:
        decisions = self.storage.list_decisions(bid)
        return sum(1 for f in self.storage.list_findings(bid)
                   if f["pending"] and f["id"] not in decisions)

    def _update_rules(self, bid: str) -> None:
        self.storage.require_open(bid)
        batch = self._get_batch_or_404(bid)
        body = self._json_body()
        rules = body.get("rules")
        if not isinstance(rules, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULES",
                           "需要 rules 对象")
        merged = default_rules()
        merged.update(rules)
        problems = validate_rules(merged)
        if problems:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULES",
                           "竞赛规则校验失败", problems)
        self.storage.update_rules(bid, merged)
        batch["rules"] = merged
        computed = self._compute_results(batch)
        self.storage.replace_findings(bid, computed["findings"])
        self._send_json({"batch": _public_batch(self.storage.get_batch(bid)),
                         "findings_count": len(computed["findings"]),
                         "results_summary": computed["results"]["summary"]})

    def _upload_logs(self, bid: str) -> None:
        self.storage.require_open(bid)
        batch = self._get_batch_or_404(bid)
        ctype = self.headers.get("Content-Type", "")
        files: list[dict[str, str]] = []
        if ctype.startswith("application/json"):
            body = self._json_body()
            if "logs" in body and isinstance(body["logs"], list):
                files = [{"filename": str(x.get("filename") or
                                          f"upload-{i + 1}.log"),
                          "content": str(x.get("content") or "")}
                         for i, x in enumerate(body["logs"])]
            else:
                files = [{"filename": str(body.get("filename") or
                                          "upload.log"),
                          "content": str(body.get("content") or "")}]
        elif ctype.startswith("multipart/form-data"):
            parts = parse_multipart(self._read_body(), ctype)
            files = [{"filename": p["filename"], "content": p["content"]}
                     for p in parts if p.get("filename")]
        else:
            raw = self._read_body()
            q = self._query()
            files = [{"filename": q.get("filename", "upload.log"),
                      "content": raw.decode("utf-8", "replace")}]

        if not files:
            raise ApiError(HTTPStatus.BAD_REQUEST, "NO_FILES",
                           "未收到任何日志文件")

        received = []
        existing = {l["filename"] for l in self.storage.list_logs(bid)}
        for f in files:
            filename = f["filename"].replace("\\", "/").split("/")[-1]
            if not filename:
                filename = f"upload-{len(existing) + 1}.log"
            if filename in existing:
                stem, _, ext = filename.rpartition(".")
                filename = f"{stem}-{uuid.uuid4().hex[:6]}.{ext or 'log'}"
            parsed = parse_cabrillo(f["content"], batch["rules"])
            log_id = new_id("L")
            self.storage.add_log(log_id, bid, filename, f["content"], parsed)
            existing.add(filename)
            received.append({
                "log_id": log_id, "filename": filename,
                "station_call": parsed["station_call"],
                "qso_lines": len(parsed["qsos"]),
                "xqso_lines": len(parsed["xqsos"]),
                "invalid_lines": len(parsed["invalid_qsos"]),
                "errors": [i for i in parsed["issues"]
                           if i["severity"] == "error"],
                "warnings": [i for i in parsed["issues"]
                             if i["severity"] == "warning"],
            })

        computed = self._rerun_store(bid)
        self._send_json({
            "received": received,
            "findings_count": len(computed["findings"]),
            "pending_count": computed["results"]["summary"]["pending"],
            "results_summary": computed["results"]["summary"],
        }, HTTPStatus.CREATED)

    def _list_logs(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        self._send_json({"logs": self.storage.list_logs(bid)})

    def _get_log(self, bid: str, log_id: str) -> None:
        self._get_batch_or_404(bid)
        log = self.storage.get_log(log_id)
        if not log or log["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "LOG_NOT_FOUND",
                           f"日志 {log_id} 不存在")
        self._send_json({
            "id": log["id"], "filename": log["filename"],
            "station_call": log["station_call"],
            "upload_ts": log["upload_ts"], "raw_text": log["raw_text"],
            "parsed": {k: log["parsed"][k] for k in
                       ("headers", "raw_tags", "qsos", "xqsos",
                        "invalid_qsos", "issues")},
        })

    def _rerun(self, bid: str) -> None:
        self.storage.require_open(bid)
        computed = self._rerun_store(bid)
        self._send_json({"findings_count": len(computed["findings"]),
                         "summary": computed["results"]["summary"]})

    def _list_findings(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        q = self._query()
        status = q.get("status")
        if status and status not in (
                "MATCH", "EXCHANGE_DIFF", "TIME_DRIFT", "SUSPECT_CALL",
                "NO_PARTNER_LOG", "UNIQUE", "DUP"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_STATUS",
                           f"未知状态 {status}")
        pending = None
        if "pending" in q:
            pending = q["pending"] in ("1", "true", "yes")
        station = q.get("station")
        findings = self.storage.list_findings(bid, status, pending, station)
        decisions = self.storage.list_decisions(bid)
        # list_findings returns raw evidence without decisions attached;
        # rebuild annotations so scores-in-context are visible.
        batch = self.storage.get_batch(bid)
        annotated = apply_decisions(batch["rules"], findings, decisions)
        if pending is False:
            annotated = [f for f in annotated
                         if not f["pending"] or f["id"] in decisions]
        if pending is True:
            annotated = [f for f in annotated
                         if f["pending"] and f["id"] not in decisions]
        self._send_json({"findings": annotated, "count": len(annotated)})

    def _disputes(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        batch = self.storage.get_batch(bid)
        decisions = self.storage.list_decisions(bid)
        findings = [f for f in self.storage.list_findings(bid)
                    if f["pending"] and f["id"] not in decisions]
        annotated = apply_decisions(batch["rules"], findings, decisions)
        self._send_json({"disputes": annotated, "count": len(annotated)})

    def _get_finding(self, bid: str, fid: str) -> None:
        self._get_batch_or_404(bid)
        batch = self.storage.get_batch(bid)
        finding = self.storage.get_finding(bid, fid)
        if not finding:
            raise ApiError(HTTPStatus.NOT_FOUND, "FINDING_NOT_FOUND",
                           f"证据 {fid} 不存在")
        decisions = self.storage.list_decisions(bid)
        annotated = apply_decisions(batch["rules"], [finding], decisions)[0]
        self._send_json({"finding": annotated,
                         "decision": decisions.get(fid)})

    def _put_decision(self, bid: str, fid: str) -> None:
        self.storage.require_open(bid)
        batch = self._get_batch_or_404(bid)
        finding = self.storage.get_finding(bid, fid)
        if not finding:
            raise ApiError(HTTPStatus.NOT_FOUND, "FINDING_NOT_FOUND",
                           f"证据 {fid} 不存在")
        body = self._json_body()
        resolution = str(body.get("resolution") or "").upper()
        if resolution not in RESOLUTIONS:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "BAD_RESOLUTION",
                f"resolution 必须是 {sorted(RESOLUTIONS)} 之一")
        reason = str(body.get("reason") or "")
        if not reason.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "REASON_REQUIRED",
                           "裁决必须填写理由 reason")
        fault = body.get("fault_station")
        if fault is not None:
            fault = str(fault).upper()
            if fault not in finding.get("stations", []) and not (
                    finding["refs"] and
                    finding["refs"][0].get("station") == fault):
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_FAULT_STATION",
                               f"fault_station 必须是相关台站之一: "
                               f"{finding.get('stations')}")
        penalty = body.get("penalty_code")
        if penalty is not None:
            penalty = str(penalty)
            if penalty not in batch["rules"].get("penalties", {}):
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "BAD_PENALTY_CODE",
                    f"penalty_code 必须在规则罚分目录中: "
                    f"{sorted(batch['rules']['penalties'])}")
        # Resolution/status sanity.
        st = finding["status"]
        allowed = {
            "EXCHANGE_DIFF": {"CONFIRMED", "REMOVED"},
            "TIME_DRIFT": {"CONFIRMED", "REMOVED"},
            "SUSPECT_CALL": {"CONFIRMED", "REMOVED"},
            "NO_PARTNER_LOG": {"GRANTED", "WAIVED", "REMOVED"},
            "UNIQUE": {"WAIVED", "REMOVED", "GRANTED"},
            "DUP": {"WAIVED", "REMOVED"},
            "MATCH": {"REMOVED"},
        }[st]
        if resolution not in allowed:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RESOLUTION_FOR_STATUS",
                           f"{st} 状态只接受 {sorted(allowed)}")
        judge = str(body.get("judge") or "").strip() or None
        dec = self.storage.upsert_decision(
            bid, fid, resolution, reason, fault, penalty, judge)
        computed = self._rerun_store(bid)
        self._send_json({"decision": dec,
                         "pending_remaining":
                         computed["results"]["summary"]["pending"]})

    def _del_decision(self, bid: str, fid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        if not self.storage.get_finding(bid, fid):
            raise ApiError(HTTPStatus.NOT_FOUND, "FINDING_NOT_FOUND",
                           f"证据 {fid} 不存在")
        self.storage.delete_decision(bid, fid)
        self._rerun_store(bid)
        self._send_json({"deleted": fid})

    def _results(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        computed = self._compute_results(batch)
        self._send_json({
            "batch_id": bid, "locked": batch["locked"],
            "rules_digest": _rules_digest(batch["rules"]),
            "generated_ts": int(time.time()),
            **computed["results"]})

    def _create_version(self, bid: str) -> None:
        q = self._query()
        batch = self._get_batch_or_404(bid)
        computed = self._compute_results(batch)
        decisions = self.storage.list_decisions(bid)
        digest = content_hash(batch["rules"], computed["findings"],
                              decisions, computed["results"])
        # Same content as the latest version -> reproducible no-op.
        prior = self.storage.list_versions(bid)
        if prior and prior[-1]["content_hash"] == digest:
            return self._send_json({
                "version_no": prior[-1]["version_no"],
                "content_hash": digest,
                "identical_to": prior[-1]["version_no"],
                "reproducible": True})
        no = self.storage.next_version_no(bid)
        note = None
        if self.headers.get("Content-Type", "").startswith("application/json"):
            body = self._json_body()
            note = str(body.get("note") or "") or None
        snapshot = {
            "batch_id": bid, "version_no": no, "content_hash": digest,
            "batch_name": batch["name"], "rules": batch["rules"],
            "decisions": decisions,
            "findings": computed["findings"],
            "results": computed["results"],
            "created_ts": int(time.time()),
        }
        self.storage.save_version(bid, no, digest, note, snapshot)
        if q.get("lock") in ("1", "true", "yes"):
            self.storage.set_locked(bid, True)
        self._send_json({"version_no": no, "content_hash": digest,
                         "note": note,
                         "locked": q.get("lock") in ("1", "true", "yes")},
                        HTTPStatus.CREATED)

    def _list_versions(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        self._send_json({"versions": self.storage.list_versions(bid)})

    def _get_version(self, bid: str, no: int) -> None:
        self._get_batch_or_404(bid)
        ver = self.storage.get_version(bid, no)
        if not ver:
            raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                           f"版本 {no} 不存在")
        self._send_json(ver)

    def _diff_versions(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        q = self._query()
        if "a" not in q:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_PARAM",
                           "需要查询参数 a=<版本号>")
        va = self.storage.get_version(bid, int(q["a"]))
        if not va:
            raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                           f"版本 {q['a']} 不存在")
        if "b" in q:
            vb = self.storage.get_version(bid, int(q["b"]))
            if not vb:
                raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                               f"版本 {q['b']} 不存在")
        else:
            batch = self.storage.get_batch(bid)
            computed = self._compute_results(batch)
            decisions = self.storage.list_decisions(bid)
            vb = {"version_no": "current",
                  "snapshot": {"results": computed["results"],
                               "decisions": decisions,
                               "findings": computed["findings"]}}
        self._send_json({
            "a": va["version_no"], "b": vb["version_no"],
            "scorecards": _diff_scorecards(
                va["snapshot"]["results"]["scorecards"],
                vb["snapshot"]["results"]["scorecards"]),
            "decisions_added": _dict_added(
                va["snapshot"].get("decisions", {}),
                vb["snapshot"].get("decisions", {})),
            "decisions_changed": _dict_changed(
                va["snapshot"].get("decisions", {}),
                vb["snapshot"].get("decisions", {})),
            "finding_status_counts": {
                "a": _status_counts(va["snapshot"]["findings"]),
                "b": _status_counts(vb["snapshot"]["findings"])},
            "content_hash": {"a": va["content_hash"],
                             "b": vb["snapshot"].get("content_hash")},
        })

    def _set_lock(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        body = self._json_body()
        locked = bool(body.get("locked"))
        self.storage.set_locked(bid, locked)
        self._send_json({"batch_id": bid, "locked": locked})

    def _download(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        computed = self._compute_results(batch)
        decisions = self.storage.list_decisions(bid)
        digest = content_hash(batch["rules"], computed["findings"],
                              decisions, computed["results"])
        payload = {
            "export": "cabrillo-judge",
            "schema_version": 1,
            "exported_ts": int(time.time()),
            "batch": _public_batch(batch),
            "rules": batch["rules"],
            "logs": [{"filename": l["filename"],
                      "station_call": l["station_call"],
                      "raw_text": self.storage.get_log(l["id"])["raw_text"]}
                     for l in self.storage.list_logs(bid)],
            "findings": computed["findings"],
            "decisions": decisions,
            "results": computed["results"],
            "versions": self.storage.list_versions(bid),
            "content_hash": digest,
        }
        self._send_json(payload,
                        download_name=f"{bid}-adjudication.json")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _public_batch(b: dict[str, Any]) -> dict[str, Any]:
    return {"id": b["id"], "name": b["name"], "rules": b["rules"],
            "locked": b["locked"], "created_ts": b["created_ts"],
            "updated_ts": b["updated_ts"]}


def _rules_digest(rules: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(rules, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _status_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return dict(sorted(counts.items()))


def _dict_added(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return sorted(set(b) - set(a))


def _dict_changed(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(a) & set(b) if a[k].get("resolution") !=
                  b[k].get("resolution") or a[k].get("reason") !=
                  b[k].get("reason"))


def _diff_scorecards(a: list[dict[str, Any]],
                     b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ca = {c["station"]: c for c in a}
    cb = {c["station"]: c for c in b}
    rows = []
    for st in sorted(set(ca) | set(cb)):
        x, y = ca.get(st, {}), cb.get(st, {})
        rows.append({
            "station": st,
            "qso_counted": {"a": x.get("qso_counted", 0),
                            "b": y.get("qso_counted", 0),
                            "delta": y.get("qso_counted", 0)
                                     - x.get("qso_counted", 0)},
            "multiplier_total": {"a": x.get("multiplier_total", 1),
                                 "b": y.get("multiplier_total", 1)},
            "penalty_points": {"a": x.get("penalty_points", 0),
                               "b": y.get("penalty_points", 0),
                               "delta": y.get("penalty_points", 0)
                                        - x.get("penalty_points", 0)},
            "total_score": {"a": x.get("total_score", 0),
                            "b": y.get("total_score", 0),
                            "delta": y.get("total_score", 0)
                                     - x.get("total_score", 0)},
        })
    return rows


# ---------------------------------------------------------------------------
# HTML landing page (very small, no external resources)
# ---------------------------------------------------------------------------

def _render_markdown() -> str:
    rows = "\n".join(
        f"<tr><td><code>{e['method']}</code></td>"
        f"<td><code>{e['path']}</code></td><td>{e['desc']}</td></tr>"
        for e in API_SPEC["endpoints"])
    statuses = "".join(
        f"<li><code>{k}</code> — {v}</li>"
        for k, v in API_SPEC["statuses"].items())
    decisions = "".join(
        f"<li><code>{k}</code> — {v}</li>"
        for k, v in API_SPEC["decisions"].items())
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{DOC_TITLE}</title><style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;
padding:0 1rem;line-height:1.5}}code{{background:#f3f3f3;padding:1px 4px;
border-radius:3px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f7f7f7}}</style></head><body>
<h1>{DOC_TITLE}</h1>
<p>{API_SPEC['description']}</p>
<p>版本 {__version__}。机器可读文档：<a href="/api/docs"><code>GET /api/docs</code></a>。</p>
<h2>接口</h2><table><tr><th>方法</th><th>路径</th><th>说明</th></tr>{rows}</table>
<h2>配对状态</h2><ul>{statuses}</ul>
<h2>裁决动作</h2><ul>{decisions}</ul>
<h2>典型流程</h2>
<ol>
<li><code>POST /api/batches</code> 创建批次（可自定义通联分/乘数/罚分/容差）</li>
<li><code>POST /api/batches/{{id}}/logs</code> 上传多份 Cabrillo 3.0 日志</li>
<li><code>GET /api/batches/{{id}}/disputes</code> 筛选待裁决争议</li>
<li><code>POST .../findings/{{fid}}/decision</code> 带理由裁决</li>
<li><code>POST /api/batches/{{id}}/versions?lock=1</code> 生成可复现版本并锁定</li>
<li><code>GET /api/batches/{{id}}/versions/diff?a=1&amp;b=2</code> 比较版本</li>
<li><code>GET /api/batches/{{id}}/download</code> 下载完整 JSON</li>
</ol></body></html>"""


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    storage = Storage(db_path)

    class _Handler(JudgeHandler):
        pass

    _Handler.storage = storage
    server = ThreadingHTTPServer((host, port), _Handler)
    server.storage = storage  # type: ignore[attr-defined]
    return server
