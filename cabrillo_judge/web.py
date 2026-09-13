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
from .appeals import (
    CASE_STATUSES,
    CONCLUSION_LABELS,
    SUBJECT_LABELS,
    apply_rulings,
    case_content_hash,
    outcome_scorecard_diff,
    preview_digest,
    snapshot_finding_index,
    validate_claims,
    validate_rulings,
)
from .clockskew import DEFAULT_MIN_SAMPLES, analyze_clock_skew
from .engine import (
    PENDING_STATUSES,
    RESOLUTIONS,
    adjudicate,
    apply_decisions,
    content_hash,
    score,
    validate_judge_decision,
)
from .feedback import (
    build_external_report,
    build_station_report,
    render_text,
    report_content_hash,
    version_bound_logs,
)
from .parser import normalize_callsign, parse_cabrillo
from .rules import default_rules, validate_rules
from .storage import AppealStale, Storage, new_id

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
        {"method": "GET", "path": "/api/batches/{id}/clock-analysis",
         "desc": "批次级时钟偏差分析（只读）。查询参数 reference_log_id（必填）、"
                 "max_window_seconds、min_samples；按日志给出时间差中位数、"
                 "离散度(MAD)、样本数、覆盖时段与整分钟偏移建议"},
        {"method": "POST", "path": "/api/batches/{id}/clock-schemes",
         "desc": "建立整分钟校正方案。JSON: {reference_log_id, name?, "
                 "max_window_seconds?, offsets?{日志ID:整分钟}, "
                 "use_suggested?, activate?}"},
        {"method": "GET", "path": "/api/batches/{id}/clock-schemes",
         "desc": "列出本批次全部校正方案"},
        {"method": "GET", "path": "/api/batches/{id}/clock-schemes/{sid}",
         "desc": "方案详情（含创建时的分析快照）"},
        {"method": "PUT", "path": "/api/batches/{id}/clock-schemes/{sid}",
         "desc": "修改方案名称/偏移；若方案已启用则重跑配对并归档失效裁决"},
        {"method": "DELETE", "path": "/api/batches/{id}/clock-schemes/{sid}",
         "desc": "删除方案（启用中的方案须先停用）"},
        {"method": "POST",
         "path": "/api/batches/{id}/clock-schemes/{sid}/activate",
         "desc": "启用方案：按整分钟偏移重跑配对，失去依据的裁决归档待复核"},
        {"method": "POST",
         "path": "/api/batches/{id}/clock-schemes/{sid}/deactivate",
         "desc": "停用方案：恢复原始时间重跑配对，同样归档失效裁决"},
        {"method": "POST",
         "path": "/api/batches/{id}/clock-schemes/{sid}/preview",
         "desc": "预览方案（可在 JSON.offsets 临时覆盖）：重跑后的状态计数、"
                 "计分变化与将失去依据的裁决；不改动任何数据"},
        {"method": "POST", "path": "/api/batches/{id}/clock-schemes/preview",
         "desc": "临时偏移预览。JSON: {offsets: {日志ID: 整分钟}}"},
        {"method": "GET", "path": "/api/batches/{id}/clock-schemes/compare",
         "desc": "比较两个方案的配对状态计数与计分。查询参数 a/b 为方案 ID"},
        {"method": "GET", "path": "/api/batches/{id}/archived-decisions",
         "desc": "待复核的已归档裁决（因校正方案变化失去依据）"},
        {"method": "DELETE",
         "path": "/api/batches/{id}/archived-decisions/{aid}",
         "desc": "复核后移除归档条目"},
        {"method": "GET", "path": "/api/batches/{id}/download",
         "desc": "下载完整 JSON（规则、日志原文、证据、裁决、校正方案、"
                 "归档裁决、当前结果）"},
        {"method": "POST", "path": "/api/batches/{id}/feedback",
         "desc": "生成站级赛后反馈包草稿（不可变快照）。JSON: "
                 "{version_no（必填，冻结的计分版本）, station?（省略则整批"
                 "每台一份）, corrects?（显式指定被更正的旧包 ID）}。"
                 "该台站已有不同版本的已发布包时自动生成更正包"},
        {"method": "GET", "path": "/api/batches/{id}/feedback",
         "desc": "反馈包列表。查询参数 station/status/version_no/kind"},
        {"method": "GET", "path": "/api/batches/{id}/feedback/{rid}",
         "desc": "反馈包详情（默认内部完整稿；?view=external 看对外脱敏稿，"
                 "需已预览）"},
        {"method": "POST", "path": "/api/batches/{id}/feedback/{rid}/preview",
         "desc": "生成对外脱敏预览（草稿状态；发布前必须预览）"},
        {"method": "POST", "path": "/api/batches/{id}/feedback/{rid}/publish",
         "desc": "发布反馈包（需先预览；发布后不可变）"},
        {"method": "GET", "path": "/api/batches/{id}/feedback/{rid}/download",
         "desc": "下载反馈包。查询参数 format=json|txt、"
                 "view=internal|external（已发布默认 external）"},
        {"method": "GET", "path": "/api/batches/{id}/feedback/{rid}/lineage",
         "desc": "版本追踪：该包的更正链（被谁替代、替代谁）"},
        {"method": "POST", "path": "/api/batches/{id}/appeals",
         "desc": "创建赛后复议案件（须针对已发布反馈包）。JSON: "
                 "{report_id, claims:[{subject（log_line|pairing_status|"
                 "exchange_diff|penalty|summary_score）, summary, "
                 "finding_id?, log_refs?[{filename,line}]?}], applicant?}。"
                 "案件绑定反馈包、计分版本及内容哈希；引用不属本台、"
                 "目标不在报告中或包已被更正时明确拒绝且不自动改绑"},
        {"method": "GET", "path": "/api/batches/{id}/appeals",
         "desc": "案件筛选。查询参数 status（submitted|in_review|"
                 "withdrawn|closed）、station、report_id"},
        {"method": "GET", "path": "/api/batches/{id}/appeals/{cid}",
         "desc": "案件详情：争议项、逐项结论、状态轨迹、绑定快照与结果"},
        {"method": "POST", "path": "/api/batches/{id}/appeals/{cid}/accept",
         "desc": "裁判受理案件（submitted → in_review）。JSON: {judge?}"},
        {"method": "POST", "path": "/api/batches/{id}/appeals/{cid}/withdraw",
         "desc": "申请方撤回未受理案件（submitted → withdrawn）。JSON: {reason?}"},
        {"method": "POST", "path": "/api/batches/{id}/appeals/{cid}/preview",
         "desc": "处理预览：逐项给出 upheld|revised|insufficient 结论"
                 "（revised 须带 resolution/reason，可带 fault_station/"
                 "penalty_code/judge）；返回裁决变更与计分预览，不写入"},
        {"method": "POST", "path": "/api/batches/{id}/appeals/{cid}/confirm",
         "desc": "确认处理：再次核对全部引用与绑定哈希，任一项失效整案不"
                 "写入；通过后一次性写入改判、冻结新计分版本，得分变化时"
                 "生成沿用既有替代关系的更正包并结案。JSON 与预览一致，"
                 "另可带 digest（预览返回值）、note、judge"},
        {"method": "GET", "path": "/api/batches/{id}/appeals/{cid}/download",
         "desc": "下载案件 JSON（含绑定快照、争议项、结论、轨迹、"
                 "裁决变更、计分预览/结果、更正包信息）"},
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
    "clock_skew": {
        "summary": "批次级时钟偏差分析与整分钟校正方案；"
                   "原始 Cabrillo 文本与时间永不修改",
        "offset_semantics": "校正时间 = 原始时间 + 偏移（整分钟）；"
                            "建议偏移使各日志与参考日志对齐",
        "candidate_rule": "候选对须呼号精确互指、频段/模式一致、时间差在"
                          "最大搜索窗口内，且双方在窗口内都只有彼此一个候选"
                          "（无歧义）",
        "no_suggestion_when": [
            "样本不足（无歧义候选对少于 min_samples，默认 3）",
            "偏差随时间变化（前后半程中位数相差超过 60 秒）",
            "日志关系图与参考日志不连通",
        ],
        "evidence_fields": "启用方案后每条证据的 QSO 引用同时含原时间 "
                           "ts/date/time、校正时间 corrected_* 与 "
                           "offset_seconds",
        "archiving": "启用/停用/修改方案会重跑配对；失去依据的既有裁决自动"
                     "归档并列入待复核（GET .../archived-decisions），"
                     "绝不静默沿用；方案随计分版本快照与 content_hash 持久化",
    },
    "feedback": {
        "summary": "站级赛后反馈包：以冻结的计分版本和参赛日志为输入，"
                   "每次生成都保存为不可变快照；sqlite3 记录报告、发布状态"
                   "与替代关系",
        "workflow": [
            "POST .../feedback {version_no, station?} 生成草稿"
            "（省略 station 则整批每台一份）",
            "GET .../feedback/{rid} 查看内部完整稿",
            "POST .../feedback/{rid}/preview 生成对外脱敏预览",
            "POST .../feedback/{rid}/publish 发布（须先预览，发布后不可变）",
            "GET .../feedback/{rid}/download?format=txt 下载纯文本",
        ],
        "content": "逐条列出原日志行、配对状态、是否计分、QSO 分、"
                   "新增乘数项、罚分与裁决理由，并汇总 CLAIMED-SCORE、"
                   "最终得分与差额；无法关联原行的证据单列，不猜测归属",
        "correction": "计分版本变化不改写已发布报告；对新版本再生成时自动"
                      "产出注明旧包与替代版本的更正包（kind=correction），"
                      "替代关系见 .../feedback/{rid}/lineage",
        "log_binding": "版本快照内含冻结当时的日志清单（logs）；旧版本报告"
                       "只取清单内的日志，冻结后上传的日志不得混入",
        "redaction": "对外包（external view）不含其他台站的原始行、邮件地址"
                     "或完整交换内容，只保留解释本台得失所需的对方呼号与"
                     "差异项；裁决理由等自由文本中的邮件地址与他台 QSO "
                     "原文一律隐去（本台自己的原日志行保留）",
    },
    "appeals": {
        "summary": "赛后复议案件：台站对已发布反馈包提出异议；"
                   "状态 submitted→in_review→closed，未受理可 withdrawn；"
                   "sqlite3 记录案件、争议项、处理意见与状态轨迹",
        "claims": "争议项对象 subject 为 log_line/pairing_status/"
                  "exchange_diff/penalty/summary_score，可引用 finding_id "
                  "与本台日志行 log_refs（文件名+行号）",
        "binding": "创建即绑定反馈包 ID 与内容哈希、计分版本号与内容哈希，"
                   "并计算案件绑定哈希；引用不属于该台站、目标未出现在报告、"
                   "或反馈包已有后续更正时明确拒绝且不自动改绑",
        "workflow": [
            "POST .../appeals {report_id, claims:[...]} 创建案件（submitted）",
            "POST .../appeals/{cid}/accept 裁判受理（in_review）",
            "POST .../appeals/{cid}/preview 逐项 upheld/revised/"
            "insufficient，预览裁决变更与计分（不写入）",
            "POST .../appeals/{cid}/confirm 复核全部绑定后一次性写入，"
            "冻结新计分版本；得分变化时生成沿用替代关系的更正包并结案",
            "未受理案件可由申请方 POST .../appeals/{cid}/withdraw 撤回",
        ],
        "atomicity": "确认时再次核对反馈包/版本内容哈希、报告未被替代、"
                     "配对证据集与现行裁决未漂移、逐项引用仍有效；"
                     "任一项失效返回 409 APPEAL_STALE 且整案不写入",
        "immutability": "原反馈包内容永不改写；得分变化时新建 kind=correction "
                        "的已发布更正包，旧包只写 superseded_by 指针，"
                        "沿用反馈包既有的线性替代关系",
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
                   status: int = 200,
                   download_name: str | None = None) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{download_name}"')
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
            m = re.fullmatch(r"/api/batches/([^/]+)/clock-analysis", path)
            if m and method == "GET":
                return self._clock_analysis(m.group(1))
            m = re.fullmatch(r"/api/batches/([^/]+)/clock-schemes", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._list_schemes(bid)
                if method == "POST":
                    return self._create_scheme(bid)
            # 字面量路由必须先于 {sid} 通配
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/preview", path)
            if m and method == "POST":
                return self._preview_scheme(m.group(1), None)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/compare", path)
            if m and method == "GET":
                return self._compare_schemes(m.group(1))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/([^/]+)/activate", path)
            if m and method == "POST":
                return self._activate_scheme(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/([^/]+)/deactivate", path)
            if m and method == "POST":
                return self._deactivate_scheme(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/([^/]+)/preview", path)
            if m and method == "POST":
                return self._preview_scheme(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/clock-schemes/([^/]+)", path)
            if m:
                bid, sid = m.group(1), m.group(2)
                if method == "GET":
                    return self._get_scheme(bid, sid)
                if method == "PUT":
                    return self._update_scheme(bid, sid)
                if method == "DELETE":
                    return self._delete_scheme(bid, sid)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/archived-decisions", path)
            if m and method == "GET":
                return self._list_archived(m.group(1))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/archived-decisions/([^/]+)", path)
            if m and method == "DELETE":
                return self._dismiss_archived(m.group(1), m.group(2))
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
            # 站级赛后反馈包：字面量后缀路由先于 {rid} 通配
            m = re.fullmatch(r"/api/batches/([^/]+)/feedback", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._list_feedback(bid)
                if method == "POST":
                    return self._generate_feedback(bid)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/feedback/([^/]+)/preview", path)
            if m and method == "POST":
                return self._preview_feedback(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/feedback/([^/]+)/publish", path)
            if m and method == "POST":
                return self._publish_feedback(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/feedback/([^/]+)/download", path)
            if m and method == "GET":
                return self._download_feedback(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/feedback/([^/]+)/lineage", path)
            if m and method == "GET":
                return self._feedback_lineage(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/batches/([^/]+)/feedback/([^/]+)", path)
            if m and method == "GET":
                return self._get_feedback(m.group(1), m.group(2))
            # 赛后复议案件：字面量后缀路由先于 {cid} 通配
            m = re.fullmatch(r"/api/batches/([^/]+)/appeals", path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    return self._list_appeals(bid)
                if method == "POST":
                    return self._create_appeal(bid)
            m = re.fullmatch(
                r"/api/batches/([^/]+)/appeals/([^/]+)/accept", path)
            if m and method == "POST":
                return self._accept_appeal(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/appeals/([^/]+)/withdraw", path)
            if m and method == "POST":
                return self._withdraw_appeal(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/appeals/([^/]+)/preview", path)
            if m and method == "POST":
                return self._preview_appeal(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/appeals/([^/]+)/confirm", path)
            if m and method == "POST":
                return self._confirm_appeal(m.group(1), m.group(2))
            m = re.fullmatch(
                r"/api/batches/([^/]+)/appeals/([^/]+)/download", path)
            if m and method == "GET":
                return self._download_appeal(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/batches/([^/]+)/appeals/([^/]+)", path)
            if m and method == "GET":
                return self._get_appeal(m.group(1), m.group(2))
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
        except AppealStale as exc:
            self._send_json({"error": "APPEAL_STALE",
                             "message": "案件绑定依据已失效，整案不写入；"
                                        "请核对后重新预览/确认，或针对最新"
                                        "反馈包重新提案",
                             "details": exc.problems}, HTTPStatus.CONFLICT)
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

    def _compute_results(self, batch: dict[str, Any],
                         offsets_override: dict[str, int] | None = None
                         ) -> dict[str, Any]:
        submissions = self.storage.get_submissions(batch["id"])
        if offsets_override is None:
            scheme = self.storage.get_active_clock_scheme(batch["id"])
            offsets = ({k: v * 60 for k, v in scheme["offsets"].items()}
                       if scheme else None)
        else:
            offsets = offsets_override
        raw = adjudicate(batch["rules"], submissions,
                         time_offsets=offsets)["findings"]
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

    def _rerun_with_archiving(self, bid: str,
                              scheme_id: str | None = None) -> dict[str, Any]:
        """校正方案启用/停用/修改后的重跑：失去依据的裁决归档待复核。

        裁决的 finding 在新配对中消失（含状态改变——finding id 含状态）
        即视为失去依据，移入 decision_archive，绝不静默沿用。
        """
        batch = self._get_batch_or_404(bid)
        old_findings = self.storage.list_findings(bid)
        old_status = {f["id"]: f["status"] for f in old_findings}
        old_refs = {f["id"]: _refs_key(f) for f in old_findings}
        computed = self._compute_results(batch)
        new_findings = computed["findings"]
        new_ids = {f["id"] for f in new_findings}
        new_status_by_refs: dict[tuple, str] = {}
        for f in new_findings:
            new_status_by_refs.setdefault(_refs_key(f), f["status"])
        archived = []
        for fid, dec in sorted(self.storage.list_decisions(bid).items()):
            if fid in new_ids:
                continue
            old_st = old_status.get(fid)
            became = new_status_by_refs.get(old_refs.get(fid))
            if old_st and became and became != old_st:
                reason = (f"时钟校正方案变化后，原证据状态由 {old_st} 变为 "
                          f"{became}，原裁决失去依据，归档待复核")
            else:
                reason = (f"时钟校正方案变化后，原证据（{old_st or '未知状态'}）"
                          f"在新配对中已不存在，原裁决失去依据，归档待复核")
            archived.append(self.storage.archive_decision(
                new_id("A"), bid, fid, dec, reason, scheme_id))
        self.storage.replace_findings(bid, new_findings)
        self.storage.touch_batch(bid)
        return {"computed": computed, "archived": archived,
                "status_before": _status_counts(old_findings)}

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
        active_scheme = self.storage.get_active_clock_scheme(bid)
        self._send_json({
            "batch": _public_batch(batch),
            "logs": log_summaries,
            "findings_count": self.storage.findings_count(bid),
            "pending_count": self._pending_count(bid),
            "decisions_count": len(self.storage.list_decisions(bid)),
            "versions": self.storage.list_versions(bid),
            "active_clock_scheme": (_public_scheme(active_scheme)
                                    if active_scheme else None),
            "clock_schemes_count": len(self.storage.list_clock_schemes(bid)),
            "archived_decisions_count":
                len(self.storage.list_archived_decisions(bid)),
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

    # -- 时钟偏差分析与校正方案 ---------------------------------------------
    def _get_scheme_or_404(self, bid: str, sid: str) -> dict[str, Any]:
        scheme = self.storage.get_clock_scheme(sid)
        if not scheme or scheme["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "SCHEME_NOT_FOUND",
                           f"校正方案 {sid} 不存在")
        return scheme

    def _validate_offsets(self, bid: str, offsets: Any,
                          reference_log_id: str | None = None
                          ) -> dict[str, int]:
        """校验 {日志ID: 整分钟} 偏移表，返回去掉零值的规范化 dict。"""
        if not isinstance(offsets, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                           "offsets 必须是 {日志ID: 整分钟} 对象")
        log_ids = {l["id"] for l in self.storage.list_logs(bid)}
        out: dict[str, int] = {}
        for lid, mins in offsets.items():
            if lid not in log_ids:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                               f"日志 {lid} 不在批次 {bid} 中")
            if isinstance(mins, bool) or not isinstance(mins, int):
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                               f"偏移必须以整分钟为单位（整数）："
                               f"{lid}={mins!r}")
            if abs(mins) > 720:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                               f"偏移超过 ±720 分钟上限：{lid}={mins}")
            if lid == reference_log_id and mins != 0:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                               "参考日志的偏移必须为 0")
            if mins:
                out[lid] = mins
        return out

    def _clock_analysis(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        q = self._query()
        ref = q.get("reference_log_id")
        if not ref:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_PARAM",
                           "需要查询参数 reference_log_id=<日志ID>")
        log = self.storage.get_log(ref)
        if not log or log["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "LOG_NOT_FOUND",
                           f"参考日志 {ref} 不在批次 {bid} 中")
        default_window = int(batch["rules"].get("pairing", {})
                             .get("near_window_seconds", 1800))
        try:
            window = int(q.get("max_window_seconds") or default_window)
            min_samples = int(q.get("min_samples") or DEFAULT_MIN_SAMPLES)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "max_window_seconds/min_samples 必须是整数") from exc
        if window <= 0 or min_samples < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "max_window_seconds 必须为正，min_samples 至少为 1")
        submissions = self.storage.get_submissions(bid)
        report = analyze_clock_skew(submissions, ref, window,
                                    min_samples=min_samples)
        report["batch_id"] = bid
        self._send_json(report)

    def _create_scheme(self, bid: str) -> None:
        self.storage.require_open(bid)
        batch = self._get_batch_or_404(bid)
        body = self._json_body()
        ref = str(body.get("reference_log_id") or "")
        log = self.storage.get_log(ref) if ref else None
        if not log or log["batch_id"] != bid:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_REFERENCE",
                           "reference_log_id 必须是批次内某份日志的 ID")
        window = body.get("max_window_seconds")
        if window is None:
            window = int(batch["rules"].get("pairing", {})
                         .get("near_window_seconds", 1800))
        if isinstance(window, bool) or not isinstance(window, int) \
                or window <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_WINDOW",
                           "max_window_seconds 必须是正整数秒")
        min_samples = body.get("min_samples") or DEFAULT_MIN_SAMPLES
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) \
                or min_samples < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "min_samples 必须是正整数")
        offsets = self._validate_offsets(bid, body.get("offsets") or {},
                                         reference_log_id=ref)
        submissions = self.storage.get_submissions(bid)
        analysis = analyze_clock_skew(submissions, ref, window,
                                      min_samples=min_samples)
        if body.get("use_suggested"):
            # 显式 offsets 优先；未指定的日志用分析建议填充
            for lid, mins in analysis["suggested_offsets_minutes"].items():
                offsets.setdefault(lid, mins)
        name = str(body.get("name") or "").strip() or \
            f"时钟校正方案（参考 {log['filename']}）"
        sid = new_id("S")
        scheme = self.storage.create_clock_scheme(
            sid, bid, name, ref, window, offsets, analysis)
        resp: dict[str, Any] = {
            "scheme": _public_scheme(scheme),
            "analysis": analysis,
        }
        if body.get("activate"):
            self.storage.set_active_clock_scheme(bid, sid)
            outcome = self._rerun_with_archiving(bid, scheme_id=sid)
            # 重新读取持久化后的方案，避免把创建时的 active=False 旧快照
            # 返回给客户端
            resp["scheme"] = _public_scheme(
                self.storage.get_clock_scheme(sid))
            resp["activated"] = True
            resp["status_counts"] = {
                "before": outcome["status_before"],
                "after": _status_counts(outcome["computed"]["findings"])}
            resp["archived_decisions"] = outcome["archived"]
            resp["results_summary"] = outcome["computed"]["results"]["summary"]
        self._send_json(resp, HTTPStatus.CREATED)

    def _list_schemes(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        schemes = self.storage.list_clock_schemes(bid)
        self._send_json({"schemes": [_public_scheme(s) for s in schemes],
                         "count": len(schemes)})

    def _get_scheme(self, bid: str, sid: str) -> None:
        self._get_batch_or_404(bid)
        scheme = self._get_scheme_or_404(bid, sid)
        self._send_json({"scheme": {**_public_scheme(scheme),
                                    "analysis": scheme["analysis"]}})

    def _update_scheme(self, bid: str, sid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        scheme = self._get_scheme_or_404(bid, sid)
        body = self._json_body()
        name = body.get("name")
        if name is not None:
            name = str(name).strip() or scheme["name"]
        offsets = None
        if "offsets" in body:
            offsets = self._validate_offsets(
                bid, body["offsets"] or {},
                reference_log_id=scheme["reference_log_id"])
        self.storage.update_clock_scheme(sid, name=name, offsets=offsets)
        updated = self.storage.get_clock_scheme(sid)
        resp: dict[str, Any] = {"scheme": _public_scheme(updated)}
        if updated["active"] and offsets is not None:
            outcome = self._rerun_with_archiving(bid, scheme_id=sid)
            resp["status_counts"] = {
                "before": outcome["status_before"],
                "after": _status_counts(outcome["computed"]["findings"])}
            resp["archived_decisions"] = outcome["archived"]
            resp["results_summary"] = outcome["computed"]["results"]["summary"]
        self._send_json(resp)

    def _delete_scheme(self, bid: str, sid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        scheme = self._get_scheme_or_404(bid, sid)
        if scheme["active"]:
            raise ApiError(HTTPStatus.CONFLICT, "SCHEME_ACTIVE",
                           "方案处于启用状态，请先停用再删除")
        self.storage.delete_clock_scheme(sid)
        self._send_json({"deleted": sid})

    def _activate_scheme(self, bid: str, sid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        scheme = self._get_scheme_or_404(bid, sid)
        if scheme["active"]:
            return self._send_json({"scheme": _public_scheme(scheme),
                                    "already_active": True})
        self.storage.set_active_clock_scheme(bid, sid)
        outcome = self._rerun_with_archiving(bid, scheme_id=sid)
        self._send_json({
            "scheme": _public_scheme(self.storage.get_clock_scheme(sid)),
            "status_counts": {
                "before": outcome["status_before"],
                "after": _status_counts(outcome["computed"]["findings"])},
            "archived_decisions": outcome["archived"],
            "results_summary": outcome["computed"]["results"]["summary"]})

    def _deactivate_scheme(self, bid: str, sid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        scheme = self._get_scheme_or_404(bid, sid)
        if not scheme["active"]:
            raise ApiError(HTTPStatus.CONFLICT, "SCHEME_NOT_ACTIVE",
                           f"方案 {sid} 未启用")
        self.storage.set_active_clock_scheme(bid, None)
        outcome = self._rerun_with_archiving(bid, scheme_id=sid)
        self._send_json({
            "deactivated": sid,
            "status_counts": {
                "before": outcome["status_before"],
                "after": _status_counts(outcome["computed"]["findings"])},
            "archived_decisions": outcome["archived"],
            "results_summary": outcome["computed"]["results"]["summary"]})

    def _preview_scheme(self, bid: str, sid: str | None) -> None:
        # 只读预览：批次锁定后仍可用，且不改动任何数据
        batch = self._get_batch_or_404(bid)
        body = self._json_body()
        if sid is not None:
            scheme = self._get_scheme_or_404(bid, sid)
            offsets = scheme["offsets"]
            if "offsets" in body:
                offsets = self._validate_offsets(
                    bid, body["offsets"] or {},
                    reference_log_id=scheme["reference_log_id"])
        else:
            if not isinstance(body.get("offsets"), dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_OFFSETS",
                               "预览需要 offsets 对象（{日志ID: 整分钟}）")
            offsets = self._validate_offsets(bid, body["offsets"])
        current = self._compute_results(batch)
        preview = self._compute_results(
            batch, offsets_override={k: v * 60 for k, v in offsets.items()})
        decisions = self.storage.list_decisions(bid)
        preview_ids = {f["id"] for f in preview["findings"]}
        at_risk = [{"finding_id": fid, "resolution": d["resolution"],
                    "reason": d["reason"]}
                   for fid, d in sorted(decisions.items())
                   if fid not in preview_ids]
        self._send_json({
            "offsets_minutes": offsets,
            "current": {
                "status_counts": _status_counts(current["findings"]),
                "findings_count": len(current["findings"]),
                "results_summary": current["results"]["summary"]},
            "preview": {
                "status_counts": _status_counts(preview["findings"]),
                "findings_count": len(preview["findings"]),
                "results_summary": preview["results"]["summary"]},
            "scorecard_diff": _diff_scorecards(
                current["results"]["scorecards"],
                preview["results"]["scorecards"]),
            "decisions_at_risk": at_risk})

    def _compare_schemes(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        q = self._query()
        if "a" not in q or "b" not in q:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_PARAM",
                           "需要查询参数 a=<方案ID>&b=<方案ID>")
        sa = self._get_scheme_or_404(bid, q["a"])
        sb = self._get_scheme_or_404(bid, q["b"])
        ca = self._compute_results(
            batch, offsets_override={k: v * 60
                                     for k, v in sa["offsets"].items()})
        cb = self._compute_results(
            batch, offsets_override={k: v * 60
                                     for k, v in sb["offsets"].items()})
        self._send_json({
            "a": {"scheme": _public_scheme(sa),
                  "status_counts": _status_counts(ca["findings"]),
                  "results_summary": ca["results"]["summary"]},
            "b": {"scheme": _public_scheme(sb),
                  "status_counts": _status_counts(cb["findings"]),
                  "results_summary": cb["results"]["summary"]},
            "scorecard_diff": _diff_scorecards(
                ca["results"]["scorecards"], cb["results"]["scorecards"])})

    def _list_archived(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        archived = self.storage.list_archived_decisions(bid)
        self._send_json({"archived_decisions": archived,
                         "count": len(archived)})

    def _dismiss_archived(self, bid: str, aid: str) -> None:
        self.storage.require_open(bid)
        self._get_batch_or_404(bid)
        if not self.storage.delete_archived_decision(bid, aid):
            raise ApiError(HTTPStatus.NOT_FOUND, "ARCHIVE_NOT_FOUND",
                           f"归档裁决 {aid} 不存在")
        self._send_json({"dismissed": aid})

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
        penalty = body.get("penalty_code")
        if penalty is not None:
            penalty = str(penalty)
        problems = validate_judge_decision(
            batch["rules"], finding, resolution=resolution,
            reason=reason, fault_station=fault, penalty_code=penalty)
        # 保持既有错误码粒度：逐类返回与旧接口一致的 400
        if problems:
            if resolution not in RESOLUTIONS:
                raise ApiError(
                    HTTPStatus.BAD_REQUEST, "BAD_RESOLUTION", problems[0])
            if not reason.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, "REASON_REQUIRED",
                               problems[0])
            p = next((x for x in problems if "fault_station" in x), None)
            if p:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_FAULT_STATION", p)
            p = next((x for x in problems if "penalty_code" in x), None)
            if p:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PENALTY_CODE", p)
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "BAD_RESOLUTION_FOR_STATUS",
                problems[0])
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
        # 启用中的校正方案随版本持久化（哈希只含影响结果的内容）
        scheme = self.storage.get_active_clock_scheme(bid)
        scheme_slim = None
        if scheme:
            scheme_slim = {"reference_log_id": scheme["reference_log_id"],
                           "max_window_seconds": scheme["max_window_seconds"],
                           "offsets": scheme["offsets"]}
        digest = content_hash(batch["rules"], computed["findings"],
                              decisions, computed["results"],
                              clock_scheme=scheme_slim)
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
        # 冻结时在场的日志清单随快照持久化：旧版本的反馈包只取这些日志，
        # 冻结后上传的日志不得混入（版本-日志绑定）
        log_manifest = [
            {"log_id": s["log_id"], "filename": s["filename"],
             "station_call": s["station_call"], "upload_ts": s["upload_ts"]}
            for s in self.storage.get_submissions(bid)]
        snapshot = {
            "batch_id": bid, "version_no": no, "content_hash": digest,
            "batch_name": batch["name"], "rules": batch["rules"],
            "decisions": decisions,
            "findings": computed["findings"],
            "results": computed["results"],
            "logs": log_manifest,
            "clock_scheme": ({**scheme_slim, "id": scheme["id"],
                              "name": scheme["name"]}
                             if scheme else None),
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
        scheme = self.storage.get_active_clock_scheme(bid)
        scheme_slim = None
        if scheme:
            scheme_slim = {"reference_log_id": scheme["reference_log_id"],
                           "max_window_seconds": scheme["max_window_seconds"],
                           "offsets": scheme["offsets"]}
        digest = content_hash(batch["rules"], computed["findings"],
                              decisions, computed["results"],
                              clock_scheme=scheme_slim)
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
            "clock_schemes": [{**_public_scheme(s),
                               "analysis": s["analysis"]}
                              for s in self.storage.list_clock_schemes(bid)],
            "active_clock_scheme": (_public_scheme(scheme)
                                    if scheme else None),
            "archived_decisions": self.storage.list_archived_decisions(bid),
            "feedback_reports": [
                _slim_feedback(self.storage.get_feedback_report(r["id"]))
                for r in self.storage.list_feedback_reports(bid)],
            "appeals": [self._appeal_payload(c)
                        for c in self.storage.list_appeal_cases(bid)],
            "content_hash": digest,
        }
        self._send_json(payload,
                        download_name=f"{bid}-adjudication.json")

    # -- 站级赛后反馈包 ------------------------------------------------------
    # 输入为冻结的计分版本快照与参赛日志；生成/预览/发布均不改动批次数据，
    # 因此批次锁定后仍可正常使用（赛后场景）。
    def _get_feedback_or_404(self, bid: str, rid: str) -> dict[str, Any]:
        rep = self.storage.get_feedback_report(rid)
        if not rep or rep["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "FEEDBACK_NOT_FOUND",
                           f"反馈包 {rid} 不存在")
        return rep

    def _generate_feedback(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        body = self._json_body()
        if "version_no" not in body:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_PARAM",
                           "需要 version_no（冻结的计分版本号）作为输入")
        try:
            version_no = int(body["version_no"])
        except (TypeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "version_no 必须是整数") from exc
        ver = self.storage.get_version(bid, version_no)
        if not ver:
            raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                           f"计分版本 {version_no} 不存在；请先用 "
                           f"POST /api/batches/{bid}/versions 冻结版本")
        corrects_id = body.get("corrects")
        station_param = body.get("station")
        submissions = self.storage.get_submissions(bid)
        # 版本-日志绑定：旧版本的报告只取冻结当时的日志，
        # 冻结之后上传的日志不得混入
        bound = version_bound_logs(ver, submissions)
        if station_param is not None:
            st = normalize_callsign(str(station_param))
            if not st:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_STATION",
                               f"台站呼号 {station_param!r} 无法归一化")
            stations = [st]
        else:
            stations = sorted({s["station_call"] for s in bound
                               if s["station_call"]})
            if not stations:
                if submissions:
                    raise ApiError(
                        HTTPStatus.BAD_REQUEST, "NO_LOGS",
                        f"计分版本 v{version_no} 冻结时批次内没有日志；"
                        f"之后上传的日志不参与旧版本反馈包")
                raise ApiError(HTTPStatus.BAD_REQUEST, "NO_LOGS",
                               "批次内还没有任何日志")

        out = []
        any_created = False
        for st in stations:
            logs = [s for s in bound if s["station_call"] == st]
            if not logs:
                if station_param is not None:
                    if any(s["station_call"] == st for s in submissions):
                        raise ApiError(
                            HTTPStatus.NOT_FOUND, "NO_STATION_LOG",
                            f"台站 {st} 的日志上传于计分版本 v{version_no} "
                            f"冻结之后；该版本的反馈包只取冻结当时的日志")
                    raise ApiError(HTTPStatus.NOT_FOUND, "NO_STATION_LOG",
                                   f"台站 {st} 在本批次没有提交日志")
                continue
            latest_pub = self.storage.latest_published_feedback(bid, st)
            kind, corrects = "normal", None
            if corrects_id:
                old = self.storage.get_feedback_report(str(corrects_id))
                if not old or old["batch_id"] != bid:
                    raise ApiError(HTTPStatus.NOT_FOUND,
                                   "FEEDBACK_NOT_FOUND",
                                   f"被更正的反馈包 {corrects_id} 不存在")
                if old["station"] != st:
                    raise ApiError(HTTPStatus.BAD_REQUEST,
                                   "FEEDBACK_STATION_MISMATCH",
                                   f"旧包 {corrects_id} 属于 {old['station']}，"
                                   f"与目标台站 {st} 不符")
                if old["status"] != "published":
                    raise ApiError(HTTPStatus.CONFLICT,
                                   "FEEDBACK_NOT_PUBLISHED",
                                   "只能更正已发布的反馈包；草稿请直接重新生成")
                if old["version_no"] == version_no:
                    raise ApiError(HTTPStatus.BAD_REQUEST,
                                   "FEEDBACK_SAME_VERSION",
                                   "更正包须基于与旧包不同的计分版本")
                if latest_pub and latest_pub["id"] != old["id"]:
                    raise ApiError(
                        HTTPStatus.CONFLICT, "FEEDBACK_CHAIN",
                        f"只能替代该台站最新发布的包 {latest_pub['id']}，"
                        f"以保更正链线性可追踪")
                kind, corrects = "correction", old
            elif latest_pub and latest_pub["version_no"] != version_no:
                # 计分版本已演进：不改写已发布报告，自动生成注明旧包与
                # 替代版本的更正包
                kind, corrects = "correction", latest_pub

            report = build_station_report(
                batch=batch, version=ver, station=st, logs=logs,
                kind=kind, corrects=corrects)
            digest = report_content_hash(report)
            existing = self.storage.find_feedback_by_hash(bid, st, digest)
            if existing:
                out.append({"report": _slim_feedback(existing),
                            "identical_to": existing["id"]})
                continue
            rid = new_id("R")
            self.storage.create_feedback_report(
                rid, bid, st, version_no, kind,
                corrects["id"] if corrects else None,
                corrects["version_no"] if corrects else None,
                digest, report)
            if corrects:
                # 只记录替代关系指针，旧报告内容保持不可变
                self.storage.set_feedback_superseded(corrects["id"], rid)
            any_created = True
            out.append({"report": _slim_feedback(
                self.storage.get_feedback_report(rid)), "created": True})

        self._send_json({"batch_id": bid, "version_no": version_no,
                         "reports": out, "count": len(out)},
                        HTTPStatus.CREATED if any_created else HTTPStatus.OK)

    def _list_feedback(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        q = self._query()
        version_no = None
        if q.get("version_no"):
            try:
                version_no = int(q["version_no"])
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                               "version_no 必须是整数") from exc
        status = q.get("status")
        if status and status not in ("draft", "published"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "status 只能是 draft 或 published")
        kind = q.get("kind")
        if kind and kind not in ("normal", "correction"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "kind 只能是 normal 或 correction")
        station = q.get("station")
        if station:
            station = normalize_callsign(station) or station.upper()
        reports = self.storage.list_feedback_reports(
            bid, station=station, status=status,
            version_no=version_no, kind=kind)
        self._send_json({"reports": reports, "count": len(reports)})

    def _get_feedback(self, bid: str, rid: str) -> None:
        self._get_batch_or_404(bid)
        rep = self._get_feedback_or_404(bid, rid)
        view = self._query().get("view", "internal")
        if view == "external":
            if not rep["external"]:
                raise ApiError(HTTPStatus.CONFLICT, "FEEDBACK_NOT_PREVIEWED",
                               "尚未生成对外预览：请先 POST .../preview")
            content = rep["external"]
        elif view == "internal":
            content = rep["report"]
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "view 只能是 internal 或 external")
        self._send_json({
            "report": _slim_feedback(rep), "view": view,
            "lineage": {"corrects": rep["corrects_report_id"],
                        "superseded_by": rep["superseded_by"]},
            "content": content})

    def _preview_feedback(self, bid: str, rid: str) -> None:
        self._get_batch_or_404(bid)
        rep = self._get_feedback_or_404(bid, rid)
        if rep["status"] == "published":
            raise ApiError(HTTPStatus.CONFLICT, "FEEDBACK_IMMUTABLE",
                           "反馈包已发布，不可再变动；"
                           "如需更新请对新计分版本生成更正包")
        external = build_external_report(rep["report"])
        self.storage.set_feedback_external(rid, external)
        self._send_json({"report_id": rid, "previewed": True,
                         "external": external})

    def _publish_feedback(self, bid: str, rid: str) -> None:
        self._get_batch_or_404(bid)
        rep = self._get_feedback_or_404(bid, rid)
        if rep["status"] == "published":
            return self._send_json({"report": _slim_feedback(rep),
                                    "already_published": True})
        if not rep["external"]:
            raise ApiError(HTTPStatus.CONFLICT, "FEEDBACK_NOT_PREVIEWED",
                           "草稿预览后才能发布：请先 POST .../preview 生成并"
                           "核对对外脱敏稿")
        self.storage.set_feedback_published(rid)
        self._send_json({"report": _slim_feedback(
            self.storage.get_feedback_report(rid)), "published": True})

    def _download_feedback(self, bid: str, rid: str) -> None:
        self._get_batch_or_404(bid)
        rep = self._get_feedback_or_404(bid, rid)
        q = self._query()
        view = q.get("view")
        if view is None:
            view = "external" if rep["status"] == "published" else "internal"
        if view == "external":
            if not rep["external"]:
                raise ApiError(HTTPStatus.CONFLICT, "FEEDBACK_NOT_PREVIEWED",
                               "尚未生成对外预览：请先 POST .../preview")
            content = rep["external"]
        elif view == "internal":
            content = rep["report"]
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                           "view 只能是 internal 或 external")
        fmt = q.get("format", "json")
        stem = f"{rep['station']}-v{rep['version_no']}-feedback-{view}"
        if fmt == "txt":
            return self._send_text(render_text(content), "text/plain",
                                   download_name=f"{stem}.txt")
        if fmt == "json":
            return self._send_json(content, download_name=f"{stem}.json")
        raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_PARAM",
                       "format 只能是 json 或 txt")

    def _feedback_lineage(self, bid: str, rid: str) -> None:
        self._get_batch_or_404(bid)
        rep = self._get_feedback_or_404(bid, rid)
        back: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = rep
        seen: set[str] = set()
        while cur and cur.get("corrects_report_id") \
                and cur["corrects_report_id"] not in seen:
            seen.add(cur["corrects_report_id"])
            cur = self.storage.get_feedback_report(cur["corrects_report_id"])
            if not cur or cur["batch_id"] != bid:
                break
            back.append(cur)
        back.reverse()
        fwd: list[dict[str, Any]] = []
        cur = rep
        seen = set()
        while cur and cur.get("superseded_by") \
                and cur["superseded_by"] not in seen:
            seen.add(cur["superseded_by"])
            cur = self.storage.get_feedback_report(cur["superseded_by"])
            if not cur or cur["batch_id"] != bid:
                break
            fwd.append(cur)
        chain = [_slim_feedback(r) for r in [*back, rep, *fwd]]
        self._send_json({"report_id": rid, "chain": chain,
                         "length": len(chain)})

    # -- 赛后复议案件 --------------------------------------------------------
    def _get_appeal_or_404(self, bid: str, cid: str) -> dict[str, Any]:
        case = self.storage.get_appeal_case(cid)
        if not case or case["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "APPEAL_NOT_FOUND",
                           f"复议案件 {cid} 不存在")
        return case

    def _bound_version(self, case: dict[str, Any]) -> dict[str, Any]:
        ver = self.storage.get_version(case["batch_id"], case["version_no"])
        if not ver:
            raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                           f"案件绑定的计分版本 v{case['version_no']} 不存在")
        return ver

    def _normalize_claims(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ApiError(HTTPStatus.BAD_REQUEST, "CLAIMS_REQUIRED",
                           "claims 必须是非空数组（每项一条异议主张）")
        out: list[dict[str, Any]] = []
        for n, c in enumerate(raw, start=1):
            if not isinstance(c, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_CLAIM",
                               f"第 {n} 项争议必须是对象")
            refs_in = c.get("log_refs") or []
            refs: list[dict[str, Any]] = []
            if refs_in:
                if not isinstance(refs_in, list):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_LOG_REFS",
                                   f"第 {n} 项 log_refs 必须是数组")
                for ref in refs_in:
                    if not isinstance(ref, dict) or "filename" not in ref \
                            or "line" not in ref:
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST, "BAD_LOG_REF",
                            f"第 {n} 项日志引用须为 {{filename, line}}")
                    line = ref["line"]
                    if isinstance(line, bool) or not isinstance(line, int):
                        raise ApiError(
                            HTTPStatus.BAD_REQUEST, "BAD_LOG_REF",
                            f"第 {n} 项日志引用行号必须是整数：{line!r}")
                    refs.append({"filename": str(ref["filename"]),
                                 "line": line})
            fid = c.get("finding_id")
            out.append({
                "id": new_id("CL"),
                "subject": str(c.get("subject") or ""),
                "summary": str(c.get("summary") or ""),
                "finding_id": str(fid) if fid else None,
                "log_refs": refs,
            })
        return out

    def _create_appeal(self, bid: str) -> None:
        batch = self._get_batch_or_404(bid)
        body = self._json_body()
        rid = body.get("report_id")
        if not rid:
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_PARAM",
                           "需要 report_id（被异议的已发布反馈包 ID）")
        rep = self.storage.get_feedback_report(str(rid))
        if not rep or rep["batch_id"] != bid:
            raise ApiError(HTTPStatus.NOT_FOUND, "FEEDBACK_NOT_FOUND",
                           f"反馈包 {rid} 不存在")
        if rep["status"] != "published":
            raise ApiError(
                HTTPStatus.CONFLICT, "FEEDBACK_NOT_PUBLISHED",
                f"反馈包 {rid} 尚未发布；复议只受理针对已发布反馈包的异议，"
                f"草稿问题请直接在裁判流程中处理")
        # 反馈包已有后续更正：明确提示，不自动改绑到更正包
        if rep.get("superseded_by"):
            newer = self.storage.get_feedback_report(rep["superseded_by"])
            hint = (f"（最新包 {newer['id']}，基于计分版本 "
                    f"v{newer['version_no']}）") if newer else ""
            raise ApiError(
                HTTPStatus.CONFLICT, "REPORT_SUPERSEDED",
                f"反馈包 {rid} 已有后续更正包 {rep['superseded_by']}{hint}，"
                f"原包不再受理复议；请针对最新发布的反馈包重新提案"
                f"（系统不会自动改绑）")
        ver = self.storage.get_version(bid, rep["version_no"])
        if not ver:
            raise ApiError(HTTPStatus.NOT_FOUND, "VERSION_NOT_FOUND",
                           f"反馈包绑定的计分版本 v{rep['version_no']} 不存在")
        # 存储的包哈希与重算值必须一致（不可变完整性自检）
        if rep["content_hash"] != report_content_hash(rep["report"]):
            raise ApiError(
                HTTPStatus.CONFLICT, "REPORT_HASH_MISMATCH",
                f"反馈包 {rid} 的内容哈希与现存正文不一致，无法建立绑定")
        prior = self.storage.list_appeal_cases(
            bid, station=rep["station"], report_id=rep["id"])
        open_case = next((c for c in prior
                          if c["status"] in ("submitted", "in_review")), None)
        if open_case:
            raise ApiError(
                HTTPStatus.CONFLICT, "APPEAL_ALREADY_EXISTS",
                f"反馈包 {rid} 已有 {open_case['status']} 状态的案件 "
                f"{open_case['id']}；已撤回或已结案后可重新提案")

        claims = self._normalize_claims(body.get("claims"))
        problems = validate_claims(
            claims, station=rep["station"], report=rep["report"],
            version_snapshot=ver["snapshot"])
        if any(problems):
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "INVALID_CLAIMS",
                "争议项引用校验未通过；系统只提示问题，不自动改绑，请修正后"
                "重新提交",
                [{"seq": i + 1, "problems": p}
                 for i, p in enumerate(problems) if p])

        applicant = body.get("applicant")
        applicant = str(applicant).strip() if applicant else None
        binding_hash = case_content_hash(
            report_id=rep["id"], station=rep["station"],
            version_no=rep["version_no"],
            version_content_hash=ver["content_hash"],
            report_content_hash=rep["content_hash"], claims=claims)
        cid = new_id("CASE")
        case = self.storage.create_appeal_case(
            cid, bid, rep["station"], rep["id"], rep["version_no"],
            ver["content_hash"], rep["content_hash"], binding_hash,
            claims, applicant)

        warnings: list[str] = []
        latest_ver = self.storage.list_versions(bid)[-1]["version_no"] \
            if self.storage.list_versions(bid) else rep["version_no"]
        if latest_ver > rep["version_no"]:
            warnings.append(
                f"批次已冻结至计分版本 v{latest_ver}（本包基于 "
                f"v{rep['version_no']}）；该反馈包尚无后续更正包，"
                f"案件仍按 v{rep['version_no']} 绑定快照受理")
        self._send_json(
            {"case": _slim_appeal(case),
             "binding": {"report_id": rep["id"],
                         "report_content_hash": rep["content_hash"],
                         "version_no": rep["version_no"],
                         "version_content_hash": ver["content_hash"],
                         "binding_content_hash": binding_hash},
             "claims": self.storage.list_appeal_claims(cid),
             "warnings": warnings}, HTTPStatus.CREATED)

    def _list_appeals(self, bid: str) -> None:
        self._get_batch_or_404(bid)
        q = self._query()
        status = q.get("status")
        if status and status not in CASE_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_STATUS",
                           f"status 必须是 {list(CASE_STATUSES)} 之一")
        station = q.get("station")
        if station:
            station = normalize_callsign(station) or station.upper()
        cases = self.storage.list_appeal_cases(
            bid, status=status, station=station, report_id=q.get("report_id"))
        out = []
        for c in cases:
            item = _slim_appeal(c)
            claims = self.storage.list_appeal_claims(c["id"])
            item["claim_count"] = len(claims)
            item["subjects"] = sorted({cl["subject"] for cl in claims})
            out.append(item)
        self._send_json({"cases": out, "count": len(out)})

    def _appeal_payload(self, case: dict[str, Any]) -> dict[str, Any]:
        rep = self.storage.get_feedback_report(case["report_id"])
        ver = self.storage.get_version(case["batch_id"], case["version_no"])
        claims = []
        for cl in self.storage.list_appeal_claims(case["id"]):
            claims.append({**cl, "subject_label":
                           SUBJECT_LABELS.get(cl["subject"]),
                           "conclusion_label":
                           CONCLUSION_LABELS.get(cl["conclusion"] or "")})
        outcome = None
        if case["status"] == "closed":
            correction = None
            if case.get("correction_report_id"):
                corr = self.storage.get_feedback_report(
                    case["correction_report_id"])
                correction = _slim_feedback(corr) if corr else None
            outcome = {
                "resolved_version_no": case["resolved_version_no"],
                "correction_report": correction,
                "score_before": case["score_before"],
                "score_after": case["score_after"],
                "score_delta": ((case["score_after"] or 0)
                                - (case["score_before"] or 0)),
                "revised": [{"seq": cl["seq"], "finding_id": cl["finding_id"],
                             "resolution": cl["resolution"],
                             "rationale": cl["rationale"]}
                            for cl in claims if cl["conclusion"] == "revised"]}
        return {
            "case": _slim_appeal(case),
            "claims": claims,
            "events": self.storage.list_appeal_events(case["id"]),
            "binding": {
                "report": _slim_feedback(rep) if rep else None,
                "version": ({"version_no": ver["version_no"],
                             "content_hash": ver["content_hash"],
                             "note": ver["note"],
                             "created_ts": ver["created_ts"]}
                            if ver else None),
                "binding_content_hash": case["binding_content_hash"]},
            "outcome": outcome}

    def _get_appeal(self, bid: str, cid: str) -> None:
        self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        self._send_json(self._appeal_payload(case))

    def _accept_appeal(self, bid: str, cid: str) -> None:
        self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        if case["status"] != "submitted":
            raise ApiError(
                HTTPStatus.CONFLICT, "APPEAL_NOT_SUBMITTED",
                f"案件当前为 {case['status']}，仅 submitted 可受理")
        body = self._json_body() if self.headers.get("Content-Length") else {}
        judge = str(body.get("judge") or "").strip() or None
        self.storage.set_appeal_status(
            cid, "in_review", judge=judge, actor=judge,
            detail={"note": str(body.get("note") or "").strip() or None})
        self._send_json({"case": _slim_appeal(
            self.storage.get_appeal_case(cid)), "accepted": True})

    def _withdraw_appeal(self, bid: str, cid: str) -> None:
        self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        if case["status"] != "submitted":
            raise ApiError(
                HTTPStatus.CONFLICT, "APPEAL_NOT_WITHDRAWABLE",
                f"案件当前为 {case['status']}；仅未受理（submitted）案件"
                f"可由申请方撤回，受理后须由裁判结案")
        body = self._json_body() if self.headers.get("Content-Length") else {}
        reason = str(body.get("reason") or "").strip() or None
        actor = str(body.get("applicant") or case.get("applicant") or ""
                    ).strip() or None
        self.storage.set_appeal_status(
            cid, "withdrawn", actor=actor, detail={"reason": reason})
        self._send_json({"case": _slim_appeal(
            self.storage.get_appeal_case(cid)), "withdrawn": True})

    def _load_rulings(self, case: dict[str, Any], ver: dict[str, Any]
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """读取并形式校验 rulings；返回 (rulings, outcome)，不合法抛 400。"""
        body = self._json_body()
        rulings = body.get("rulings")
        if not isinstance(rulings, list) or not rulings:
            raise ApiError(HTTPStatus.BAD_REQUEST, "RULINGS_REQUIRED",
                           "rulings 必须是非空数组（逐项结论）")
        for r in rulings:
            if not isinstance(r, dict) or not r.get("claim_id"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULING",
                               "每条结论须含 claim_id")
        if len({r["claim_id"] for r in rulings}) != len(rulings):
            raise ApiError(HTTPStatus.BAD_REQUEST, "BAD_RULING",
                           "同一争议项不得给出多条结论")
        claims = self.storage.list_appeal_claims(case["id"])
        problems = validate_rulings(
            claims, rulings, rules=ver["snapshot"]["rules"],
            findings_index=snapshot_finding_index(ver["snapshot"]))
        problems = {k: v for k, v in problems.items() if v}
        if problems:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "INVALID_RULINGS",
                "逐项结论校验未通过", sorted(
                    ({"claim_id": k, "problems": v}
                     for k, v in problems.items()),
                    key=lambda x: x["claim_id"]))
        return rulings, body

    def _scheme_slim(self, ver: dict[str, Any]) -> dict[str, Any] | None:
        scheme = (ver["snapshot"] or {}).get("clock_scheme")
        if not scheme:
            return None
        return {"reference_log_id": scheme.get("reference_log_id"),
                "max_window_seconds": scheme.get("max_window_seconds"),
                "offsets": scheme.get("offsets")}

    def _preview_appeal(self, bid: str, cid: str) -> None:
        self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        if case["status"] != "in_review":
            raise ApiError(
                HTTPStatus.CONFLICT, "APPEAL_NOT_IN_REVIEW",
                f"案件当前为 {case['status']}；先 POST .../accept 受理后才能"
                f"作出逐项处理")
        ver = self._bound_version(case)
        rulings, body = self._load_rulings(case, ver)
        snapshot = ver["snapshot"]
        outcome = apply_rulings(
            rules=snapshot["rules"], station=case["station"],
            claims=self.storage.list_appeal_claims(case["id"]),
            rulings=rulings, snapshot=snapshot)
        digest = preview_digest(rulings)
        prospective_hash = content_hash(
            snapshot["rules"], outcome["annotated_findings"],
            outcome["decisions"], outcome["results_after"],
            clock_scheme=self._scheme_slim(ver))
        self._send_json({
            "case_id": cid, "digest": digest,
            "judge": body.get("judge"),
            "items": outcome["items"],
            "changed_decisions": outcome["changed_decisions"],
            "revision_count": outcome["revision_count"],
            "station_score": outcome["station_score"],
            "scorecard_diff": outcome_scorecard_diff(outcome),
            "prospective_version_no": self.storage.next_version_no(bid),
            "prospective_content_hash": prospective_hash,
            "correction_required": outcome["score_changed"],
            "note": "预览不写入任何数据；确认（POST .../confirm）时将再次核对"
                    "全部引用与绑定哈希，任一项失效则整案不写入"})

    def _confirm_appeal(self, bid: str, cid: str) -> None:
        batch = self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        if case["status"] != "in_review":
            raise ApiError(
                HTTPStatus.CONFLICT, "APPEAL_NOT_IN_REVIEW",
                f"案件当前为 {case['status']}；仅 in_review 案件可确认处理")
        ver = self._bound_version(case)
        rulings, body = self._load_rulings(case, ver)
        digest = body.get("digest")
        if not digest:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "PREVIEW_REQUIRED",
                "确认前须先 POST .../preview 取得处理预览，并在确认时回传 "
                "digest，防止凭陈旧处理意见写入")
        if digest != preview_digest(rulings):
            raise ApiError(
                HTTPStatus.CONFLICT, "PREVIEW_DIGEST_MISMATCH",
                "确认内容与最近一次预览不一致；请重新生成预览并核对后再确认")
        snapshot = ver["snapshot"]
        outcome = apply_rulings(
            rules=snapshot["rules"], station=case["station"],
            claims=self.storage.list_appeal_claims(case["id"]),
            rulings=rulings, snapshot=snapshot)
        judge = str(body.get("judge") or case.get("judge") or ""
                    ).strip() or None
        note = str(body.get("note") or "").strip() or (
            f"赛后复议案件 {cid} 改判冻结")
        old_rep = self.storage.get_feedback_report(case["report_id"])
        submissions = self.storage.get_submissions(bid)
        station = case["station"]

        def build_correction(new_version: dict[str, Any],
                             _new_snapshot: dict[str, Any]
                             ) -> dict[str, Any] | None:
            if not outcome["score_changed"]:
                return None
            bound_logs = version_bound_logs(new_version, submissions)
            station_logs = [s for s in bound_logs
                            if s["station_call"] == station]
            if not station_logs:
                return None  # 极端情况：新版本清单内无该台日志
            return build_station_report(
                batch=batch, version=new_version, station=station,
                logs=station_logs, kind="correction", corrects=old_rep)

        result = self.storage.commit_appeal_rulings(
            cid, rulings=rulings,
            annotated_findings=outcome["annotated_findings"],
            results=outcome["results_after"], rules=snapshot["rules"],
            version_digest=None, clock_scheme_slim=self._scheme_slim(ver),
            score_before=outcome["station_score"]["before"],
            score_after=outcome["station_score"]["after"],
            judge=judge, note=note, build_correction=build_correction)
        payload = self._appeal_payload(self.storage.get_appeal_case(cid))
        payload["confirmed"] = True
        payload["frozen"] = result["frozen"]
        payload["frozen_version"] = (
            {"version_no": result["version_no"],
             "content_hash": result["content_hash"]}
            if result["frozen"] else None)
        payload["correction_published"] = result["correction_report_id"]
        self._send_json(payload)

    def _download_appeal(self, bid: str, cid: str) -> None:
        self._get_batch_or_404(bid)
        case = self._get_appeal_or_404(bid, cid)
        payload = self._appeal_payload(case)
        envelope = {
            "export": "cabrillo-judge-appeal",
            "schema_version": 1,
            "exported_ts": int(time.time()),
            "batch_id": bid,
            **payload,
        }
        self._send_json(envelope,
                        download_name=f"{cid}-appeal.json")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _public_batch(b: dict[str, Any]) -> dict[str, Any]:
    return {"id": b["id"], "name": b["name"], "rules": b["rules"],
            "locked": b["locked"], "created_ts": b["created_ts"],
            "updated_ts": b["updated_ts"]}


