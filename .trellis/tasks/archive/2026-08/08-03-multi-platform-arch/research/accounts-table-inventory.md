# 调研：accounts 表全量读写盘点

调研时间：2026-08-03。为「拆分成身份层 + platform_accounts」的迁移做准备。

## 0. 表结构基线

- `src/models/database.py:13-21` — V1 建表：`id / email UNIQUE / login_password / email_password / status DEFAULT 'registered' / created_at / updated_at`
- `database.py:117-118`（V5）`credits_balance REAL`、`balance_updated_at TEXT`
- `database.py:156-157`（V9）`bound_card_count INTEGER`、`cards_checked_at TEXT`
- `database.py:164-166`（V10）`apikey TEXT`、`apikey_updated_at TEXT`、`email_verify_link TEXT`

**`email` 是事实上的外键**，被 5 张表以裸字符串引用且**无 FK 约束**：

- `card_bindings.bound_to_email`（`database.py:40`；读写 `src/models/card_binding.py:27,122,205,224,257`）
- `recharge_logs.email`（`database.py:48`）
- `valid_cards.source_email`（`database.py:105`）
- `adspower_profiles.email`（`src/models/adspower_profile.py` 全表，且与 accounts JOIN）
- `proxies.assigned_email`（`database.py:180`，目前未写入）
- 内存态 `AccountRegistry`（`src/web/worker.py:38`）也以 email 为 key

## 1. account.py 之外的直接 SQL

### 生产代码

| 位置 | SQL | 作用 |
|---|---|---|
| `src/models/adspower_profile.py:87-93` | `SELECT p.email, p.profile_id, a.status FROM adspower_profiles p JOIN accounts a ON a.email = p.email WHERE a.status IN (...) ORDER BY CASE a.status ...` | **唯一的跨表 JOIN**。按 `accounts.status` 优先级挑可回收环境。优先级常量 `adspower_profile.py:22-24`：`('failed','pending','rejected','recharged','archived','subscribed','flagged','banned','suspended')`；`registered` 刻意不可回收。调用方 `src/browser/adspower_driver.py:187` |
| `src/models/database.py:259` | `SELECT COUNT(*) FROM accounts` | `_import_txt_if_needed` 首启判空 |
| `src/models/database.py:284-286` | `INSERT OR IGNORE INTO accounts (...)` | 从 `registered_accounts.txt` 首次迁移导入 |

### 脚本

- `scripts/login_github_manual.py:34-36` — `SELECT email, login_password FROM accounts WHERE email=?`
- `scripts/fix_failed_accounts_status.py:24-27` — 状态分布统计
- `scripts/fetch_apikeys.py:88` — `SELECT email FROM accounts ORDER BY id`
- `scripts/fetch_apikeys.py:91` — `SELECT email FROM accounts WHERE credits_balance>0 ORDER BY id`

### 测试（迁移时同样要改）

`tests/test_daily_pipeline.py:73,86,112,152-153,156-157,173`、`tests/test_adspower_pool.py:76`

## 2. AccountModel 方法调用点

模型注册：`src/web/app.py:1339`；取用 `src/api/routes.py:31 get_models()`。

**`upsert`** — `app.py:1019-1021`（suspended）、`app.py:1026-1028`（registered）、`scripts/run_hotmail_github_signup.py:60-62,72-74,77-78`

**`update_status`** — `app.py:1016`(pending)、`app.py:1024`(failed)、`app.py:1076`(flagged)、`app.py:1105`(subscribed)、`src/services/registration.py:165`(flagged)、`registration.py:180`(archived)、`registration.py:241`(recharged)、`scripts/run_subscribe_once.py:119`、`scripts/run_hotmail_github_signup.py:79`、`scripts/login_github_manual.py:85,94,97,100,112`

**`update_balance`** — `registration.py:181`、`registration.py:245`、`routes.py:466`

**`update_apikey`** — 仅 `scripts/fetch_apikeys.py:103`（生产代码零调用）

**`update_bound_cards`** — **生产代码零调用**。`registration.py:40,51` 的 `register_and_bind_cards` / `bind_cards_to_existing_account` 都是 `raise NotImplementedError`。仅测试桩 `tests/test_bind_retry.py:34`

**`get_email_password`** — **全项目零调用**

**`backfill_email_verify_link`** — 仅 `scripts/backfill_verify_links.py:37`

**`reset_failed_to_registered`** — 仅 `scripts/fix_failed_accounts_status.py:49`；测试 `tests/test_account_status.py:15,30`

**`get_all`** — `app.py:687-691`（`_payable_now`）、`app.py:697-700`（`_registerable_imported`）、`app.py:1181-1182`（`_needing`）、`routes.py:873`、`routes.py:948-953`、`routes.py:995-999`

**`get_paginated`** — `routes.py:283-287`、`routes.py:558-562`。注意 status 过滤是 `LIKE '%term%'`（`account.py:154-155`）

**`search`** — `routes.py:359,412,548`，三处都是 LIKE 后在 Python 里精确匹配

**`delete_by_emails`** — `routes.py:330`；同 handler `:326-328` 先手删 `card_bindings WHERE bound_to_email IN (...)`

**`count`** — `routes.py:46`

### 直接读 dict 字段

- `app.py:779,783` — `a.get('id', 0)` 传给 `_acquire_proxy_for` 做代理取模兜底（**accounts.id 被业务逻辑消费**）
- `app.py:861` — `login_password`；`app.py:867` — `email_verify_link`
- `app.py:969,974` — `_hotmail_for_account` 读 `email_verify_link` + `email_password`
- `app.py:1052` — `status not in ('registered','subscribed')` 决定注册还是订阅分支

## 3. status 全部取值与写入点

