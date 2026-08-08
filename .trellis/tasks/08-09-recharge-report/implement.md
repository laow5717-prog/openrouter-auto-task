# 执行计划 — 充值报表与账号金额视图

按后端 → 接口 → 前端顺序推进，每步都能独立验证。前三步做完即可用 curl 验收全部数据口径，
前端只负责呈现，不再产生新的口径分歧。

## Step 1 — Model 聚合方法

**文件**：`src/models/recharge_log.py`

- [ ] `report_summary(platform, date_from='', date_to='')`
      —— 两条 SQL：金额/笔数（CASE 分岔）+ 去重卡数/账号数（success 子集）。
- [ ] `report_today(platform)` —— 复用 `report_summary` 的实现，日期条件固定为
      `DATE(created_at)=DATE('now','localtime')`。提取一个私有 `_range_clause(date_from, date_to)`
      返回 `(sql_fragment, params)`，三个方法共用，避免日期条件写三遍走样。
- [ ] `report_daily(platform, date_from, date_to)` —— 两条 `GROUP BY DATE(created_at)`，
      Python 侧按日期合并，缺失键补 0，日期倒序返回。
- [ ] `report_by_account(platform, date_from, date_to, limit=100)` —— 金额倒序，
      `MAX(created_at) AS last_at`，只统计 success 行。
- [ ] `amount_by_emails(platform, emails)` —— 空 `emails` 直接返回 `{}`。
- [ ] 每个方法写 docstring，**必须写明**：只算 success、按 platform、
      以及 design.md 里那条「卡数/账号数都受 `card_display` 非空过滤裁剪」的口径。

**验证**：

```bash
python3 -c "
from src.models.database import Database
from src.models.recharge_log import RechargeLogModel
db = Database('openrouter_auto.db'); m = RechargeLogModel(db)
print(m.report_today('opencode'))
print(m.report_summary('opencode','2026-07-11','2026-08-09'))
print(m.report_daily('opencode','2026-07-11','2026-08-09')[:3])
print(m.report_by_account('opencode','2026-07-11','2026-08-09')[:3])
"
```
（`Database` 的构造签名以 `src/models/database.py` 实际为准，若不同则照实调整。）

**对账**（这一步是口径正确性的唯一硬证据，不能跳）：

```bash
sqlite3 openrouter_auto.db "SELECT ROUND(SUM(amount),2) FROM recharge_logs
  WHERE platform='opencode' AND status='success'
    AND DATE(created_at) BETWEEN '2026-07-11' AND '2026-08-09';"
```
结果须与 `report_summary(...)['total_amount']` 一致。

## Step 2 — 报表接口

**文件**：`src/api/routes.py`

- [ ] 加模块级小工具 `_report_range(args)`：缺省补最近 30 天，返回 `(date_from, date_to)`。
- [ ] `@api.route('/api/reports/recharge')` — 按 design.md 的响应结构组装。
- [ ] `success_rate` 后端算好，分母为 0 时给 `0.0`。
- [ ] 榜单每行补 `identity_status` / `is_verified`；`verified` / `active` 两段汇总在 Python 里拆。
- [ ] 路由位置放在 `/api/recharge-logs` 一组附近，保持相关接口聚拢。

**验证**：

```bash
python3 server.py &   # 或项目既有启动方式
curl -s 'http://127.0.0.1:5000/api/reports/recharge?platform=opencode' | python3 -m json.tool | head -40
curl -s 'http://127.0.0.1:5000/api/reports/recharge?platform=opencode&date_from=2026-08-01&date_to=2026-08-09' | python3 -m json.tool | head -20
```
（端口以 `server.py` 实际为准。）检查：区间收窄后 `summary` 变小但 `today` 不变。

## Step 3 — 账号列表金额字段

**文件**：`src/api/routes.py::get_accounts`

- [ ] `emails` 已有，在 `card_counts` 旁加 `recharge_amounts = ...amount_by_emails(platform, emails)`。
- [ ] 组装循环补 `recharge_today` / `recharge_total`，缺省 `0`（**不是 None**）。

**验证**：

```bash
curl -s 'http://127.0.0.1:5000/api/accounts?platform=opencode&page_size=5' \
  | python3 -c "import sys,json; [print(a['email'], a['recharge_today'], a['recharge_total']) for a in json.load(sys.stdin)['data']]"
```
挑一个非零账号，与 `/api/recharge-logs/<email>` 里该平台成功记录之和对账。

## Step 4 — 前端 API 与路由

- [ ] `frontend/src/api/index.js`：`export const getRechargeReport = (params) => get('/api/reports/recharge', params)`，
      放在 `// Recharge logs` 分组下。
- [ ] `frontend/src/router/index.js`：`{ path: '/reports', name: 'reports', component: () => import('../views/Reports.vue') }`。
- [ ] `frontend/src/App.vue`：侧栏加入口（放在「充值记录」之后），并检查 `pageTitle` 的映射逻辑
      是否需要补一条（看它是按 route name 还是 meta 取标题，照现有写法补齐）。

## Step 5 — `Reports.vue`

- [ ] 今日 KPI 区（`.stats-grid` + `.stat-card`），4 张卡。
- [ ] `FilterBar` 日期区间 + 查询/重置；默认最近 30 天，重置回默认区间而非清空。
- [ ] 区间汇总条：总金额 / 成功·失败笔数 / 成功率 / 卡片数 / 账号数 + 已核销 vs 在用两组。
- [ ] 每日趋势表 + 纯 CSS 柱状条；`maxAmount` 为 0 时宽度取 0，不产生 NaN。
- [ ] 账号榜单表；`is_verified` 为真时打「已核销」`.status-tag`。
- [ ] **`watch(() => store.platform, load)`** —— 平台切换必须重新拉取。
- [ ] 「使用卡片数」表头加 `title`，说明历史脱敏卡号会被计为独立卡片（见 design.md 风险表）。

## Step 6 — `Accounts.vue`

- [ ] 表头与单元格加「今日充值」「累计充值」两列，插在「绑定卡片」与「Credits 余额」之间。
- [ ] **`colspan` 12 → 14**，加载中与空态两行都要改。
- [ ] `computed` 汇总带：当前页今日合计/累计合计，按 `identity_status === 'retired'` 拆成
      「已核销」与「在用」两组；文案写明「当前页合计」。
- [ ] 身份状态下拉 `retired` 文案 →「已核销（已归档）」，`accStatusLabel` 同步。

## Step 7 — 构建与整体验收

```bash
cd frontend && npm run build
```

- [ ] 构建通过，`git diff --stat frontend/package.json` 为空（无新依赖）。
- [ ] 逐条走 prd.md「验收标准」6 项。
- [ ] 两个平台各切一次，确认报表页与账号列表金额都随平台变化。

## 回滚点

- Step 1–3 之后若发现口径不对：只回退 model 与 routes 的改动，前端尚未接线，不影响任何现有页面。
- Step 4 之后：新路由是独立页面，删掉路由项即可让它不可达，其余页面不受影响。
- 全量回滚：`git revert` 本任务的单个 commit，无 DB 迁移需要撤销。

## 检查门

- Step 1 的**对账**（sqlite3 直查 vs model 返回）不通过则不得进入 Step 2。
- Step 3 的账号金额与该账号充值记录之和对不上则不得进入前端步骤。
