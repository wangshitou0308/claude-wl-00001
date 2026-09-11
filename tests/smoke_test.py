"""Smoke test for the reported parser/entry regressions and full flow."""
import json
import sys
import tempfile
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cabrillo_judge.parser import (
    parse_cabrillo, is_valid_callsign, normalize_callsign)
from cabrillo_judge.rules import default_rules
from cabrillo_judge.engine import adjudicate, apply_decisions, score
from cabrillo_judge.storage import Storage, new_id

GOOD_LOG = """START-OF-LOG: 3.0
CALLSIGN: BG1AAA
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0111 BG1AAA 599 001 BG2BBB 599 001
END-OF-LOG:
"""

failures = []

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)

# --- Bug 2: TypeError on a normal CALLSIGN header line ----------------------
try:
    rules = default_rules()
    parsed = parse_cabrillo(GOOD_LOG, rules)
    check("普通 CALLSIGN 行不再抛 TypeError", True)
except TypeError as exc:
    check("普通 CALLSIGN 行不再抛 TypeError", False, repr(exc))
    parsed = None

if parsed is not None:
    check("必填头齐全时无 MISSING_HEADER",
          not [i for i in parsed["issues"] if i["code"] == "MISSING_HEADER"],
          str([i["message"] for i in parsed["issues"] if i["code"] == "MISSING_HEADER"]))
    check("普通 QSO 被解析（1 条）", len(parsed["qsos"]) == 1,
          f"qsos={len(parsed['qsos'])} issues={parsed['issues']}")
    check("station_call 归一化为 BG1AAA",
          parsed["station_call"] == "BG1AAA", parsed["station_call"])

# --- Bug 3: default POWER category config must apply -------------------------
POWER_BAD = GOOD_LOG.replace("CATEGORY-POWER: LOW", "CATEGORY-POWER: QRO")
parsed_bad = parse_cabrillo(POWER_BAD, default_rules())
check("默认 POWER 配置生效：QRO 被判 BAD_CATEGORY",
      any(i["code"] == "BAD_CATEGORY" and "POWER" in i["message"]
          for i in parsed_bad["issues"]),
      str(parsed_bad["issues"]))
parsed_ok = parse_cabrillo(GOOD_LOG, default_rules())
check("默认 POWER 配置：LOW 合法",
      not any(i["code"] == "BAD_CATEGORY" for i in parsed_ok["issues"]))

# --- Bug 4: F/ON4XYZ prefix-style portable callsign --------------------------
check("F/ON4XYZ 合法", is_valid_callsign("F/ON4XYZ"))
check("F/ON4XYZ 归一化为 ON4XYZ",
      normalize_callsign("F/ON4XYZ") == "ON4XYZ",
      normalize_callsign("F/ON4XYZ"))
check("EA4E/P 仍合法", is_valid_callsign("EA4E/P"))
check("EA4E/P 归一化为 EA4E", normalize_callsign("EA4E/P") == "EA4E")
check("ON4XYZ/QRP 合法", is_valid_callsign("ON4XYZ/QRP"))
check("乱码呼号仍被拒", not is_valid_callsign("@@@"))

# Header CALLSIGN with prefix form must normalize too.
PREFIX_HEADER = GOOD_LOG.replace("CALLSIGN: BG1AAA", "CALLSIGN: F/ON4XYZ") \
                        .replace("BG1AAA 599 001 BG2BBB",
                                 "ON4XYZ 599 001 BG2BBB")
parsed_prefix = parse_cabrillo(PREFIX_HEADER, default_rules())
check("CALLSIGN: F/ON4XYZ 头归一化为 ON4XYZ",
      parsed_prefix["station_call"] == "ON4XYZ",
      str(parsed_prefix["station_call"]))
check("F/ON4XYZ 头不报 BAD_STATION_CALL",
      not any(i["code"] == "BAD_STATION_CALL" for i in parsed_prefix["issues"]),
      str(parsed_prefix["issues"]))

# --- Engine smoke test with demo-style two logs ------------------------------
from cabrillo_judge.__main__ import DEMO_LOG_A, DEMO_LOG_B, DEMO_LOG_C
pa = parse_cabrillo(DEMO_LOG_A, rules)
pb = parse_cabrillo(DEMO_LOG_B, rules)
pc = parse_cabrillo(DEMO_LOG_C, rules)
subs = [{"log_id": "L1", "filename": "BG1AAA.log",
         "station_call": pa["station_call"], "parsed": pa},
        {"log_id": "L2", "filename": "BG2BBB.log",
         "station_call": pb["station_call"], "parsed": pb},
        {"log_id": "L3", "filename": "BG3CCC.log",
         "station_call": pc["station_call"], "parsed": pc}]
