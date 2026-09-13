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

# --- 时钟偏差分析（clockskew） -------------------------------------------------
from cabrillo_judge.clockskew import analyze_clock_skew
from cabrillo_judge.__main__ import (SKEW_LOG_REF, SKEW_LOG_FAST,
                                     SKEW_LOG_SLOW, SKEW_LOG_THIN)


def _sub(log_id, filename, text, rules_):
    p = parse_cabrillo(text, rules_)
    return {"log_id": log_id, "filename": filename,
            "station_call": p["station_call"], "parsed": p}


s_subs = [_sub("LR", "ref.log", SKEW_LOG_REF, rules),
          _sub("LF", "fast.log", SKEW_LOG_FAST, rules),
          _sub("LS", "slow.log", SKEW_LOG_SLOW, rules),
          _sub("LT", "thin.log", SKEW_LOG_THIN, rules)]
rep = analyze_clock_skew(s_subs, "LR", 1800)
sugg = rep["suggested_offsets_minutes"]
check("时钟偏差分析建议快 6 分钟的日志偏移 -6 分钟",
      sugg.get("LF") == -6, str(sugg))
check("时钟偏差分析建议慢 8 分钟的日志偏移 +8 分钟",
      sugg.get("LS") == 8, str(sugg))
check("样本不足的日志不建议偏移", "LT" not in sugg, str(sugg))
thin = next(l for l in rep["logs"] if l["log_id"] == "LT")
check("样本不足只列证据（连通、有 1 个样本、说明原因）",
      thin["connected"] and thin["suggested_offset_minutes"] is None
      and thin["sample_count"] == 1
      and any("样本不足" in r for r in thin["suppress_reasons"]),
      json.dumps(thin, ensure_ascii=False))
fast = next(l for l in rep["logs"] if l["log_id"] == "LF")
check("按日志给出中位数/离散度/样本数/覆盖时段",
      fast["median_seconds"] == 360 and fast["mad_seconds"] == 0
      and fast["sample_count"] == 4
      and fast["coverage"]["span_seconds"] == 7560
      and fast["coverage"]["first"] == "2026-09-10T01:10",
      json.dumps(fast, ensure_ascii=False))
check("参考日志偏移恒为 0",
      next(l for l in rep["logs"] if l["is_reference"])
      ["suggested_offset_minutes"] == 0)

# 偏差随时间变化 -> 不建议
DRIFT_REF = """START-OF-LOG: 3.0
CALLSIGN: BG1AAA
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0100 BG1AAA 599 001 BG8DDD 599 001
QSO: 7023 CW 2026-09-10 0140 BG1AAA 599 002 BG8DDD 599 002
QSO: 7023 CW 2026-09-10 0220 BG1AAA 599 003 BG8DDD 599 003
QSO: 7023 CW 2026-09-10 0300 BG1AAA 599 004 BG8DDD 599 004
END-OF-LOG:
"""
DRIFT_LOG = """START-OF-LOG: 3.0
CALLSIGN: BG8DDD
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0101 BG8DDD 599 001 BG1AAA 599 001
QSO: 7023 CW 2026-09-10 0141 BG8DDD 599 002 BG1AAA 599 002
QSO: 7023 CW 2026-09-10 0229 BG8DDD 599 003 BG1AAA 599 003
QSO: 7023 CW 2026-09-10 0309 BG8DDD 599 004 BG1AAA 599 004
END-OF-LOG:
"""
d_subs = [_sub("DR", "drift-ref.log", DRIFT_REF, rules),
          _sub("DD", "drift.log", DRIFT_LOG, rules)]
drep = analyze_clock_skew(d_subs, "DR", 1800)
dd = next(l for l in drep["logs"] if l["log_id"] == "DD")
check("偏差随时间变化被检出", dd["drift_detected"] is True,
      json.dumps(dd, ensure_ascii=False))
check("偏差随时间变化时不建议偏移",
      dd["suggested_offset_minutes"] is None
      and any("随时间变化" in r for r in dd["suppress_reasons"]),
      str(dd["suppress_reasons"]))

# 日志关系不连通 -> 只列证据
LONE_LOG = """START-OF-LOG: 3.0
CALLSIGN: BG5EEE
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0100 BG5EEE 599 001 BG9ZZZ 599 001
QSO: 7023 CW 2026-09-10 0200 BG5EEE 599 002 BG9ZZZ 599 002
END-OF-LOG:
"""
l_subs = [_sub("LR2", "ref2.log", SKEW_LOG_REF, rules),
          _sub("LL", "lone.log", LONE_LOG, rules)]
lrep = analyze_clock_skew(l_subs, "LR2", 1800)
ll = next(l for l in lrep["logs"] if l["log_id"] == "LL")
check("关系不连通的日志只列证据不建议",
      ll["connected"] is False and ll["suggested_offset_minutes"] is None
      and any("不连通" in r for r in ll["suppress_reasons"]),
      json.dumps(ll, ensure_ascii=False))

# 两跳关系（参考->中间->末端）：末端日志也要有中位数与离散度
TWOHOP_REF = """START-OF-LOG: 3.0
CALLSIGN: BG1AAA
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0100 BG1AAA 599 001 BG2MMM 599 001
QSO: 7023 CW 2026-09-10 0140 BG1AAA 599 002 BG2MMM 599 002
QSO: 7023 CW 2026-09-10 0220 BG1AAA 599 003 BG2MMM 599 003
END-OF-LOG:
"""
TWOHOP_MID = """START-OF-LOG: 3.0
CALLSIGN: BG2MMM
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0103 BG2MMM 599 001 BG1AAA 599 001
QSO: 7023 CW 2026-09-10 0144 BG2MMM 599 002 BG1AAA 599 002
QSO: 7023 CW 2026-09-10 0225 BG2MMM 599 003 BG1AAA 599 003
QSO: 7023 CW 2026-09-10 0110 BG2MMM 599 004 BG3EEE 599 001
QSO: 7023 CW 2026-09-10 0150 BG2MMM 599 005 BG3EEE 599 002
QSO: 7023 CW 2026-09-10 0230 BG2MMM 599 006 BG3EEE 599 003
END-OF-LOG:
"""
TWOHOP_END = """START-OF-LOG: 3.0
CALLSIGN: BG3EEE
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
QSO: 7023 CW 2026-09-10 0114 BG3EEE 599 001 BG2MMM 599 004
QSO: 7023 CW 2026-09-10 0154 BG3EEE 599 002 BG2MMM 599 005
QSO: 7023 CW 2026-09-10 0234 BG3EEE 599 003 BG2MMM 599 006
END-OF-LOG:
"""
t_subs = [_sub("TR", "twohop-ref.log", TWOHOP_REF, rules),
          _sub("TM", "twohop-mid.log", TWOHOP_MID, rules),
          _sub("TE", "twohop-end.log", TWOHOP_END, rules)]
trep = analyze_clock_skew(t_subs, "TR", 1800)
tm = next(l for l in trep["logs"] if l["log_id"] == "TM")
te = next(l for l in trep["logs"] if l["log_id"] == "TE")
check("中间日志直连：中位数 240 秒、MAD 60 秒、建议 -4 分钟",
      tm["median_seconds"] == 240 and tm["mad_seconds"] == 60
      and tm["suggested_offset_minutes"] == -4,
      json.dumps(tm, ensure_ascii=False))
check("两跳末端日志连通且路径长为 2、累计 6 个样本",
      te["connected"] and len(te["path"]) == 2
      and te["sample_count"] == 6,
      json.dumps(te, ensure_ascii=False))
check("两跳末端日志给出累计中位数与离散度（不再为 null）",
      te["median_seconds"] == 480 and te["mad_seconds"] == 60,
      json.dumps(te, ensure_ascii=False))
