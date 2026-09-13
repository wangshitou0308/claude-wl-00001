"""Contest rule definitions.

A *rule set* is a plain JSON-serialisable dict (so it can be stored in SQLite
and versioned).  It controls:

* which Cabrillo headers / categories are required or permitted,
* which bands and modes count,
* what the exchange looks like (ordered list of fields),
* the time tolerance used when pairing logs,
* QSO points, multipliers and the penalty catalogue used in scoring.

Nothing here performs any network lookups.
"""

from __future__ import annotations

from typing import Any


# Bands expressed as inclusive kHz ranges.  First match wins.
DEFAULT_BANDS: list[dict[str, Any]] = [
    {"name": "160m", "low_khz": 1800, "high_khz": 2000},
    {"name": "80m", "low_khz": 3500, "high_khz": 4000},
    {"name": "40m", "low_khz": 7000, "high_khz": 7300},
    {"name": "30m", "low_khz": 10100, "high_khz": 10150},
    {"name": "20m", "low_khz": 14000, "high_khz": 14350},
    {"name": "17m", "low_khz": 18068, "high_khz": 18168},
    {"name": "15m", "low_khz": 21000, "high_khz": 21450},
    {"name": "12m", "low_khz": 24890, "high_khz": 24990},
    {"name": "10m", "low_khz": 28000, "high_khz": 29700},
]

# Allowed values for CATEGORY-* headers (Cabrillo 3.0 vocabulary).
DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "ASSISTED": ["ASSISTED", "NON-ASSISTED"],
    "POWER": ["HIGH", "LOW", "QRP"],
    "MODE": ["CW", "SSB", "DATA", "FM", "RTTY", "MIXED"],
    "BANDS": ["ALL", "HIGH", "LOW", "SINGLE"],
    "OPERATOR": ["SINGLE-OP", "MULTI-OP", "CHECKLOG"],
    "STATION": ["FIXED", "MOBILE", "PORTABLE", "EXPEDITION"],
    "TIME": ["6-HOURS", "12-HOURS", "24-HOURS"],
    "TRANSMITTER": ["ONE", "TWO", "LIMITED", "UNLIMITED"],
}

DEFAULT_REQUIRED_HEADERS = [
    "START-OF-LOG",
    "CALLSIGN",
    "CONTEST",
    "CATEGORY-MODE",
    "CATEGORY-BANDS",
    "CATEGORY-OPERATOR",
    "CATEGORY-POWER",
]

# Standard Cabrillo 3.0 header tags (everything else gets a warning).
KNOWN_TAGS = {
    "START-OF-LOG", "END-OF-LOG", "CALLSIGN", "CONTEST",
    "CATEGORY-ASSISTED", "CATEGORY-POWER", "CATEGORY-MODE",
    "CATEGORY-BANDS", "CATEGORY-OPERATOR", "CATEGORY-STATION",
    "CATEGORY-TIME", "CATEGORY-TRANSMITTER", "CATEGORY-OVERLAY",
    "CERTIFICATE", "CLAIMED-SCORE", "CLUB", "CREATED-BY", "EMAIL",
    "GRID-LOCATOR", "LOCATION", "NAME", "ADDRESS", "ADDRESS-CITY",
    "ADDRESS-STATE-PROVINCE", "ADDRESS-POSTALCODE", "ADDRESS-COUNTRY",
    "OFFTIME", "SOAPBOX", "OPERATORS",
}

MODE_ALIASES = {"RY": "CW", "RTTY": "DATA"}

EXCHANGE_TYPES = {"rst", "integer", "string", "grid", "callsign", "enum"}

# 频段切换合规：三类结论对应的默认罚目（须在 penalties 目录中）
BAND_PENALTY_EXCESS = "BAND_SWITCH_EXCESS"
BAND_PENALTY_DWELL = "BAND_DWELL_SHORT"
BAND_PENALTY_AMBIGUOUS = "BAND_SWITCH_AMBIGUOUS"

# 固定语义：首个 QSO 不计数；计数窗口按 UTC 时钟整点；
# 同一分钟（Cabrillo 时间分辨率为分钟）跨频段的多条记录顺序不明，保留歧义。
BAND_FIRST_QSO_FREE = "free"
BAND_HOUR_BOUNDARY_CLOCK_UTC = "clock_utc"
BAND_SAME_MINUTE_AMBIGUOUS = "ambiguous"


