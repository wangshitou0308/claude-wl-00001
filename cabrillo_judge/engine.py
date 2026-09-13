"""Cross-log pairing, adjudication and scoring engine.

The engine is deliberately *pure*: it takes parsed submissions plus stored
judge decisions and returns data structures.  All persistence lives in
:mod:`cabrillo_judge.storage`.

Finding statuses
----------------
MATCH          双方记录在频段/模式/容差时间上一致（交换也一致）
EXCHANGE_DIFF  配对成功，但交换字段不一致 -> 待裁决
TIME_DRIFT     互有记录但时间差超出容差、在近邻窗口内 -> 待裁决
SUSPECT_CALL   呼号模糊匹配上的疑似抄错 -> 待裁决
NO_PARTNER_LOG 对方未交日志（批次内无其日志）-> 待裁决
UNIQUE         对方交了日志但其中无此记录（单方记录）
DUP            本方日志内的重复通联

凡仅凭现有日志不能唯一判定的状态 ``pending=True``，裁判必须给出理由才能改判。

时钟校正
--------
:func:`adjudicate` 接受 ``time_offsets={log_id: seconds}``（来自已启用的
整分钟校正方案）。偏移只作用于配对用的时间戳；每条 QSO 引用同时保留
原始 ``ts``/``date``/``time`` 与 ``corrected_*``/``offset_seconds``，
原始 Cabrillo 文本与时间永不修改。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from .bandswitch import analyze_band_switching
from .parser import _exchange_equal  # internal reuse, same package

PENDING_STATUSES = {"EXCHANGE_DIFF", "TIME_DRIFT", "SUSPECT_CALL",
                    "NO_PARTNER_LOG", "BAND_SWITCH_AMBIGUOUS"}

# Resolution values a judge may record on a decision.
RESOLUTIONS = {"CONFIRMED", "REMOVED", "GRANTED", "WAIVED"}

# 各配对状态允许的裁决动作（普通裁决与赛后复议改判共用同一口径）
RESOLUTION_ALLOWED: dict[str, set[str]] = {
    "EXCHANGE_DIFF": {"CONFIRMED", "REMOVED"},
    "TIME_DRIFT": {"CONFIRMED", "REMOVED"},
    "SUSPECT_CALL": {"CONFIRMED", "REMOVED"},
    "NO_PARTNER_LOG": {"GRANTED", "WAIVED", "REMOVED"},
    "UNIQUE": {"WAIVED", "REMOVED", "GRANTED"},
    "DUP": {"WAIVED", "REMOVED"},
    "MATCH": {"REMOVED"},
    # 频段切换合规：确定性违规可豁免/剔除；歧义段经裁判确认构成违规、
    # 豁免（顺序不构成违规）或剔除
    "BAND_SWITCH_EXCESS": {"WAIVED", "REMOVED"},
    "BAND_DWELL_SHORT": {"WAIVED", "REMOVED"},
    "BAND_SWITCH_AMBIGUOUS": {"CONFIRMED", "WAIVED", "REMOVED"},
}

# 频段切换合规 finding 状态
BAND_DETERMINED_STATUSES = ("BAND_SWITCH_EXCESS", "BAND_DWELL_SHORT")
BAND_CLAUSE_STATUS = {
    "max_switches_per_clock_hour": "BAND_SWITCH_EXCESS",
    "min_dwell_seconds": "BAND_DWELL_SHORT",
    "ambiguous_same_minute": "BAND_SWITCH_AMBIGUOUS",
}


def validate_judge_decision(rules: dict[str, Any], finding: dict[str, Any],
                            *, resolution: str, reason: str,
                            fault_station: str | None = None,
                            penalty_code: str | None = None) -> list[str]:
    """校验一条裁决；返回中文问题清单（空列表表示合法）。"""
    problems: list[str] = []
    if resolution not in RESOLUTIONS:
        return [f"resolution 必须是 {sorted(RESOLUTIONS)} 之一"]
    if not reason or not str(reason).strip():
        problems.append("裁决必须填写理由 reason")
    allowed = RESOLUTION_ALLOWED.get(finding["status"], set())
    if resolution not in allowed:
        problems.append(
            f"{finding['status']} 状态只接受 {sorted(allowed)}")
    if fault_station is not None:
        fault = str(fault_station).upper()
        if fault not in finding.get("stations", []) and not (
                finding.get("refs") and
                finding["refs"][0].get("station") == fault):
            problems.append(
                f"fault_station 必须是相关台站之一: "
                f"{finding.get('stations')}")
    if penalty_code is not None and \
            penalty_code not in rules.get("penalties", {}):
        problems.append(
            f"penalty_code 必须在规则罚分目录中: "
            f"{sorted(rules.get('penalties', {}))}")
    return problems


# ---------------------------------------------------------------------------
# Fuzzy callsign comparison
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str, cutoff: int) -> int:
    """Edit distance with early exit when the row minimum exceeds *cutoff*."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cutoff:
            return cutoff + 1
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def _ts_date_time(ts: int) -> tuple[str, str]:
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H%M")


