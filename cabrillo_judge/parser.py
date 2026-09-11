"""Cabrillo 3.0 log parser and field validator.

The parser keeps every raw line (with its 1-based line number) so findings can
always be traced back to ``文件名:行号``.  Validation is driven by a rule-set
dict from :mod:`cabrillo_judge.rules`; no external databases are consulted.
"""

from __future__ import annotations

import re
import datetime as _dt
from typing import Any

from .rules import (
    DEFAULT_CATEGORIES,
    DEFAULT_REQUIRED_HEADERS,
    KNOWN_TAGS,
)

# ---------------------------------------------------------------------------
# Callsign helpers
# ---------------------------------------------------------------------------

# Loose ITU-style base call: optional digit prefix, 1-3 letters, separating
# digit(s), 1-4 trailing letters.  Covers e.g. W1AW, BG7AAF, 4X1ABC, 3DA0A.
_BASE_CALL_RE = re.compile(r"^[0-9]?[A-Z]{1,3}[0-9]+[A-Z]{1,4}$")
# A slash segment that is *not* a base call (P, QRP, M, AM ...).  Base calls
# themselves may be up to 9 characters (e.g. 3DA0ABC); ordinary suffixes are
# shorter.  Length 15 leaves room for "F/EA8/MM"-style composites.
_SEGMENT_RE = re.compile(r"^[A-Z0-9]{1,9}$")


def normalize_callsign(call: str | None) -> str | None:
    """Return the base call (uppercase) or ``None``.

    Handles both suffix form (``EA4E/P``, ``W1AW/QRP``) and prefix form
    (``F/ON4XYZ``, ``EA8/DL1ABC/P``): every slash-separated segment is tested
    and the first one matching the base-call grammar wins.
    """
    if not call:
        return None
    token = call.strip().upper()
    segments = token.split("/")
    # Prefer the first segment if it itself is a base call (the common case),
    # otherwise scan the remaining segments for a prefix-style base call.
    for seg in segments:
        if _BASE_CALL_RE.match(seg) and 3 <= len(seg) <= 9 \
                and any(ch.isalpha() for ch in seg):
            return seg
    return None


def is_valid_callsign(call: str | None) -> bool:
    """Validate a full Cabrillo call field, allowing portable/mobile suffixes.

    Accepts ``EA4E``, ``EA4E/P``, ``F/ON4XYZ``, ``ON4XYZ/QRP`` etc.  At least
    one slash-separated segment must be a valid base call.
    """
    if not call:
        return False
    token = call.strip().upper()
    if not token or len(token) > 15:
        return False
    segments = token.split("/")
    has_base = False
    for seg in segments:
        if not seg or not _SEGMENT_RE.match(seg):
            return False
        if _BASE_CALL_RE.match(seg):
            has_base = True
    return has_base


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------

_RST_RE = re.compile(r"^[1-9][0-9]{1,2}$")
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3])[0-5][0-9]$")
_DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")


def _parse_utc_timestamp(date_s: str, time_s: str) -> int | None:
    if not _DATE_RE.match(date_s) or not _TIME_RE.match(time_s):
        return None
    try:
        dt = _dt.datetime.strptime(date_s + " " + time_s, "%Y-%m-%d %H%M")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())


def band_for_freq(freq_khz: int, bands: list[dict[str, Any]]) -> str | None:
    for band in bands:
        if band["low_khz"] <= freq_khz <= band["high_khz"]:
            return band["name"]
    return None


def _validate_exchange_value(spec: dict[str, Any], value: str) -> bool:
    etype = spec["type"]
    if etype == "rst":
        return bool(_RST_RE.match(value))
    if etype == "integer":
        return bool(re.match(r"^[0-9]+$", value))
    if etype == "string":
        return bool(value.strip())
    if etype == "grid":
        return bool(_GRID_RE.match(value))
    if etype == "callsign":
        return is_valid_callsign(value)
    if etype == "enum":
        return value in spec.get("values", [])
    return False


def _exchange_equal(spec: dict[str, Any], a: str, b: str) -> bool:
    if spec["type"] == "integer":
        try:
            return int(a) == int(b)
        except ValueError:
            return a == b
    return a == b


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _issue(severity: str, code: str, message: str, line: int | None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "line": line}


