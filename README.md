# Cabrillo 离线日志裁决 API

面向业余无线电竞赛裁判的**完全离线** Cabrillo 3.0 日志交叉核对与计分系统。

- **仅使用 Python 3.10+ 标准库**：`http.server` 提供 JSON 接口，`sqlite3` 存储数据。
- **不联网、不查呼号库**：仅凭批次内提交的日志做交叉核对。
- 保留每一份日志的**原始行**，所有校验错误/配对证据都定位到 `文件名:行号`。
- 仅凭现有日志不能唯一判定时一律标为**待裁决（pending）**，绝不借外部信息自动定错。

## 快速开始

```bash
# 启动并自动创建两个示例赛事：
#   1) 3 份日志，覆盖全部 7 种配对状态
#      （冻结计分版本 1 并生成站级反馈包：BG1AAA 已发布，其余为草稿；
#        另有一条针对 BG1AAA 已发布反馈包的赛后复议案件，submitted 待受理）
#   2) 4 份日志的时钟偏差示例（快 6 分钟 / 慢 8 分钟 / 样本不足）
python3 -m cabrillo_judge --reset-demo --demo --port 8080

# 浏览器/终端打开
#   http://127.0.0.1:8080/            人类可读 API 文档
#   http://127.0.0.1:8080/api/docs    机器可读接口说明
#   http://127.0.0.1:8080/api/health  健康检查
```

参数：`--host 127.0.0.1`（默认仅本机监听）、`--port 8080`、`--db cabrillo_judge.db`。
数据全部存于单个 SQLite 文件，可直接拷贝归档。

## 典型裁判流程

```bash
B=http://127.0.0.1:8080

# 1. 创建批次（不传 rules 即用内置示例规则；也可提交自定义规则，见下）
curl -s -X POST $B/api/batches -H 'Content-Type: application/json' \
  -d '{"name":"2026 秋季 CW 测试"}'

# 2. 一次上传多份 Cabrillo 3.0 日志（multipart，也支持 JSON）
curl -s -X POST $B/api/batches/$BID/logs \
  -F "files=@BG1AAA.log" -F "files=@BG2BBB.log"

# 3. 看待裁决争议（只列 pending 且尚未裁决的条目）
curl -s $B/api/batches/$BID/disputes

# 4. 对某条证据填写理由并裁决（reason 必填）
curl -s -X POST $B/api/batches/$BID/findings/$FID/decision \
  -H 'Content-Type: application/json' \
  -d '{"resolution":"CONFIRMED","fault_station":"BG1AAA",
       "penalty_code":"BAD_EXCHANGE","judge":"裁判甲",
       "reason":"BG1AAA 第14行抄收559，对方实发599，判其抄错，通联仍计分"}'

# 5. 看当前计分（可随时重复，结果由规则+日志+裁决确定性导出）
curl -s $B/api/batches/$BID/results

# 6. 生成可复现的计分版本（内容相同则返回相同 SHA-256，幂等不新增版本）
curl -s -X POST "$B/api/batches/$BID/versions?lock=1" \
  -H 'Content-Type: application/json' -d '{"note":"终版"}'

# 7. 比较两个版本（或某版本与当前结果）
curl -s "$B/api/batches/$BID/versions/diff?a=1&b=2"

# 8. 下载完整 JSON 归档（规则、原文、证据、裁决、结果、版本哈希）
curl -s $B/api/batches/$BID/download -o result.json
```

锁定后任何修改日志/规则/裁决的请求都会返回 `409 BATCH_LOCKED`；
需要复议时可 `POST /api/batches/{id}/lock` 传 `{"locked":false}` 解锁。

## 校验内容（parser）

每份日志逐项检查，并在上传响应与 `GET .../logs/{log_id}` 中返回问题清单：

