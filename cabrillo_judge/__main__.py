"""Entry point: ``python -m cabrillo_judge``.

Options:
    --host HOST    default 127.0.0.1 (offline loopback; use 0.0.0.0 deliberately)
    --port PORT    default 8080
    --db PATH      SQLite file, default cabrillo_judge.db
    --demo         seed an example contest with two Cabrillo logs
    --reset-demo   wipe the DB before seeding the demo
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from .parser import parse_cabrillo
from .rules import default_rules
from .storage import Storage, new_id
from .web import make_server, DOC_TITLE


DEMO_LOG_A = """START-OF-LOG: 3.0
CALLSIGN: BG1AAA
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
GRID-LOCATOR: OM89
NAME: 示例甲台
CLAIMED-SCORE: 7
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0111 BG1AAA 599 001 BG2BBB 599 001
QSO: 7023 CW 2026-09-10 0123 BG1AAA 599 002 BG3CCC 599 010
QSO: 14023 CW 2026-09-10 0140 BG1AAA 599 003 BG2BBB 559 003
QSO: 14023 CW 2026-09-10 0205 BG1AAA 599 004 BG4DDD 599 004
QSO: 7023 CW 2026-09-10 0230 BG1AAA 599 005 BG2BBB 599 005
QSO: 7023 CW 2026-09-10 0231 BG1AAA 599 006 BG2BBB 599 006
QSO: 3523 CW 2026-09-10 0302 BG1AAA 579 007 BG3CCC 579 001
END-OF-LOG:
"""

# BG2BBB: #001 干净配对；#003 交换不一致（A 抄收 559，B 实发 599）；
# #005 时间漂移 16 分钟（超 5 分钟容差、在 30 分钟近邻窗口内）。
# BG3CCC 交了日志但没记与 BG1AAA 的 #002 通联 -> UNIQUE；
# BG4DDD 根本没交日志 -> NO_PARTNER_LOG；A 的 #006 与 #005 相隔 1 分钟 -> DUP；
# 0302 与 BG3CCC 的 80m 通联中，C 把 BG1AAA 抄成 BG1AAB（错一字母）
# -> SUSPECT_CALL，需裁判确认。
DEMO_LOG_B = """START-OF-LOG: 3.0
CALLSIGN: BG2BBB
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: HIGH
GRID-LOCATOR: OM90
NAME: 示例乙台
CLAIMED-SCORE: 3
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0111 BG2BBB 599 001 BG1AAA 599 001
QSO: 14023 CW 2026-09-10 0140 BG2BBB 599 003 BG1AAA 599 003
QSO: 7023 CW 2026-09-10 0214 BG2BBB 599 005 BG1AAA 599 005
END-OF-LOG:
"""

# BG3CCC：QSO 行把 BG1AAA 错抄成 BG1AAB（仅末位不同），频段/模式/时间/交换
# 都对得上 -> SUSPECT_CALL；X-QSO 是台方自行标注不计分的记录，永不配对。
DEMO_LOG_C = """START-OF-LOG: 3.0
CALLSIGN: BG3CCC
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: QRP
GRID-LOCATOR: PN01
NAME: 示例丙台
CLAIMED-SCORE: 1
CREATED-BY: hand-written
QSO: 3523 CW 2026-09-10 0302 BG3CCC 579 001 BG1AAB 579 007
X-QSO: 3523 CW 2026-09-10 0330 BG3CCC 579 002 BG9ZZZ 579 001
END-OF-LOG:
"""


# ---------------------------------------------------------------------------
# 时钟偏差示例（独立批次）：BG2BBB 的钟快 6 分钟、BG3CCC 的钟慢 8 分钟
# （均相对 BG1AAA），BG4DDD 只有 1 条互指 QSO（样本不足，只列证据不建议）。
# 未校正时 A↔B、A↔C 全部落入 TIME_DRIFT；按建议整分钟校正后全部变为 MATCH。
# ---------------------------------------------------------------------------
SKEW_LOG_REF = """START-OF-LOG: 3.0
CALLSIGN: BG1AAA
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
NAME: 参考台（时钟准确）
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0110 BG1AAA 599 001 BG2BBB 599 001
QSO: 7023 CW 2026-09-10 0120 BG1AAA 599 002 BG3CCC 599 001
QSO: 7023 CW 2026-09-10 0130 BG1AAA 599 003 BG4DDD 599 001
QSO: 7023 CW 2026-09-10 0150 BG1AAA 599 004 BG2BBB 599 002
QSO: 7023 CW 2026-09-10 0200 BG1AAA 599 005 BG3CCC 599 002
QSO: 7023 CW 2026-09-10 0230 BG1AAA 599 006 BG2BBB 599 003
QSO: 7023 CW 2026-09-10 0240 BG1AAA 599 007 BG3CCC 599 003
QSO: 7023 CW 2026-09-10 0310 BG1AAA 599 008 BG2BBB 599 004
END-OF-LOG:
"""

SKEW_LOG_FAST = """START-OF-LOG: 3.0
CALLSIGN: BG2BBB
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
NAME: 快 6 分钟的台
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0116 BG2BBB 599 001 BG1AAA 599 001
QSO: 7023 CW 2026-09-10 0156 BG2BBB 599 002 BG1AAA 599 004
QSO: 7023 CW 2026-09-10 0236 BG2BBB 599 003 BG1AAA 599 006
QSO: 7023 CW 2026-09-10 0316 BG2BBB 599 004 BG1AAA 599 008
END-OF-LOG:
"""

SKEW_LOG_SLOW = """START-OF-LOG: 3.0
CALLSIGN: BG3CCC
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
NAME: 慢 8 分钟的台
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0112 BG3CCC 599 001 BG1AAA 599 002
QSO: 7023 CW 2026-09-10 0152 BG3CCC 599 002 BG1AAA 599 005
QSO: 7023 CW 2026-09-10 0232 BG3CCC 599 003 BG1AAA 599 007
END-OF-LOG:
"""

SKEW_LOG_THIN = """START-OF-LOG: 3.0
CALLSIGN: BG4DDD
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: LOW
NAME: 样本不足的台
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0130 BG4DDD 599 001 BG1AAA 599 003
END-OF-LOG:
"""


def seed_demo(storage: Storage, reset: bool = False) -> str:
    existing = storage.list_batches()
    if not reset:
        for b in existing:
            if b["name"] == "示例赛事 DEMO-CW":
                return b["id"]
    rules = default_rules()
    batch = storage.create_batch(new_id("B"), "示例赛事 DEMO-CW", rules)
    bid = batch["id"]
    for filename, text in (("BG1AAA.log", DEMO_LOG_A),
                           ("BG2BBB.log", DEMO_LOG_B),
                           ("BG3CCC.log", DEMO_LOG_C)):
        parsed = parse_cabrillo(text, rules)
        storage.add_log(new_id("L"), bid, filename, text, parsed)

    # Run the same cross-log pass the upload endpoint performs.
    from .engine import adjudicate, apply_decisions
    submissions = storage.get_submissions(bid)
    findings = adjudicate(rules, submissions)["findings"]
    findings = apply_decisions(rules, findings, storage.list_decisions(bid))
    storage.replace_findings(bid, findings)
    return bid


def seed_clock_demo(storage: Storage) -> str:
    """准备时钟偏差示例批次（不存在则创建）。"""
    for b in storage.list_batches():
        if b["name"] == "示例赛事 CLOCK-SKEW 时钟偏差":
            return b["id"]
    rules = default_rules()
    batch = storage.create_batch(new_id("B"), "示例赛事 CLOCK-SKEW 时钟偏差",
                                 rules)
    bid = batch["id"]
    for filename, text in (("skew-BG1AAA.log", SKEW_LOG_REF),
                           ("skew-BG2BBB.log", SKEW_LOG_FAST),
                           ("skew-BG3CCC.log", SKEW_LOG_SLOW),
                           ("skew-BG4DDD.log", SKEW_LOG_THIN)):
        parsed = parse_cabrillo(text, rules)
        storage.add_log(new_id("L"), bid, filename, text, parsed)

    from .engine import adjudicate, apply_decisions
    submissions = storage.get_submissions(bid)
    findings = adjudicate(rules, submissions)["findings"]
    findings = apply_decisions(rules, findings, storage.list_decisions(bid))
    storage.replace_findings(bid, findings)
    return bid


def seed_feedback_demo(storage: Storage, bid: str) -> bool:
    """为示例赛事冻结计分版本 1 并生成站级赛后反馈包示例。

    每台一份草稿；BG1AAA 的包走完整流程（预览 -> 发布）作为示范。
    已存在版本时跳过（幂等）。
    """
    if storage.list_versions(bid):
        return False
    from .engine import adjudicate, apply_decisions, content_hash, score
    from .feedback import (build_external_report, build_station_report,
                           report_content_hash)
    batch = storage.get_batch(bid)
    submissions = storage.get_submissions(bid)
    raw = adjudicate(batch["rules"], submissions)["findings"]
    decisions = storage.list_decisions(bid)
    annotated = apply_decisions(batch["rules"], raw, decisions)
    results = score(batch["rules"], annotated)
    digest = content_hash(batch["rules"], annotated, decisions, results)
    snapshot = {
        "batch_id": bid, "version_no": 1, "content_hash": digest,
        "batch_name": batch["name"], "rules": batch["rules"],
        "decisions": decisions, "findings": annotated,
        "results": results,
        "logs": [{"log_id": s["log_id"], "filename": s["filename"],
                  "station_call": s["station_call"],
                  "upload_ts": s["upload_ts"]} for s in submissions],
        "clock_scheme": None,
        "created_ts": int(time.time()),
    }
    storage.save_version(bid, 1, digest, "示例冻结版本（反馈包演示）", snapshot)
    version = storage.get_version(bid, 1)
    for st in sorted({s["station_call"] for s in submissions
                      if s["station_call"]}):
        logs = [s for s in submissions if s["station_call"] == st]
        report = build_station_report(batch=batch, version=version,
                                      station=st, logs=logs)
        rid = new_id("R")
        storage.create_feedback_report(
            rid, bid, st, 1, "normal", None, None,
            report_content_hash(report), report)
        if st == "BG1AAA":
            storage.set_feedback_external(
                rid, build_external_report(report))
            storage.set_feedback_published(rid)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m cabrillo_judge",
        description=f"{DOC_TITLE}（完全离线，标准库实现）")
    ap.add_argument("--host", default=os.environ.get("CJ_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CJ_PORT", "8080")))
    ap.add_argument("--db", default=os.environ.get("CJ_DB",
                                                   "cabrillo_judge.db"))
    ap.add_argument("--demo", action="store_true",
                    help="启动时准备示例赛事（不存在则创建）")
    ap.add_argument("--reset-demo", action="store_true",
                    help="删除现有数据库后重建示例赛事")
    args = ap.parse_args(argv)

    if args.reset_demo and os.path.exists(args.db):
        os.remove(args.db)

    server = make_server(args.host, args.port, args.db)
    bid = None
    skew_bid = None
    feedback_seeded = False
    if args.demo or args.reset_demo:
        bid = seed_demo(server.storage, reset=args.reset_demo)  # type: ignore[attr-defined]
        skew_bid = seed_clock_demo(server.storage)  # type: ignore[attr-defined]
        feedback_seeded = seed_feedback_demo(server.storage, bid)  # type: ignore[attr-defined]

    print("=" * 64)
    print(f"{DOC_TITLE}")
    print(f"监听地址  : http://{args.host}:{args.port}")
    print(f"数据库    : {os.path.abspath(args.db)}")
    print(f"首页/文档 : http://{args.host}:{args.port}/  (JSON: /api/docs)")
    if bid:
        print(f"示例赛事  : 批次 {bid}")
        print(f"  GET http://{args.host}:{args.port}/api/batches/{bid}/disputes")
        print(f"站级反馈包示例: GET http://{args.host}:{args.port}"
              f"/api/batches/{bid}/feedback")
        if feedback_seeded:
            print("  （已冻结计分版本 1；BG1AAA 的反馈包已预览并发布，"
                  "其余为草稿）")
    if skew_bid:
        print(f"时钟偏差示例: 批次 {skew_bid}")
        print(f"  GET http://{args.host}:{args.port}/api/batches/{skew_bid}"
              f"/clock-analysis?reference_log_id=<参考日志ID>")
    print("按 Ctrl+C 停止服务。")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在关闭……")
    finally:
        server.server_close()
        server.storage.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    sys.exit(main())