def default_rules(contest: str = "DEMO-CW") -> dict[str, Any]:
    """Return a ready-to-use example rule set."""
    return {
        "contest": contest,
        "name": "示例 CW 通联竞赛规则",
        "required_headers": list(DEFAULT_REQUIRED_HEADERS),
        "allowed_categories": {k: list(v) for k, v in DEFAULT_CATEGORIES.items()},
        "bands": [dict(b) for b in DEFAULT_BANDS if b["name"] in
                  ("80m", "40m", "20m", "15m", "10m")],
        "allowed_modes": ["CW"],
        "mode_aliases": dict(MODE_ALIASES),
        "exchange_fields": [
            {"name": "rst", "type": "rst", "label": "RST"},
            {"name": "serial", "type": "integer", "label": "序号",
             "multiplier": False},
        ],
        "pairing": {
            # 同一频段/模式下，双方时间差不超过该秒数才视为候选配对
            "time_tolerance_seconds": 300,
            # 超过容差但在该秒数内 -> 待裁决的时间漂移
            "near_window_seconds": 1800,
            # 呼号模糊匹配（编辑距离）阈值
            "call_fuzzy_distance": 2,
        },
        "qso_points": {
            # "default" 或按 模式/频段 给出分值
            "default": 1,
            "by_band": {},
            "by_mode": {},
        },
        # UNIQUE（对方交了日志但无此记录）默认仅 0 分、不自动罚分；
        # 裁判可在裁决时施加 NOT_IN_LOG 罚目。如赛事规则要求自动罚分，
        # 将其设为 true。
        "auto_penalty_unique": False,
        "multipliers": [
            # 示例：按通联到的不同呼号计乘数
            {"type": "worked_call", "name": "不同对方呼号"},
        ],
        "penalties": {
            # 裁判可施加的命名罚分目录（分值为从总分扣减的正数）
            "BAD_EXCHANGE": {"points": 2, "label": "抄收交换错误"},
            "NOT_IN_LOG": {"points": 5, "label": "对方日志无此记录"},
            "DUP": {"points": 0, "label": "重复通联（默认不扣分，可配置）"},
            "UNIQUE_PENALTY": {"points": 10, "label": "裁判手动罚分"},
            BAND_PENALTY_EXCESS: {"points": 3, "label": "单时钟小时切换频段超限"},
            BAND_PENALTY_DWELL: {"points": 2, "label": "换频后驻留时长不足"},
            BAND_PENALTY_AMBIGUOUS: {"points": 3,
                                     "label": "同分钟跨频段（顺序不明，待裁决）"},
        },
        # 频段切换合规分析（每个时钟小时最多切换次数 / 换频后最短驻留时长）。
        # 策略按日志的 CATEGORY-* 参赛类别匹配（如 CATEGORY-OPERATOR）。
        # 固定语义：每个台站的第一个 QSO 不视为切换；计数窗口按 UTC 时钟
        # 整点（[H:00, H+1:00)）；同一分钟内跨频段的多条记录顺序不明，
        # 只列歧义待裁决，绝不自行排序。
        "band_compliance": {
            "enabled": True,
            # null 表示该限制不检查；启用分析但不设限时只产出歧义待裁决
            "max_switches_per_clock_hour": None,
            "min_dwell_seconds": None,
            "penalty_excess": BAND_PENALTY_EXCESS,
            "penalty_dwell": BAND_PENALTY_DWELL,
            "penalty_ambiguous": BAND_PENALTY_AMBIGUOUS,
            # 按参赛类别覆盖：{"category": "OPERATOR", "match": {"SINGLE-OP": {...}}}
            # 命中第一个匹配值（合并默认限制）；也可写
            # {"category": "OPERATOR", "value": "SINGLE-OP", ...} 的单值写法
            "by_category": [],
        },
        # 可选竞赛窗口，UTC，ISO-8601；为 None 时不检查
        "contest_start": None,
        "contest_end": None,
    }


