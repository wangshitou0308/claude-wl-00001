"""批次级时钟偏差分析（纯函数模块，不触碰持久层）。

裁判选定**参考日志**与**最大搜索窗口**后，本模块在呼号互指、频段与
模式一致的 QSO 中筛选**无歧义**候选对，估计每份日志相对参考日志的
时钟偏差，并按日志给出时间差中位数、离散度（MAD）、样本数与覆盖时段。

以下情形**只列证据、不建议偏移**：

* 样本不足（无歧义候选对少于 ``min_samples``）；
* 偏差随时间变化（前后半程中位数之差超过 ``drift_threshold_seconds``）；
* 日志关系图不与参考日志连通（批次内没有可串联的互指候选对）。

只经中间日志间接连通的日志，其中位数与离散度由沿途各段样本的
逐跳累计分布（笛卡尔和，超限后确定性等距抽样）给出。

分析只读取解析结果，绝不修改原始 Cabrillo 文本或时间。
"""

from __future__ import annotations

import bisect
import datetime as _dt
import math
import statistics
from typing import Any

DEFAULT_MIN_SAMPLES = 3
DEFAULT_DRIFT_THRESHOLD_SECONDS = 60
DEFAULT_MAX_MAD_SECONDS = 90
# 多跳路径累计偏差样本的上限（超出后确定性等距抽样，防止组合爆炸）
MAX_COMBINED_SAMPLES = 4096


def _iso(ts: int) -> str:
    return _dt.datetime.fromtimestamp(
        ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _iso_to_ts(text: str) -> int:
    dt = _dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())


def _round_minutes(seconds: float) -> int:
    """四舍五入到整分钟（半点远离零取整）。"""
    if not seconds:
        return 0
    return int(math.copysign(math.floor(abs(seconds) / 60.0 + 0.5), seconds))


def _unambiguous_pairs(a_qsos: list[dict[str, Any]],
                       b_qsos: list[dict[str, Any]],
                       window: int) -> list[tuple[dict, dict, int]]:
    """双方呼号精确互指且时间差在窗口内的 1:1 无歧义候选对。

    返回 ``[(qa, qb, delta_seconds)]``，``delta = qb.ts - qa.ts``。
    某条 QSO 在对方日志的窗口内若有多个候选（或反之），整组舍弃——
    重复/密集记录天然有歧义，不能用来估计时钟偏差。
    """
    b_sorted = sorted(b_qsos, key=lambda q: (q["ts"], q["line"]))
    b_ts = [q["ts"] for q in b_sorted]
    from_a: dict[int, list[dict]] = {}
    from_b: dict[int, list[int]] = {}
    for qa in a_qsos:
        lo = bisect.bisect_left(b_ts, qa["ts"] - window)
        hi = bisect.bisect_right(b_ts, qa["ts"] + window)
        for qb in b_sorted[lo:hi]:
            from_a.setdefault(qa["line"], []).append(qb)
            from_b.setdefault(qb["line"], []).append(qa["line"])
    pairs = []
    for qa in a_qsos:
        cands = from_a.get(qa["line"], [])
        if len(cands) != 1:
            continue
        qb = cands[0]
        if from_b.get(qb["line"]) != [qa["line"]]:
            continue
        pairs.append((qa, qb, qb["ts"] - qa["ts"]))
    return pairs