check("两跳末端日志建议 -8 分钟",
      te["suggested_offset_minutes"] == -8
      and trep["suggested_offsets_minutes"].get("TE") == -8,
      str(trep["suggested_offsets_minutes"]))

# --- 引擎时间偏移：校正后配对与证据字段 ---------------------------------------
res_raw = adjudicate(rules, s_subs)
st_raw = sorted(f["status"] for f in res_raw["findings"])
check("未校正时 7 条 TIME_DRIFT + 1 条 MATCH",
      st_raw == ["MATCH"] + ["TIME_DRIFT"] * 7, str(st_raw))
res_fix = adjudicate(rules, s_subs, time_offsets={"LF": -360, "LS": 480})
st_fix = sorted(f["status"] for f in res_fix["findings"])
check("按建议校正后全部变为 MATCH", st_fix == ["MATCH"] * 8, str(st_fix))
check("每条证据都带原时间/校正时间/偏移",
      all({"ts", "corrected_ts", "offset_seconds", "corrected_time"}
          <= set(r) for f in res_fix["findings"] for r in f["refs"]))
fast_ref = next(r for f in res_fix["findings"] for r in f["refs"]
                if r["log_id"] == "LF" and r["line"] == 10)
check("原始时间不改，校正时间 = 原时间 + 偏移",
      fast_ref["time"] == "0116" and fast_ref["corrected_time"] == "0110"
      and fast_ref["offset_seconds"] == -360
      and fast_ref["corrected_ts"] == fast_ref["ts"] - 360,
      json.dumps(fast_ref, ensure_ascii=False))
check("校正证据标记 clock_corrected",
      all(f["clock_corrected"] for f in res_fix["findings"]
          if any(r["log_id"] in ("LF", "LS") for r in f["refs"])))
raw_id = {f["id"] for f in res_raw["findings"] if f["status"] == "MATCH"}
fix_id = {f["id"] for f in res_fix["findings"]}
check("配对不变时 finding id 稳定（MATCH 证据保持身份）",
      raw_id <= fix_id)

# --- 存储层：方案 CRUD 与裁决归档 ----------------------------------------------
with tempfile.TemporaryDirectory() as d:
    st = Storage(os.path.join(d, "t.db"))
    b = st.create_batch(new_id("B"), "skew", rules)
    bid = b["id"]
    st.add_log("LR", bid, "ref.log", SKEW_LOG_REF,
               parse_cabrillo(SKEW_LOG_REF, rules))
    st.add_log("LF", bid, "fast.log", SKEW_LOG_FAST,
               parse_cabrillo(SKEW_LOG_FAST, rules))
    scheme = st.create_clock_scheme("S-1", bid, "测试方案", "LR", 1800,
                                    {"LF": -6}, None)
    check("方案创建后可读回", scheme["offsets"] == {"LF": -6}
          and scheme["active"] is False)
    st.set_active_clock_scheme(bid, "S-1")
    check("启用后 get_active_clock_scheme 命中",
          st.get_active_clock_scheme(bid)["id"] == "S-1")
    st.set_active_clock_scheme(bid, None)
    check("停用后无启用方案", st.get_active_clock_scheme(bid) is None)
    dec = st.upsert_decision(bid, "F-x", "CONFIRMED", "测试理由", judge="甲")
    st.archive_decision("A-1", bid, "F-x", dec, "测试归档", "S-1")
    check("归档后原裁决不再生效", "F-x" not in st.list_decisions(bid))
    arch = st.list_archived_decisions(bid)
    check("归档裁决可列出（待复核）",
          len(arch) == 1 and arch[0]["decision"]["resolution"] == "CONFIRMED"
          and arch[0]["archive_reason"] == "测试归档")
    check("归档条目可移除", st.delete_archived_decision(bid, "A-1")
          and st.list_archived_decisions(bid) == [])
    st.close()

# --- HTTP 层：分析/方案/预览/归档/版本/下载 ------------------------------------
import http.client
import threading

from cabrillo_judge.web import make_server