def _qso_ref(sub: dict[str, Any], qso: dict[str, Any],
             offset_seconds: int = 0) -> dict[str, Any]:
    raw_text = ""
    for rl in sub["parsed"]["raw_lines"]:
        if rl["line"] == qso["line"]:
            raw_text = rl["text"]
            break
    corrected_ts = qso["ts"] + offset_seconds
    corrected_date, corrected_time = _ts_date_time(corrected_ts)
    return {
        "log_id": sub["log_id"],
        "filename": sub["filename"],
        "station": sub["station_call"],
        "line": qso["line"],
        "ts": qso["ts"],
        "date": qso["date"],
        "time": qso["time"],
        "offset_seconds": offset_seconds,
        "corrected_ts": corrected_ts,
        "corrected_date": corrected_date,
        "corrected_time": corrected_time,
        "freq_khz": qso["freq_khz"],
        "band": qso["band"],
        "mode": qso["mode"],
        "worked_call": qso["worked_call_raw"],
        "sent": qso["sent"],
        "recv": qso["recv"],
        "raw": raw_text,
    }


def _finding_id(status: str, refs: list[dict[str, Any]]) -> str:
    key = json.dumps(
        {"s": status,
         "r": sorted(f"{r['filename']}:{r['line']}" for r in refs)},
        ensure_ascii=False, sort_keys=True)
    return "F-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _make_finding(status: str, refs: list[dict[str, Any]], band: str,
                  mode: str, reason: str, ts_hint: int,
                  exchange_diffs: list[dict[str, Any]] | None = None,
                  time_delta: int | None = None,
                  call_detail: dict[str, str] | None = None) -> dict[str, Any]:
    stations = sorted({r["station"] for r in refs if r.get("station")})
    return {
        "id": _finding_id(status, refs),
        "status": status,
        "pending": status in PENDING_STATUSES,
        "band": band,
        "mode": mode,
        "ts_hint": ts_hint,
        "stations": stations,
        "refs": refs,
        "clock_corrected": any(r.get("offset_seconds") for r in refs),
        "time_delta_seconds": time_delta,
        "exchange_diffs": exchange_diffs or [],
        "call_detail": call_detail or {},
        "auto_reason": reason,
    }