def _pair_report(sub_a: dict[str, Any], sub_b: dict[str, Any],
                 window: int, drift_threshold: int) -> dict[str, Any]:
    """两份日志间（跨全部频段/模式）的候选对统计。

    时间差一律为 ``ts_b - ts_a``（b 相对 a 的时钟偏差）。
    """
    a_rel = [q for q in sub_a["parsed"]["qsos"]
             if (q.get("worked_call_norm") or q.get("worked_call_raw"))
             == sub_b["station_call"]]
    b_rel = [q for q in sub_b["parsed"]["qsos"]
             if (q.get("worked_call_norm") or q.get("worked_call_raw"))
             == sub_a["station_call"]]
    bm_a: dict[tuple[str, str], list] = {}
    for q in a_rel:
        bm_a.setdefault((q["band"], q["mode"]), []).append(q)
    bm_b: dict[tuple[str, str], list] = {}
    for q in b_rel:
        bm_b.setdefault((q["band"], q["mode"]), []).append(q)

    raw_pairs: list[tuple[dict, dict, int]] = []
    for key in bm_a:
        if key not in bm_b:
            continue
        raw_pairs.extend(_unambiguous_pairs(bm_a[key], bm_b[key], window))
    raw_pairs.sort(key=lambda p: (p[0]["ts"], p[0]["line"]))

    deltas = [d for _qa, _qb, d in raw_pairs]
    evidence = [{
        "a_line": qa["line"], "b_line": qb["line"],
        "band": qa["band"], "mode": qa["mode"],
        "a_time": _iso(qa["ts"]), "b_time": _iso(qb["ts"]),
        "delta_seconds": delta,
    } for qa, qb, delta in raw_pairs]

    report: dict[str, Any] = {
        "a": {"log_id": sub_a["log_id"], "filename": sub_a["filename"],
              "station_call": sub_a["station_call"]},
        "b": {"log_id": sub_b["log_id"], "filename": sub_b["filename"],
              "station_call": sub_b["station_call"]},
        "sample_count": len(deltas),
        "median_seconds": None,
        "mad_seconds": None,
        "min_seconds": None,
        "max_seconds": None,
        "coverage": None,
        "drift_detected": False,
        "drift_detail": None,
        "evidence": evidence,
    }
    if not deltas:
        return report

    med = statistics.median(deltas)
    mad = statistics.median([abs(d - med) for d in deltas])
    report["median_seconds"] = med
    report["mad_seconds"] = mad
    report["min_seconds"] = min(deltas)
    report["max_seconds"] = max(deltas)
    all_ts = [t for qa, qb, _d in raw_pairs for t in (qa["ts"], qb["ts"])]
    report["coverage"] = {"first": _iso(min(all_ts)),
                          "last": _iso(max(all_ts)),
                          "span_seconds": max(all_ts) - min(all_ts)}
    # 偏差随时间变化：按时间分前后半程，比较半程中位数。
    if len(deltas) >= 4:
        half = len(deltas) // 2
        m1 = statistics.median(deltas[:half])
        m2 = statistics.median(deltas[-half:])
        report["drift_detail"] = {
            "first_half_median_seconds": m1,
            "second_half_median_seconds": m2,
        }
        report["drift_detected"] = abs(m2 - m1) > drift_threshold
    return report


def _merge_coverage(coverages: list[dict[str, Any] | None]
                    ) -> dict[str, Any] | None:
    covs = [c for c in coverages if c]
    if not covs:
        return None
    first = min(c["first"] for c in covs)   # ISO 字符串可按字典序比较
    last = max(c["last"] for c in covs)
    return {"first": first, "last": last,
            "span_seconds": _iso_to_ts(last) - _iso_to_ts(first)}


def _downsample_sorted(values: list[float], cap: int) -> list[float]:
    """确定性等距抽样，把有序样本压到 *cap* 以内。"""
    if len(values) <= cap:
        return values
    step = len(values) / cap
    return [values[int(i * step)] for i in range(cap)]