| 类别 | 检查项 |
|---|---|
| 必填头 | `START-OF-LOG: 3.0` 首行、`CALLSIGN`、`CONTEST`、`CATEGORY-MODE/BANDS/OPERATOR/POWER`（可在规则中改） |
| 参赛类别 | 各 `CATEGORY-*` 取值必须在规则允许表内（默认采用 Cabrillo 3.0 词表） |
| 呼号 | 台站呼号与对方呼号的格式校验；支持 `EA4E/P`、`F/ON4XYZ`、`ON4XYZ/QRP` 等便携/前缀写法并归一化为基呼号 |
| 频率/频段 | 频率须为整数 kHz 且落在规则定义的频段区间内 |
| 模式 | 须在 `allowed_modes` 内（支持别名，如 `RY→CW`） |
| UTC 时间 | `YYYY-MM-DD HHMM` 可解析；可选竞赛窗口越界检查 |
| 交换字段 | 按规则中 `exchange_fields` 的顺序与类型（rst/integer/string/grid/callsign/enum）逐字段校验 |
| 其他 | `END-OF-LOG`、重复头、未知标签、QSO 字段数、X-QSO（永不参与配对计分）等 |

严重问题（缺头、坏频段、坏模式、坏时间、坏频率、越竞赛窗口、坏对方呼号、
交换字段不合法等）为 `error`；未知标签、缺 END 行等为 `warning`。
**任何一行只要出现 error 级字段问题，整行就进入 `invalid_qsos`（保留原始行
与全部错误码），不会进入 `qsos`/`xqsos`，因而绝不可能参与交叉配对或计分。**

## 配对状态与裁决动作

系统按**双方呼号 + 频段 + 模式 + 容差时间**交叉配对（全局稳定 1:1 匹配，
编辑距离用于呼号疑似抄错的模糊匹配）：

| 状态 | pending | 含义 | 默认计分 |
|---|---|---|---|
| `MATCH` | 否 | 频段/模式/容差时间一致，交换也一致 | 双方计分 |
| `EXCHANGE_DIFF` | **是** | 配对成立但交换字段不一致（不猜抄错方） | 不计分，待裁判判定 |
| `TIME_DRIFT` | **是** | 互有记录但时间差超容差、在近邻窗口内 | 不计分，待裁判判定 |
| `SUSPECT_CALL` | **是** | 呼号模糊匹配上的疑似抄错（附双方原抄收） | 不计分，待裁判判定 |
| `NO_PARTNER_LOG` | **是** | 对方在批次中未交日志，无法核实 | 不计分，待裁判判定 |
| `UNIQUE` | 否 | 对方交了日志但其中无此记录（单方记录） | 不计分；如规则 `auto_penalty_unique=true` 则自动扣 `NOT_IN_LOG` |
| `DUP` | 否 | 与本方较早同呼号/频段/模式记录相隔很近且未能交叉配对 | 不计分 |

裁决动作（**必须填写 `reason`**，接口会校验）：

- `CONFIRMED` —— 确认通联成立（用于三种待裁决的双方记录）；可指定
  `fault_station` 与 `penalty_code` 对抄错方罚分。
- `GRANTED` —— 认定单方/对方未交日志的记录有效计分。
- `WAIVED` —— 豁免：不计分且免除罚分。
- `REMOVED` —— 剔除记录。

## 批次级时钟偏差分析与校正方案

竞赛中常见某台计算机时钟快走/慢走几分钟，导致大量 `TIME_DRIFT` 待裁决。
本功能在**不改动任何原始 Cabrillo 文本与时间**的前提下，估计并按整分钟
校正这种系统性偏差。

### 1. 分析（只读）

```bash
curl -s "$B/api/batches/$BID/clock-analysis?reference_log_id=$REF&max_window_seconds=1800"
```

裁判选定**参考日志**（时钟可信的那一份）与**最大搜索窗口**后，系统：

1. 在任意两份日志间筛出**无歧义候选对**：呼号精确互指（不用模糊匹配）、
   频段与模式一致、时间差在窗口内，且双方在窗口内都只有彼此一个候选；
2. 按日志给出**时间差中位数、离散度（MAD）、样本数与覆盖时段**，并沿
   候选对关系图从参考日志连通累计每份日志的估计偏差（只经中间日志
   间接连通时，中位数与离散度由沿途各段样本的逐跳累计分布给出）；
