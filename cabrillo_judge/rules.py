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
        "multipliers": [
            # 示例：按通联到的不同呼号计乘数
            {"type": "worked_call", "name": "不同对方呼号"},
        ],
        "penalties": {
            # 裁判可施加的命名罚分目录（分值为从总分扣减的正数）
            "BAD_EXCHANGE": {"points": 2, "label": "抄收交换错误"},
            "NOT_IN_LOG": {"points": 5, "label": "对方日志无此记录"},
            "UNIQUE_PENALTY": {"points": 10, "label": "裁判手动罚分"},
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
    return problems