def parse_cabrillo(text: str, rules: dict[str, Any]) -> dict[str, Any]:
    """Parse one Cabrillo 3.0 submission.

    Returns a dict with keys:
        headers       -- first-seen value per header tag (uppercase tag)
        raw_tags      -- [(line_no, tag, value)] every header occurrence
        qsos          -- valid, pairable QSO records
        xqsos         -- X-QSO records (never paired/scored)
        invalid_qsos  -- malformed QSO lines (kept with raw text + error)
        raw_lines     -- [{line, text, kind}] every physical line
        issues        -- error/warning list located at file:line
        station_call  -- normalized own callsign or None
        contest       -- declared contest tag
    """
    bands = rules.get("bands", [])
    allowed_modes = rules.get("allowed_modes", [])
    mode_aliases = rules.get("mode_aliases", {})
    exchange_specs = rules.get("exchange_fields", [])
    n_exch = len(exchange_specs)
    required_headers = [h.upper() for h in
                        rules.get("required_headers") or DEFAULT_REQUIRED_HEADERS]
    allowed_categories = rules.get("allowed_categories") or DEFAULT_CATEGORIES

    # Normalise newlines; keep line numbers faithful to the submitted text.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    headers: dict[str, str] = {}
    raw_tags: list[tuple[int, str, str]] = []
    qsos: list[dict[str, Any]] = []
    xqsos: list[dict[str, Any]] = []
    invalid_qsos: list[dict[str, Any]] = []
    raw_lines: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    seen_tags: set[str] = set()
    qso_started = False
    end_seen = False
    first_line_ok = False

    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        kind = "blank" if stripped == "" else "data"
        raw_lines.append({"line": idx, "text": raw, "kind": kind})
        if stripped == "":
            continue

        if end_seen:
            issues.append(_issue("warning", "TEXT_AFTER_END",
                                 "END-OF-LOG 之后仍有内容", idx))

        if stripped.startswith("QSO:") or stripped.startswith("X-QSO:"):
            kind = "qso" if stripped.startswith("QSO:") else "xqso"
            raw_lines[-1]["kind"] = kind
            qso_started = True
            is_x = stripped.startswith("X-QSO:")
            tokens = stripped.split()
            # tag, freq, mode, date, time, call, exch..., call, exch...
            expected_tokens = 7 + 2 * n_exch
            if len(tokens) != expected_tokens:
                issues.append(_issue("error", "QSO_FORMAT",
                                     f"QSO 行字段数 {len(tokens) - 1} 与规则要求 "
                                     f"{expected_tokens - 1} 不符", idx))
                invalid_qsos.append({"line": idx, "raw": raw,
                                     "error": "QSO_FORMAT"})
                continue

            _, freq_s, mode_s, date_s, time_s = tokens[:5]
            critical = False

            if not re.match(r"^[0-9]+$", freq_s):
                issues.append(_issue("error", "BAD_FREQUENCY",
                                     f"频率字段 {freq_s!r} 不是整数 kHz", idx))
                critical = True
            ts = _parse_utc_timestamp(date_s, time_s)
            if ts is None:
                issues.append(_issue("error", "BAD_TIME",
                                     f"UTC 日期/时间 {date_s} {time_s} 无法解析",
                                     idx))
                critical = True

            mode_norm = mode_aliases.get(mode_s.upper(), mode_s.upper())

            if critical:
                invalid_qsos.append({"line": idx, "raw": raw,
                                     "error": "QSO_CRITICAL"})
                continue

            freq = int(freq_s)
            band = band_for_freq(freq, bands)
            if band is None:
                issues.append(_issue("error", "BAD_BAND",
                                     f"频率 {freq} kHz 不在规则定义的任何频段内",
                                     idx))
                invalid_qsos.append({"line": idx, "raw": raw,
                                     "error": "BAD_BAND"})
                continue
            if allowed_modes and mode_norm not in allowed_modes:
                issues.append(_issue("error", "BAD_MODE",
                                     f"模式 {mode_s!r}（归一为 {mode_norm}）"
                                     f"不在允许模式 {allowed_modes} 内", idx))
                # 记录保留但不参与配对
                invalid_qsos.append({"line": idx, "raw": raw,
                                     "error": "BAD_MODE"})
                continue

            # Contest window (optional).
            window_ok = True
            if rules.get("contest_start") and rules.get("contest_end"):
                start_ts = _parse_utc_timestamp(rules["contest_start"][:10],
                                                rules["contest_start"][11:15]
                                                if len(rules["contest_start"]) > 10
                                                else "0000")
                end_ts = _parse_utc_timestamp(rules["contest_end"][:10],
                                              rules["contest_end"][11:15]
                                              if len(rules["contest_end"]) > 10
                                              else "2359")
                if start_ts is not None and end_ts is not None and not (start_ts <= ts <= end_ts):
                    issues.append(_issue("error", "OUTSIDE_WINDOW",
                                         f"时间 {date_s} {time_s} 超出竞赛窗口",
                                         idx))
                    window_ok = False

            call1, call2 = tokens[5], tokens[5 + n_exch + 1]
            sent = tokens[6:6 + n_exch]
            recv = tokens[6 + n_exch + 1:6 + n_exch + 1 + n_exch]

            if not is_valid_callsign(call1):
                issues.append(_issue("warning", "OWN_CALL_IN_QSO",
                                     f"QSO 中本方呼号 {call1!r} 格式可疑", idx))
            if not is_valid_callsign(call2):
                issues.append(_issue("error", "BAD_WORKED_CALL",
                                     f"对方呼号 {call2!r} 格式不合法", idx))

            exch_problems = []
            for spec, val in zip(exchange_specs, sent):
                if not _validate_exchange_value(spec, val):
                    exch_problems.append(f"发出 {spec['name']}={val!r}")
            for spec, val in zip(exchange_specs, recv):
                if not _validate_exchange_value(spec, val):
                    exch_problems.append(f"收到 {spec['name']}={val!r}")
            if exch_problems:
                issues.append(_issue("error", "BAD_EXCHANGE",
                                     "交换字段不合法：" + "，".join(exch_problems),
                                     idx))

            qso = {
                "seq": (len(qsos) + len(xqsos) + 1),
                "line": idx,
                "freq_khz": freq,
                "band": band,
                "mode": mode_norm,
                "raw_mode": mode_s.upper(),
                "ts": ts,
                "date": date_s,
                "time": time_s,
                "call1": call1.upper(),
                "call2": call2.upper(),
                "worked_call_raw": call2.upper(),
                "worked_call_norm": normalize_callsign(call2),
                "sent": {spec["name"]: v for spec, v in zip(exchange_specs, sent)},
                "recv": {spec["name"]: v for spec, v in zip(exchange_specs, recv)},
                "exchange_valid": not exch_problems,
                "in_window": window_ok,
                "is_x": is_x,
            }
            (xqsos if is_x else qsos).append(qso)
            continue

        if ":" not in stripped:
            issues.append(_issue("error", "UNPARSEABLE_LINE",
                                 "无法识别的行（既非头标签也非 QSO）", idx))
            continue

        tag, _, value = stripped.partition(":")
        tag = tag.strip().upper()
        value = value.strip()
        kind = "tag"
        raw_lines[-1]["kind"] = kind

        if tag == "START-OF-LOG":
            first_line_ok = (idx == 1 and value == "3.0")
            if idx != 1:
                issues.append(_issue("error", "BAD_START_POSITION",
                                     "START-OF-LOG 必须位于第一行", idx))
            if value != "3.0":
                issues.append(_issue("error", "BAD_LOG_VERSION",
                                     f"仅支持 Cabrillo 3.0，收到 {value!r}",
                                     idx))
        elif tag == "END-OF-LOG":
            if value:
                issues.append(_issue("warning", "END_HAS_VALUE",
                                     "END-OF-LOG 不应带值", idx))
            end_seen = True
        else:
            if qso_started:
                issues.append(_issue("warning", "HEADER_AFTER_QSO",
                                     f"头标签 {tag} 出现在 QSO 行之后", idx))
            if tag not in KNOWN_TAGS:
                issues.append(_issue("warning", "UNKNOWN_TAG",
                                     f"未知头标签 {tag}", idx))
            if tag == "CONTEST" and value.upper() != str(rules.get("contest", "")).upper():
                issues.append(_issue("error", "CONTEST_MISMATCH",
                                     f"CONTEST={value!r} 与规则 "
                                     f"{rules.get('contest')!r} 不符", idx))
            if tag.startswith("CATEGORY-") and tag[9:] in allowed_categories:
                valid_values = allowed_categories[tag[9:]]
                if value not in valid_values:
                    issues.append(_issue("error", "BAD_CATEGORY",
                                         f"{tag}={value!r}，允许值："
                                         f"{valid_values}", idx))
            if tag == "CLAIMED-SCORE" and not re.match(r"^-?[0-9]+$", value):
                issues.append(_issue("error", "BAD_CLAIMED_SCORE",
                                     f"CLAIMED-SCORE={value!r} 不是整数", idx))

        if tag in seen_tags and tag not in ("SOAPBOX", "ADDRESS", "OPERATORS",
                                            "OFFTIME"):
            issues.append(_issue("warning", "DUPLICATE_HEADER",
                                 f"头标签 {tag} 重复出现", idx))
        seen_tags.add(tag)
        raw_tags.append((idx, tag, value))
        headers.setdefault(tag, value)

    # Required-header presence checks (file level, line = None).
    for req in required_headers:
        if req not in seen_tags:
            issues.append(_issue("error", "MISSING_HEADER",
                                 f"缺少必填头 {req}", None))
    if not first_line_ok:
        if not any(i["code"] in ("BAD_START_POSITION", "BAD_LOG_VERSION")
                   for i in issues):
            issues.append(_issue("error", "MISSING_HEADER",
                                 "缺少合法的 START-OF-LOG: 3.0 首行", None))
    if not end_seen:
        issues.append(_issue("warning", "MISSING_END",
                             "缺少 END-OF-LOG 结束行（建议补全）", None))

    contest = headers.get("CONTEST")
    station_call = normalize_callsign(headers.get("CALLSIGN", ""))
    if "CALLSIGN" in headers and station_call is None:
        issues.append(_issue("error", "BAD_STATION_CALL",
                             f"提交台呼号 {headers.get('CALLSIGN')!r} 无法归一化",
                             raw_tags_line(raw_tags, "CALLSIGN")))

    return {
        "headers": headers,
        "raw_tags": [{"line": ln, "tag": t, "value": v}
                     for ln, t, v in raw_tags],
        "qsos": qsos,
        "xqsos": xqsos,
        "invalid_qsos": invalid_qsos,
        "raw_lines": raw_lines,
        "issues": issues,
        "station_call": station_call,
        "contest": contest,
    }


def raw_tags_line(raw_tags: list[tuple[int, str, str]], tag: str) -> int | None:
    for ln, t, _v in raw_tags:
        if t == tag:
            return ln
    return None