def validate_rules(rules: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with a rule set."""
    problems: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    need(isinstance(rules.get("contest"), str) and rules["contest"].strip(),
          "规则必须包含非空 contest 字段")
    bands = rules.get("bands")
    need(isinstance(bands, list) and bands, "bands 必须是非空列表")
    if isinstance(bands, list):
        for i, b in enumerate(bands):
            need(isinstance(b, dict)
                 and isinstance(b.get("name"), str)
                 and isinstance(b.get("low_khz"), int)
                 and isinstance(b.get("high_khz"), int)
                 and b["low_khz"] <= b["high_khz"],
                 f"bands[{i}] 需要 name/low_khz/high_khz（整数 kHz）")
    modes = rules.get("allowed_modes")
    need(isinstance(modes, list) and modes and all(isinstance(m, str) for m in modes),
          "allowed_modes 必须是非空字符串列表")

    fields = rules.get("exchange_fields", [])
    need(isinstance(fields, list), "exchange_fields 必须是列表")
    if isinstance(fields, list):
        for i, f in enumerate(fields):
            need(isinstance(f, dict), f"exchange_fields[{i}] 必须是对象")
            if not isinstance(f, dict):
                continue
            need(isinstance(f.get("name"), str) and f["name"],
                 f"exchange_fields[{i}] 需要 name")
            need(f.get("type") in EXCHANGE_TYPES,
                 f"exchange_fields[{i}].type 必须是 {sorted(EXCHANGE_TYPES)} 之一")
            if f.get("type") == "enum":
                need(isinstance(f.get("values"), list) and f["values"],
                     f"exchange_fields[{i}] enum 类型需要 values 列表")

    pairing = rules.get("pairing", {})
    need(isinstance(pairing, dict), "pairing 必须是对象")
    if isinstance(pairing, dict):
        tol = pairing.get("time_tolerance_seconds", 0)
        need(isinstance(tol, int) and tol >= 0,
              "pairing.time_tolerance_seconds 必须是非负整数")

    qp = rules.get("qso_points", {})
    need(isinstance(qp, dict) and isinstance(qp.get("default"), int),
          "qso_points.default 必须是整数")

    for i, mult in enumerate(rules.get("multipliers", [])):
        need(isinstance(mult, dict) and mult.get("type") in
             ("worked_call", "exchange_field", "band", "band_mode"),
             f"multipliers[{i}].type 非法")
        if isinstance(mult, dict) and mult.get("type") == "exchange_field":
            names = [f.get("name") for f in fields]
            need(mult.get("field") in names,
                 f"multipliers[{i}].field 必须是 exchange 字段名之一")

    penalties = rules.get("penalties", {})
    need(isinstance(penalties, dict), "penalties 必须是对象")
    if isinstance(penalties, dict):
        for code, p in penalties.items():
            need(isinstance(p, dict) and isinstance(p.get("points"), int)
                 and p["points"] >= 0,
                 f"penalties.{code} 需要非负整数 points")

    bc = rules.get("band_compliance", {})
    if bc is not None:
        need(isinstance(bc, dict), "band_compliance 必须是对象")
        if isinstance(bc, dict):
            msp = bc.get("max_switches_per_clock_hour")
            need(msp is None or (isinstance(msp, int) and not isinstance(msp, bool)
                                 and msp >= 0),
                 "band_compliance.max_switches_per_clock_hour "
                 "必须是非负整数或 null")
            dwell = bc.get("min_dwell_seconds")
            need(dwell is None or (isinstance(dwell, int)
                                   and not isinstance(dwell, bool) and dwell >= 0),
                 "band_compliance.min_dwell_seconds 必须是非负整数或 null")
            for key in ("penalty_excess", "penalty_dwell",
                        "penalty_ambiguous"):
                code = bc.get(key)
                need(code is None or code in penalties,
                     f"band_compliance.{key} 必须在 penalties 目录中或为 null")
            by_cat = bc.get("by_category", [])
            need(isinstance(by_cat, list),
                 "band_compliance.by_category 必须是列表")
            if isinstance(by_cat, list):
                for i, entry in enumerate(by_cat):
                    need(isinstance(entry, dict)
                         and isinstance(entry.get("category"), str),
                         f"band_compliance.by_category[{i}] "
                         f"需要字符串 category（如 OPERATOR）")
                    if not isinstance(entry, dict):
                        continue
                    has_value = "value" in entry
                    has_match = isinstance(entry.get("match"), dict)
                    need(has_value or has_match,
                         f"band_compliance.by_category[{i}] 需要 value 单值"
                         f"或 match 映射（类别取值 -> 限制对象）")
                    overrides = ([entry] if has_value else [])
                    if has_match:
                        overrides = [
                            {"max_switches_per_clock_hour":
                             ov.get("max_switches_per_clock_hour"),
                             "min_dwell_seconds": ov.get("min_dwell_seconds")}
                            for ov in entry["match"].values()
                            if isinstance(ov, dict)]
                    for ov in overrides:
                        m = ov.get("max_switches_per_clock_hour")
                        need(m is None or (isinstance(m, int)
                                           and not isinstance(m, bool) and m >= 0),
                             f"band_compliance.by_category[{i}]"
                             f".max_switches_per_clock_hour 非法")
                        d = ov.get("min_dwell_seconds")
                        need(d is None or (isinstance(d, int)
                                           and not isinstance(d, bool) and d >= 0),
                             f"band_compliance.by_category[{i}].min_dwell_seconds "
                             f"非法")
    return problems
