# 技术设计 — 充值报表与账号金额视图

## 分层与落点

沿用项目既有三层：`models/*.py`（纯 SQL）→ `api/routes.py`（HTTP + 组装）→ `frontend/src/views/*.vue`。
报表是**纯读取**，不碰任何写路径、不碰 `AppSharedState`、不碰浏览器/支付流程。

| 层 | 文件 | 改动 |
|---|---|---|
| Model | `src/models/recharge_log.py` | 新增 4 个只读聚合方法 |
| API | `src/api/routes.py` | 新增 `/api/reports/recharge`；`/api/accounts` 每行补两字段 |
| 前端 API | `frontend/src/api/index.js` | 新增 `getRechargeReport` |
| 路由 | `frontend/src/router/index.js` + `App.vue` | 新增 `/reports` 与侧栏入口 |
| 视图 | `frontend/src/views/Reports.vue`（新） | 报表页 |
| 视图 | `frontend/src/views/Accounts.vue` | 两列 + 当前页汇总带 + 文案 |

## Model 层契约

全部方法签名首参为 `platform`，与本模块既有统计类方法一致（文件头注释已把
「统计类查询一律要求 platform」定为规则，新方法不得例外）。

```python
def report_summary(self, platform, date_from='', date_to='') -> dict
# {'total_amount': float, 'success_count': int, 'failed_count': int,
#  'card_count': int, 'account_count': int}

def report_today(self, platform) -> dict
# 同上结构，区间固定为 DATE(created_at)=DATE('now','localtime')，不受入参区间影响

def report_daily(self, platform, date_from='', date_to='') -> list[dict]
# [{'date','amount','success_count','failed_count','card_count','account_count'}, ...] 日期倒序

def report_by_account(self, platform, date_from='', date_to='', limit=100) -> list[dict]
# [{'email','amount','success_count','card_count','last_at'}, ...] 金额倒序

def amount_by_emails(self, platform, emails) -> dict
# {email: {'today': float, 'total': float}}；供 /api/accounts 批量取数
```

### 口径落到 SQL 的三个要点

1. **金额与笔数的 status 分岔在同一次查询里做**，不要为「成功金额」和「失败笔数」各打一条 SQL：

```sql
SUM(CASE WHEN status='success' THEN amount ELSE 0 END) AS amount,
SUM(CASE WHEN status='success' THEN 1 ELSE 0 END)      AS success_count,
SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END)      AS failed_count
```

2. **去重卡片数不能和上面同查询混算**。`COUNT(DISTINCT ...)` 在带 `CASE` 的行里无法只统计成功行，
   所以卡片/账号去重计数**只在 `status='success'` 的过滤子集里算**，写成独立的一条查询：

```sql
SELECT COUNT(DISTINCT replace(card_display,' ','')) AS card_count,
       COUNT(DISTINCT email)                        AS account_count
FROM recharge_logs
WHERE platform=? AND status='success'
  AND card_display IS NOT NULL AND card_display != ''
  AND <日期条件>
```

   注意 `card_count` 的 `card_display` 非空过滤会让 `account_count` 也被同一条件裁掉。
   这是**有意的**：一条 success 记录没有 card_display 属于异常数据（写入路径总会带卡号），
   与其为它多打一条 SQL，不如让两个数字口径一致、都只看有卡号的成功记录。
   **实现时必须在方法 docstring 里写明这一点**，否则将来对不上账时无从查起。

   `report_daily` 同理：一条 `GROUP BY DATE(created_at)` 出金额与笔数，
   另一条同样 `GROUP BY DATE(created_at)` 但带 success 过滤出去重卡数/账号数，
   在 Python 里按日期 key 合并（缺失的日期补 0）。

3. **日期条件统一用 `DATE(created_at)` 比较**，参数直接传 `YYYY-MM-DD`：

```sql
AND DATE(created_at) >= ?   -- date_from
AND DATE(created_at) <= ?   -- date_to
```

   不沿用 `get_paginated` 那种 `created_at <= date_to + ' 23:59:59'` 的字符串拼接——
   那是为了兼容全时间戳比较，而这里两侧都归一到日期，语义更直白且不受秒级边界影响。

4. `amount_by_emails` 一条 SQL 同时出 today 与 total：

```sql
SELECT email,
       SUM(amount) AS total,
       SUM(CASE WHEN DATE(created_at)=DATE('now','localtime') THEN amount ELSE 0 END) AS today
FROM recharge_logs
WHERE platform=? AND status='success' AND email IN (...)
GROUP BY email
```

   `emails` 为空立即返回 `{}`（与 `count_by_emails` / `success_amount_by_email` 一致），
   否则 `IN ()` 是语法错误。

## API 层

### `GET /api/reports/recharge`

```python
@api.route('/api/reports/recharge')
def get_recharge_report():
    platform = _req_platform()
    date_from, date_to = _report_range(request.args)   # 缺省 → 最近 30 天
    m = get_models()['recharge_log']
    summary  = m.report_summary(platform, date_from, date_to)
    today    = m.report_today(platform)
    daily    = m.report_daily(platform, date_from, date_to)
    accounts = m.report_by_account(platform, date_from, date_to)
    # 核销拆分：账号身份状态来自 accounts 表，一次性取回本榜单涉及的账号
    status_map = {a['email']: (a.get('identity_status') or '')
                  for a in get_models()['account'].get_by_emails([r['email'] for r in accounts])}
    ...
```

