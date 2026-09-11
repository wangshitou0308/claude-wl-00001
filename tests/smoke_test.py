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
annotated = apply_decisions(rules, res["findings"], {})
scored = score(rules, annotated)
print(json.dumps(scored["scorecards"], ensure_ascii=False, indent=1))
check("两家台站都有计分卡",
      {c["station"] for c in scored["scorecards"]} == {"BG1AAA", "BG2BBB"})

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