with tempfile.TemporaryDirectory() as d:
    server = make_server("127.0.0.1", 0, os.path.join(d, "t.db"))
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def api(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    st_code, r = api("POST", "/api/batches", {"name": "skew-http"})
    hid = r["batch"]["id"]
    st_code, r = api("POST", f"/api/batches/{hid}/logs", {"logs": [
        {"filename": "ref.log", "content": SKEW_LOG_REF},
        {"filename": "fast.log", "content": SKEW_LOG_FAST},
        {"filename": "slow.log", "content": SKEW_LOG_SLOW},
        {"filename": "thin.log", "content": SKEW_LOG_THIN}]})
    check("HTTP 上传 4 份日志", st_code == 201
          and len(r["received"]) == 4, str(r)[:200])
    lids = {x["station_call"]: x["log_id"] for x in r["received"]}
    ref_id = lids["BG1AAA"]

    st_code, r = api("GET", f"/api/batches/{hid}/clock-analysis"
                            f"?reference_log_id={ref_id}&max_window_seconds=1800")
    check("HTTP 分析接口给出建议", st_code == 200
          and r["suggested_offsets_minutes"].get(lids["BG2BBB"]) == -6
          and r["suggested_offsets_minutes"].get(lids["BG3CCC"]) == 8,
          json.dumps(r.get("suggested_offsets_minutes")))
    check("HTTP 分析按日志给出统计与覆盖时段",
          any(l["station_call"] == "BG2BBB" and l["sample_count"] == 4
              and l["median_seconds"] == 360
              and l["coverage"]["span_seconds"] == 7560
              for l in r["logs"]))
    st_code, r = api("GET", f"/api/batches/{hid}/clock-analysis")
    check("缺少 reference_log_id 返回 400", st_code == 400)

    # 先对一条 TIME_DRIFT 裁决，启用方案后它应失去依据被归档
    st_code, r = api("GET", f"/api/batches/{hid}/findings?status=TIME_DRIFT")
    drift_fid = r["findings"][0]["id"]
    st_code, r = api("POST",
                     f"/api/batches/{hid}/findings/{drift_fid}/decision",
                     {"resolution": "CONFIRMED", "reason": "测试：确认通联",
                      "judge": "测试员"})
    check("HTTP 裁决 TIME_DRIFT", st_code == 200, str(r)[:200])

    st_code, r = api("POST", f"/api/batches/{hid}/clock-schemes",
                     {"reference_log_id": ref_id, "name": "自动建议方案",
                      "use_suggested": True, "activate": True})
    check("HTTP 创建并启用方案", st_code == 201
          and r["scheme"]["offsets"].get(lids["BG2BBB"]) == -6
          and r["scheme"]["offsets"].get(lids["BG3CCC"]) == 8,
          json.dumps(r.get("scheme"), ensure_ascii=False))
    check("创建并启用时响应中的方案状态与持久化一致（active=true）",
          r.get("activated") is True and r["scheme"]["active"] is True,
          json.dumps(r.get("scheme"), ensure_ascii=False))
    sid = r["scheme"]["id"]
    check("启用后 TIME_DRIFT 全部转为 MATCH",
          r["status_counts"]["before"].get("TIME_DRIFT") == 7
          and r["status_counts"]["after"] == {"MATCH": 8},
          json.dumps(r["status_counts"]))
    check("失去依据的裁决被归档而非静默沿用",
          len(r["archived_decisions"]) == 1
          and r["archived_decisions"][0]["finding_id"] == drift_fid
          and r["archived_decisions"][0]["decision"]["reason"]
          == "测试：确认通联",
          json.dumps(r["archived_decisions"], ensure_ascii=False))

    st_code, r = api("GET", f"/api/batches/{hid}/archived-decisions")
    check("待复核归档列表可查", st_code == 200 and r["count"] == 1)
    aid = r["archived_decisions"][0]["id"]

    st_code, r = api("GET", f"/api/batches/{hid}/findings?status=MATCH")
    fref = next(x for f in r["findings"] for x in f["refs"]
                if x["filename"] == "fast.log")
    check("HTTP 证据同时保留原时间/校正时间/偏移",
          fref["time"] == "0116" and fref["corrected_time"] == "0110"
          and fref["offset_seconds"] == -360,
          json.dumps(fref, ensure_ascii=False))

    # 预览：临时去掉全部偏移 -> 回到 7 条 TIME_DRIFT；不改动数据
    st_code, r = api("POST", f"/api/batches/{hid}/clock-schemes/preview",
                     {"offsets": {}})
    check("预览给出状态计数与计分变化",
          st_code == 200
          and r["current"]["status_counts"] == {"MATCH": 8}
          and r["preview"]["status_counts"].get("TIME_DRIFT") == 7
          and isinstance(r["scorecard_diff"], list),
          json.dumps(r, ensure_ascii=False)[:300])
    st_code, r = api("GET", f"/api/batches/{hid}/findings?status=MATCH")
    check("预览不改动数据（仍 8 条 MATCH）", r["count"] == 8)

    # 第二个方案（只校正快钟）用于比较
    st_code, r = api("POST", f"/api/batches/{hid}/clock-schemes",
                     {"reference_log_id": ref_id, "name": "只校正快钟",
                      "offsets": {lids["BG2BBB"]: -6}})
    sid2 = r["scheme"]["id"]
    st_code, r = api("GET", f"/api/batches/{hid}/clock-schemes/compare"
                            f"?a={sid}&b={sid2}")
    check("方案比较接口给出两方案状态计数",
          st_code == 200
          and r["a"]["status_counts"] == {"MATCH": 8}
          and r["b"]["status_counts"].get("TIME_DRIFT") == 3,
          json.dumps(r, ensure_ascii=False)[:300])

    # 方案随计分版本持久化
    st_code, r = api("POST", f"/api/batches/{hid}/versions",
                     {"note": "校正后版本"})
    check("版本快照包含启用中的方案", st_code == 201, str(r)[:200])
    st_code, r = api("GET", f"/api/batches/{hid}/versions/1")
    check("版本快照包含启用中的方案",
          r["snapshot"]["clock_scheme"]["offsets"].get(lids["BG2BBB"]) == -6,
          json.dumps(r["snapshot"].get("clock_scheme"), ensure_ascii=False))

    # 停用方案 -> 恢复原始时间；归档列表仍在
    st_code, r = api("POST",
                     f"/api/batches/{hid}/clock-schemes/{sid}/deactivate")
    check("停用方案恢复 TIME_DRIFT",
          st_code == 200
          and r["status_counts"]["after"].get("TIME_DRIFT") == 7,
          json.dumps(r, ensure_ascii=False)[:300])

    # 下载包含方案与归档
    st_code, r = api("GET", f"/api/batches/{hid}/download")
    check("下载 JSON 含校正方案与归档裁决",
          st_code == 200 and len(r["clock_schemes"]) == 2
          and len(r["archived_decisions"]) == 1,
          str(list(r.keys())))

    # 复核后移除归档条目；未启用方案可删除
    st_code, r = api("DELETE",
                     f"/api/batches/{hid}/archived-decisions/{aid}")
    check("复核后移除归档条目", st_code == 200)
    st_code, r = api("DELETE", f"/api/batches/{hid}/clock-schemes/{sid2}")
    check("删除未启用方案", st_code == 200)

    server.shutdown()
    server.server_close()

# --- HTTP 层：站级赛后反馈包 ---------------------------------------------------
from cabrillo_judge.feedback import (build_external_report,
                                     build_station_report,
                                     render_text, report_content_hash)

with tempfile.TemporaryDirectory() as d:
    server = make_server("127.0.0.1", 0, os.path.join(d, "t.db"))
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def api2(method, path, body=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return (resp.status, data) if raw else (resp.status, json.loads(data))

    st_code, r = api2("POST", "/api/batches", {"name": "feedback-http"})
    fbid = r["batch"]["id"]
    st_code, r = api2("POST", f"/api/batches/{fbid}/logs", {"logs": [
        {"filename": "BG1AAA.log", "content": DEMO_LOG_A},
        {"filename": "BG2BBB.log", "content": DEMO_LOG_B},
        {"filename": "BG3CCC.log", "content": DEMO_LOG_C}]})
    check("反馈包：上传 3 份日志", st_code == 201)

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 1, "station": "BG1AAA"})
    check("反馈包：无冻结版本时 404", st_code == 404
          and r["error"] == "VERSION_NOT_FOUND", str(r)[:200])

    st_code, r = api2("POST", f"/api/batches/{fbid}/versions",
                      {"note": "v1"})
    check("反馈包：冻结计分版本 1", st_code == 201
          and r["version_no"] == 1, str(r)[:200])

    # 单站草稿
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 1, "station": "BG1AAA"})
    check("反馈包：单站草稿生成", st_code == 201 and r["count"] == 1
          and r["reports"][0]["report"]["status"] == "draft"
          and r["reports"][0]["report"]["kind"] == "normal",
          json.dumps(r, ensure_ascii=False)[:300])
    rid_a = r["reports"][0]["report"]["id"]

    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}")
    rep = r["content"]
    check("反馈包：7 条有效行逐条列出（含原日志行）",
          len(rep["entries"]) == 7
          and all(e["raw"].startswith("QSO:") for e in rep["entries"]),
          str(len(rep["entries"])))
    check("反馈包：汇总 CLAIMED-SCORE/最终得分/差额",
          rep["summary"]["claimed_score"] == 7
          and rep["summary"]["final_score"] == 1
          and rep["summary"]["difference"] == -6,
          json.dumps(rep["summary"], ensure_ascii=False))
    first = rep["entries"][0]
    check("反馈包：首条计分 QSO 带 QSO 分与新增乘数项",
          first["counted"] is True and first["qso_points"] == 1
          and first["new_multipliers"] == [
              {"type": "worked_call", "name": "不同对方呼号",
               "value": "BG2BBB"}],
          json.dumps(first, ensure_ascii=False)[:300])
    check("反馈包：无法关联原行的证据为空且字段存在",
          rep["unassociated_evidence"] == []
          and rep["summary"]["lines"]["unassociated_evidence"] == 0)
    st_code, r2 = api2("GET", f"/api/batches/{fbid}/results")
    check("反馈包：最终得分与计分结果一致",
          next(c for c in r2["scorecards"] if c["station"] == "BG1AAA")
          ["total_score"] == rep["summary"]["final_score"])

    # 发布前必须预览
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a}/publish")
    check("反馈包：未预览不能发布", st_code == 409
          and r["error"] == "FEEDBACK_NOT_PREVIEWED", str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a}/preview")
    check("反馈包：生成对外脱敏预览", st_code == 200
          and r["external"]["view"] == "external"
          and r["external"]["redaction_note"], str(r)[:200])
    ext = r["external"]
    check("反馈包：对外稿不含其他台站原始行（无 refs）",
          all("refs" not in e for e in ext["entries"]))
    check("反馈包：对外稿保留本台原行与差异项",
          all(e["raw"].startswith("QSO:") for e in ext["entries"])
          and any(e["exchange_diffs"] for e in ext["entries"]))
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}")
    check("反馈包：内部稿保留对方原始行（对照）",
          any(len(e["refs"]) == 2 for e in r["content"]["entries"]))
    st_code, r = api2("GET",
                      f"/api/batches/{fbid}/feedback/{rid_a}?view=external")
    check("反馈包：预览后可查对外稿", st_code == 200
          and r["view"] == "external")

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a}/publish")
    check("反馈包：预览后发布成功", st_code == 200
          and r["report"]["status"] == "published", str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a}/preview")
    check("反馈包：已发布不可再预览（不可变）", st_code == 409
          and r["error"] == "FEEDBACK_IMMUTABLE", str(r)[:200])

    # 整批生成：BG1AAA 内容相同幂等返回，其余两台新建草稿
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 1})
    by_st = {x["report"]["station"]: x for x in r["reports"]}
    check("反馈包：整批生成每台一份", st_code == 201
          and set(by_st) == {"BG1AAA", "BG2BBB", "BG3CCC"},
          json.dumps(r, ensure_ascii=False)[:300])
    check("反馈包：相同内容幂等（identical_to）",
          by_st["BG1AAA"].get("identical_to") == rid_a)
    rid_b = by_st["BG2BBB"]["report"]["id"]

    # 裁决变化 -> 冻结版本 2 -> 对已发布的 BG1AAA 自动生成更正包
    st_code, r = api2("GET",
                      f"/api/batches/{fbid}/findings?status=EXCHANGE_DIFF")
    fid_ex = r["findings"][0]["id"]
    st_code, r = api2("POST",
                      f"/api/batches/{fbid}/findings/{fid_ex}/decision",
                      {"resolution": "CONFIRMED", "fault_station": "BG1AAA",
                       "penalty_code": "BAD_EXCHANGE",
                       "reason": "BG1AAA 抄收 559，对方实发 599，判其抄错",
                       "judge": "测试员"})
    check("反馈包：裁决 EXCHANGE_DIFF", st_code == 200, str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/versions", {"note": "v2"})
    check("反馈包：冻结计分版本 2", st_code == 201
          and r["version_no"] == 2, str(r)[:200])

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 2, "station": "BG1AAA"})
    rep2meta = r["reports"][0]["report"]
    check("反馈包：版本演进自动生成更正包",
          rep2meta["kind"] == "correction"
          and rep2meta["corrects_report_id"] == rid_a,
          json.dumps(rep2meta, ensure_ascii=False))
    rid_a2 = rep2meta["id"]
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a2}")
    rep2 = r["content"]
    check("反馈包：更正包注明旧包与替代版本",
          rep2["correction"]["supersedes_report_id"] == rid_a
          and rep2["correction"]["supersedes_version_no"] == 1
          and rep2["correction"]["previous_final_score"] == 1
          and rep2["correction"]["score_delta"] == -1,
          json.dumps(rep2["correction"], ensure_ascii=False))
    check("反馈包：更正包反映新裁决（第14行计分且罚 2 分）",
          rep2["summary"]["final_score"] == 0
          and rep2["summary"]["penalty_points"] == 2
          and any(e["line"] == 14 and e["counted"]
                  and e["penalty_points"] == 2
                  and e["decision"]["resolution"] == "CONFIRMED"
                  for e in rep2["entries"]),
          json.dumps(rep2["summary"], ensure_ascii=False))
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}")
    check("反馈包：旧包不被改写且记录替代关系",
          r["report"]["status"] == "published"
          and r["report"]["superseded_by"] == rid_a2
          and r["content"]["summary"]["final_score"] == 1)
    st_code, r = api2("GET",
                      f"/api/batches/{fbid}/feedback/{rid_a2}/lineage")
    check("反馈包：更正链可追踪（长度 2）",
          r["length"] == 2
          and [c["id"] for c in r["chain"]] == [rid_a, rid_a2],
          json.dumps(r, ensure_ascii=False)[:300])

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 2, "station": "BG2BBB"})
    check("反馈包：无已发布旧包时新版本仍为普通包",
          r["reports"][0]["report"]["kind"] == "normal")

    # 显式 corrects 校验
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 2, "station": "BG3CCC",
                       "corrects": rid_b})
    check("反馈包：不能更正其他台站的包", st_code == 400
          and r["error"] == "FEEDBACK_STATION_MISMATCH", str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 2, "station": "BG2BBB",
                       "corrects": rid_b})
    check("反馈包：草稿不能被更正（须已发布）", st_code == 409
          and r["error"] == "FEEDBACK_NOT_PUBLISHED", str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 1, "station": "BG1AAA",
                       "corrects": rid_a})
    check("反馈包：更正包须基于不同版本", st_code == 400
          and r["error"] == "FEEDBACK_SAME_VERSION", str(r)[:200])

    # 下载：已发布默认对外稿；纯文本含汇总且不含对方原行
    st_code, txt = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}"
                               f"/download?format=txt", raw=True)
    check("反馈包：纯文本下载（默认对外）",
          st_code == 200 and "CLAIMED-SCORE" in txt
          and "对方原行" not in txt and "脱敏说明" in txt, txt[:160])
    st_code, txt = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}"
                               f"/download?format=txt&view=internal",
                        raw=True)
    check("反馈包：内部稿纯文本含对方原行",
          st_code == 200 and "对方原行" in txt, txt[:160])
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a}"
                             f"/download?format=json")
    check("反馈包：JSON 下载默认对外视图",
          st_code == 200 and r["view"] == "external"
          and all("refs" not in e for e in r["entries"]))
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback"
                             f"?station=BG1AAA&kind=correction")
    check("反馈包：列表过滤（更正包）", st_code == 200
          and r["count"] == 1
          and r["reports"][0]["id"] == rid_a2, str(r)[:200])

    # --- 缺陷回归 1：裁决理由中的邮件地址/他台完整 QSO 行必须在对外输出中清掉 ---
    st_code, r = api2("GET",
                      f"/api/batches/{fbid}/findings?status=TIME_DRIFT")
    fid_td = r["findings"][0]["id"]
    leaky_reason = ("已邮件联系 judge@example.org 确认；对方原行 "
                    "QSO: 7023 CW 2026-09-10 0214 BG2BBB 599 005 "
                    "BG1AAA 599 005 与台站自述一致")
    st_code, r = api2("POST",
                      f"/api/batches/{fbid}/findings/{fid_td}/decision",
                      {"resolution": "CONFIRMED", "reason": leaky_reason,
                       "judge": "测试员"})
    check("反馈包：含敏感内容的裁决理由可登记", st_code == 200, str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/versions", {"note": "v3"})
    check("反馈包：冻结计分版本 3", st_code == 201
          and r["version_no"] == 3, str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 3, "station": "BG1AAA"})
    rid_a3 = r["reports"][0]["report"]["id"]
    check("反馈包：v3 自动生成更正包",
          r["reports"][0]["report"]["kind"] == "correction")

    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a3}")
    ent16 = next(e for e in r["content"]["entries"] if e["line"] == 16)
    check("反馈包：内部稿保留裁决理由原文（供裁判核对）",
          ent16["decision"]["reason"] == leaky_reason,
          ent16["decision"]["reason"][:120])

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a3}/preview")
    ext3 = r["external"]
    ext3_text = json.dumps(ext3, ensure_ascii=False)
    check("反馈包：对外稿清掉裁决理由中的邮件地址",
          "judge@example.org" not in ext3_text
          and "〔已隐去邮件地址〕" in ext3_text)
    check("反馈包：对外稿清掉他台完整 QSO 行",
          "QSO: 7023 CW 2026-09-10 0214" not in ext3_text
          and "〔已隐去他台QSO原文〕" in ext3_text)
    ent16x = next(e for e in ext3["entries"] if e["line"] == 16)
    check("反馈包：对外稿裁决理由被占位符替换",
          "judge@example.org" not in ent16x["decision"]["reason"]
          and "〔已隐去邮件地址〕" in ent16x["decision"]["reason"]
          and "〔已隐去他台QSO原文〕" in ent16x["decision"]["reason"],
          ent16x["decision"]["reason"])
    check("反馈包：对外稿仍保留本台原行与对方呼号",
          ent16x["raw"].startswith("QSO:") and ent16x["partner"] == "BG2BBB")

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback/{rid_a3}/publish")
    check("反馈包：脱敏后可发布", st_code == 200
          and r["report"]["status"] == "published", str(r)[:200])
    st_code, txt = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a3}"
                               f"/download?format=txt", raw=True)
    check("反馈包：发布的纯文本不含敏感内容",
          st_code == 200 and "judge@example.org" not in txt
          and "QSO: 7023 CW 2026-09-10 0214" not in txt
          and "〔已隐去邮件地址〕" in txt, txt[:160])
    st_code, txt = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a3}"
                               f"/download?format=txt&view=internal",
                        raw=True)
    check("反馈包：内部稿纯文本仍可见原始理由",
          st_code == 200 and "judge@example.org" in txt, txt[:160])

    # --- 缺陷回归 2：版本-日志绑定，冻结后上传的日志不得混入旧版本报告 ---------
    NEW_LOG_A = DEMO_LOG_A.replace("CLAIMED-SCORE: 7",
                                   "CLAIMED-SCORE: 999").replace(
        "END-OF-LOG:",
        "QSO: 7023 CW 2026-09-10 0400 BG1AAA 599 008 BG9ZZZ 599 001\n"
        "END-OF-LOG:")
    st_code, r = api2("POST", f"/api/batches/{fbid}/logs",
                      {"filename": "BG1AAA-v2.log", "content": NEW_LOG_A})
    check("反馈包：冻结后可再上传该台站新日志", st_code == 201,
          str(r)[:200])

    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 3, "station": "BG1AAA"})
    check("反馈包：新日志不混入 v3（内容不变，幂等返回）",
          r["reports"][0].get("identical_to") == rid_a3,
          json.dumps(r, ensure_ascii=False)[:300])
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a3}")
    rep3 = r["content"]
    check("反馈包：v3 报告只含冻结时的日志",
          [l["filename"] for l in rep3["logs"]] == ["BG1AAA.log"]
          and rep3["summary"]["claimed_score"] == 7
          and len(rep3["entries"]) == 7,
          json.dumps(rep3["logs"], ensure_ascii=False))

    NEW_LOG_Z = GOOD_LOG.replace("BG1AAA", "BG9ZZZ")
    st_code, r = api2("POST", f"/api/batches/{fbid}/logs",
                      {"filename": "BG9ZZZ.log", "content": NEW_LOG_Z})
    check("反馈包：新台站日志上传", st_code == 201, str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 3, "station": "BG9ZZZ"})
    check("反馈包：冻结后才有日志的台站不能生成旧版本报告",
          st_code == 404 and r["error"] == "NO_STATION_LOG"
          and "冻结之后" in r["message"], str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 3})
    check("反馈包：整批生成也不含冻结后才有日志的台站",
          "BG9ZZZ" not in {x["report"]["station"] for x in r["reports"]},
          json.dumps(r, ensure_ascii=False)[:300])

    st_code, r = api2("POST", f"/api/batches/{fbid}/versions", {"note": "v4"})
    check("反馈包：冻结计分版本 4（含新日志）", st_code == 201
          and r["version_no"] == 4, str(r)[:200])
    st_code, r = api2("POST", f"/api/batches/{fbid}/feedback",
                      {"version_no": 4, "station": "BG1AAA"})
    rid_a4 = r["reports"][0]["report"]["id"]
    st_code, r = api2("GET", f"/api/batches/{fbid}/feedback/{rid_a4}")
    rep4 = r["content"]
    check("反馈包：v4 绑定冻结时的全部日志（含新上传）",
          sorted(l["filename"] for l in rep4["logs"])
          == ["BG1AAA-v2.log", "BG1AAA.log"]
          and rep4["summary"]["claimed_score"] == 999
          and len(rep4["entries"]) == 15
          and any(e["filename"] == "BG1AAA-v2.log"
                  for e in rep4["entries"]),
          json.dumps(rep4["logs"], ensure_ascii=False))

    server.shutdown()
    server.server_close()