3. 给出以**整分钟**为单位的建议偏移（语义：`校正时间 = 原始时间 + 偏移`）。

以下情形**只列证据、不建议偏移**（`suggested_offset_minutes = null`，
`suppress_reasons` 说明原因）：

- 样本不足（默认少于 3 个无歧义候选对，可用 `min_samples` 调整）；
- 偏差随时间变化（前后半程中位数相差超过 60 秒，单一整分钟偏移不可靠）；
- 日志关系图与参考日志不连通（批次内没有可串联的互指候选对）。

### 2. 建立与预览校正方案

```bash
# 手工指定整分钟偏移；也可用 "use_suggested": true 采纳分析建议（显式 offsets 优先）
curl -s -X POST $B/api/batches/$BID/clock-schemes -H 'Content-Type: application/json' \
  -d "{\"reference_log_id\":\"$REF\",\"name\":\"以 BG1AAA 为参考\",
       \"offsets\":{\"$FAST_LOG\":-6}}"

# 预览重跑配对后的状态计数与计分变化（不改动任何数据）
curl -s -X POST $B/api/batches/$BID/clock-schemes/$SID/preview \
  -H 'Content-Type: application/json' -d '{}'

# 比较两个方案
curl -s "$B/api/batches/$BID/clock-schemes/compare?a=$SID&b=$SID2"
```

`offsets` 只接受**整分钟整数**（±720 以内），参考日志偏移恒为 0；
方案可停用、可删除（启用中须先停用）。

### 3. 启用之后

```bash
curl -s -X POST $B/api/batches/$BID/clock-schemes/$SID/activate    # 启用（同时只启用一个）
curl -s -X POST $B/api/batches/$BID/clock-schemes/$SID/deactivate  # 停用
curl -s $B/api/batches/$BID/archived-decisions                     # 待复核的已归档裁决
```

- 启用后重跑交叉配对；每条证据的每个 QSO 引用同时保留**原时间**
  （`ts/date/time`）、**校正时间**（`corrected_*`）与**偏移**
  （`offset_seconds`），证据另有 `clock_corrected` 标记；
- 配对或状态改变使既有裁决失去依据时，裁决被**归档并列入待复核**
  （归档原因写明原状态与新状态），绝不静默沿用；复核后可
  `DELETE .../archived-decisions/{aid}` 移除；
- 启用中的方案随计分版本快照与 `content_hash` 持久化；
  `GET .../download` 的完整 JSON 包含全部方案与归档裁决。

## 站级赛后反馈包

赛后以**冻结的计分版本**和参赛日志为输入，为每个台站生成反馈包；
每次生成都保存为**不可变快照**，报告、发布状态与替代关系均由 sqlite3
记录。报告逐条列出本台**原日志行、配对状态、是否计分、QSO 分、
新增乘数项、罚分和裁决理由**，并汇总 **CLAIMED-SCORE、最终得分与差额**；
无法关联本台原日志行的证据单列于 `unassociated_evidence`，**不猜测归属**。

```bash
B=http://127.0.0.1:8080

# 1. 先冻结计分版本（反馈包只接受冻结版本作为输入）
curl -s -X POST "$B/api/batches/$BID/versions?lock=1" \
  -H 'Content-Type: application/json' -d '{"note":"终版"}'

# 2. 生成草稿：整批每台一份（省略 station），或只生成单站
curl -s -X POST $B/api/batches/$BID/feedback \
  -H 'Content-Type: application/json' -d '{"version_no":1}'
curl -s -X POST $B/api/batches/$BID/feedback \
  -H 'Content-Type: application/json' -d '{"version_no":1,"station":"BG1AAA"}'

# 3. 预览：先看内部完整稿，再生成对外脱敏稿（发布前必须预览）
curl -s $B/api/batches/$BID/feedback/$RID
curl -s -X POST $B/api/batches/$BID/feedback/$RID/preview

# 4. 发布（发布后不可变）；下载 JSON 或纯文本
curl -s -X POST $B/api/batches/$BID/feedback/$RID/publish
curl -s "$B/api/batches/$BID/feedback/$RID/download?format=txt" -o BG1AAA.txt
curl -s "$B/api/batches/$BID/feedback/$RID/download?format=json" -o BG1AAA.json

# 5. 计分版本演进后：已发布报告不被改写，再生成时自动产出
#    注明旧包与替代版本的更正包（kind=correction）
curl -s -X POST $B/api/batches/$BID/feedback \
  -H 'Content-Type: application/json' -d '{"version_no":2,"station":"BG1AAA"}'
curl -s $B/api/batches/$BID/feedback/$RID2/lineage   # 更正链追踪
```

