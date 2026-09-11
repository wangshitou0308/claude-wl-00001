# Cabrillo 离线日志裁决 API

面向业余无线电竞赛裁判的**完全离线** Cabrillo 3.0 日志交叉核对与计分系统。

- **仅使用 Python 3.10+ 标准库**：`http.server` 提供 JSON 接口，`sqlite3` 存储数据。
- **不联网、不查呼号库**：仅凭批次内提交的日志做交叉核对。
- 保留每一份日志的**原始行**，所有校验错误/配对证据都定位到 `文件名:行号`。
- 仅凭现有日志不能唯一判定时一律标为**待裁决（pending）**，绝不借外部信息自动定错。

## 快速开始

```bash
# 启动并自动创建包含 3 份日志的示例赛事（覆盖全部 7 种配对状态）
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

严重问题（缺头、坏频段、坏模式、坏时间、坏对方呼号、交换字段不合法等）为 `error`；
未知标签、缺 END 行等为 `warning`。字段级错误的 QSO 行保留在 `invalid_qsos` 中但不参与配对。

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
| GET | `/api/batches/{id}/download` | 完整 JSON 下载 |

## 测试

```bash
python3 tests/smoke_test.py
```

覆盖：必填头/POWER 类别默认值、前缀式便携呼号、普通头不再抛 `TypeError`、
七种配对状态、计分聚合与存储层往返。

## 安全说明

服务默认只绑定 `127.0.0.1`，没有鉴权层——它是裁判工作机上的离线工具。
如需在受信任内网共享，请自行通过反向代理加访问控制。