| 取值 | 写入位置 | 语义 |
|---|---|---|
| `registered` | `account.py:10`(默认)、`app.py:1027`、`account.py:47`、`run_hotmail_github_signup.py:35`、`database.py:284` | 已注册可用 |
| `imported` | `run_hotmail_github_signup.py:61,78` | 仅从 xlsx 导入、未注册 |
| `pending` | `app.py:1016`、`run_hotmail_github_signup.py:41-42` | 碰 Arkose 跳过 |
| `suspended` | `app.py:1020`、`run_hotmail_github_signup.py:36`、`login_github_manual.py:94` | 注册即挂起 |
| `rejected` | `run_hotmail_github_signup.py:37` | GitHub 拒绝 |
| `failed` | `app.py:1024`、`run_hotmail_github_signup.py:38-40,43` | 注册失败（**仅注册链路写**） |
| `flagged` | `app.py:1076`、`registration.py:165` | GitHub 反滥用 flag，永久终态 |
| `archived` | `registration.py:180` | 余额 ≥ $20 跳过充值 |
| `recharged` | `registration.py:241` | 充值成功 |
| `subscribed` | `app.py:1105`、`run_subscribe_once.py:119` | Stripe 订阅成功 |
| `bound_{N}_cards` | `account.py:67`（**当前无生产调用**） | 历史绑卡数编码 |
| `logged_in` / `need_device_verification` | `login_github_manual.py:85,97,100` | 人工登录脚本 |
| `banned` | **无写入点**，只被读端过滤 | 历史遗留 |
| `bound` / `billing_page` / `interrupted` / `all_bindings_failed` / `error` | **无写入点**，仅前端下拉 | Cloudflare 时代遗留 |

### 按 status 筛选的地方

- `app.py:689-691` 可充值集：`not in ('banned','archived','flagged','recharged')`
- `app.py:698` 待注册集：`== 'imported'`
- `app.py:1052` 注册 vs 订阅分支：`not in ('registered','subscribed')`
- `app.py:1178,1181` 待订阅集 `_DONE = ('subscribed','banned','suspended','flagged')`
- `routes.py:952` daily/start 启动门：`not in ('banned','archived')` ← **与 app.py:690 的四元组不一致，既存偏差**
- `routes.py:998` daily/subscribe 启动门：`not in ('subscribed','banned','suspended','flagged')`
- `adspower_profile.py:22-24,90-91` 环境回收优先级 9 档
- `account.py:154-155` `get_paginated` 模糊过滤；`account.py:48` `WHERE status='failed'`
- `routes.py:897` 有效卡导出「账号状态」列

## 4. 字段消费者

- **`login_password`**（GitHub 密码）：`app.py:688,861`、`routes.py:296(API 字段名 password),369-371,422-424,582,895,951`、`scripts/login_github_manual.py:35,42,70`。最终消费 `src/browser/opencode_billing.py:157,180,185`
- **`email_password`**：`routes.py:299,583,895`、`app.py:974`
- **`credits_balance`/`balance_updated_at`**：写 `registration.py:181,245`、`routes.py:466`；读 `routes.py:306-307,586`、`scripts/fetch_apikeys.py:91`。归档判据用**实时页面余额**（`registration.py:174-178`），不用 DB 值
- **`bound_card_count`/`cards_checked_at`**：写入方零生产调用；读 `routes.py:304-305`、`Accounts.vue:76-80,285-290`
- **`apikey`**：写仅 `fetch_apikeys.py:103`；读 `routes.py:308-309,585`、`Accounts.vue:90-92`
- **`email_verify_link`**：写 `account.py:20,22,28,99-112`；读 `app.py:867,969`、`routes.py:310,584`、`registration.py:86`、`opencode_billing.py:93,157`
- **`updated_at`**：所有 UPDATE 都写，**无读取点**

## 5. 前端消费点

`frontend/src/views/Accounts.vue` 是主战场，endpoint 定义在 `frontend/src/api/index.js:51-62`。`static/assets/*.js` 是 vite 产物，改源码后需重新构建。

- `Accounts.vue:314` `loadData()` → `GET /api/accounts`（`routes.py:272`）
- `Accounts.vue:15-27` status 下拉：`registered/bound/billing_page/interrupted/all_bindings_failed/banned/flagged/archived/failed/error` —— **缺 imported/pending/suspended/rejected/recharged/subscribed，多 5 个死状态**
- `Accounts.vue:69,269-303` `statusMap` 14 项 + `bound_(\d+)_cards` 正则（`:293-294`）+ 配色（`:297-303`）
- `Accounts.vue:65,66,67,72-80,83-87,90-92,95-97,99` 逐字段渲染
- 操作端点：open-browser（`routes.py:401`）、recharge（`routes.py:341`）、recharge-logs（`routes.py:530`）、delete（`routes.py:316`）、export（`routes.py:537`）、cards（`routes.py:334`）
- `Dashboard.vue:17` ← `stores/app.js:77` ← `GET /api/status` 的 `total_inventory`
- `Workbench.vue:173,193` 显示可充值/待订阅账号数
- `CardPool.vue:262,325`、`CardHistory.vue:32,57` 显示邮箱但不直接读 accounts；导出接口 `routes.py:873,895-897` 会 JOIN accounts

## 6. 迁移视角的关键提示

1. 字段天然分层：`email/email_password/email_verify_link` 属邮箱身份；`login_password`（GitHub 密码）属 GitHub 身份；`status/credits_balance/apikey/...` 属平台。
2. 可顺手清掉的死代码：`get_email_password`、`update_bound_cards`、`bound_N_cards`、前端 5 个死状态、`banned` 状态。
3. 两处 status 过滤集合不一致（`app.py:689-691` vs `routes.py:952`），建议抽共享常量。
4. `accounts.id` 被业务消费（`app.py:779,783`），拆表时要明确归属。