def _public_scheme(s: dict[str, Any]) -> dict[str, Any]:
    return {"id": s["id"], "name": s["name"],
            "reference_log_id": s["reference_log_id"],
            "max_window_seconds": s["max_window_seconds"],
            "offsets": s["offsets"], "active": s["active"],
            "created_ts": s["created_ts"], "updated_ts": s["updated_ts"]}


def _slim_feedback(rep: dict[str, Any]) -> dict[str, Any]:
    """反馈包元数据（不含报告正文）。"""
    return {"id": rep["id"], "station": rep["station"],
            "version_no": rep["version_no"], "kind": rep["kind"],
            "status": rep["status"], "content_hash": rep["content_hash"],
            "corrects_report_id": rep["corrects_report_id"],
            "corrects_version_no": rep["corrects_version_no"],
            "superseded_by": rep["superseded_by"],
            "previewed_ts": rep["previewed_ts"],
            "published_ts": rep["published_ts"],
            "created_ts": rep["created_ts"]}


def _slim_appeal(c: dict[str, Any]) -> dict[str, Any]:
    """复议案件元数据。"""
    return {"id": c["id"], "batch_id": c["batch_id"],
            "station": c["station"], "report_id": c["report_id"],
            "version_no": c["version_no"], "status": c["status"],
            "applicant": c["applicant"], "judge": c["judge"],
            "version_content_hash": c["version_content_hash"],
            "report_content_hash": c["report_content_hash"],
            "binding_content_hash": c["binding_content_hash"],
            "resolved_version_no": c["resolved_version_no"],
            "correction_report_id": c["correction_report_id"],
            "score_before": c["score_before"],
            "score_after": c["score_after"],
            "received_ts": c["received_ts"],
            "accepted_ts": c["accepted_ts"],
            "withdrawn_ts": c["withdrawn_ts"],
            "closed_ts": c["closed_ts"]}