要点：

- **不可变与幂等**：报告内容哈希相同则返回既有包（`identical_to`），
  不重复建包；已发布的包不能再预览/改动。
- **版本-日志绑定**：计分版本快照内含冻结当时的日志清单
  （`logs`：log_id/文件名/台站/上传时间）。任何旧版本的报告只取清单内
  的日志计算申报分与逐行明细；冻结后上传的日志**不得混入**——重新生成
  旧版本报告会得到与之前完全一致的内容（早期无清单的版本按冻结时间
  截断）。
- **更正包**：只能更正**已发布**的包，且须基于**不同**的计分版本、
  沿该台站最新发布包线性链接；旧包只记录 `superseded_by` 指针，
  内容保持原样。
- **对外脱敏**（`view=external`，发布与整批分发的默认视图）：不含其他
  台站的原始行、邮件地址或完整交换内容，只保留解释本台得失所需的
  **对方呼号和差异项**（字段级的"抄收 vs 对方实发"）；本台自己的原始
  行完整保留。裁决理由等**自由文本**中的邮件地址与他台 `QSO:`/`X-QSO:`
  原文一律替换为占位符（`〔已隐去邮件地址〕`/`〔已隐去他台QSO原文〕`），
  预览、发布、下载全程生效。内部稿（`view=internal`）保留双方原始行与
  理由原文供裁判核对。
- 生成/预览/发布不改动批次数据，批次锁定后照常可用。
- **重复生成幂等**：对同一冻结版本重复生成反馈包时，只要该台站在该版本上
  已有已发布包（普通包或更正包），始终返回同一个包（`identical_to`，
  `reused=published_same_version`），不会因同秒发布的并列排序而偶发新建
  `kind=normal` 的草稿；内容哈希相同的草稿同样幂等复用。

## 赛后复议案件

台站可对**已发布**的反馈包提出异议。每个案件创建时即绑定反馈包、计分版本
及两者的内容哈希（另算案件绑定哈希），状态维护为
`submitted → in_review → closed`，未受理案件可由申请方 `withdrawn`。
案件、争议项、处理意见与状态轨迹全部存于 sqlite3。

