"""站级赛后反馈包：从冻结的计分版本生成不可变的台站反馈报告。

输入为某一批次**已冻结的计分版本快照**（rules/findings/decisions/results，
见 :func:`cabrillo_judge.engine.content_hash` 的版本机制）与该台站的参赛
日志（原始行）。每份报告生成后即不可变；计分版本演进时不改写已发布报告，
只能生成注明旧包与替代版本的**更正包**（``kind="correction"``）。

报告逐条列出本台原日志行、配对状态、是否计分、QSO 分、新增乘数项、
罚分与裁决理由，并汇总 CLAIMED-SCORE、最终得分与差额。无法关联本台
原日志行的证据单列于 ``unassociated_evidence``，不猜测归属。

对外包（external view）脱敏规则：

* 不含其他台站的原始行（``refs`` 仅内部稿保留）；
* 不含任何邮件地址/日志头（报告本就不收录日志头，CLAIMED-SCORE 仅以
  数值形式出现在汇总中）；
* 不含其他台站的完整交换内容，只保留解释本台得失所需的对方呼号与
  差异项（``exchange_diffs``/``call_detail`` 均为字段级差异）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from typing import Any

from .engine import _qso_point_value  # internal reuse, same package
from .parser import normalize_callsign

REDACTION_NOTE = (
    "对外包：不含其他台站的原始行、邮件地址与完整交换内容；"
    "仅保留解释本台得失所需的对方呼号与字段级差异项。")


# ---------------------------------------------------------------------------
# 内容哈希（幂等生成：相同内容不重复建包）
# ---------------------------------------------------------------------------

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def report_content_hash(report: dict[str, Any]) -> str:
    """报告内容哈希；只含决定报告内容的字段（不含生成时间）。"""
    core = {k: report.get(k) for k in (
        "batch_id", "station", "version_no", "version_content_hash",
        "kind", "corrects", "entries", "unassociated_evidence", "summary")}
    return hashlib.sha256(_canonical(core)).hexdigest()


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "—"
    dt = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claimed_score(logs: list[dict[str, Any]]) -> int | None:
    """取该台站最近一份日志中合法的 CLAIMED-SCORE（整数）。"""
    claimed: int | None = None
    for log in sorted(logs, key=lambda l: (l.get("upload_ts", 0),
                                           l["filename"])):
        value = (log["parsed"].get("headers") or {}).get("CLAIMED-SCORE")
        if value is None:
            continue
        try:
            claimed = int(str(value).strip())
        except ValueError:
            continue
    return claimed


def _slim_decision(dec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not dec:
        return None
    return {"resolution": dec.get("resolution"),
            "fault_station": dec.get("fault_station"),
            "penalty_code": dec.get("penalty_code"),
            "reason": dec.get("reason"),
            "judge": dec.get("judge")}


def _primary_finding(cands: list[tuple[dict[str, Any], int]]
                     ) -> tuple[dict[str, Any], int] | None:
    """同一行可能出现在多条证据中（如被后续 DUP 引用为较早记录）。

    主证据选取：本行作为 refs[0]（证据主体）者优先；其次非 DUP 证据；
    其余引用在 ``also_in`` 中体现。
    """
    if not cands:
        return None
    ordered = sorted(
        cands,
        key=lambda fi: (0 if fi[1] == 0 else 1,
                        0 if fi[0]["status"] != "DUP" else 1,
                        fi[0]["id"]))
    return ordered[0]


def build_station_report(*, batch: dict[str, Any],
                         version: dict[str, Any],
                         station: str,
                         logs: list[dict[str, Any]],
                         kind: str = "normal",
                         corrects: dict[str, Any] | None = None,
                         now: int | None = None) -> dict[str, Any]:
    """从冻结版本快照构建单个台站的内部完整反馈报告。

    *version* 为 ``storage.get_version`` 返回的行（含 snapshot）；
    *logs* 为该台站在本批次的全部提交（含 parsed）；
    *corrects* 为被替代旧包的存储行（生成更正包时传入）。
    """
    snapshot = version["snapshot"]
    rules = snapshot.get("rules") or {}
    findings = snapshot.get("findings") or []
    results = snapshot.get("results") or {}
    now = int(now if now is not None else time.time())

    log_ids = {l["log_id"] for l in logs}

    # 索引：本台 (log_id, 行号) -> [(finding, ref_index)]
    by_line: dict[tuple[str, int], list[tuple[dict[str, Any], int]]] = {}
    station_findings: list[dict[str, Any]] = []
    for f in findings:
        touches = False
        for idx, r in enumerate(f.get("refs", [])):
            if r.get("log_id") in log_ids:
                by_line.setdefault((r["log_id"], r["line"]),
                                   []).append((f, idx))
                touches = True
        if touches or station in f.get("stations", []):
            station_findings.append(f)

    entries: list[dict[str, Any]] = []
    used_findings: set[str] = set()

    for log in sorted(logs, key=lambda l: (l.get("upload_ts", 0),
                                           l["filename"])):
        parsed = log["parsed"]
        raw_by_line = {rl["line"]: rl["text"]
                       for rl in parsed.get("raw_lines", [])}

        def _base(qso: dict[str, Any]) -> dict[str, Any]:
            return {"filename": log["filename"], "line": qso["line"],
                    "raw": raw_by_line.get(qso["line"], ""),
                    "band": qso.get("band"), "mode": qso.get("mode"),
                    "date": qso.get("date"), "time": qso.get("time"),
                    "ts": qso.get("ts"),
                    "worked_call": qso.get("worked_call_raw")}

        # 无效行（error 级字段问题，从未参与配对计分）
        for iv in parsed.get("invalid_qsos", []):
            errors = iv.get("errors") or [iv.get("error")]
            entries.append({
                "filename": log["filename"], "line": iv["line"],
                "raw": raw_by_line.get(iv["line"], iv.get("raw", "")),
                "band": iv.get("band"), "mode": iv.get("mode"),
                "date": iv.get("date"), "time": iv.get("time"),
                "ts": iv.get("ts"),
                "worked_call": iv.get("call2"),
                "finding_id": None, "status": "INVALID", "pending": False,
                "counted": False, "qso_points": 0, "partner": None,
                "new_multipliers": [],
                "penalty_points": 0, "penalty_codes": [],
                "reason": ("该行存在 error 级问题（"
                           + "、".join(str(e) for e in errors if e)
                           + "），未参与配对与计分；原始行保留备查"),
                "errors": errors,
                "decision": None, "exchange_diffs": [],
                "time_delta_seconds": None, "clock_corrected": False,
                "call_detail": {}, "refs": [], "also_in": []})

        # X-QSO：台方自行标注，永不配对计分
        for qso in parsed.get("xqsos", []):
            entries.append({
                **_base(qso),
                "finding_id": None, "status": "X-QSO", "pending": False,
                "counted": False, "qso_points": 0, "partner": None,
                "new_multipliers": [],
                "penalty_points": 0, "penalty_codes": [],
                "reason": "台方以 X-QSO 自行标注，该记录不参与配对与计分",
                "decision": None, "exchange_diffs": [],
                "time_delta_seconds": None, "clock_corrected": False,
                "call_detail": {}, "refs": [], "also_in": []})

        # 有效 QSO 行：关联计分版本中的证据
        for qso in parsed.get("qsos", []):
            cands = by_line.get((log["log_id"], qso["line"]), [])
            picked = _primary_finding(cands)
            base = _base(qso)
            if picked is None:
                # 防御：合法行未出现在版本证据中（正常流程不会发生）
                entries.append({
                    **base, "finding_id": None, "status": "UNTRACKED",
                    "pending": False, "counted": False, "qso_points": 0,
                    "partner": None, "new_multipliers": [],
                    "penalty_points": 0, "penalty_codes": [],
                    "reason": "该行未出现在所依据计分版本的证据中，"
                              "不猜测其配对归属",
                    "decision": None, "exchange_diffs": [],
                    "time_delta_seconds": None, "clock_corrected": False,
                    "call_detail": {}, "refs": [], "also_in": []})
                continue
            f, own_idx = picked
            used_findings.add(f["id"])
            also_in = [of["id"] for of, _ in cands if of["id"] != f["id"]]
            used_findings.update(also_in)
            eff = (f.get("effects") or {}).get(station) or {}
            own_ref = (f.get("refs") or [{}])[own_idx] \
                if f.get("refs") else {}
            counted = bool(eff.get("counted"))
            entries.append({
                **base,
                "corrected_date": own_ref.get("corrected_date"),
                "corrected_time": own_ref.get("corrected_time"),
                "offset_seconds": own_ref.get("offset_seconds", 0),
                "finding_id": f["id"], "status": f["status"],
                "pending": bool(f.get("pending")),
                "counted": counted,
                "qso_points": (_qso_point_value(rules, f["band"], f["mode"])
                               if counted else 0),
                "partner": eff.get("partner"),
                "new_multipliers": [],  # 下方按时间序累计填充
                "penalty_points": int(eff.get("penalty_points", 0)),
                "penalty_codes": list(eff.get("penalty_codes", [])),
                "reason": f.get("auto_reason"),
                "decision": _slim_decision(f.get("decision")),
                "exchange_diffs": f.get("exchange_diffs", []),
                "time_delta_seconds": f.get("time_delta_seconds"),
                "clock_corrected": bool(f.get("clock_corrected")),
                "call_detail": f.get("call_detail", {}),
                "refs": f.get("refs", []),  # 仅内部稿保留，对外包剔除
                "also_in": also_in,
                # 以下仅供新增乘数累计，序列化前移除
                "_exchange": eff.get("exchange") or {},
                "_partner_norm": (normalize_callsign(eff["partner"])
                                  if eff.get("partner") else None)})

    # 无法关联本台原日志行的证据：单列，不猜测归属
    unassociated: list[dict[str, Any]] = []
    for f in station_findings:
        if f["id"] in used_findings:
            continue
        if any(r.get("log_id") in log_ids for r in f.get("refs", [])):
            continue  # 关联得上但未选为主证据（also_in 已体现）
        unassociated.append({
            "finding_id": f["id"], "status": f["status"],
            "pending": bool(f.get("pending")),
            "stations": f.get("stations", []),
            "band": f.get("band"), "mode": f.get("mode"),
            "auto_reason": f.get("auto_reason"),
            "note": "该证据涉及本台但无法关联到本台日志的具体行，"
                    "不猜测归属"})

    _accumulate_new_multipliers(rules, station, entries)
    for e in entries:
        e.pop("_exchange", None)
        e.pop("_partner_norm", None)

    card = next((c for c in results.get("scorecards", [])
                 if c.get("station") == station), None) or {}
    claimed = _claimed_score(logs)
    final = int(card.get("total_score", 0))
    n_valid = sum(1 for e in entries
                  if e["status"] not in ("INVALID", "X-QSO"))
    n_counted = sum(1 for e in entries if e["counted"])
    summary = {
        "claimed_score": claimed,
        "final_score": final,
        "difference": (final - claimed) if claimed is not None else None,
        "qso_counted": int(card.get("qso_counted", 0)),
        "qso_points": int(card.get("qso_points", 0)),
        "multiplier_components": card.get("multiplier_components", {}),
        "multiplier_total": int(card.get("multiplier_total", 1)),
        "raw_score": int(card.get("raw_score", 0)),
        "penalty_points": int(card.get("penalty_points", 0)),
        "penalty_codes": list(card.get("penalty_codes", [])),
        "lines": {
            "valid": n_valid,
            "counted": n_counted,
            "not_counted": n_valid - n_counted,
            "xqso": sum(1 for e in entries if e["status"] == "X-QSO"),
            "invalid": sum(1 for e in entries if e["status"] == "INVALID"),
            "pending": sum(1 for e in entries
                           if e.get("pending") and not e.get("decision")),
            "unassociated_evidence": len(unassociated),
        },
    }

    correction = None
    if kind == "correction" and corrects:
        old_report = corrects.get("report") or {}
        old_final = (old_report.get("summary") or {}).get("final_score")
        correction = {
            "supersedes_report_id": corrects["id"],
            "supersedes_version_no": corrects["version_no"],
            "supersedes_content_hash": corrects["content_hash"],
            "previous_final_score": old_final,
            "score_delta": (final - old_final)
            if isinstance(old_final, int) else None,
            "note": (f"本包为更正包：替代报告 {corrects['id']}"
                     f"（基于计分版本 v{corrects['version_no']}），"
                     f"基于计分版本 v{version['version_no']} 重新生成；"
                     f"已发布的原报告保持不可变、不被改写。"),
        }

    return {
        "package_type": "station-feedback",
        "schema_version": 1,
        "view": "internal",
        "batch_id": batch["id"],
        "batch_name": batch["name"],
        "station": station,
        "version_no": version["version_no"],
        "version_content_hash": version["content_hash"],
        "kind": kind,
        "corrects": ({"report_id": corrects["id"],
                      "version_no": corrects["version_no"],
                      "content_hash": corrects["content_hash"]}
                     if corrects else None),
        "correction": correction,
        "generated_ts": now,
        "logs": [{"log_id": l["log_id"], "filename": l["filename"],
                  "station_call": l["station_call"],
                  "upload_ts": l.get("upload_ts")} for l in logs],
        "entries": entries,
        "unassociated_evidence": unassociated,
        "summary": summary,
    }


def _accumulate_new_multipliers(rules: dict[str, Any], station: str,
                                entries: list[dict[str, Any]]) -> None:
    """按时间序累计每条计分 QSO 新贡献的乘数取值（展示用）。

    与 :func:`cabrillo_judge.engine.score` 的乘数口径一致；汇总中的乘数
    组件以冻结版本快照为准，这里只标注"该行为本台新带来的乘数项"。
    """
    mult_defs = rules.get("multipliers", [])
    seen_call: set[str] = set()
    seen_band: set[str] = set()
    seen_bm: set[str] = set()
    seen_ex: dict[str, set[str]] = {}
    counted = [e for e in entries if e["counted"] and e.get("ts") is not None]
    counted.sort(key=lambda e: (e["ts"], e["filename"], e["line"]))
    for e in counted:
        news: list[dict[str, Any]] = []
        for mdef in mult_defs:
            mtype = mdef.get("type")
            name = mdef.get("name", mtype)
            value: str | None = None
            if mtype == "worked_call":
                norm = e.get("_partner_norm")
                if not norm or norm == station:
                    continue
                if norm in seen_call:
                    continue
                seen_call.add(norm)
                value = norm
            elif mtype == "band":
                if e["band"] in seen_band:
                    continue
                seen_band.add(e["band"])
                value = e["band"]
            elif mtype == "band_mode":
                key = f"{e['band']}|{e['mode']}"
                if key in seen_bm:
                    continue
                seen_bm.add(key)
                value = key
            elif mtype == "exchange_field":
                field = mdef.get("field", "")
                raw_val = (e.get("_exchange") or {}).get(field)
                if raw_val is None:
                    continue
                val = str(raw_val)
                bucket = seen_ex.setdefault(field, set())
                if val in bucket:
                    continue
                bucket.add(val)
                value = val
            else:
                continue
            news.append({"type": mtype, "name": name, "value": value})
        e["new_multipliers"] = news


# ---------------------------------------------------------------------------
# 对外脱敏包
# ---------------------------------------------------------------------------

def build_external_report(internal: dict[str, Any]) -> dict[str, Any]:
    """由内部稿派生对外包：剔除其他台站原始行等敏感内容。"""
    ext = json.loads(json.dumps(internal, ensure_ascii=False))
    ext["view"] = "external"
    for e in ext.get("entries", []):
        e.pop("refs", None)  # 其他台站的原始行不进入对外包
    ext["redaction_note"] = REDACTION_NOTE
    return ext


# ---------------------------------------------------------------------------
# 纯文本渲染
# ---------------------------------------------------------------------------

def render_text(report: dict[str, Any]) -> str:
    """把报告渲染为纯文本（view 由报告自身携带）。"""
    view = report.get("view", "internal")
    internal = view == "internal"
    station = report["station"]
    out: list[str] = []
    A = out.append

    title = "站级赛后反馈包（裁判内部稿）" if internal \
        else "站级赛后反馈包（对外发布版）"
    kind = "更正包" if report.get("kind") == "correction" else "反馈包"
    A("=" * 70)
    A(f"{title}  [{kind}]")
    A(f"赛事批次 : {report.get('batch_name')} ({report.get('batch_id')})")
    A(f"台站     : {station}")
    A(f"计分版本 : v{report.get('version_no')}  "
      f"内容哈希 {str(report.get('version_content_hash'))[:16]}…")
    A(f"生成时间 : {_fmt_ts(report.get('generated_ts'))}")
    corr = report.get("correction")
    if corr:
        A(f"更正说明 : 替代报告 {corr['supersedes_report_id']}"
          f"（计分版本 v{corr['supersedes_version_no']}）；"
          f"原已发布报告保持不可变")
        if corr.get("score_delta") is not None:
            A(f"           最终得分 {corr['previous_final_score']} → "
              f"{report['summary']['final_score']}"
              f"（变化 {corr['score_delta']:+d}）")
    if not internal:
        A(f"脱敏说明 : {REDACTION_NOTE}")
    A("=" * 70)

    s = report["summary"]
    claimed = s["claimed_score"]
    diff = s["difference"]
    A("汇总")
    A(f"  CLAIMED-SCORE : "
      f"{claimed if claimed is not None else '（日志未申报）'}")
    A(f"  最终得分      : {s['final_score']}")
    A(f"  差额          : "
      f"{('%+d' % diff) if diff is not None else '—'}")
    A(f"  计分 QSO      : {s['qso_counted']} 条，QSO 分 "
      f"{s['qso_points']}，总乘数 {s['multiplier_total']}"
      f"（{_fmt_mults(s['multiplier_components'])}），"
      f"原始分 {s['raw_score']}")
    pen = s["penalty_points"]
    A(f"  罚分          : {pen}"
      + (f"（{'、'.join(s['penalty_codes'])}）" if s["penalty_codes"]
         else ""))
    ln = s["lines"]
    A(f"  行数          : 有效 {ln['valid']}（计分 {ln['counted']} / "
      f"未计分 {ln['not_counted']}），X-QSO {ln['xqso']}，"
      f"无效行 {ln['invalid']}，待裁决 {ln['pending']}")
    A("-" * 70)

    A(f"逐条明细（共 {len(report['entries'])} 行）")
    for e in report["entries"]:
        A("")
        head = (f"[{e['filename']} 行 {e['line']}] "
                f"{e.get('date') or '????-??-??'} {e.get('time') or '????'}"
                f"  {e.get('band') or '?'} {e.get('mode') or '?'}"
                f"  对方 {e.get('worked_call') or '?'}")
        A(head)
        A(f"  原行    : {e.get('raw', '')}")
        counted = "是" if e["counted"] else "否"
        A(f"  状态    : {e['status']}  计分: {counted}  "
          f"QSO分: {e['qso_points']}")
        if e.get("offset_seconds"):
            A(f"  时钟校正: 偏移 {e['offset_seconds']} 秒，校正后 "
              f"{e.get('corrected_date')} {e.get('corrected_time')}")
        if e.get("new_multipliers"):
            A("  新增乘数: " + "、".join(
                f"{m['name']}={m['value']}" for m in e["new_multipliers"]))
        if e.get("penalty_points") or e.get("penalty_codes"):
            A(f"  罚分    : {e['penalty_points']}"
              f"（{'、'.join(e['penalty_codes'])}）")
        if e.get("reason"):
            A(f"  理由    : {e['reason']}")
        dec = e.get("decision")
        if dec:
            judge = f"（裁判: {dec['judge']}）" if dec.get("judge") else ""
            A(f"  裁决    : {dec['resolution']} — {dec['reason']}{judge}")
        for d in e.get("exchange_diffs", []):
            A(f"  差异    : {d['field']} 抄收 {d['received']} / "
              f"对方实发 {d['counterparty_sent']}（记录方 {d['logged_by']}）")
        cd = e.get("call_detail") or {}
        if cd and e["status"] == "SUSPECT_CALL":
            # a/b 侧对应证据 refs[0]/refs[1]；对外包无 refs，按实际呼号推断
            if cd.get("b_actual") == station:
                a_name, b_name = station, (cd.get("a_actual") or "对方")
            elif cd.get("a_actual") == station:
                a_name, b_name = (cd.get("b_actual") or "对方"), station
            else:
                a_name, b_name = "A方", "B方"
            A(f"  呼号细节: {a_name} 记 {cd.get('a_logged')}（实为 "
              f"{cd.get('a_actual')}），{b_name} 记 {cd.get('b_logged')}"
              f"（实为 {cd.get('b_actual')}）")
        if internal:
            for r in e.get("refs", []):
                if r.get("station") != station:
                    A(f"  对方原行: {r.get('filename')} 行 {r.get('line')}"
                      f": {r.get('raw')}")
        if e.get("also_in"):
            A(f"  另见证据: {'、'.join(e['also_in'])}")

    if report.get("unassociated_evidence"):
        A("")
        A("-" * 70)
        A("无法关联原日志行的证据（单列，不猜测归属）")
        for u in report["unassociated_evidence"]:
            A(f"  {u['finding_id']}  {u['status']}  "
              f"涉及 {'、'.join(u.get('stations', []))}")
            if u.get("auto_reason"):
                A(f"    {u['auto_reason']}")
    A("")
    A("本包为不可变快照；计分版本演进时不改写本包，"
      "仅以注明旧包与替代版本的更正包更新。")
    return "\n".join(out) + "\n"


def _fmt_mults(components: dict[str, Any]) -> str:
    if not components:
        return "无乘数项"
    return "、".join(f"{k}={v}" for k, v in components.items())