- `_report_range` 是本文件内的小工具函数：读 `date_from`/`date_to`，任一缺省时按
  「`date_to` = 今天、`date_from` = 今天 - 29 天」补齐。日期计算用 `datetime.date.today()`，
  与 SQLite 的 `'now','localtime'` 同为本机本地时区，不会错位。
- **`verified` / `active` 的拆分在 Python 里做，不下沉到 SQL**。跨 `recharge_logs` 与 `accounts`
  两张表 JOIN 会把「账号身份」这个概念泄进充值模型；而榜单上限 100 行，一次
  `get_by_emails` 的成本可以忽略。
- 榜单每行补 `identity_status` 与 `is_verified`（= `identity_status == 'retired'`）。
  前端只读 `is_verified`，不重复判断字符串——核销判据将来若扩到多个状态，只改后端一处。

响应结构：

```json
{
  "platform": "opencode",
  "date_from": "2026-07-11", "date_to": "2026-08-09",
  "today":   {"amount": 0, "success_count": 0, "card_count": 0, "account_count": 0},
  "summary": {"total_amount": 0, "success_count": 0, "failed_count": 0,
              "success_rate": 0.0, "card_count": 0, "account_count": 0},
  "verified": {"amount": 0, "account_count": 0},
  "active":   {"amount": 0, "account_count": 0},
  "daily":    [{"date": "2026-08-09", "amount": 0, "success_count": 0,
                "failed_count": 0, "card_count": 0, "account_count": 0}],
  "accounts": [{"email": "", "amount": 0, "success_count": 0, "card_count": 0,
                "last_at": "", "identity_status": "", "is_verified": false}]
}
```

`success_rate` 在后端算好（`success/(success+failed)`，分母 0 时给 `0.0`），避免前端重复实现。

### `/api/accounts` 的改动

在既有 `card_counts` / `pa_map` 旁再加一行取数，然后在组装循环里补两个字段：

```python
recharge_amounts = models['recharge_log'].amount_by_emails(platform, emails)
...
amt = recharge_amounts.get(acc['email']) or {}
"recharge_today": amt.get('today', 0),
"recharge_total": amt.get('total', 0),
```

`emails` 取自**分页后**的账号（现有代码即如此），所以聚合规模恒等于 page_size。
注意 `emails` 在 `platform_status` 前端过滤之前就已算出，多查几个被过滤掉的账号无害。

## 前端

### `Reports.vue`

- `onMounted` 与「查询」按钮都调 `getRechargeReport({date_from, date_to})`；
  platform 由 `api/index.js` 的 `get()` 自动注入。
- **平台切换要重新拉取**：`stores/app.js` 持有 `platform`，页面 `watch(() => store.platform, load)`。
  这是最容易漏的一处——不加 watch，切平台后页面数字会停在旧平台上，且看不出来。
- 柱状条：`width: (row.amount / maxAmount * 100) + '%'`，`maxAmount` 为 `daily` 中金额最大值，
  为 0 时降级为 0 宽（避免除零得 NaN 让整行样式失效）。
- 复用既有 class：`.panel` / `.panel-header` / `.stats-grid` / `.stat-card` / `.log-table` /
  `.status-tag`，只为柱状条和汇总带写 scoped 样式。

### `Accounts.vue`

- 两列插在「绑定卡片」与「Credits 余额」之间——金额与余额相邻更好对读。
  `colspan` 从 12 改为 14（加载中/空态两行各一处，别漏）。
- 汇总带由 `computed` 从当前 `accounts` 数组算出，按 `identity_status === 'retired'` 分两组。
- 身份状态下拉里 `retired` 的文案改为「已核销（已归档）」，`accStatusLabel` 中对应文案同步。

## 兼容与回滚

- 全部为**新增**：新方法、新路由、新页面，加上 `/api/accounts` 两个新字段。
  旧字段一个未改、未删，老前端拿到多余字段会直接忽略。
- 无 DB schema 变更，无迁移，无新依赖。
- 回滚 = `git revert` 单个 commit；无残留状态需要清理。

## 风险

| 风险 | 处置 |
|---|---|
| 历史 `card_display` 格式不统一（完整卡号 / `•••• 1234`） | 报表卡片数按完整串去重，历史脱敏行会被当成独立一张卡，导致早期日期卡片数偏大。在页面上对「使用卡片数」加 title 说明，不做数据清洗（清洗属于独立任务） |
| `recharge_logs.platform` 历史行可能为空串 | V16 迁移已把既有行 UPDATE 成 `'opencode'`；仍为空的行会落在所有平台之外、不计入任何报表。这是有意的保守选择——宁可少算也不要错算到某个平台 |
| 报表 SQL 全表扫 | 当前量级可接受；PRD 已把加索引列为范围外 |
