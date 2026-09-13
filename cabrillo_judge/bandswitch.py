"""频段切换合规分析（纯函数模块，不触碰持久层）。

系统按各台站**校正后的 UTC 时间**排列有效 QSO（X-QSO、invalid 行不参与），
为每个台站重建频段切换链，并按参赛类别（如 ``CATEGORY-OPERATOR``）匹配
策略快照：

* 每个 UTC 时钟小时（``[H:00, H+1:00)``）统计确定切换次数，超过
  ``max_switches_per_clock_hour`` 的切换逐条标记超限；
* 换频后的驻留时长不足 ``min_dwell_seconds``（在下次离开该频段时判定）
  标记驻留不足；
* 每个台站的第一个 QSO 不视为切换；
* 同一分钟（Cabrillo 时间分辨率为分钟）内出现跨频段多条记录时，先后顺序
  无法判定——保留**歧义**，绝不自行排序，也不把跨越歧义段的切换计入
  确定计数；歧义段逐条列为待裁决项。

每次确定切换记录前后频段、原日志行、间隔、计数窗口与触发条款。
分析只读取解析结果与整分钟时钟校正偏移，绝不修改原始 Cabrillo 文本。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from .rules import (
    BAND_FIRST_QSO_FREE,
    BAND_HOUR_BOUNDARY_CLOCK_UTC,
    BAND_PENALTY_AMBIGUOUS,
    BAND_PENALTY_DWELL,
    BAND_PENALTY_EXCESS,
    BAND_SAME_MINUTE_AMBIGUOUS,
)

CLAUSE_MAX_SWITCHES = "max_switches_per_clock_hour"
CLAUSE_MIN_DWELL = "min_dwell_seconds"

# 固定语义（写入每个策略快照，便于版本间比对）
SEMANTICS = {
    "first_qso": BAND_FIRST_QSO_FREE,
    "hour_boundary": BAND_HOUR_BOUNDARY_CLOCK_UTC,
    "same_minute_multi_band": BAND_SAME_MINUTE_AMBIGUOUS,
    "time_basis": "corrected_utc",
}


def _hour_window(ts: int) -> tuple[str, int, int]:
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    start = dt.replace(minute=0, second=0, microsecond=0)
    start_ts = int(start.timestamp())
    return start.strftime("%Y-%m-%d %H:00"), start_ts, start_ts + 3600


def _ts_parts(ts: int) -> tuple[str, str]:
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H%M")


def _raw_text(sub: dict[str, Any], line: int) -> str:
    for rl in sub["parsed"].get("raw_lines", []):
        if rl["line"] == line:
            return rl["text"]
    return ""


def _category_location(sub: dict[str, Any], category: str
                       ) -> tuple[str | None, int | None]:
    tag = f"CATEGORY-{category}"
    for rt in sub["parsed"].get("raw_tags", []):
        if rt.get("tag") == tag:
            return sub["filename"], rt.get("line")
    return None, None


def resolve_policy(rules: dict[str, Any],
                   submissions: list[dict[str, Any]]) -> dict[str, Any]:
    """按台站解析适用的频段切换策略快照。

    一个台站有多份日志时，取列表序（存储层即上传序）第一份声明了该
    类别头的日志；所有日志的类别取值一并记录。缺类别/缺配置时回退默认。
    """
    bc = rules.get("band_compliance") or {}
    default_max = bc.get("max_switches_per_clock_hour")
    default_dwell = bc.get("min_dwell_seconds")
    by_category = bc.get("by_category") or []
    enabled = bool(bc.get("enabled", True))

    def _override_for(category: str | None, value: str | None
                      ) -> tuple[dict[str, Any] | None, str | None]:
        if not category or not value:
            return None, None
        for entry in by_category:
            if not isinstance(entry, dict) or entry.get("category") != category:
                continue
            if "value" in entry:
                if str(entry.get("value")) == value:
                    return entry, "by_category"
            match = entry.get("match")
            if isinstance(match, dict) and value in match \
                    and isinstance(match[value], dict):
                return match[value], "by_category"
        return None, None

    policies: dict[str, dict[str, Any]] = {}
    station_logs: dict[str, list[dict[str, Any]]] = {}
    for sub in submissions:
        st = sub.get("station_call")
        if st:
            station_logs.setdefault(st, []).append(sub)

    for station, subs in station_logs.items():
        # 候选类别按策略条目出现的顺序取；没有任何条目时用 OPERATOR 兜底
        categories = []
        for entry in by_category:
            if isinstance(entry, dict) and entry.get("category"):
                categories.append(str(entry["category"]))
        if not categories:
            categories = ["OPERATOR"]
        categories = list(dict.fromkeys(categories))

        chosen = None
        chosen_cat = None
        chosen_val = None
        chosen_sub = None
        for cat in categories:
            for sub in subs:  # 列表序即上传序
                val = (sub["parsed"].get("headers") or {}).get(
                    f"CATEGORY-{cat}")
                if val:
                    chosen = cat
                    chosen_cat = cat
                    chosen_val = val
                    chosen_sub = sub
                    break
            if chosen:
                break

        override, match_kind = (None, None)
        if chosen_cat and chosen_val:
            override, match_kind = _override_for(chosen_cat, chosen_val)

        max_switches = default_max
        dwell = default_dwell
        if override is not None:
            if override.get("max_switches_per_clock_hour") is not None:
                max_switches = override["max_switches_per_clock_hour"]
            if override.get("min_dwell_seconds") is not None:
                dwell = override["min_dwell_seconds"]

        fn, ln = (None, None)
        if chosen_sub is not None and chosen_cat:
            fn, ln = _category_location(chosen_sub, chosen_cat)
        all_categories = []
        for sub in subs:
            for rt in sub["parsed"].get("raw_tags", []):
                tag = rt.get("tag") or ""
                if tag.startswith("CATEGORY-"):
                    all_categories.append(
                        {"filename": sub["filename"], "line": rt.get("line"),
                         "category": tag[9:], "value": rt.get("value")})

        policies[station] = {
            "enabled": enabled,
            "max_switches_per_clock_hour": max_switches,
            "min_dwell_seconds": dwell,
            "penalty_excess": bc.get("penalty_excess", BAND_PENALTY_EXCESS),
            "penalty_dwell": bc.get("penalty_dwell", BAND_PENALTY_DWELL),
            "penalty_ambiguous": bc.get(
                "penalty_ambiguous", BAND_PENALTY_AMBIGUOUS),
            "matched": (match_kind or ("default" if chosen_val
                                       else "no_category")),
            "category": chosen_cat,
            "category_value": chosen_val,
            "category_source": ({"filename": fn, "line": ln}
                                if fn else None),
            "all_log_categories": all_categories,
            "semantics": dict(SEMANTICS),
        }
    return policies


def _qso_ref(sub: dict[str, Any], qso: dict[str, Any],
             offset_seconds: int) -> dict[str, Any]:
    ets = qso["ts"] + offset_seconds
    edate, etime = _ts_parts(ets)
    return {
        "log_id": sub["log_id"],
        "filename": sub["filename"],
        "station": sub["station_call"],
        "line": qso["line"],
        "ts": qso["ts"],
        "date": qso["date"],
        "time": qso["time"],
        "offset_seconds": offset_seconds,
        "corrected_ts": ets,
        "corrected_date": edate,
        "corrected_time": etime,
        "freq_khz": qso["freq_khz"],
        "band": qso["band"],
        "mode": qso["mode"],
        "worked_call": qso["worked_call_raw"],
        "raw": _raw_text(sub, qso["line"]),
    }


def analyze_station(station: str,
                    subs: list[dict[str, Any]],
                    offsets: dict[str, int],
                    policy: dict[str, Any]) -> dict[str, Any]:
    """重建一个台站的频段切换链并判定超限/驻留/歧义。"""
    # 1) 收集全部有效 QSO（X-QSO、invalid 行不参与），按校正时间排列。
    events: list[tuple[int, int, dict[str, Any], dict[str, Any], int]] = []
    for sub_index, sub in enumerate(subs):
        off = int(offsets.get(sub["log_id"], 0))
        for qso in sub["parsed"]["qsos"]:
            events.append((qso["ts"] + off, sub_index, sub, qso, off))
    # 同一时刻不自行假定跨频段先后；同频段时按文件:行号稳定排列。
    events.sort(key=lambda e: (e[0], e[1], e[3]["line"]))

    # 2) 同一校正时刻分组：单频段 => 确定；跨频段 => 歧义段。
    groups: list[dict[str, Any]] = []
    i = 0
    while i < len(events):
        ts = events[i][0]
        members = [e for e in events[i:] if e[0] == ts]
        bands = list(dict.fromkeys(e[3]["band"] for e in members))
        groups.append({
            "ts": ts,
            "bands": bands,
            "definite": len(bands) == 1,
            "band": bands[0] if len(bands) == 1 else None,
            "members": members,
        })
        i += len(members)

    chain: list[dict[str, Any]] = []
    hour_counts: dict[str, int] = {}
    hour_meta: dict[str, dict[str, Any]] = {}
    current_band: str | None = None
    last_switch: dict[str, Any] | None = None
    pending_ambiguity = False
    violations: list[dict[str, Any]] = []

    def _refs(members) -> list[dict[str, Any]]:
        return [_qso_ref(sub, qso, off)
                for _ts, _si, sub, qso, off in sorted(
                    members, key=lambda e: (e[1], e[3]["line"]))]

    for group in groups:
        ts = group["ts"]
        hour, win_start, win_end = _hour_window(ts)
        if not group["definite"]:
            # 同分钟跨频段：顺序不明，保留歧义，不计确定切换。
            refs = _refs(group["members"])
            node = {
                "seq": len(chain) + 1,
                "type": "ambiguous",
                "ts": ts,
                "date": refs[0]["corrected_date"],
                "time": refs[0]["corrected_time"],
                "hour_window": hour,
                "window_start_ts": win_start,
                "window_end_ts": win_end,
                "band": None,
                "from_band": current_band,
                "to_band": None,
                "ambiguous_bands": group["bands"],
                "after_ambiguous": pending_ambiguity,
                "refs": refs,
                "violations": [],
                "first_qso": False,
            }
            chain.append(node)
            violations.append({
                "node_seq": node["seq"],
                "clause": "ambiguous_same_minute",
                "band": None,
                "penalty_code": policy.get("penalty_ambiguous"),
                "detail": ("同一分钟内出现跨频段多条记录，先后顺序无法判定，"
                           "保留歧义待裁决，不自行排序")})
            node["violations"].append(violations[-1])
            pending_ambiguity = True
            continue

        band = group["band"]
        refs = _refs(group["members"])
        if current_band is None:
            chain.append({
                "seq": len(chain) + 1,
                "type": "initial",
                "ts": ts,
                "date": refs[0]["corrected_date"],
                "time": refs[0]["corrected_time"],
                "hour_window": hour,
                "window_start_ts": win_start,
                "window_end_ts": win_end,
                "band": band,
                "from_band": None,
                "to_band": None,
                "ambiguous_bands": [],
                "after_ambiguous": pending_ambiguity,
                "refs": refs,
                "violations": [],
                "first_qso": not pending_ambiguity,
            })
            current_band = band
            pending_ambiguity = False
            last_switch = None
            continue

        if band == current_band and not pending_ambiguity:
            # 同频段连续记录：不是切换（其原行仍随相关节点/证据可追溯）。
            continue

        if pending_ambiguity:
            # 歧义段之后的首个确定频段：至少发生过一次切换但时刻与次数
            # 不可判定——不补造确定切换节点；该确定记录作为新锚点。
            chain.append({
                "seq": len(chain) + 1,
                "type": "anchor",
                "ts": ts,
                "date": refs[0]["corrected_date"],
                "time": refs[0]["corrected_time"],
                "hour_window": hour,
                "window_start_ts": win_start,
                "window_end_ts": win_end,
                "band": band,
                "from_band": current_band if band != current_band else None,
                "to_band": None,
                "ambiguous_bands": [],
                "after_ambiguous": True,
                "refs": refs,
                "violations": [],
                "first_qso": False,
            })
            current_band = band
            pending_ambiguity = False
            last_switch = None
            continue

        # 确定切换：时刻、前后频段与间隔都可判定。
        interval = ts - last_switch["ts"] if last_switch else None
        count = hour_counts.get(hour, 0) + 1
        hour_counts[hour] = count
        meta = hour_meta.setdefault(hour, {
            "hour_window": hour, "window_start_ts": win_start,
            "window_end_ts": win_end, "switch_count": 0,
            "excess_count": 0})
        meta["switch_count"] = count
        node_violations: list[dict[str, Any]] = []
        limit = policy.get("max_switches_per_clock_hour")
        if limit is not None and count > int(limit):
            meta["excess_count"] += 1
            node_violations.append({
                "node_seq": len(chain) + 1,
                "clause": CLAUSE_MAX_SWITCHES,
                "band": band,
                "penalty_code": policy.get("penalty_excess"),
                "detail": (f"{hour} 时钟小时内第 {count} 次切换，"
                           f"超过每小时最多 {limit} 次的限制"),
                "hourly_count": count,
                "limit": int(limit)})
        # 离开上一频段时，驻留时长 = 本次与上次切换的间隔（不足即触发）。
        dwell_limit = policy.get("min_dwell_seconds")
        if dwell_limit is not None and interval is not None \
                and interval < int(dwell_limit):
            node_violations.append({
                "node_seq": len(chain) + 1,
                "clause": CLAUSE_MIN_DWELL,
                "band": last_switch["to_band"] if last_switch else None,
                "penalty_code": policy.get("penalty_dwell"),
                "detail": (f"换入 {last_switch['to_band']} 后仅驻留 "
                           f"{interval} 秒即离开，短于最短驻留 "
                           f"{int(dwell_limit)} 秒"),
                "dwell_seconds": interval,
                "interval_from_seq": last_switch["seq"],
                "limit": int(dwell_limit)})
        node = {
            "seq": len(chain) + 1,
            "type": "switch",
            "ts": ts,
            "date": refs[0]["corrected_date"],
            "time": refs[0]["corrected_time"],
            "hour_window": hour,
            "window_start_ts": win_start,
            "window_end_ts": win_end,
            "band": band,
            "from_band": current_band,
            "to_band": band,
            "ambiguous_bands": [],
            "after_ambiguous": False,
            "refs": refs,
            "interval_seconds": interval,
            "interval_from_seq": last_switch["seq"] if last_switch else None,
            "hourly_count": count,
            "violations": node_violations,
            "first_qso": False,
        }
        chain.append(node)
        violations.extend(node_violations)
        current_band = band
        last_switch = node

    hour_windows = [
        {"hour_window": h, "window_start_ts": m["window_start_ts"],
         "window_end_ts": m["window_end_ts"],
         "switch_count": m["switch_count"],
         "limit": policy.get("max_switches_per_clock_hour"),
         "excess_count": m["excess_count"]}
        for h, m in sorted(hour_meta.items())]

    n_switches = sum(1 for n in chain if n["type"] == "switch")
    n_ambiguous = sum(1 for n in chain if n["type"] == "ambiguous")
    return {
        "station": station,
        "policy": policy,
        "chain": chain,
        "hour_windows": hour_windows,
        "summary": {
            "valid_qso": len(events),
            "switch_count": n_switches,
            "ambiguous_count": n_ambiguous,
            "excess_count": sum(1 for v in violations
                                if v["clause"] == CLAUSE_MAX_SWITCHES),
            "dwell_short_count": sum(1 for v in violations
                                     if v["clause"] == CLAUSE_MIN_DWELL),
            "violation_count": len(violations),
        },
        "violations": violations,
    }


def analyze_band_switching(rules: dict[str, Any],
                           submissions: list[dict[str, Any]],
                           time_offsets: dict[str, int] | None = None
                           ) -> dict[str, Any]:
    """全批次频段切换合规分析；返回每站策略快照、切换链、计数窗口与违规。"""
    offsets = {str(k): int(v) for k, v in (time_offsets or {}).items()}
    subs = [s for s in submissions if s.get("station_call")]
    station_logs: dict[str, list[dict[str, Any]]] = {}
    for sub in subs:
        station_logs.setdefault(sub["station_call"], []).append(sub)
    policies = resolve_policy(rules, submissions)
    stations = []
    for station in sorted(station_logs):
        stations.append(analyze_station(
            station, station_logs[station], offsets,
            policies.get(station) or {"enabled": False}))
    enabled = bool((rules.get("band_compliance") or {}).get("enabled", True))
    return {
        "enabled": enabled,
        "time_basis": "corrected_utc",
        "semantics": dict(SEMANTICS),
        "stations": stations,
    }