res = adjudicate(rules, subs)
statuses = sorted(f["status"] for f in res["findings"])
print("引擎产出状态:", statuses)
check("演示数据出现 MATCH", "MATCH" in statuses)
check("演示数据出现 EXCHANGE_DIFF", "EXCHANGE_DIFF" in statuses)
check("演示数据出现 TIME_DRIFT", "TIME_DRIFT" in statuses)
check("演示数据出现 UNIQUE(BG3CCC)", "UNIQUE" in statuses)
check("演示数据出现 NO_PARTNER_LOG(BG4DDD)", "NO_PARTNER_LOG" in statuses)
check("演示数据出现 DUP", "DUP" in statuses)
check("演示数据出现 SUSPECT_CALL（BG1AAB 疑似抄错）",
      "SUSPECT_CALL" in statuses)
check("示例数据覆盖全部七种配对状态",
      statuses == sorted(["MATCH", "EXCHANGE_DIFF", "TIME_DRIFT",
                          "SUSPECT_CALL", "NO_PARTNER_LOG", "UNIQUE", "DUP"]),
      str(statuses))
suspect = next(f for f in res["findings"] if f["status"] == "SUSPECT_CALL")
check("SUSPECT_CALL 标为待裁决", suspect["pending"] is True)
check("SUSPECT_CALL 证据定位到双方原始行",
      [r["filename"] for r in suspect["refs"]] == ["BG1AAA.log", "BG3CCC.log"]
      and all(r["raw"].startswith("QSO:") for r in suspect["refs"]))
check("SUSPECT_CALL 抄错细节 BG1AAA->BG1AAB",
      suspect["call_detail"].get("b_logged") == "BG1AAB"
      and suspect["call_detail"].get("b_actual") == "BG1AAA",
      str(suspect["call_detail"]))

# --- Field-level invalid QSO rows must never participate in pairing ---------
BAD_LOG = """START-OF-LOG: 3.0
CALLSIGN: BG7BAD
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0500 BG7BAD 599 001 !!NOPE!! 599 001
QSO: 7023 CW 2026-09-10 0510 BG7BAD 59A 002 BG2BBB 599 abc
QSO: 7023 CW 2026-09-10 0520 BG7BAD 599 003 BG2BBB 599 030
END-OF-LOG:
"""
PARTNER_LOG = """START-OF-LOG: 3.0
CALLSIGN: BG2BBB
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: HIGH
QSO: 7023 CW 2026-09-10 0510 BG2BBB 599 002 BG7BAD 59A 002
QSO: 7023 CW 2026-09-10 0520 BG2BBB 599 030 BG7BAD 599 003
END-OF-LOG:
"""
pbad = parse_cabrillo(BAD_LOG, rules)
pptn = parse_cabrillo(PARTNER_LOG, rules)
check("坏对方呼号/坏交换行进入 invalid_qsos 而非 qsos",
      len(pbad["qsos"]) == 1
      and {iv["line"] for iv in pbad["invalid_qsos"]} == {8, 9}
      and {c for iv in pbad["invalid_qsos"] for c in iv["errors"]}
      == {"BAD_WORKED_CALL", "BAD_EXCHANGE"},
      f"qsos={len(pbad['qsos'])} invalid={pbad['invalid_qsos']}")
check("invalid_qsos 保留原始行",
      all(iv["raw"].startswith("QSO:") for iv in pbad["invalid_qsos"]))
check("对方日志收到坏交换也被判无效",
      len(pptn["qsos"]) == 1 and pptn["invalid_qsos"][0]["errors"]
      == ["BAD_EXCHANGE"])
bsubs = [{"log_id": "X1", "filename": "bad.log",
          "station_call": pbad["station_call"], "parsed": pbad},
         {"log_id": "X2", "filename": "partner.log",
          "station_call": pptn["station_call"], "parsed": pptn}]
bfindings = adjudicate(rules, bsubs)["findings"]
check("无效 QSO 行不参与配对（仅合法行 0520 成 MATCH）",
      len(bfindings) == 1 and bfindings[0]["status"] == "MATCH"
      and {r["line"] for r in bfindings[0]["refs"]} == {10, 9},
      str([(f["status"], [r["line"] for r in f["refs"]])
           for f in bfindings]))

annotated = apply_decisions(rules, res["findings"], {})
scored = score(rules, annotated)
print(json.dumps(scored["scorecards"], ensure_ascii=False, indent=1))
check("三家台站都有计分卡（含 X-QSO 不配对的 BG3CCC 在 SUSPECT_CALL 中）",
      {c["station"] for c in scored["scorecards"]}
      == {"BG1AAA", "BG2BBB", "BG3CCC"})

# --- Storage round trip -------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    st = Storage(os.path.join(d, "t.db"))
    b = st.create_batch(new_id("B"), "t", rules)
    st.add_log(new_id("L"), b["id"], "a.log", DEMO_LOG_A, pa)
    st.add_log(new_id("L"), b["id"], "b.log", DEMO_LOG_B, pb)
    st.add_log(new_id("L"), b["id"], "c.log", DEMO_LOG_C, pc)
    check("存储层往返正常", len(st.list_logs(b["id"])) == 3)
    st.close()

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部通过。")