# --- 反馈包：无法关联原行的证据单列（单元级） -------------------------------------
fake_version = {"version_no": 9, "content_hash": "x" * 64, "created_ts": 1,
                "snapshot": {
                    "rules": rules,
                    "findings": [{
                        "id": "F-unrel", "status": "MATCH", "pending": False,
                        "band": "40m", "mode": "CW", "ts_hint": 0,
                        "stations": ["BG1AAA", "BG9ZZZ"],
                        "refs": [{"log_id": "L-else", "filename": "else.log",
                                  "station": "BG9ZZZ", "line": 3,
                                  "raw": "QSO: 7023 CW ..."}],
                        "effects": {}, "auto_reason": "测试证据",
                        "exchange_diffs": [], "call_detail": {},
                        "decision": None}],
                    "results": {"scorecards": [], "summary": {}},
                    "decisions": {}}}
rep_u = build_station_report(
    batch={"id": "B-u", "name": "单测"}, version=fake_version,
    station="BG1AAA",
    logs=[{"log_id": "L1", "filename": "BG1AAA.log",
           "station_call": "BG1AAA", "upload_ts": 1, "parsed": pa}])
check("无法关联原行的证据单列、不猜测归属",
      [u["finding_id"] for u in rep_u["unassociated_evidence"]] == ["F-unrel"]
      and "不猜测归属" in rep_u["unassociated_evidence"][0]["note"],
      json.dumps(rep_u["unassociated_evidence"], ensure_ascii=False))
