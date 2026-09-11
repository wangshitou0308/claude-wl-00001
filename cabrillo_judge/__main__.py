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
CLAIMED-SCORE: 6
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0111 BG1AAA 599 001 BG2BBB 599 001
QSO: 7023 CW 2026-09-10 0123 BG1AAA 599 002 BG3CCC 599 010
QSO: 14023 CW 2026-09-10 0140 BG1AAA 599 003 BG2BBB 559 003
QSO: 14023 CW 2026-09-10 0205 BG1AAA 599 004 BG4DDD 599 004
QSO: 7023 CW 2026-09-10 0230 BG1AAA 599 005 BG2BBB 599 005
QSO: 7023 CW 2026-09-10 0231 BG1AAA 599 006 BG2BBB 599 006
END-OF-LOG:
"""

# BG2BBB: #001 matches cleanly; #003 exchange mismatch (A 抄收 559 vs B 实发
# 599)；#005 时间漂移 14 分钟（超 5 分钟容差、在 30 分钟近邻窗口内）；
# BG3CCC 交了日志但没记与 BG1AAA 的通联 -> UNIQUE；
# BG4DDD 根本没交日志 -> NO_PARTNER_LOG；A 的 006 与 005 相隔 1 分钟 -> DUP。
DEMO_LOG_B = """START-OF-LOG: 3.0
CALLSIGN: BG2BBB
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: HIGH
GRID-LOCATOR: OM90
NAME: 示例乙台
CLAIMED-SCORE: 4
CREATED-BY: hand-written
QSO: 7023 CW 2026-09-10 0111 BG2BBB 599 001 BG1AAA 599 001
QSO: 14023 CW 2026-09-10 0140 BG2BBB 599 003 BG1AAA 599 003
QSO: 7023 CW 2026-09-10 0214 BG2BBB 599 005 BG1AAA 599 005
END-OF-LOG:
"""

# BG3CCC 提交了日志（X-QSO 表示台方自己标注不计分的记录），但其中没有
# 与 BG1AAA 在 0123 的通联 -> BG1AAA 的该条成为单方记录 UNIQUE。
DEMO_LOG_C = """START-OF-LOG: 3.0
CALLSIGN: BG3CCC
CONTEST: DEMO-CW
CATEGORY-MODE: CW
CATEGORY-BANDS: ALL
CATEGORY-OPERATOR: SINGLE-OP
CATEGORY-POWER: QRP
GRID-LOCATOR: PN01
NAME: 示例丙台
CLAIMED-SCORE: 0
CREATED-BY: hand-written
X-QSO: 3523 CW 2026-09-10 0302 BG3CCC 579 001 BG9ZZZ 579 001
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
    if args.demo or args.reset_demo:
        bid = seed_demo(server.storage, reset=args.reset_demo)  # type: ignore[attr-defined]

    print("=" * 64)
    print(f"{DOC_TITLE}")
    print(f"监听地址  : http://{args.host}:{args.port}")
    print(f"数据库    : {os.path.abspath(args.db)}")
    print(f"首页/文档 : http://{args.host}:{args.port}/  (JSON: /api/docs)")
    if bid:
        print(f"示例赛事  : 批次 {bid}")
        print(f"  GET http://{args.host}:{args.port}/api/batches/{bid}/disputes")
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