```bash
B=http://127.0.0.1:8080

# 1. 提案：多项主张（subject）+ 引用 finding_id / 本台日志行
curl -s -X POST $B/api/batches/$BID/appeals -H 'Content-Type: application/json' -d '{
  "report_id": "'$RID'", "applicant": "BG1AAA",
  "claims": [
    {"subject": "pairing_status",
     "summary": "第14行交换差异系抄收笔误，请求确认计分",
     "finding_id": "F-061123457071",
     "log_refs": [{"filename": "BG1AAA.log", "line": 14}]},
    {"subject": "penalty", "summary": "第17行不构成重复，不应按 DUP 处理",
     "finding_id": "F-dd02c285a3e6"}
  ]}'
# subject 取值：log_line（原日志行）/ pairing_status（配对状态）/
#               exchange_diff（交换差异）/ penalty（罚分）/ summary_score（汇总分）

# 2. 未受理可撤回；裁判受理
curl -s -X POST $B/api/batches/$BID/appeals/$CID/withdraw \
  -H 'Content-Type: application/json' -d '{"reason":"补充材料后重新提交"}'
curl -s -X POST $B/api/batches/$BID/appeals/$CID/accept \
  -H 'Content-Type: application/json' -d '{"judge":"裁判甲"}'

# 3. 逐项处理预览（upheld 维持 / revised 改判 / insufficient 证据不足）
curl -s -X POST $B/api/batches/$BID/appeals/$CID/preview \
  -H 'Content-Type: application/json' -d '{
    "rulings": [
      {"claim_id":"CL-...","conclusion":"revised","resolution":"CONFIRMED",
       "fault_station":"BG1AAA","penalty_code":"BAD_EXCHANGE",
       "rationale":"交换差异为台站抄收笔误，通联确认计分并罚抄收错误"},
      {"claim_id":"CL-...","conclusion":"insufficient",
       "rationale":"未提供新证据，DUP 认定不变"}],
    "judge":"裁判甲"}'
# 返回：逐项 before/after、裁决变更（changed_decisions）、
#       各台站计分变化（scorecard_diff）、新版本号与内容哈希预览、digest、
#       binding_current（false 表示绑定已漂移、该预览不能用于确认）；
#       预览由服务端落库为当前唯一有效的确认依据

# 4. 确认：rulings 必须与最近一次服务端预览一致（digest 可省略，
#    自行伪造 digest 无法绕过预览）；服务端再核对绑定未漂移
curl -s -X POST $B/api/batches/$BID/appeals/$CID/confirm \
  -H 'Content-Type: application/json' -d '{ ...同预览的 rulings...,
    "note":"复议改判冻结"}'

# 5. 筛选 / 详情 / 下载
curl -s "$B/api/batches/$BID/appeals?status=closed&station=BG1AAA"
curl -s $B/api/batches/$BID/appeals/$CID
curl -s $B/api/batches/$BID/appeals/$CID/download -o $CID-appeal.json
```

要点：

- **预览由服务端落库、确认无法绕过**：`preview` 生成的预览由服务端持久化为
  该案件当前唯一有效的确认依据（绑定证据集/现行裁决指纹）。确认时只认可
  服务端实际生成、且与最新绑定快照和处理意见一致的预览：从未预览
  （`PREVIEW_REQUIRED`）、处理意见与预览不一致（`PREVIEW_MISMATCH`）、
  或预览后配对证据集/现行裁决发生变化（`PREVIEW_STALE`）一律返回
  `409 APPEAL_STALE` 且整案不写入——客户端自行计算 `digest` 无法跳过预览；
  请求体中的 `digest` 仅作可选核对。预览不改动批次数据，但响应中的
  `binding_current=false` 表示生成时绑定已漂移、该预览不能用于确认。
  确认成功后预览立即被消费删除，处理意见不可重放。
- **引用硬校验（只提示、不自动改绑）**：finding/日志行不属于该台站
  （`CLAIM_STATION_MISMATCH`）、目标未出现在被异议反馈包
  （`TARGET_NOT_IN_REPORT`）、证据不在绑定版本快照中
  （`CLAIM_FINDING_NOT_FOUND`）等，逐项返回问题清单且不创建案件；
  罚分主张的目标在报告中没有罚分记录时同样拒绝（`TARGET_NO_PENALTY`）。
- **反馈包已有后续更正**：对已被替代的旧包提案返回 `409 REPORT_SUPERSEDED`，
  明确提示最新更正包，系统不会自动把案件改绑到新包。
- **确认时的绑定复核**：确认在**绑定版本快照**上重新核对反馈包/版本内容
  哈希、报告未被替代、当前配对证据集与现行裁决相对快照未漂移、逐项引用仍
  有效；**任一项失效返回 `409 APPEAL_STALE` 且整案不写入**（同一事务，
  裁决/证据/版本/更正包要么全部生效要么全不写）。
- **一次性冻结**：核对通过后改判写入裁决表、重算证据并冻结**新计分版本**
  （快照以 `created_by_appeal` 标注案件 ID）；纯维持/证据不足（无改判项）
  的案件只记录意见并结案，不制造重复版本。
- **原反馈包不可变**：其内容永不改写；仅当申请台站得分发生变化时，新建
  `kind=correction`、**直接发布且对外脱敏稿就绪**的更正包，旧包只写
  `superseded_by` 指针，沿用反馈包既有的线性替代关系；无分差不出更正包。