check("无证据的合法行标 UNTRACKED 而不猜测配对",
      all(e["status"] == "UNTRACKED" for e in rep_u["entries"]))
ext_u = build_external_report(rep_u)
check("对外包剔除 refs 且标注脱敏",
      all("refs" not in e for e in ext_u["entries"])
      and ext_u["redaction_note"])
txt_u = render_text(ext_u)
check("纯文本渲染包含无法关联证据段",
      "无法关联原日志行的证据" in txt_u and "F-unrel" in txt_u)
check("报告内容哈希稳定（幂等）",
      report_content_hash(rep_u) == report_content_hash(
          json.loads(json.dumps(rep_u))))

# --- 反馈包：版本-日志绑定（单元级） ---------------------------------------------
from cabrillo_judge.feedback import version_bound_logs, _sanitize_text

logs_two = [
    {"log_id": "L1", "filename": "a.log", "station_call": "BG1AAA",
     "upload_ts": 1000, "parsed": pa},
    {"log_id": "L2", "filename": "a2.log", "station_call": "BG1AAA",
     "upload_ts": 2000, "parsed": pa},
]
ver_legacy = {"version_no": 1, "content_hash": "h", "created_ts": 1500,
              "snapshot": {"rules": rules, "findings": [],
                           "results": {"scorecards": []}}}
rep_b = build_station_report(batch={"id": "B", "name": "t"},
                             version=ver_legacy, station="BG1AAA",
                             logs=logs_two)
check("无清单的旧版快照按冻结时间绑定日志",
      [l["filename"] for l in rep_b["logs"]] == ["a.log"],
      json.dumps(rep_b["logs"], ensure_ascii=False))
ver_manifest = {"version_no": 2, "content_hash": "h", "created_ts": 3000,
                "snapshot": {"rules": rules, "findings": [],
                             "results": {"scorecards": []},
                             "logs": [{"log_id": "L2", "filename": "a2.log",
                                       "station_call": "BG1AAA",
                                       "upload_ts": 2000}]}}
rep_m = build_station_report(batch={"id": "B", "name": "t"},
                             version=ver_manifest, station="BG1AAA",
                             logs=logs_two)
check("含清单的快照按 log_id 绑定日志（不受上传时间影响）",
      [l["filename"] for l in rep_m["logs"]] == ["a2.log"],
      json.dumps(rep_m["logs"], ensure_ascii=False))
check("version_bound_logs 不改动原列表",
      len(logs_two) == 2)

