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
#      （另冻结计分版本 1 并生成站级赛后反馈包：BG1AAA 已发布，其余为草稿）
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
- **更正包**：只能更正**已发布**的包，且须基于**不同**的计分版本、
  沿该台站最新发布包线性链接；旧包只记录 `superseded_by` 指针，
  内容保持原样。
- **对外脱敏**（`view=external`，发布与整批分发的默认视图）：不含其他
  台站的原始行、邮件地址或完整交换内容，只保留解释本台得失所需的
  **对方呼号和差异项**（字段级的"抄收 vs 对方实发"）；本台自己的原始
  行完整保留。内部稿（`view=internal`）保留双方原始行供裁判核对。
- 生成/预览/发布不改动批次数据，批次锁定后照常可用。

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
| GET | `/api/batches/{id}/download` | 完整 JSON 下载 |

## 测试

```bash
python3 tests/smoke_test.py
```

覆盖：必填头/POWER 类别默认值、前缀式便携呼号、普通头不再抛 `TypeError`、
七种配对状态、计分聚合与存储层往返、时钟偏差分析与校正方案、
站级反馈包（冻结版本输入、逐条明细与汇总、预览-发布门控、对外脱敏、
更正包与更正链、JSON/纯文本下载、无法关联证据单列）。

## 安全说明

服务默认只绑定 `127.0.0.1`，没有鉴权层——它是裁判工作机上的离线工具。
如需在受信任内网共享，请自行通过反向代理加访问控制。