- 批次数据在复议期间被改动会使确认失效（见上），裁判需重新预览或请申请方
  针对最新包重新提案；撤回后/结案后均可就同一反馈包重新提案，未结案件
  （submitted/in_review）期间重复提案返回 `409 APPEAL_ALREADY_EXISTS`。

## 规则配置（可配置通联分、乘数、罚分）

`POST /api/batches` 的 `rules` 字段（或 `PUT .../rules`）接受完整/部分规则对象，
部分对象与内置默认值合并。主要字段：

```json
{
  "contest": "DEMO-CW",
  "time_tolerance_seconds 在 pairing 内": 300,
  "pairing": {
    "time_tolerance_seconds": 300,
    "near_window_seconds": 1800,
    "call_fuzzy_distance": 2,
    "duplicate_window_seconds": 600
  },
  "bands": [{"name": "40m", "low_khz": 7000, "high_khz": 7300}],
  "allowed_modes": ["CW", "SSB"],
  "mode_aliases": {"RY": "CW"},
  "exchange_fields": [
    {"name": "rst", "type": "rst"},
    {"name": "serial", "type": "integer"}
  ],
  "qso_points": {"default": 1, "by_band": {"10m": 2}, "by_mode": {}},
  "multipliers": [
    {"type": "worked_call", "name": "不同对方呼号"},
    {"type": "band", "name": "不同频段"},
    {"type": "band_mode", "name": "频段×模式"},
    {"type": "exchange_field", "name": "序号不同值", "field": "serial"}
  ],
  "penalties": {
    "BAD_EXCHANGE": {"points": 2, "label": "抄收交换错误"},
    "NOT_IN_LOG": {"points": 5, "label": "对方日志无此记录"},
    "DUP": {"points": 0, "label": "重复通联"}
  },
  "auto_penalty_unique": false,
  "contest_start": "2026-09-10T00:00",
  "contest_end": "2026-09-10T23:59"
}
```

得分 = `Σ通联分 × 各乘数之积 − Σ罚分`。乘数支持：不同对方呼号、不同频段、
频段×模式、某交换字段的不同取值数。规则校验失败会返回 `400 BAD_RULES`
及中文问题清单。

## 可复现性与版本

每个计分版本保存规则、全部证据、裁决、得分卡的完整快照，并计算 SHA-256
`content_hash`。哈希只包含决定裁决结果的输入；**相同输入永远得到相同哈希**，
重复冻结不会产生新版本（返回 `identical_to`）。版本比较接口给出每个台站
QSO 数、乘数、罚分、总分的 a/b 差值及新增/变更的裁决。

## 接口一览

