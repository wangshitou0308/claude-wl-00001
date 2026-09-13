"""赛后复议案件：领域逻辑（纯函数，不接触 sqlite/http）。

案件记录台站对**已发布反馈包**提出的异议，生命周期为
``submitted → in_review → closed``（未受理可 ``withdrawn``）。创建案件时
绑定反馈包、计分版本与各自的内容哈希，并冻结**绑定快照**（创建时刻的
全部争议项与引用）。

争议项（claim）可针对：

* ``log_line``       —— 原日志行（定位到 文件名:行号）
* ``pairing_status`` —— 配对状态
* ``exchange_diff``  —— 交换差异
* ``penalty``        —— 罚分
* ``summary_score``  —— 汇总分

每项可引用现有 finding（``finding_id``）与本台日志行（``log_refs``）。
引用校验遵循三条硬规则（不自动改绑）：

1. finding/日志不属于该台站 → ``CLAIM_STATION_MISMATCH``；
2. 目标未出现在被异议反馈包 → ``TARGET_NOT_IN_REPORT``；
3. 反馈包已有后续更正（已被更正包替代）→ 由接口层返回
   ``REPORT_SUPERSEDED``。

裁判逐项作出 ``upheld``（维持）/ ``revised``（改判）/
``insufficient``（证据不足）结论；改判项携带一条与普通裁决同口径的
新裁决（见 :func:`cabrillo_judge.engine.validate_judge_decision`）。
:func:`apply_rulings` 在**绑定版本快照**上推导裁决变更与计分预览，
确认时接口层会再次核对全部引用与绑定哈希，任一项失效则整案不写入。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .engine import apply_decisions, score, validate_judge_decision

# 案件状态
CASE_STATUSES = ("submitted", "in_review", "withdrawn", "closed")

# 争议对象
SUBJECTS = ("log_line", "pairing_status", "exchange_diff",
            "penalty", "summary_score")
SUBJECT_LABELS = {
    "log_line": "原日志行",
    "pairing_status": "配对状态",
    "exchange_diff": "交换差异",
    "penalty": "罚分",
    "summary_score": "汇总分",
}

# 裁判逐项结论
CONCLUSIONS = ("upheld", "revised", "insufficient")
CONCLUSION_LABELS = {
    "upheld": "维持",
    "revised": "改判",
    "insufficient": "证据不足",
}


# ---------------------------------------------------------------------------
# 内容哈希
# ---------------------------------------------------------------------------

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# 绑定指纹只取影响裁决依据的字段（不含 updated_ts 等元数据）
_FINGERPRINT_DECISION_KEYS = ("resolution", "fault_station",
                              "penalty_code", "reason", "judge")


def binding_fingerprint(finding_ids: list[str],
                        decisions: dict[str, dict[str, Any]]) -> str:
    """当前配对证据集与现行裁决的指纹；预览生成与确认时必须一致。

    证据集合变化（增删/状态改变导致 finding id 变化）或任一条现行裁决
    变化都会改变指纹——此时服务端此前生成的预览即告失效。
    """
    payload = {
        "findings": sorted(finding_ids),
        "decisions": {
            fid: {k: (decisions.get(fid) or {}).get(k)
                  for k in _FINGERPRINT_DECISION_KEYS}
            for fid in sorted(finding_ids)},
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def case_content_hash(*, report_id: str, station: str, version_no: int,
                      version_content_hash: str, report_content_hash: str,
                      claims: list[dict[str, Any]]) -> str:
    """案件绑定哈希：反馈包/计分版本/内容哈希 + 全部争议项与引用。

    确认时重新计算并与案件创建时的快照比对（连同数据库中反馈包、
    计分版本当前哈希一起核对），任一不一致则整案不写入。
    """
    slim_claims = [{
        "seq": i + 1,
        "subject": c.get("subject"),
        "summary": c.get("summary"),
        "finding_id": c.get("finding_id"),
        "log_refs": sorted(
            [f"{r.get('filename')}:{r.get('line')}"
             for r in (c.get("log_refs") or [])]),
    } for i, c in enumerate(claims)]
    payload = {"report_id": report_id, "station": station,
               "version_no": version_no,
               "version_content_hash": version_content_hash,
               "report_content_hash": report_content_hash,
               "claims": slim_claims}
    return hashlib.sha256(_canonical(payload)).hexdigest()


# ---------------------------------------------------------------------------
# 报告/快照索引与引用校验
# ---------------------------------------------------------------------------

def report_index(report: dict[str, Any]) -> dict[str, Any]:
    """从反馈包内部稿构建引用索引。"""
    entries = report.get("entries") or []
    entries_by_line: dict[tuple[str, int], dict[str, Any]] = {}
    finding_entries: dict[str, list[dict[str, Any]]] = {}
    findings_in_report: set[str] = set()
    own_filenames = {l.get("filename") for l in report.get("logs") or []}
    for e in entries:
        entries_by_line[(e.get("filename"), e.get("line"))] = e
        fid = e.get("finding_id")
        if fid:
            finding_entries.setdefault(fid, []).append(e)
            findings_in_report.add(fid)
        for other in e.get("also_in") or []:
            findings_in_report.add(other)
    for u in report.get("unassociated_evidence") or []:
        if u.get("finding_id"):
            findings_in_report.add(u["finding_id"])
    return {
        "entries": entries,
        "entries_by_line": entries_by_line,
        "finding_entries": finding_entries,
        "findings_in_report": findings_in_report,
        "own_filenames": own_filenames,
    }


def snapshot_finding_index(version_snapshot: dict[str, Any]
                           ) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in (version_snapshot.get("findings") or [])}


def finding_touches_station(finding: dict[str, Any], station: str) -> bool:
    if station in (finding.get("stations") or []):
        return True
    return any(r.get("station") == station
               for r in finding.get("refs") or [])


def validate_claims(claims: list[dict[str, Any]], *, station: str,
                    report: dict[str, Any],
                    version_snapshot: dict[str, Any]
                    ) -> list[dict[str, Any]]:
    """逐争议项校验引用；返回每项的问题清单（空列表表示合法）。

    问题项形如 ``{"code": ..., "message": ...}``。**只提示、不自动改绑**。
    """
    idx = report_index(report)
    findings = snapshot_finding_index(version_snapshot)
    problems: list[dict[str, Any]] = []
    seen_targets: set[str] = set()

    for n, c in enumerate(claims, start=1):
        item_problems: list[dict[str, Any]] = []
        subject = c.get("subject")
        if subject not in SUBJECTS:
            item_problems.append({
                "code": "BAD_SUBJECT",
                "message": f"第 {n} 项 subject 必须是 {list(SUBJECTS)} 之一"})
            problems.append(item_problems)
            continue
        if not str(c.get("summary") or "").strip():
            item_problems.append({
                "code": "CLAIM_SUMMARY_REQUIRED",
                "message": f"第 {n} 项须填写异议主张 summary"})

        # -- finding 引用：存在性 → 台站归属 → 是否出现在报告 ----------
        fid = c.get("finding_id")
        finding = None
        if fid:
            finding = findings.get(fid)
            if finding is None:
                item_problems.append({
                    "code": "CLAIM_FINDING_NOT_FOUND",
                    "message": f"第 {n} 项引用的证据 {fid} 不在绑定计分版本"
                               f" v{report.get('version_no')} 的快照中，"
                               f"不自动改绑到其他证据"})
            else:
                if not finding_touches_station(finding, station):
                    item_problems.append({
                        "code": "CLAIM_STATION_MISMATCH",
                        "message": f"第 {n} 项引用的证据 {fid} 不属于台站 "
                                   f"{station}（涉及台站："
                                   f"{finding.get('stations')}），"
                                   f"不自动改绑"})
                if fid not in idx["findings_in_report"]:
                    item_problems.append({
                        "code": "TARGET_NOT_IN_REPORT",
                        "message": f"第 {n} 项引用的证据 {fid} "
                                   f"（{finding.get('status')}）未出现在被"
                                   f"异议反馈包中，不自动改绑"})
                if fid in seen_targets:
                    item_problems.append({
                        "code": "CLAIM_DUPLICATE_TARGET",
                        "message": f"证据 {fid} 已被另一争议项引用，"
                                   f"同一案件不得重复主张同一目标"})
                seen_targets.add(fid)

        # -- 日志行引用：必须是报告中本台自己的行 ----------------------
        log_refs = c.get("log_refs") or []
        if not isinstance(log_refs, list):
            item_problems.append({
                "code": "BAD_LOG_REFS",
                "message": f"第 {n} 项 log_refs 必须是数组"})
            log_refs = []
        for ref in log_refs:
            fn, line = ref.get("filename"), ref.get("line")
            if fn not in idx["own_filenames"]:
                item_problems.append({
                    "code": "CLAIM_STATION_MISMATCH",
                    "message": f"第 {n} 项引用的日志 {fn!r} 不属于台站 "
                               f"{station}（本包日志："
                               f"{sorted(x for x in idx['own_filenames'] if x)}）"
                               f"，不自动改绑"})
                continue
            try:
                line_i = int(line)
            except (TypeError, ValueError):
                item_problems.append({
                    "code": "BAD_LOG_REF",
                    "message": f"第 {n} 项日志引用行号必须是整数：{line!r}"})
                continue
            if (fn, line_i) not in idx["entries_by_line"]:
                item_problems.append({
                    "code": "TARGET_NOT_IN_REPORT",
                    "message": f"第 {n} 项引用的 {fn}:{line_i} "
                               f"未出现在被异议反馈包中，不自动改绑"})

        # -- 各类主张的目标要求 ----------------------------------------
        if subject in ("pairing_status", "exchange_diff") and not fid:
            item_problems.append({
                "code": "CLAIM_TARGET_REQUIRED",
                "message": f"第 {n} 项（{SUBJECT_LABELS[subject]}）必须通过 "
                           f"finding_id 引用目标证据"})
        if subject == "penalty":
            penalised_entries = [
                e for e in idx["finding_entries"].get(fid, [])
                if int(e.get("penalty_points") or 0) > 0]
            penalised_refs = []
            for r in log_refs:
                e = idx["entries_by_line"].get(
                    (r.get("filename"), r.get("line")))
                if e and int(e.get("penalty_points") or 0) > 0:
                    penalised_refs.append(e)
            if not penalised_entries and not penalised_refs:
                item_problems.append({
                    "code": "TARGET_NO_PENALTY",
                    "message": f"第 {n} 项主张罚分争议，但引用目标在反馈包"
                               f"中没有罚分记录，不自动改绑"})
        if subject == "log_line" and not log_refs:
            item_problems.append({
                "code": "CLAIM_TARGET_REQUIRED",
                "message": f"第 {n} 项（原日志行）必须通过 log_refs 引用"
                           f" 文件名+行号"})

        problems.append(item_problems)
    return problems


# ---------------------------------------------------------------------------
# 改判应用与计分预览（在绑定版本快照上推导）
# ---------------------------------------------------------------------------

def _card(results: dict[str, Any], station: str) -> dict[str, Any]:
    return next((c for c in results.get("scorecards", [])
                 if c.get("station") == station), None) or {}


def validate_rulings(claims: list[dict[str, Any]],
                     rulings: list[dict[str, Any]], *,
                     rules: dict[str, Any],
                     findings_index: dict[str, dict[str, Any]]
                     ) -> dict[str, list[dict[str, str]]]:
    """校验裁判逐项结论的形式；返回 ``{claim_id: [问题]}``（不含的项亦缺结论）。"""
    by_id = {c["id"]: c for c in claims}
    out: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for r in rulings or []:
        cid = r.get("claim_id")
        c = by_id.get(cid)
        if c is None:
            out.setdefault(f"__ruling_{cid}", []).append(
                {"code": "RULING_CLAIM_NOT_FOUND",
                 "message": f"结论引用的争议项 {cid} 不属于本案件"})
            continue
        seen.add(cid)
        probs: list[dict[str, str]] = []
        conclusion = r.get("conclusion")
        if conclusion not in CONCLUSIONS:
            probs.append({"code": "BAD_CONCLUSION",
                          "message": f"争议项 {cid} 的 conclusion 必须是 "
                                     f"{list(CONCLUSIONS)} 之一"})
        rationale = str(r.get("rationale") or r.get("reason") or "")
        if not rationale.strip():
            probs.append({"code": "RATIONALE_REQUIRED",
                          "message": f"争议项 {cid} 的处理意见 rationale 必填"})
        if conclusion == "revised":
            resolution = str(r.get("resolution") or "").upper()
            fid = c.get("finding_id")
            if not fid:
                probs.append({"code": "REVISION_TARGET_REQUIRED",
                              "message": f"争议项 {cid} 改判必须引用目标证据"
                                         f"（finding_id）"})
            else:
                finding = findings_index.get(fid)
                if finding is None:
                    probs.append({"code": "CLAIM_FINDING_NOT_FOUND",
                                  "message": f"争议项 {cid} 的目标证据 "
                                             f"{fid} 不在绑定快照中"})
                else:
                    for p in validate_judge_decision(
                            rules, finding, resolution=resolution,
                            reason=rationale,
                            fault_station=r.get("fault_station"),
                            penalty_code=r.get("penalty_code")):
                        probs.append({"code": "BAD_DECISION", "message":
                                      f"争议项 {cid}：{p}"})
        elif conclusion in ("upheld", "insufficient"):
            for k in ("resolution", "fault_station", "penalty_code"):
                if r.get(k):
                    probs.append({"code": "UNEXPECTED_DECISION_FIELDS",
                                  "message": f"争议项 {cid} 结论为 "
                                             f"{CONCLUSION_LABELS[conclusion]}，"
                                             f"不应携带 {k}"})
        out[cid] = probs
    for cid in by_id:
        if cid not in seen:
            out.setdefault(cid, []).append(
                {"code": "RULING_REQUIRED",
                 "message": f"争议项 {cid} 尚未给出逐项结论"})
    return out


def apply_rulings(*, rules: dict[str, Any], station: str,
                  claims: list[dict[str, Any]],
                  rulings: list[dict[str, Any]],
                  snapshot: dict[str, Any]) -> dict[str, Any]:
    """在绑定快照上应用改判，返回逐项效果、新裁决集与计分预览。

    调用前应先经 :func:`validate_rulings` 形式校验通过。
    维持/证据不足不改动裁决；改判项按 finding_id 覆盖绑定快照中的裁决。
    """
    bound_findings = snapshot.get("findings") or []
    bound_decisions = dict(snapshot.get("decisions") or {})
    findings_index = {f["id"]: f for f in bound_findings}

    before_annotated = apply_decisions(rules, bound_findings, bound_decisions)
    before_results = score(rules, before_annotated)
    before_by_id = {f["id"]: f for f in before_annotated}

    rulings_by_claim = {r["claim_id"]: r for r in rulings}
    new_decisions = {k: dict(v) for k, v in bound_decisions.items()}
    claim_items: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for c in claims:
        r = rulings_by_claim.get(c["id"], {})
        conclusion = r.get("conclusion")
        decision: dict[str, Any] | None = None
        fid = c.get("finding_id")
        if conclusion == "revised" and fid:
            decision = {
                "resolution": str(r.get("resolution") or "").upper(),
                "fault_station": (str(r.get("fault_station")).upper()
                                  if r.get("fault_station") else None),
                "penalty_code": r.get("penalty_code"),
                "reason": str(r.get("rationale") or "").strip(),
                "judge": r.get("judge"),
            }
            new_decisions[fid] = decision
            changed.append({"claim_id": c["id"], "finding_id": fid,
                            "from": (bound_decisions.get(fid) or {}).get(
                                "resolution"),
                            "to": decision["resolution"],
                            "decision": decision})

        before_eff = after_eff = None
        if fid and fid in before_by_id:
            before_eff = before_by_id[fid]["effects"].get(station)
        claim_items.append({
            "claim_id": c["id"], "seq": c.get("seq"),
            "subject": c["subject"],
            "subject_label": SUBJECT_LABELS[c["subject"]],
            "summary": c.get("summary"),
            "finding_id": fid,
            "log_refs": c.get("log_refs") or [],
            "conclusion": conclusion,
            "conclusion_label": CONCLUSION_LABELS.get(conclusion),
            "rationale": str(r.get("rationale") or "").strip() or None,
            "judge": r.get("judge"),
            "decision": decision,
            "before": ({"counted": before_eff.get("counted"),
                        "penalty_codes": before_eff.get("penalty_codes"),
                        "penalty_points": before_eff.get("penalty_points")}
                       if before_eff is not None else None),
            # after 由下方二次打分后回填
        })

    after_annotated = apply_decisions(rules, bound_findings, new_decisions)
    after_results = score(rules, after_annotated)
    after_by_id = {f["id"]: f for f in after_annotated}
    for item in claim_items:
        fid = item["finding_id"]
        if fid and fid in after_by_id:
            eff = after_by_id[fid]["effects"].get(station)
            item["after"] = ({"counted": eff.get("counted"),
                              "penalty_codes": eff.get("penalty_codes"),
                              "penalty_points": eff.get("penalty_points")}
                             if eff is not None else None)
        else:
            item["after"] = None

    cb, ca = _card(before_results, station), _card(after_results, station)
    station_before = int(cb.get("total_score", 0))
    station_after = int(ca.get("total_score", 0))
    return {
        "items": claim_items,
        "decisions": new_decisions,
        "annotated_findings": after_annotated,
        "results_before": before_results,
        "results_after": after_results,
        "changed_decisions": changed,
        "station_score": {"station": station,
                          "before": station_before,
                          "after": station_after,
                          "delta": station_after - station_before},
        "score_changed": station_after != station_before,
        "revision_count": len(changed),
    }


def _scorecard_rows(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["station"]: c for c in results.get("scorecards", [])}


def outcome_scorecard_diff(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """预览中各台站的计分变化（改判可能影响他台乘数等）。"""
    a, b = (_scorecard_rows(outcome["results_before"]),
            _scorecard_rows(outcome["results_after"]))
    rows = []
    for st in sorted(set(a) | set(b)):
        x, y = a.get(st, {}), b.get(st, {})
        rows.append({
            "station": st,
            "qso_counted": {"before": x.get("qso_counted", 0),
                            "after": y.get("qso_counted", 0)},
            "multiplier_total": {"before": x.get("multiplier_total", 1),
                                 "after": y.get("multiplier_total", 1)},
            "penalty_points": {"before": x.get("penalty_points", 0),
                               "after": y.get("penalty_points", 0)},
            "total_score": {"before": x.get("total_score", 0),
                            "after": y.get("total_score", 0),
                            "delta": int(y.get("total_score", 0))
                                     - int(x.get("total_score", 0))}})
    return rows


def preview_digest(rulings: list[dict[str, Any]]) -> str:
    """处理预览的确定性摘要；确认时须原样回传，防止凭陈旧预览确认。"""
    slim = [
        {"claim_id": r.get("claim_id"),
         "conclusion": r.get("conclusion"),
         "resolution": (str(r.get("resolution") or "").upper() or None),
         "fault_station": r.get("fault_station"),
         "penalty_code": r.get("penalty_code"),
         "rationale": str(r.get("rationale") or r.get("reason") or "").strip()}
        for r in rulings]
    slim.sort(key=lambda x: str(x.get("claim_id")))
    return hashlib.sha256(_canonical(slim)).hexdigest()