# --- 反馈包：自由文本脱敏（单元级） ----------------------------------------------
s = _sanitize_text("联系 judge@example.org，见 "
                   "QSO: 7023 CW 2026-09-10 0214 BG2BBB 599 005 "
                   "BG1AAA 599 005 可证")
check("自由文本隐去邮件地址与他台 QSO 原文",
      "judge@example.org" not in s and "QSO:" not in s
      and "〔已隐去邮件地址〕" in s and "〔已隐去他台QSO原文〕" in s, s)
check("X-QSO 行同样被隐去",
      "QSO:" not in _sanitize_text(
          "X-QSO: 3523 CW 2026-09-10 0330 BG3CCC 579 002 BG9ZZZ 579 001"))
check("普通中文理由不受影响",
      _sanitize_text("双方记录一致，确认计分") == "双方记录一致，确认计分")
check("呼号与行号文本不误伤",
      _sanitize_text("BG1AAA 第 14 行抄收 559，对方实发 599")
      == "BG1AAA 第 14 行抄收 559，对方实发 599")

# --- HTTP 层：赛后复议案件 ---------------------------------------------------
from cabrillo_judge.appeals import (
    CASE_STATUSES, case_content_hash, validate_claims, apply_rulings,
    validate_rulings, preview_digest, outcome_scorecard_diff,
    finding_touches_station)
from cabrillo_judge.engine import content_hash