完整字段见 `GET /api/docs`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| POST/GET | `/api/batches` | 创建/列出批次 |
| GET | `/api/batches/{id}` | 批次详情、日志校验汇总、待决数、版本 |
| PUT | `/api/batches/{id}/rules` | 修改规则并重跑配对 |
| POST/GET | `/api/batches/{id}/logs` | 上传（JSON 或 multipart，可多份）/列出日志 |
| GET | `/api/batches/{id}/logs/{lid}` | 原文+解析结果+按行号的错误 |
| POST | `/api/batches/{id}/rerun` | 手动重跑交叉配对 |
| GET | `/api/batches/{id}/findings` | 证据列表（`status/pending/station` 过滤） |
| GET | `/api/batches/{id}/findings/{fid}` | 单条证据（双方原始行、差异、裁决） |
| GET | `/api/batches/{id}/disputes` | 待裁决争议清单 |
| POST/DELETE | `/api/batches/{id}/findings/{fid}/decision` | 提交/撤销裁决（理由必填） |
| GET | `/api/batches/{id}/results` | 当前确定性计分结果 |
| POST/GET | `/api/batches/{id}/versions` | 生成版本快照（`?lock=1`）/列表 |
| GET | `/api/batches/{id}/versions/{no}` | 版本快照 |
| GET | `/api/batches/{id}/versions/diff?a=&b=` | 版本比较 |
| POST | `/api/batches/{id}/lock` | 锁定/解锁 |
| GET | `/api/batches/{id}/clock-analysis` | 时钟偏差分析（`reference_log_id` 必填，`max_window_seconds`/`min_samples` 可选） |
| POST/GET | `/api/batches/{id}/clock-schemes` | 建立/列出整分钟校正方案 |
| GET/PUT/DELETE | `/api/batches/{id}/clock-schemes/{sid}` | 方案详情/修改/删除 |
| POST | `/api/batches/{id}/clock-schemes/{sid}/activate` | 启用方案（重跑配对，失效裁决归档） |
| POST | `/api/batches/{id}/clock-schemes/{sid}/deactivate` | 停用方案（恢复原始时间） |
| POST | `/api/batches/{id}/clock-schemes/{sid}/preview` | 预览方案的状态计数与计分变化 |
| POST | `/api/batches/{id}/clock-schemes/preview` | 临时偏移预览 |
| GET | `/api/batches/{id}/clock-schemes/compare?a=&b=` | 比较两个方案 |
| GET/DELETE | `/api/batches/{id}/archived-decisions[/{aid}]` | 待复核归档裁决列表/复核后移除 |
| POST/GET | `/api/batches/{id}/feedback` | 生成反馈包草稿（`{version_no, station?, corrects?}`；省略 station 整批每台一份）/列表（`station/status/version_no/kind` 过滤） |
| GET | `/api/batches/{id}/feedback/{rid}` | 反馈包详情（`?view=external` 看对外脱敏稿） |
| POST | `/api/batches/{id}/feedback/{rid}/preview` | 生成对外脱敏预览（发布前必须） |
| POST | `/api/batches/{id}/feedback/{rid}/publish` | 发布反馈包（不可变） |
| GET | `/api/batches/{id}/feedback/{rid}/download` | 下载（`format=json\|txt`、`view=internal\|external`） |
| GET | `/api/batches/{id}/feedback/{rid}/lineage` | 更正链版本追踪 |
| POST/GET | `/api/batches/{id}/appeals` | 创建复议案件（须针对已发布反馈包，绑定版本/哈希，引用强校验）/筛选（`status/station/report_id`） |
| GET | `/api/batches/{id}/appeals/{cid}` | 案件详情（争议项、逐项结论、状态轨迹、绑定快照与结果） |
| POST | `/api/batches/{id}/appeals/{cid}/accept` | 裁判受理（submitted→in_review） |
| POST | `/api/batches/{id}/appeals/{cid}/withdraw` | 申请方撤回未受理案件（→withdrawn） |
| POST | `/api/batches/{id}/appeals/{cid}/preview` | 逐项 upheld/revised/insufficient 的裁决变更与计分预览（不写入） |
| POST | `/api/batches/{id}/appeals/{cid}/confirm` | 二次核对全部绑定后一次性写入、冻结新版本、出更正包并结案（任一失效整案不写入） |
| GET | `/api/batches/{id}/appeals/{cid}/download` | 下载案件 JSON |
| GET | `/api/batches/{id}/download` | 完整 JSON 下载（含复议案件） |

## 测试

```bash
python3 tests/smoke_test.py
```

覆盖：必填头/POWER 类别默认值、前缀式便携呼号、普通头不再抛 `TypeError`、
七种配对状态、计分聚合与存储层往返、时钟偏差分析与校正方案、
站级反馈包（冻结版本输入、逐条明细与汇总、预览-发布门控、对外脱敏——
含自由文本中邮件地址/他台 QSO 原文清洗、更正包与更正链、
版本-日志绑定（冻结后上传不混入）、JSON/纯文本下载、无法关联证据单列）、
赛后复议案件（创建绑定与引用强校验、撤回/受理门控、逐项结论校验、
服务端落库预览与确认门控——自算 digest 不能绕过、未预览/预览漂移整案不写入、
预览一次性消费不可重放、绑定漂移整案不写入、一次性冻结新计分版本、
得分变化生成已发布更正包且旧包不可变、纯维持不出新版本、同版本重复生成
始终幂等复用已发布包、筛选/详情/下载）。

## 安全说明

服务默认只绑定 `127.0.0.1`，没有鉴权层——它是裁判工作机上的离线工具。
如需在受信任内网共享，请自行通过反向代理加访问控制。
