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

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部通过。")