with tempfile.TemporaryDirectory() as d:
    server = make_server("127.0.0.1", 0, os.path.join(d, "t.db"))
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def api3(method, path, body=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return (resp.status, data) if raw else (resp.status, json.loads(data))

    st_code, r = api3("POST", "/api/batches", {"name": "appeal-http"})
    abid = r["batch"]["id"]
    api3("POST", f"/api/batches/{abid}/logs", {"logs": [
        {"filename": "BG1AAA.log", "content": DEMO_LOG_A},
        {"filename": "BG2BBB.log", "content": DEMO_LOG_B},
        {"filename": "BG3CCC.log", "content": DEMO_LOG_C}]})
    api3("POST", f"/api/batches/{abid}/versions", {"note": "v1"})
    # 发布 BG1AAA 的 v1 反馈包
    st_code, r = api3("POST", f"/api/batches/{abid}/feedback",
                      {"version_no": 1, "station": "BG1AAA"})
    rid_a = r["reports"][0]["report"]["id"]
    api3("POST", f"/api/batches/{abid}/feedback/{rid_a}/preview")
    api3("POST", f"/api/batches/{abid}/feedback/{rid_a}/publish")
    # BG2BBB 的草稿包（不可复议）
    st_code, r = api3("POST", f"/api/batches/{abid}/feedback",
                      {"version_no": 1, "station": "BG2BBB"})
    rid_b_draft = r["reports"][0]["report"]["id"]

    st_code, r = api3("POST", f"/api/batches/{abid}/appeals",
                      {"report_id": rid_b_draft,
                       "claims": [{"subject": "summary_score",
                                   "summary": "草稿有问题"}]})
    check("复议：草稿包不可提案", st_code == 409
          and r["error"] == "FEEDBACK_NOT_PUBLISHED", str(r)[:200])

    # 取 EXCHANGE_DIFF（第14行）与 DUP（第17行）证据
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/findings?status=EXCHANGE_DIFF")
    fid_ex = r["findings"][0]["id"]
    st_code, r = api3("GET", f"/api/batches/{abid}/findings?status=DUP")
    fid_dup = r["findings"][0]["id"]

    # 引用校验：他台 finding / 他台日志行 / 不存在的行 / 无罚分目标
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_a, "claims": [
            {"subject": "pairing_status", "summary": "不存在的证据",
             "finding_id": "F-deadbeefdead"},
            {"subject": "log_line", "summary": "他台行",
             "log_refs": [{"filename": "BG2BBB.log", "line": 8}]},
            {"subject": "log_line", "summary": "不存在的行",
             "log_refs": [{"filename": "BG1AAA.log", "line": 99}]},
            {"subject": "penalty", "summary": "无罚分目标",
             "finding_id": fid_ex},
            {"subject": "exchange_diff", "summary": "缺引用"}]})
    check("复议：非法引用被逐项拒绝且不自动改绑",
          st_code == 400 and r["error"] == "INVALID_CLAIMS"
          and len(r["details"]) == 5
          and any(p["code"] == "CLAIM_FINDING_NOT_FOUND"
                  for p in r["details"][0]["problems"])
          and any(p["code"] == "CLAIM_STATION_MISMATCH"
                  for p in r["details"][1]["problems"])
          and any(p["code"] == "TARGET_NOT_IN_REPORT"
                  for p in r["details"][2]["problems"])
          and any(p["code"] == "TARGET_NO_PENALTY"
                  for p in r["details"][3]["problems"])
          and any(p["code"] == "CLAIM_TARGET_REQUIRED"
                  for p in r["details"][4]["problems"]),
          json.dumps(r, ensure_ascii=False)[:500])

    # 合法案件创建
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_a, "applicant": "BG1AAA", "claims": [
            {"subject": "pairing_status",
             "summary": "第14行交换差异系抄收笔误，请求确认计分",
             "finding_id": fid_ex,
             "log_refs": [{"filename": "BG1AAA.log", "line": 14}]},
            {"subject": "log_line",
             "summary": "第17行不构成重复",
             "finding_id": fid_dup,
             "log_refs": [{"filename": "BG1AAA.log", "line": 17}]}]})
    check("复议：合法案件创建（submitted）", st_code == 201
          and r["case"]["status"] == "submitted"
          and r["case"]["report_id"] == rid_a
          and r["case"]["version_no"] == 1
          and len(r["binding"]["binding_content_hash"]) == 64
          and len(r["claims"]) == 2, str(r)[:200])
    cid = r["case"]["id"]
    binding_hash = r["binding"]["binding_content_hash"]

    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_a,
        "claims": [{"subject": "summary_score", "summary": "重复提案"}]})
    check("复议：同一未结案件期间禁止重复提案",
          st_code == 409 and r["error"] == "APPEAL_ALREADY_EXISTS",
          str(r)[:160])

    # 详情含绑定快照与状态轨迹
    st_code, r = api3("GET", f"/api/batches/{abid}/appeals/{cid}")
    check("复议：详情含争议项/轨迹/绑定",
          st_code == 200 and [c["seq"] for c in r["claims"]] == [1, 2]
          and [e["event_type"] for e in r["events"]] == ["created"]
          and r["binding"]["report"]["id"] == rid_a
          and r["binding"]["version"]["version_no"] == 1, str(r)[:200])

    # 未受理不能预览/确认；可以且仅可以撤回
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals/{cid}/preview",
                      {"rulings": []})
    check("复议：未受理不能预览", st_code == 409
          and r["error"] == "APPEAL_NOT_IN_REVIEW", str(r)[:120])

    # 撤回
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals/{cid}/withdraw",
                      {"reason": "申请方补充材料后重新提交",
                       "applicant": "BG1AAA"})
    check("复议：未受理案件可撤回", st_code == 200
          and r["case"]["status"] == "withdrawn"
          and r["case"]["withdrawn_ts"] is not None, str(r)[:200])
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals/{cid}/accept",
                      {"judge": "甲"})
    check("复议：撤回后不能受理", st_code == 409, str(r)[:120])
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/appeals?status=withdrawn")
    check("复议：状态筛选 withdrawn",
          r["count"] == 1 and r["cases"][0]["id"] == cid, str(r)[:160])

    # 撤回后可重新提案
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_a, "claims": [
            {"subject": "pairing_status", "summary": "重新提案",
             "finding_id": fid_ex}]})
    check("复议：撤回后可重新提案", st_code == 201
          and r["case"]["status"] == "submitted", str(r)[:160])
    cid2 = r["case"]["id"]

    # 受理
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals/{cid2}/accept",
                      {"judge": "裁判甲"})
    check("复议：受理成功 in_review", st_code == 200
          and r["case"]["status"] == "in_review"
          and r["case"]["judge"] == "裁判甲", str(r)[:160])
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals/{cid2}/withdraw",
                      {"reason": "受理后不能撤"})
    check("复议：受理后不能撤回", st_code == 409
          and r["error"] == "APPEAL_NOT_WITHDRAWABLE", str(r)[:120])

    st_code, r = api3("GET", f"/api/batches/{abid}/appeals/{cid2}")
    claim_ids = [c["id"] for c in r["claims"]]
    check("复议：重新提案仅含 1 个争议项", len(claim_ids) == 1)
    rulings_bad = [
        {"claim_id": claim_ids[0], "conclusion": "revised",
         "resolution": "WAIVED", "rationale": "EXCHANGE_DIFF 不允许 WAIVED"},
        {"claim_id": "nope", "conclusion": "upheld", "rationale": "悬挂结论"}]
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/preview",
                      {"rulings": rulings_bad})
    check("复议：结论形式校验（动作与状态不符/悬挂项）",
          st_code == 400 and r["error"] == "INVALID_RULINGS"
          and any(d["claim_id"] == "__ruling_nope" for d in r["details"])
          and any(p["code"] == "BAD_DECISION"
                  for d in r["details"] if d["claim_id"] == claim_ids[0]
                  for p in d["problems"]),
          json.dumps(r, ensure_ascii=False)[:400])
    # 缺结论项也必须报错
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/preview",
                      {"rulings": []})
    check("复议：空结论列表 400", st_code == 400
          and r["error"] == "RULINGS_REQUIRED", str(r)[:120])

    # 合法预览：仅 1 项改判 CONFIRMED+罚 BG1AAA
    rulings = [
        {"claim_id": claim_ids[0], "conclusion": "revised",
         "resolution": "CONFIRMED", "fault_station": "BG1AAA",
         "penalty_code": "BAD_EXCHANGE",
         "rationale": "交换差异为台站抄收笔误，通联确认计分并罚抄收错误",
         "judge": "裁判甲"}]
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/preview",
                      {"rulings": rulings, "judge": "裁判甲"})
    check("复议：处理预览给出变更与计分",
          st_code == 200 and r["revision_count"] == 1
          and r["station_score"] == {"station": "BG1AAA", "before": 1,
                                     "after": 0, "delta": -1}
          and r["correction_required"] is True
          and r["prospective_version_no"] == 2
          and len(r["digest"]) == 64
          and r["items"][0]["conclusion"] == "revised"
          and r["items"][0]["after"]["counted"] is True
          and r["items"][0]["after"]["penalty_points"] == 2,
          json.dumps(r, ensure_ascii=False)[:300])
    digest = r["digest"]
    check("复议：预览不写入（版本仍为 1、案件仍 in_review）",
          len(api3("GET", f"/api/batches/{abid}/versions")[1]["versions"]) == 1)

    # 无 digest / digest 不符拒绝确认
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/confirm",
                      {"rulings": rulings})
    check("复议：无预览 digest 不能确认", st_code == 400
          and r["error"] == "PREVIEW_REQUIRED", str(r)[:120])
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/confirm",
                      {"rulings": rulings, "digest": "0" * 64})
    check("复议：digest 不符拒绝确认", st_code == 409
          and r["error"] == "PREVIEW_DIGEST_MISMATCH", str(r)[:120])

    # 绑定漂移：确认前改动另一条证据裁决 -> 整案不写入
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/findings?status=NO_PARTNER_LOG")
    fid_npl = r["findings"][0]["id"]
    api3("POST", f"/api/batches/{abid}/findings/{fid_npl}/decision",
         {"resolution": "GRANTED",
          "reason": "赛后另行改判，使绑定快照漂移"})
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/confirm",
                      {"rulings": rulings, "digest": digest})
    check("复议：绑定失效时整案不写入（APPEAL_STALE）",
          st_code == 409 and r["error"] == "APPEAL_STALE"
          and any(p["code"] == "DECISION_STALE" for p in r["details"])
          and any("整案不写入" in p["message"] for p in r["details"]),
          json.dumps(r, ensure_ascii=False)[:300])
    st_code, r = api3("GET", f"/api/batches/{abid}/appeals/{cid2}")
    check("复议：失败后案件仍 in_review、版本未增加",
          r["case"]["status"] == "in_review"
          and len(api3("GET", f"/api/batches/{abid}/versions")[1]
                  ["versions"]) == 1)

    # 撤销那条漂移裁决后确认成功（一次性写入并冻结 v2）
    api3("DELETE", f"/api/batches/{abid}/findings/{fid_npl}/decision")
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid2}/confirm",
                      {"rulings": rulings, "digest": digest,
                       "judge": "裁判甲", "note": "复议改判冻结"})
    check("复议：确认成功并结案",
          st_code == 200 and r["case"]["status"] == "closed"
          and r["frozen"] is True
          and r["frozen_version"]["version_no"] == 2
          and r["case"]["resolved_version_no"] == 2
          and r["case"]["score_before"] == 1
          and r["case"]["score_after"] == 0, str(r)[:300])
    corr_id = r["correction_published"]
    check("复议：得分变化生成更正包", bool(corr_id))
    check("复议：逐项结论落库",
          [(c["seq"], c["conclusion"], c["resolution"]) for c in r["claims"]]
          == [(1, "revised", "CONFIRMED")])
    check("复议：状态轨迹完整",
          [e["event_type"] for e in r["events"]]
          == ["created", "accepted", "correction_published", "ruled",
              "version_frozen", "closed"],
          str([e["event_type"] for e in r["events"]]))

    # 新裁决实际进入批次：v2 结果与现行 results 一致
    st_code, r = api3("GET", f"/api/batches/{abid}/results")
    card_a = next(c for c in r["scorecards"] if c["station"] == "BG1AAA")
    check("复议：改判写入后现行计分卡为改判后结果",
          card_a["total_score"] == 0 and card_a["penalty_points"] == 2
          and card_a["qso_counted"] == 2, json.dumps(card_a, ensure_ascii=False))
    st_code, r = api3("GET", f"/api/batches/{abid}/versions/2")
    check("复议：v2 快照标注来源案件且含新裁决",
          r["snapshot"].get("created_by_appeal") == cid2
          and r["snapshot"]["decisions"][fid_ex]["resolution"]
          == "CONFIRMED", str(r["snapshot"].get("created_by_appeal")))

    # 原反馈包不可变，仅记录 superseded_by
    st_code, r = api3("GET", f"/api/batches/{abid}/feedback/{rid_a}")
    check("复议：原反馈包内容不可变且记录替代",
          r["report"]["status"] == "published"
          and r["report"]["superseded_by"] == corr_id
          and r["content"]["summary"]["final_score"] == 1)

    # 更正包为已发布、沿既有替代关系、对外稿就绪
    st_code, r = api3("GET", f"/api/batches/{abid}/feedback/{corr_id}")
    check("复议：更正包已发布且注明旧包/版本/分差",
          r["report"]["kind"] == "correction"
          and r["report"]["status"] == "published"
          and r["report"]["version_no"] == 2
          and r["report"]["corrects_report_id"] == rid_a
          and r["content"]["correction"]["score_delta"] == -1
          and r["content"]["summary"]["final_score"] == 0,
          json.dumps(r["report"], ensure_ascii=False)[:300])
    st_code, txt = api3(
        "GET",
        f"/api/batches/{abid}/feedback/{corr_id}/download?format=txt",
        raw=True)
    check("复议：更正包发布即可下载对外纯文本",
          st_code == 200 and "更正包" in txt and "脱敏说明" in txt,
          txt[:100])

    # 对已被替代的旧包再提案 -> 明确拒绝且不自动改绑
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_a,
        "claims": [{"subject": "summary_score", "summary": "旧包异议"}]})
    check("复议：已被替代的旧包拒绝受理",
          st_code == 409 and r["error"] == "REPORT_SUPERSEDED"
          and corr_id in r["message"] and "不会自动改绑" in r["message"],
          r["message"][:120])

    # 结案后下载案件 JSON
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/appeals/{cid2}/download")
    check("复议：JSON 下载含导出信封/案件/争议/轨迹/结果",
          r["export"] == "cabrillo-judge-appeal"
          and r["schema_version"] == 1
          and r["case"]["id"] == cid2
          and r["outcome"]["correction_report"]["id"] == corr_id
          and len(r["claims"]) == 1 and len(r["events"]) == 6,
          str(list(r.keys())))

    # 筛选：station/report_id
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/appeals?station=BG1AAA")
    check("复议：按台站筛选", r["count"] == 2
          and {c["status"] for c in r["cases"]} == {"withdrawn", "closed"})
    st_code, r = api3(
        "GET", f"/api/batches/{abid}/appeals?report_id={rid_a}")
    check("复议：按反馈包筛选", r["count"] == 2)
    st_code, r = api3("GET",
                      f"/api/batches/{abid}/appeals?status=bogus")
    check("复议：非法状态 400", st_code == 400
          and r["error"] == "BAD_STATUS")

    # --- 纯维持/证据不足：不改判不冻结版本、不出更正包，直接结案 ---------
    st_code, r = api3("POST", f"/api/batches/{abid}/feedback",
                      {"version_no": 2, "station": "BG2BBB"})
    rid_b = r["reports"][0]["report"]["id"]
    api3("POST", f"/api/batches/{abid}/feedback/{rid_b}/preview")
    api3("POST", f"/api/batches/{abid}/feedback/{rid_b}/publish")
    st_code, r = api3("POST", f"/api/batches/{abid}/appeals", {
        "report_id": rid_b, "claims": [
            {"subject": "summary_score", "summary": "对分数有疑问"}]})
    cid3 = r["case"]["id"]
    api3("POST", f"/api/batches/{abid}/appeals/{cid3}/accept",
         {"judge": "乙"})
    st_code, dcase = api3("GET", f"/api/batches/{abid}/appeals/{cid3}")
    rulings3 = [{"claim_id": dcase["claims"][0]["id"],
                 "conclusion": "upheld", "rationale": "复核后维持"}]
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid3}/preview",
                      {"rulings": rulings3})
    check("复议：纯维持预览无改判无分差",
          r["revision_count"] == 0
          and r["correction_required"] is False
          and r["station_score"]["delta"] == 0)
    dg = r["digest"]
    st_code, r = api3("POST",
                      f"/api/batches/{abid}/appeals/{cid3}/confirm",
                      {"rulings": rulings3, "digest": dg})
    check("复议：纯维持结案但不冻结新版本/不出更正包",
          r["case"]["status"] == "closed" and r["frozen"] is False
          and r["frozen_version"] is None
          and r["correction_published"] is None
          and r["case"]["resolved_version_no"] is None, str(r)[:200])
    check("复议：版本数仍为 2",
          len(api3("GET", f"/api/batches/{abid}/versions")[1]
              ["versions"]) == 2)

    server.shutdown()
    server.server_close()