def analyze_clock_skew(submissions: list[dict[str, Any]],
                       reference_log_id: str,
                       max_window_seconds: int,
                       min_samples: int = DEFAULT_MIN_SAMPLES,
                       drift_threshold_seconds:
                           int = DEFAULT_DRIFT_THRESHOLD_SECONDS,
                       max_mad_seconds: int = DEFAULT_MAX_MAD_SECONDS
                       ) -> dict[str, Any]:
    """分析各日志相对 *reference_log_id* 的时钟偏差。

    *submissions* 与 :func:`cabrillo_judge.engine.adjudicate` 的输入相同。
    返回完整报告；``suggested_offsets_minutes`` 的语义为
    ``校正时间 = 原始时间 + 偏移（分钟）``，建议值使各日志与参考日志对齐。
    """
    if max_window_seconds <= 0:
        raise ValueError("max_window_seconds 必须为正整数秒")
    subs = [s for s in submissions if s.get("station_call")]
    excluded = [{"log_id": s["log_id"], "filename": s["filename"]}
                for s in submissions if not s.get("station_call")]
    by_id = {s["log_id"]: s for s in subs}
    ref = by_id.get(reference_log_id)
    if ref is None:
        raise KeyError(f"参考日志 {reference_log_id} 不在批次中或缺少有效呼号")

    # 1) 两两日志的无歧义候选对统计（候选对即日志关系图的边）。
    pairs: list[dict[str, Any]] = []
    adj: dict[str, list[tuple[str, float, dict[str, Any]]]] = {
        s["log_id"]: [] for s in subs}
    for i in range(len(subs)):
        for j in range(i + 1, len(subs)):
            sa, sb = subs[i], subs[j]
            if sa["station_call"] == sb["station_call"]:
                continue
            rep = _pair_report(sa, sb, max_window_seconds,
                               drift_threshold_seconds)
            if rep["sample_count"] == 0:
                continue
            problems = []
            if rep["sample_count"] < min_samples:
                problems.append(
                    f"样本不足（{rep['sample_count']} < {min_samples}）")
            if rep["drift_detected"]:
                problems.append("偏差随时间变化，单一整分钟偏移不可靠")
            if rep["mad_seconds"] is not None and \
                    rep["mad_seconds"] > max_mad_seconds:
                problems.append(
                    f"离散度过大（MAD {rep['mad_seconds']:.0f} 秒 > "
                    f"{max_mad_seconds} 秒）")
            rep["problems"] = problems
            rep["usable"] = not problems
            pairs.append(rep)
            med = rep["median_seconds"]
            adj[sa["log_id"]].append((sb["log_id"], med, rep))
            adj[sb["log_id"]].append((sa["log_id"], -med, rep))

    # 2) 从参考日志沿候选对图 BFS，逐跳累计偏差样本（笛卡尔和），
    #    得到 ts_log − ts_ref 的样本分布；中位数即估计偏差。
    acc: dict[str, list[float]] = {ref["log_id"]: [0.0]}
    est: dict[str, float] = {ref["log_id"]: 0.0}
    paths: dict[str, list[dict[str, Any]]] = {ref["log_id"]: []}
    queue = [ref["log_id"]]
    while queue:
        cur = queue.pop(0)
        for nxt, signed, rep in adj.get(cur, []):
            if nxt in est:
                continue
            hop_deltas = [e["delta_seconds"] for e in rep["evidence"]]
            if rep["a"]["log_id"] != cur:      # 反向遍历：样本取负
                hop_deltas = [-d for d in hop_deltas]
            combined = [x + d for x in acc[cur] for d in hop_deltas]
            if len(combined) > MAX_COMBINED_SAMPLES:
                combined = _downsample_sorted(sorted(combined),
                                              MAX_COMBINED_SAMPLES)
            acc[nxt] = combined
            est[nxt] = statistics.median(combined)
            paths[nxt] = paths[cur] + [{
                "from": cur, "to": nxt,
                "from_station": by_id[cur]["station_call"],
                "to_station": by_id[nxt]["station_call"],
                "median_seconds": signed,
                "sample_count": rep["sample_count"],
                "mad_seconds": rep["mad_seconds"],
                "coverage": rep["coverage"],
                "drift_detected": rep["drift_detected"],
                "usable": rep["usable"],
                "problems": rep["problems"],
            }]
            queue.append(nxt)

    # 3) 按日志汇总：连通性、统计量、建议（或不建议的理由）。
    logs: list[dict[str, Any]] = []
    suggestions: dict[str, int] = {}
    for s in subs:
        lid = s["log_id"]
        entry: dict[str, Any] = {
            "log_id": lid, "filename": s["filename"],
            "station_call": s["station_call"],
            "is_reference": lid == ref["log_id"],
        }
        if lid == ref["log_id"]:
            entry.update({
                "connected": True, "path": [],
                "estimated_skew_seconds": 0,
                "median_seconds": 0, "mad_seconds": None,
                "sample_count": None, "coverage": None,
                "drift_detected": False, "suppress_reasons": [],
                "suggested_offset_minutes": 0,
            })
        elif lid not in est:
            entry.update({
                "connected": False, "path": [],
                "estimated_skew_seconds": None,
                "median_seconds": None, "mad_seconds": None,
                "sample_count": 0, "coverage": None,
                "drift_detected": False,
                "suppress_reasons": [
                    "与参考日志不连通：批次内没有可串联的互指候选对，"
                    "只列证据，不建议偏移"],
                "suggested_offset_minutes": None,
            })
        else:
            hops = paths[lid]
            reasons = [f"{h['from_station']}↔{h['to_station']}：{p}"
                       for h in hops for p in h["problems"]]
            # 中位数/离散度来自逐跳累计的偏差样本分布（单跳时即该段样本）
            med = est[lid]
            mad = statistics.median([abs(x - med) for x in acc[lid]])
            entry.update({
                "connected": True, "path": hops,
                "estimated_skew_seconds": med,
                "median_seconds": med,
                "mad_seconds": mad,
                "sample_count": sum(h["sample_count"] for h in hops),
                "coverage": _merge_coverage([h["coverage"] for h in hops]),
                "drift_detected": any(h["drift_detected"] for h in hops),
                "suppress_reasons": reasons,
                "suggested_offset_minutes": None,
            })
            if not reasons:
                suggested = _round_minutes(-med)
                entry["suggested_offset_minutes"] = suggested
                suggestions[lid] = suggested
        logs.append(entry)

    return {
        "reference": {"log_id": ref["log_id"], "filename": ref["filename"],
                      "station_call": ref["station_call"]},
        "max_window_seconds": max_window_seconds,
        "thresholds": {
            "min_samples": min_samples,
            "drift_threshold_seconds": drift_threshold_seconds,
            "max_mad_seconds": max_mad_seconds,
        },
        "offset_semantics": (
            "estimated_skew_seconds = 该日志时钟相对参考日志的偏差"
            "（ts_log − ts_ref）；校正时间 = 原始时间 + 偏移（分钟），"
            "建议偏移 = −估计偏差（取整分钟）"),
        "logs": logs,
        "pairs": pairs,
        "suggested_offsets_minutes": suggestions,
        "excluded_logs": excluded,
    }