def _refs_key(finding: dict[str, Any]) -> tuple:
    """证据引用的稳定键（文件名:行号），与时间偏移/状态无关。"""
    return tuple(sorted(f"{r.get('filename')}:{r.get('line')}"
                        for r in finding.get("refs", [])))


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
<h2>时钟偏差校正</h2>
<ol>
<li><code>GET /api/batches/{{id}}/clock-analysis?reference_log_id=…&amp;max_window_seconds=…</code>
估计各日志相对参考日志的时钟偏差（中位数/离散度/样本数/覆盖时段；
样本不足、偏差随时间变化或关系不连通时只列证据、不建议偏移）</li>
<li><code>POST /api/batches/{{id}}/clock-schemes</code>
建立整分钟校正方案（<code>use_suggested</code> 可采纳分析建议）</li>
<li><code>POST …/clock-schemes/{{sid}}/preview</code>
预览重跑配对后的状态计数与计分变化（不改动数据）</li>
<li><code>POST …/clock-schemes/{{sid}}/activate</code>
启用方案；每条证据同时保留原时间、校正时间与偏移</li>
<li><code>GET …/archived-decisions</code>
复核因方案变化失去依据而已归档的裁决（绝不静默沿用）</li>
</ol>
<h2>站级赛后反馈包</h2>
<ol>
<li><code>POST /api/batches/{{id}}/versions?lock=1</code> 冻结计分版本</li>
<li><code>POST /api/batches/{{id}}/feedback</code> 传
<code>{{"version_no":1}}</code> 整批生成草稿，或加
<code>"station":"BG1AAA"</code> 只生成单站；每次生成都是不可变快照</li>
<li><code>GET …/feedback/{{rid}}</code> 预览内部完整稿；
<code>POST …/feedback/{{rid}}/preview</code> 生成对外脱敏稿</li>
<li><code>POST …/feedback/{{rid}}/publish</code> 发布（须先预览，发布后不可变）</li>
<li>计分版本演进后再生成，自动产出注明旧包与替代版本的更正包；
<code>GET …/feedback/{{rid}}/lineage</code> 查看更正链</li>
<li><code>GET …/feedback/{{rid}}/download?format=txt</code> 下载纯文本
（<code>format=json</code> 下载 JSON）</li>
</ol>
<h2>赛后复议案件</h2>
<ol>
<li><code>POST /api/batches/{{id}}/appeals</code> 对<strong>已发布</strong>反馈包提案，
绑定反馈包/计分版本与内容哈希；可对原日志行、配对状态、交换差异、罚分、
汇总分提出多项主张并引用 finding 与本台日志行。引用不属于该台站、目标不在
报告中、或包已有后续更正时<strong>明确拒绝且不自动改绑</strong></li>
<li><code>POST …/appeals/{{cid}}/withdraw</code> 未受理（submitted）可由申请方撤回</li>
<li><code>POST …/appeals/{{cid}}/accept</code> 裁判受理（in_review）</li>
<li><code>POST …/appeals/{{cid}}/preview</code> 逐项给出
<code>upheld</code>（维持）/<code>revised</code>（改判）/
<code>insufficient</code>（证据不足），先看裁决变更与计分预览（不写入）</li>
<li><code>POST …/appeals/{{cid}}/confirm</code> 回传预览 <code>digest</code> 确认；
系统再次核对全部引用与绑定快照，<strong>任一项失效则整案不写入</strong>，
通过后一次性写入改判、冻结新计分版本；得分变化时生成沿用既有替代关系的
已发布更正包并结案。原反馈包永不改写</li>
<li><code>GET …/appeals?status=&amp;station=&amp;report_id=</code> 筛选；
<code>GET …/appeals/{{cid}}</code> 详情（争议项/意见/状态轨迹）；
<code>GET …/appeals/{{cid}}/download</code> 下载案件 JSON</li>
</ol>
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