# --- 复议：领域层单元（绑定哈希/预览确定性/改判应用） -------------------------
with tempfile.TemporaryDirectory() as d:
    st = Storage(os.path.join(d, "t.db"))
    b = st.create_batch(new_id("B"), "appeal-unit", rules)
    bid = b["id"]
    pa = parse_cabrillo(DEMO_LOG_A, rules)
    pb = parse_cabrillo(DEMO_LOG_B, rules)
    pc = parse_cabrillo(DEMO_LOG_C, rules)
    st.add_log("LA", bid, "BG1AAA.log", DEMO_LOG_A, pa)
    st.add_log("LB", bid, "BG2BBB.log", DEMO_LOG_B, pb)
    st.add_log("LC", bid, "BG3CCC.log", DEMO_LOG_C, pc)
    computed = adjudicate(rules, st.get_submissions(bid))["findings"]
    st.replace_findings(bid, apply_decisions(rules, computed, {}))
    raw = st.list_findings(bid)
    annotated = apply_decisions(rules, raw, {})
    results = score(rules, annotated)
    digest = content_hash(rules, annotated, {}, results)
    vno = st.next_version_no(bid)
    st.save_version(bid, vno, digest, "v1", {
        "batch_id": bid, "version_no": vno, "content_hash": digest,
        "batch_name": "appeal-unit", "rules": rules, "decisions": {},
        "findings": annotated, "results": results,
        "logs": [{"log_id": s["log_id"], "filename": s["filename"],
                  "station_call": s["station_call"],
                  "upload_ts": s["upload_ts"]}
                 for s in st.get_submissions(bid)],
        "clock_scheme": None, "created_ts": 1})
    version = st.get_version(bid, vno)
    submissions = st.get_submissions(bid)
    from cabrillo_judge.feedback import build_station_report
    report = build_station_report(
        batch=b, version=version, station="BG1AAA",
        logs=[s for s in submissions if s["station_call"] == "BG1AAA"])
    fid = next(f["id"] for f in annotated if f["status"] == "EXCHANGE_DIFF")
    claims = [{"id": "CL-1", "subject": "pairing_status",
               "summary": "s", "finding_id": fid,
               "log_refs": [{"filename": "BG1AAA.log", "line": 14}]}]
    probs = validate_claims(
        claims, station="BG1AAA", report=report,
        version_snapshot=version["snapshot"])
    check("复议领域：合法引用无问题", probs == [[]], str(probs))
    h1 = case_content_hash(
        report_id="R1", station="BG1AAA", version_no=1,
        version_content_hash=digest,
        report_content_hash="h" * 64, claims=claims)
    h2 = case_content_hash(
        report_id="R1", station="BG1AAA", version_no=1,
        version_content_hash=digest,
        report_content_hash="h" * 64, claims=claims)
    h3 = case_content_hash(
        report_id="R1", station="BG1AAA", version_no=1,
        version_content_hash=digest,
        report_content_hash="x" * 64, claims=claims)
    check("复议领域：绑定哈希稳定且随内容变化", h1 == h2 and h1 != h3)
    rulings = [{"claim_id": "CL-1", "conclusion": "revised",
                "resolution": "CONFIRMED", "fault_station": "BG1AAA",
                "penalty_code": "BAD_EXCHANGE", "rationale": "r"}]
    vr = validate_rulings(
        claims, rulings, rules=rules,
        findings_index={f["id"]: f for f in annotated})
    check("复议领域：改判结论形式合法", vr == {"CL-1": []}, str(vr))
    outcome = apply_rulings(
        rules=rules, station="BG1AAA", claims=claims,
        rulings=rulings, snapshot=version["snapshot"])
    check("复议领域：改判应用后 BG1AAA 1→0 分",
          outcome["station_score"] == {"station": "BG1AAA", "before": 1,
                                       "after": 0, "delta": -1}
          and outcome["revision_count"] == 1
          and len(outcome_scorecard_diff(outcome)) == 3)
    check("复议领域：预览摘要对输入顺序不敏感",
          preview_digest(rulings) == preview_digest(list(reversed(rulings))))
    check("复议领域：finding_touches_station 覆盖单边证据",
          finding_touches_station(
              next(f for f in annotated if f["status"] == "DUP"),
              "BG1AAA") is True
          and finding_touches_station(
              next(f for f in annotated if f["status"] == "DUP"),
              "BG9ZZZ") is False)
    st.close()

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部通过。")