def _exchange_diffs(rules: dict[str, Any], a: dict[str, Any],
                    b: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare what A received from B with what B sent (and vice versa)."""
    diffs: list[dict[str, Any]] = []
    for spec in rules.get("exchange_fields", []):
        name = spec["name"]
        # A 抄收的 B 交换  vs  B 实际发出的交换
        if not _exchange_equal(spec, a["recv"][name], b["sent"][name]):
            diffs.append({"field": name, "station_at_fault_guess": b["station"],
                          "logged_by": a["station"],
                          "received": a["recv"][name],
                          "counterparty_sent": b["sent"][name]})
        if not _exchange_equal(spec, b["recv"][name], a["sent"][name]):
            diffs.append({"field": name, "station_at_fault_guess": a["station"],
                          "logged_by": b["station"],
                          "received": b["recv"][name],
                          "counterparty_sent": a["sent"][name]})
    return diffs


def _band_finding_id(status: str, refs: list[dict[str, Any]],
                     clause: str) -> str:
    # 同一分钟的多条记录与条款构成稳定身份；finding id 含状态，
    # 规则变化使状态改变时旧裁决即失去依据（与配对证据同口径归档）。
    key = json.dumps(
        {"s": status, "c": clause,
         "r": sorted(f"{r['filename']}:{r['line']}" for r in refs)},
        ensure_ascii=False, sort_keys=True)
    return "F-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _band_findings(band_analysis: dict[str, Any]
                   ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """把频段切换链上的违规/歧义节点转为 finding，并回填节点映射。"""
    findings: list[dict[str, Any]] = []
    # {(station, seq): {"finding_id": ..., "status": ..., "violations": [...]}}
    node_map: dict[tuple[str, int], dict[str, Any]] = {}
    for st in band_analysis.get("stations", []):
        policy = st.get("policy") or {}
        if not policy.get("enabled", False):
            continue
        for node in st.get("chain", []):
            refs = node.get("refs") or []
            if node["type"] == "ambiguous":
                status = BAND_CLAUSE_STATUS["ambiguous_same_minute"]
                bands = "、".join(node.get("ambiguous_bands") or [])
                reason = (f"{st['station']} 在 {node['date']} {node['time']} "
                          f"同一分钟内记录了跨频段（{bands}）多条 QSO，"
                          f"先后顺序无法判定；系统不自行排序，需裁判确认"
                          f"是否构成违规切换")
                detail = {"clause": "ambiguous_same_minute",
                          "node_seq": node["seq"],
                          "hour_window": node.get("hour_window"),
                          "from_band": node.get("from_band"),
                          "ambiguous_bands": node.get("ambiguous_bands"),
                          "policy": _policy_slim(policy)}
                finding = _make_band_finding(
                    status, refs, reason, node["ts"], detail, pending=True)
                findings.append(finding)
                node_map[(st["station"], node["seq"])] = {
                    "finding_id": finding["id"], "status": status,
                    "violations": [{
                        "finding_id": finding["id"], "status": status,
                        "clause": "ambiguous_same_minute"}]}
                continue
            for v in node.get("violations") or []:
                clause = v["clause"]
                status = BAND_CLAUSE_STATUS.get(clause)
                if not status:
                    continue
                detail = {
                    "clause": clause,
                    "node_seq": node["seq"],
                    "hour_window": node.get("hour_window"),
                    "from_band": node.get("from_band"),
                    "to_band": node.get("to_band"),
                    "interval_seconds": node.get("interval_seconds"),
                    "interval_from_seq": node.get("interval_from_seq"),
                    "hourly_count": node.get("hourly_count"),
                    "limit": v.get("limit"),
                    "policy": _policy_slim(policy)}
                if clause == "max_switches_per_clock_hour":
                    reason = (f"{st['station']} 在 {node['hour_window']} "
                              f"时钟小时内第 {v['hourly_count']} 次由 "
                              f"{node['from_band']} 切换至 {node['to_band']}"
                              f"（{node['date']} {node['time']}），"
                              f"超过每小时最多 {v['limit']} 次的限制；"
                              f"{v['detail']}")
                else:
                    reason = (f"{st['station']} 由 {node['from_band']} 切换至 "
                              f"{node['to_band']}（{node['date']} "
                              f"{node['time']}）时，上一频段驻留仅 "
                              f"{node['interval_seconds']} 秒，"
                              f"{v['detail']}")
                finding = _make_band_finding(
                    status, refs, reason, node["ts"], detail, pending=False)
                findings.append(finding)
                entry = node_map.setdefault(
                    (st["station"], node["seq"]),
                    {"finding_id": None, "status": None, "violations": []})
                entry["violations"].append(
                    {"finding_id": finding["id"], "status": status,
                     "clause": clause})
    return findings, node_map


def _policy_slim(policy: dict[str, Any]) -> dict[str, Any]:
    """写入每条频段证据的策略快照（触发条款的可复核依据）。"""
    return {k: policy.get(k) for k in (
        "enabled", "max_switches_per_clock_hour", "min_dwell_seconds",
        "matched", "category", "category_value", "category_source",
        "semantics", "penalty_excess", "penalty_dwell", "penalty_ambiguous")}


def _make_band_finding(status: str, refs: list[dict[str, Any]],
                       reason: str, ts_hint: int,
                       detail: dict[str, Any], *, pending: bool
                       ) -> dict[str, Any]:
    stations = sorted({r["station"] for r in refs if r.get("station")})
    bands = sorted({r["band"] for r in refs if r.get("band")})
    return {
        "id": _band_finding_id(status, refs, detail["clause"]),
        "status": status,
        "pending": pending,
        "band": detail.get("to_band") or (bands[-1] if bands else None),
        "mode": None,
        "ts_hint": ts_hint,
        "stations": stations,
        "refs": refs,
        "clock_corrected": any(r.get("offset_seconds") for r in refs),
        "time_delta_seconds": None,
        "exchange_diffs": [],
        "call_detail": {},
        "auto_reason": reason,
        "band_switch": detail,
    }


def adjudicate(rules: dict[str, Any],
               submissions: list[dict[str, Any]],
               time_offsets: dict[str, int] | None = None) -> dict[str, Any]:
    """Run the whole pairing pass.

    *submissions* items: ``{log_id, filename, station_call, parsed}`` where
    ``parsed`` is the result of :func:`cabrillo_judge.parser.parse_cabrillo`.
    *time_offsets* optionally maps ``log_id`` to seconds added to that log's
    timestamps for pairing (clock-skew correction scheme); original times
    are always preserved on the evidence refs.
    Returns ``{"findings": [...]}`` ordered deterministically.
    """
    tol = int(rules.get("pairing", {}).get("time_tolerance_seconds", 300))
    near = int(rules.get("pairing", {}).get("near_window_seconds",
                                           max(tol * 6, 1800)))
    fuzzy_max = int(rules.get("pairing", {}).get("call_fuzzy_distance", 2))
    offsets = {str(k): int(v) for k, v in (time_offsets or {}).items()}

    def _off(sub: dict[str, Any]) -> int:
        return offsets.get(sub["log_id"], 0)

    def _ets(sub: dict[str, Any], qso: dict[str, Any]) -> int:
        """配对用的（校正后）时间戳；原始 ts 在证据中保留。"""
        return qso["ts"] + _off(sub)

    # Only submissions with a normalisable own callsign can participate in
    # pairing; others are still kept (their validation issues are reported).
    subs = [s for s in submissions if s.get("station_call")]
    station_of: dict[str, dict[str, Any]] = {s["station_call"]: s for s in subs}

    # Attach owner to every usable QSO.  The parser has already diverted bad
    # band/mode/time/frequency/call/exchange/window lines to invalid_qsos, and
    # X-QSO lines live in a separate bucket, so every qsos entry is pairable.
    owned: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for sub in subs:
        for qso in sub["parsed"]["qsos"]:
            owned.append((sub, qso))

    findings: list[dict[str, Any]] = []

    # Every well-formed QSO is a pairing candidate; duplicates are decided
    # *after* cross-log matching so legitimate repeat band/mode contacts are
    # not mistaken for duplicates.
    canon: list[tuple[dict[str, Any], dict[str, Any]]] = list(owned)

    def call_relation(q_a: dict, q_b: dict, a_call: str, b_call: str) -> float:
        """2.0 both exact reciprocal, 1.0 one exact, 0.5 both fuzzy, 0 none."""
        wa = q_a["worked_call_norm"] or q_a["worked_call_raw"]
        wb = q_b["worked_call_norm"] or q_b["worked_call_raw"]
        a_exact = wa == b_call
        b_exact = wb == a_call
        if a_exact and b_exact:
            return 2.0
        a_fuzzy = _levenshtein(wa, b_call, fuzzy_max) <= fuzzy_max
        b_fuzzy = _levenshtein(wb, a_call, fuzzy_max) <= fuzzy_max
        if (a_exact and b_fuzzy) or (b_exact and a_fuzzy):
            return 1.0
        if a_fuzzy and b_fuzzy:
            return 0.5
        return 0.0

    # Bucket by band/mode to keep the scan small.
    bm: dict[tuple[str, str], list] = {}
    for item in canon:
        bm.setdefault((item[1]["band"], item[1]["mode"]), []).append(item)

    edges = []
    for (band, mode), items in bm.items():
        for i in range(len(items)):
            sub_a, qa = items[i]
            for j in range(i + 1, len(items)):
                sub_b, qb = items[j]
                if sub_a["station_call"] == sub_b["station_call"]:
                    continue
                cscore = call_relation(qa, qb, sub_a["station_call"],
                                       sub_b["station_call"])
                if cscore == 0:
                    continue
                delta = abs(_ets(sub_a, qa) - _ets(sub_b, qb))
                if delta <= tol:
                    tscore = 2.0
                elif delta <= near:
                    tscore = 1.0
                else:
                    continue
                edges.append((cscore + tscore, cscore, tscore, -delta,
                              sub_a, qa, sub_b, qb, delta))

    # Best edges first -> stable global 1:1 assignment.
    edges.sort(key=lambda e: (e[0], e[2], e[3]), reverse=True)
    matched: dict[tuple[str, int], dict] = {}
    for _w0, cscore, _ts, _negd, sub_a, qa, sub_b, qb, delta in edges:
        ka, kb = (sub_a["log_id"], qa["line"]), (sub_b["log_id"], qb["line"])
        if ka in matched or kb in matched:
            continue
        matched[ka] = {"sub": sub_b, "qso": qb, "cscore": cscore,
                       "delta": delta}
        matched[kb] = {"sub": sub_a, "qso": qa, "cscore": cscore,
                       "delta": delta}

    # ------------------------------------------------------------------
    # 3) Turn matched edges into findings.
    # ------------------------------------------------------------------
    seen_pairs: set[str] = set()
    for sub_a, qa in canon:
        ka = (sub_a["log_id"], qa["line"])
        m = matched.get(ka)
        if not m:
            continue
        sub_b, qb = m["sub"], m["qso"]
        pair_key = "|".join(sorted([f"{sub_a['log_id']}:{qa['line']}",
                                    f"{sub_b['log_id']}:{qb['line']}"]))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        refs = [_qso_ref(sub_a, qa, _off(sub_a)),
                _qso_ref(sub_b, qb, _off(sub_b))]
        diffs = _exchange_diffs(rules, refs[0], refs[1])
        call_detail = {}
        wa = qa["worked_call_raw"]
        wb = qb["worked_call_raw"]
        if wa != sub_b["station_call"] or wb != sub_a["station_call"]:
            call_detail = {"a_logged": wa, "a_actual": sub_b["station_call"],
                           "b_logged": wb, "b_actual": sub_a["station_call"]}

        if m["cscore"] < 2.0:
            status = "SUSPECT_CALL"
            reason = (f"呼号疑似抄错：{sub_a['station_call']} 第 {qa['line']} "
                      f"行记 {wa}，{sub_b['station_call']} 第 {qb['line']} "
                      f"行记 {wb}；频段/模式一致，时间差 {m['delta']} 秒，"
                      f"需裁判确认是否为同一通联")
        elif m["delta"] > tol:
            status = "TIME_DRIFT"
            reason = (f"双方互有记录但 UTC 时间相差 {m['delta']} 秒，"
                      f"超过容差 {tol} 秒（在近邻窗口 {near} 秒内），"
                      f"需裁判确认")
        elif diffs:
            status = "EXCHANGE_DIFF"
            fields = "、".join(sorted({d["field"] for d in diffs}))
            reason = (f"配对成立但交换字段 {fields} 双方记录不一致，"
                      f"仅凭日志无法确定抄错方，需裁判判定")
        else:
            status = "MATCH"
            reason = (f"双方在 {qa['band']}/{qa['mode']} 的记录"
                      f"（时间差 {m['delta']} 秒）与交换字段完全一致")
        findings.append(_make_finding(
            status, refs, qa["band"], qa["mode"], reason,
            min(_ets(sub_a, qa), _ets(sub_b, qb)), diffs, m["delta"],
            call_detail))

    # ------------------------------------------------------------------
    # 4) Leftover one-sided QSOs: classify as DUP / UNIQUE / NO_PARTNER_LOG.
    # ------------------------------------------------------------------
    # Group every QSO by (owner, worked-call, band, mode) in chronological
    # order.  An unmatched leftover whose previous same-key QSO is within the
    # duplicate window is a DUP (the earlier one keeps UNIQUE/NO_PARTNER_LOG).
    by_key: dict[tuple[str, str, str, str],
                 list[tuple[dict, dict]]] = {}
    for sub, q in sorted(owned, key=lambda sq: (_ets(sq[0], sq[1]),
                                                sq[1]["line"])):
        key = (sub["station_call"],
               q["worked_call_norm"] or q["worked_call_raw"],
               q["band"], q["mode"])
        by_key.setdefault(key, []).append((sub, q))

    dup_window = int(rules.get("pairing", {})
                     .get("duplicate_window_seconds", 600))
    for sub_a, qa in canon:
        ka = (sub_a["log_id"], qa["line"])
        if ka in matched:
            continue
        refs = [_qso_ref(sub_a, qa, _off(sub_a))]
        key = (sub_a["station_call"],
               qa["worked_call_norm"] or qa["worked_call_raw"],
               qa["band"], qa["mode"])
        dup_of = None
        chain = by_key[key]
        my_pos = next(i for i, (s, q) in enumerate(chain)
                      if s["log_id"] == sub_a["log_id"] and q["line"] == qa["line"])
        for prev_sub, prev_q in reversed(chain[:my_pos]):
            if abs(_ets(sub_a, qa) - _ets(prev_sub, prev_q)) <= dup_window:
                dup_of = (prev_sub, prev_q)
                break
        if dup_of is not None:
            prev_sub, prev_q = dup_of
            refs.append(_qso_ref(prev_sub, prev_q, _off(prev_sub)))
            findings.append(_make_finding(
                "DUP", refs, qa["band"], qa["mode"],
                f"{sub_a['station_call']} 第 {qa['line']} 行与 "
                f"{qa['worked_call_raw']} 在 {qa['band']}/{qa['mode']} 的"
                f"较早记录（第 {prev_q['line']} 行，"
                f"{prev_q['date']} {prev_q['time']}）相隔 "
                f"{abs(_ets(sub_a, qa) - _ets(prev_sub, prev_q))} 秒且未能交叉配对，"
                f"判为重复通联",
                _ets(sub_a, qa)))
            continue

        wanted = qa["worked_call_norm"]
        if wanted and wanted in station_of and wanted != sub_a["station_call"]:
            sub_b = station_of[wanted]
            reason = (f"{sub_a['station_call']} 第 {qa['line']} 行声称与 "
                      f"{wanted} 在 {qa['band']}/{qa['mode']} 于 "
                      f"{qa['date']} {qa['time']} 通联，但 {wanted} "
                      f"已提交的日志中无对应记录（单方记录）")
            # Point at the partner's log as evidence scope.
            findings.append(_make_finding(
                "UNIQUE", refs, qa["band"], qa["mode"], reason,
                _ets(sub_a, qa),
                call_detail={"partner_log": sub_b["filename"]}))
        else:
            status = "NO_PARTNER_LOG"
            reason = (f"{sub_a['station_call']} 第 {qa['line']} 行记录的对方 "
                      f"{qa['worked_call_raw']} 在本批次中没有提交日志，"
                      f"无法交叉核实")
            findings.append(_make_finding(
                status, refs, qa["band"], qa["mode"], reason,
                _ets(sub_a, qa)))

    findings.sort(key=lambda f: (f["ts_hint"], f["status"], f["id"]))

    # ------------------------------------------------------------------
    # 5) 频段切换合规分析（按校正后 UTC 时间；与交叉配对待的证据并列）。
    # ------------------------------------------------------------------
    band_analysis = analyze_band_switching(rules, submissions,
                                           time_offsets=offsets)
    band_findings, node_map = _band_findings(band_analysis)
    for st in band_analysis.get("stations", []):
        for node in st.get("chain", []):
            mapped = node_map.get((st["station"], node["seq"]))
            if mapped:
                node["finding"] = mapped
    band_findings.sort(key=lambda f: (f["ts_hint"], f["status"], f["id"]))
    findings.extend(band_findings)
    findings.sort(key=lambda f: (f["ts_hint"], f["status"], f["id"]))
    return {"findings": findings, "band_analysis": band_analysis}


# ---------------------------------------------------------------------------
# Decisions and scoring
# ---------------------------------------------------------------------------

def _catalogue_penalty(rules: dict[str, Any], code: str) -> int:
    return int(rules.get("penalties", {}).get(code, {}).get("points", 0))


def apply_decisions(rules: dict[str, Any], findings: list[dict[str, Any]],
                    decisions: dict[str, dict[str, Any]]
                    ) -> list[dict[str, Any]]:
    """Annotate each finding with its per-station scoring effects.

    Effects per station: ``{"counted": bool, "penalty_codes": [...],
    "penalty_points": int, "partner": str|None, "exchange": {...}}``.
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        dec = decisions.get(f["id"])
        resolution = dec.get("resolution") if dec else None
        effects: dict[str, dict[str, Any]] = {}
        refs = f["refs"]
        stations = [r["station"] for r in refs]

        def blank(st: str | None) -> dict[str, Any]:
            return {"counted": False, "penalty_codes": [],
                    "penalty_points": 0, "partner": None,
                    "exchange": {}}

        two_sided = len(refs) == 2 and len(set(stations)) == 2
        owner = refs[0]["station"]
        partner = refs[1]["station"] if len(refs) > 1 else None

        if f["status"] == "MATCH":
            for r in refs:
                effects[r["station"]] = {
                    "counted": True, "penalty_codes": [],
                    "penalty_points": 0,
                    "partner": (refs[1]["station"] if r is refs[0]
                                else refs[0]["station"]),
                    "exchange": r["recv"]}
        elif f["status"] in ("EXCHANGE_DIFF", "TIME_DRIFT", "SUSPECT_CALL"):
            for r in refs:
                e = blank(r["station"])
                if resolution == "CONFIRMED":
                    e["counted"] = True
                    e["partner"] = (refs[1]["station"] if r is refs[0]
                                    else refs[0]["station"])
                    e["exchange"] = r["recv"]
                effects[r["station"]] = e
            if resolution == "CONFIRMED" and dec.get("fault_station"):
                fault = dec["fault_station"]
                code = dec.get("penalty_code") or "BAD_EXCHANGE"
                if f["status"] != "EXCHANGE_DIFF":
                    code = dec.get("penalty_code") or code
                if fault in effects and code in rules.get("penalties", {}):
                    effects[fault]["penalty_codes"].append(code)
                    effects[fault]["penalty_points"] += _catalogue_penalty(
                        rules, code)
        elif f["status"] == "NO_PARTNER_LOG":
            e = blank(owner)
            if resolution == "GRANTED":
                e["counted"] = True
                e["partner"] = refs[0]["worked_call"]
                e["exchange"] = refs[0]["recv"]
            if dec and dec.get("penalty_code") and dec["penalty_code"] in \
                    rules.get("penalties", {}):
                e["penalty_codes"].append(dec["penalty_code"])
                e["penalty_points"] += _catalogue_penalty(
                    rules, dec["penalty_code"])
            effects[owner] = e
        elif f["status"] == "UNIQUE":
            e = blank(owner)
            if resolution == "GRANTED":
                e["counted"] = True
                e["partner"] = refs[0]["worked_call"]
                e["exchange"] = refs[0]["recv"]
            elif resolution != "WAIVED" and rules.get("auto_penalty_unique"):
                # Opt-in: some contests automatically remove points for a
                # claimed QSO missing from the other log.  Off by default —
                # judges apply NOT_IN_LOG themselves with a written reason.
                code = "NOT_IN_LOG"
                e["penalty_codes"].append(code)
                e["penalty_points"] += _catalogue_penalty(rules, code)
            if dec and dec.get("penalty_code") and dec["penalty_code"] in \
                    rules.get("penalties", {}):
                e["penalty_codes"].append(dec["penalty_code"])
                e["penalty_points"] += _catalogue_penalty(
                    rules, dec["penalty_code"])
            effects[owner] = e
        elif f["status"] == "DUP":
            # Only the duplicate (later) reference is penalised; the first
            # occurrence pairs/scores on its own merits elsewhere.
            e = blank(owner)
            if resolution != "WAIVED":
                code = "DUP"
                if _catalogue_penalty(rules, code):
                    e["penalty_codes"].append(code)
                    e["penalty_points"] += _catalogue_penalty(rules, code)
            effects[owner] = e
        elif f["status"] in ("BAND_SWITCH_EXCESS", "BAND_DWELL_SHORT",
                             "BAND_SWITCH_AMBIGUOUS"):
            # 频段切换合规证据只影响归属台站，不改变任何通联的计分。
            # 确定性违规（超限/驻留不足）默认按策略罚目自动罚分，WAIVED
            # 豁免、REMOVED 剔除；歧义段仅在裁判 CONFIRMED 后罚分，
            # 罚目可取裁决指定值或策略快照中的 penalty_ambiguous。
            e = blank(owner)
            detail = f.get("band_switch") or {}
            policy = detail.get("policy") or {}
            if f["status"] in BAND_DETERMINED_STATUSES:
                if resolution not in ("WAIVED", "REMOVED"):
                    code = (policy.get("penalty_excess")
                            if f["status"] == "BAND_SWITCH_EXCESS"
                            else policy.get("penalty_dwell"))
                    if code and code in rules.get("penalties", {}):
                        e["penalty_codes"].append(code)
                        e["penalty_points"] += _catalogue_penalty(rules, code)
            elif resolution == "CONFIRMED":
                code = (dec.get("penalty_code")
                        or policy.get("penalty_ambiguous"))
                if code and code in rules.get("penalties", {}):
                    e["penalty_codes"].append(code)
                    e["penalty_points"] += _catalogue_penalty(rules, code)
            effects[owner] = e

        g = dict(f)
        g["decision"] = dec
        g["effects"] = effects
        out.append(g)
    return out


def _qso_point_value(rules: dict[str, Any], band: str, mode: str) -> int:
    qp = rules.get("qso_points", {})
    if band in qp.get("by_band", {}):
        return int(qp["by_band"][band])
    if mode in qp.get("by_mode", {}):
        return int(qp["by_mode"][mode])
    return int(qp.get("default", 1))


def score(rules: dict[str, Any],
          annotated: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-station scorecards from annotated findings."""
    cards: dict[str, dict[str, Any]] = {}

    def card(st: str) -> dict[str, Any]:
        return cards.setdefault(st, {
            "station": st, "qso_counted": 0, "qso_points": 0,
            "penalty_points": 0, "penalty_codes": [],
            "mult_worked_call": set(), "mult_band": set(),
            "mult_band_mode": set(),
            "mult_exchange": {},   # field -> set(values)
            "qso_evidence": [],
        })

    for f in annotated:
        for st, eff in f["effects"].items():
            c = card(st)
            for code in eff["penalty_codes"]:
                if code not in c["penalty_codes"]:
                    c["penalty_codes"].append(code)
            c["penalty_points"] += eff["penalty_points"]
            if not eff["counted"]:
                continue
            ref = next((r for r in f["refs"] if r["station"] == st),
                       f["refs"][0])
            c["qso_counted"] += 1
            c["qso_points"] += _qso_point_value(rules, f["band"], f["mode"])
            c["qso_evidence"].append(
                {"finding_id": f["id"], "filename": ref["filename"],
                 "line": ref["line"], "band": f["band"], "mode": f["mode"]})
            partner = eff.get("partner")
            from .parser import normalize_callsign
            norm_partner = normalize_callsign(partner) if partner else None
            if norm_partner and norm_partner != st:
                c["mult_worked_call"].add(norm_partner)
            c["mult_band"].add(f["band"])
            c["mult_band_mode"].add(f"{f['band']}|{f['mode']}")
            for spec in rules.get("exchange_fields", []):
                name = spec["name"]
                val = eff.get("exchange", {}).get(name)
                if val is not None:
                    c["mult_exchange"].setdefault(name, set()).add(str(val))

    mult_defs = rules.get("multipliers", [])
    result_cards = []
    for st, c in cards.items():
        mult_components: dict[str, int] = {}
        for mdef in mult_defs:
            mtype = mdef["type"]
            if mtype == "worked_call":
                value = len(c["mult_worked_call"])
            elif mtype == "band":
                value = len(c["mult_band"])
            elif mtype == "band_mode":
                value = len(c["mult_band_mode"])
            elif mtype == "exchange_field":
                value = len(c["mult_exchange"].get(mdef.get("field", ""),
                                                    set()))
            else:
                continue
            mult_components[mdef.get("name", mtype)] = value
        multiplier = 1
        for value in mult_components.values():
            multiplier *= value
        raw_score = c["qso_points"] * multiplier
        total = raw_score - c["penalty_points"]
        result_cards.append({
            "station": st,
            "qso_counted": c["qso_counted"],
            "qso_points": c["qso_points"],
            "multiplier_components": mult_components,
            "multiplier_total": multiplier,
            "raw_score": raw_score,
            "penalty_points": c["penalty_points"],
            "penalty_codes": sorted(c["penalty_codes"]),
            "total_score": total,
            "qso_evidence": sorted(c["qso_evidence"],
                                   key=lambda e: (e["filename"], e["line"])),
        })
    result_cards.sort(key=lambda x: (-x["total_score"], x["station"]))
    totals = {
        "findings": len(annotated),
        "pending": sum(1 for f in annotated if f["pending"]
                       and not f.get("decision")),
        "stations": len(result_cards),
        "sum_total_score": sum(c["total_score"] for c in result_cards),
        "band_switch": {
            "excess": sum(1 for f in annotated
                          if f["status"] == "BAND_SWITCH_EXCESS"),
            "dwell_short": sum(1 for f in annotated
                               if f["status"] == "BAND_DWELL_SHORT"),
            "ambiguous": sum(1 for f in annotated
                             if f["status"] == "BAND_SWITCH_AMBIGUOUS"),
        },
    }
    return {"scorecards": result_cards, "summary": totals}


# ---------------------------------------------------------------------------
# Deterministic version snapshots
# ---------------------------------------------------------------------------

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def content_hash(rules: dict[str, Any], findings: list[dict[str, Any]],
                 decisions: dict[str, dict[str, Any]],
                 results: dict[str, Any],
                 clock_scheme: dict[str, Any] | None = None) -> str:
    # Evidence effects are derivable; hash the inputs that define a verdict.
    slim_findings = [{k: v for k, v in f.items()
                      if k in ("id", "status", "band", "mode", "ts_hint",
                               "stations", "refs", "time_delta_seconds",
                               "exchange_diffs", "call_detail",
                               "auto_reason", "pending", "band_switch")}
                     for f in findings]
    payload = {"rules": rules, "findings": slim_findings,
               "decisions": decisions, "results": results,
               "clock_scheme": clock_scheme}
    return hashlib.sha256(_canonical(payload)).hexdigest()
