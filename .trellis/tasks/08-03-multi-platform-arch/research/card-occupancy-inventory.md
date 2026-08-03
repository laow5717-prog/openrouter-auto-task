# 调研：卡池占用/绑定/冷却/去重 全量盘点

调研时间：2026-08-03。目标是判断「加 platform 维度」需要动哪些地方。

## 结论先行

「一张卡已被占用」的语义分散在 **6 个载体**，**没有任何一处带平台维度**：

| # | 载体 | 位置 | 粒度 |
|---|---|---|---|
| 1 | `card_pool.status='bound'` | `src/models/card_pool.py:327` | 全局按卡号 |
| 2 | `card_pool.status='invalid'` | `src/models/card_pool.py:309` | 全局按卡号 |
| 3 | `valid_cards.source_email` | `src/models/valid_card.py:33` | `(卡号, source_type)` |
| 4 | `card_payment_state.tds_until` | `src/models/card_payment_state.py:19` | 主键=卡号 |
| 5 | `recharge_logs` 派生统计 | `src/models/recharge_log.py:76,88,137` | 按 `card_display` |
| 6 | `PaymentCardRegistry._in_flight/_used` | `src/web/worker.py:124,135` | 内存 key=卡号 |

## 1. 七张表

### `card_groups`（`database.py:61-67`）
无唯一约束。`type`（`'bind'|'payment'`）已废弃（`src/models/card_group.py:10-12`）。

**不能用它承载平台维度**：一张卡物理上只能属于一个分组（`card_pool.py:22-33` 的 `find_cards_in_other_groups` 主动阻止跨组同号），用分组当平台维度等于「一张卡只能给一个平台用」，与需求相反。

### `card_pool`（`database.py:69-87` + V4 加 status `database.py:113`）
唯一约束 `UNIQUE(card_number, group_id)`。`status` 取值见 `src/utils.py:18-22`：

- `'bound'`（`utils.py:22`）— 已绑定到某账号，一卡一账号，不再参与选卡
- `'invalid'`（`utils.py:20`）— 拒付判废，永久退出
- `'paid'`（`utils.py:18`）— 曾成功支付，**不**阻止再选（`card_pool.py:327-343` 注释）
- `'expired'`（`utils.py:19`）— **平台无关**，唯一真正全局的状态
- 聚合常量：`CARD_STATUS_UNUSABLE = (expired, invalid)`（`utils.py:26`）、`CARD_STATUS_NOT_SELECTABLE = UNUSABLE + (bound,)`（`utils.py:32`）

**status 一列 TEXT 存不下多平台状态，必须拆表。** 受影响方法（全部要加 platform）：`_bucket_where:72-86`、`get_by_group:88`、`count_buckets:103`、`delete_invalid_by_group:130`、`move_non_invalid_to_group:140`、`move_bucket_to_group:187`、`get_usable_cards_as_list:263`（**选卡主闸门**）、`refresh_expired_status:277`（`:293` skip 列表含 BOUND）、`mark_status_by_number:302`、`mark_invalid_by_number:309`、`mark_bound_by_number:327`、`get_locations_by_number:368`

### `valid_cards`（`database.py:89-109`）
`UNIQUE(card_number, source_type)`（`database.py:108`）+ `INSERT OR IGNORE`（`valid_card.py:14`）→ **首次写入永不被覆盖**。`get_bound_email()`（`valid_card.py:33-44`）的 docstring 直接说「此值即永久绑定账号」。

**隐藏的强耦合**——valid_cards 成员身份被三处当成全局不变式：

- `card_pool.py:317-321` `mark_invalid_by_number` 守卫 `AND card_number NOT IN (SELECT card_number FROM valid_cards)` ← **加 platform 后最大的坑**：卡在 opencode 成功过 → 在新平台被拒时永远标不成 invalid
- `card_pool.py:80,84` `_bucket_where` 的 valid/unverified 桶定义
- `card_pool.py:157` `move_non_invalid_to_group(bucket='valid')`

### `card_bindings`（`database.py:35-44` + V8 `database.py:147-149`）
无唯一约束。索引 `idx_cb_status_claimed(status, claimed_at)`。占用语义：

- `status='processing'` + `worker_id` + `claimed_at` — 运行时占位（`claim_batch` `card_binding.py:62-83`，状态机注释 `:44-51`）
- `status='success'` + `bound_to_email` — 永久占用，派生 `get_successfully_bound_card_numbers()` `:142-155`
- `status='failed'` + `error LIKE '[Stripe字段错误]%'` — `get_stripe_field_error_card_numbers()` `:127-140`
- `status='failed'` + `error LIKE '[充值拒付]%'` — `mark_declined_by_number()` `:157-181` / `get_declined_card_numbers()` `:183-196`
- `count_by_emails()` `:252-260`

**`claim_batch` `:75` 的 SELECT 若不带 platform，A 平台 worker 会领走 B 平台任务的卡。** `reset_all_processing()` `:111` 与 `reap_stale()` `:96` 的跨平台语义需显式决策。

### `card_payment_state`（`database.py:123-128`）
`PRIMARY KEY(card_number)` —— 天然全局单行。一列承载两种冷却（模块 docstring `:1-12`）：3DS 拦截、「曾成功卡本次被拒」的 24h 速率冷却，靠 `tds_reason` 区分。

**最直接的跨平台污染点**：3DS 是「商户 + 发卡行」共同决定的，换平台即换 Stripe 商户号，不一定触发。方法：`set_cooldown:19`、`in_cooldown:33`、`get_tds_until:48`、`get_state_map:56`、别名 `set_tds`/`in_tds_cooldown` `:45-46`

### `invoice_payment_state`（`database.py:133-141`）
**当前是死代码**。全仓仅 `src/web/app.py:25`（import）与 `app.py:1347`（注册进 models），`mark_unpayable`/`in_cooldown` 无任何调用方。`src/browser/driver.py:3262` 的注释说「由调用方基于 invoice_payment_state 落库判定」，说明这条线没接完。

### `recharge_logs`（`database.py:48-57`）
无唯一约束。**`card_display` 的实际语义是完整卡号**（`recharge_log.py:80-81` 注释；写入方 `registration.py:112` 传 `card.get("number")`），但历史数据可能是脱敏串，故多处按末 4 位聚合（`:100-118`、`:120-135`）。

派生查询（全部实时，无落库）：

- `all_success_card_numbers()` `:76-86` → 「新卡 vs 好卡」排序（`app.py:535`）
- `last_success_at()` `:137-146` → **决定拒付时是冷却还是判 invalid**（`registration.py:278`）
- `success_count_since(num, 24) >= 2` `:88-98` → 24h 达 2 次冷却（`routes.py:682,826`）
- `get_success_card_numbers(email)` `:66-74`、`has_today_record()` `:46-57`、`count_success_by_last4()` `:100-118`

**`last_success_at` 最关键**：不按平台过滤会让「在 opencode 成功过」的坏卡在新平台被拒时也只进冷却、永远标不成 invalid，无限循环。

## 2. 选卡链路：12 条排除条件

主入口 `AppState._eligible_cards()` — `src/web/app.py:516-547`。调用者：`app.py:571,728,765,773,928,1084,1171,1190,1205,1245,1266`、`routes.py:944,991`。

| # | 排除条件 | file:line | 加 platform 后 |
|---|---|---|---|
| E1 | 有效期过期 | `card_pool.py:268` → `:277-300`；判定 `utils.py:92-116` | **保持全局**。但 `:293` skip 列表引用 BOUND，要改查平台状态表 |
| E2 | 状态 ∈ NOT_SELECTABLE | `card_pool.py:273`；`utils.py:32` | `bound`/`invalid` 查平台状态表；`expired` 留 card_pool |
| E2a | ↳ `bound` = 一卡一账号 | 写入 `card_pool.py:327-343` | `mark_bound_by_number(num, platform)` |
| E2b | ↳ `invalid` = 拒付判废 | 写入 `card_pool.py:309-321`；触发 `registration.py:288-292`、`app.py:1117` | 守卫子查询要加 `WHERE platform=?` |
| E3 | 3DS / 速率冷却 | 排除 `app.py:529-541`；`card_payment_state.py:33,56,19` | 全部带 platform |
| E4 | 「新卡优先」排序 | `app.py:534-546`；`recharge_log.py:76-86` | 按平台统计。跨平台复用的卡在新平台仍是「新卡」 |
| E5 | 本轮已被其它账号试过 | `app.py:492-514`（`_exclude_used_this_run`）；`worker.py:167-170` | key 改 `(platform, card_number)`。**`:513` 的「全被用过就放行」兜底必须保留**（`test_registry.py:250`） |
| E6 | 卡池为空 | `registration.py:122-123` | 平台无关，报错文案写死 opencode |
| E7 | 下游冷却安全网 | `registration.py:128-135` | 带 platform |
| E8 | 单次最多试 N 张 | `registration.py:193-210`，env `OPENCODE_RECHARGE_MAX_ATTEMPTS` 默认 8 | per-platform 配置 |
| E8b | 订阅同类上限 | `app.py:982`（`SUBSCRIBE_MAX_CARDS_PER_ACCOUNT = 5`）、`app.py:1087` | 同上 |
| E9 | 运行时 in-flight 排他 | 判定 `registration.py:214`；实现 `worker.py:137-156`；释放 `registration.py:298-299` | **需显式决策**：同卡在两平台同时提交支付会否叠加发卡行 velocity？若会，应保留全局 |
| E10 | `processing` 领取占位 | `card_binding.py:62-83,85-94,96-109,111-118` | `claim_batch` 内层 SELECT 加 platform |
| E11 | 已成功绑过的卡 | `card_binding.py:142-155` | 带 platform |
| E12 | Stripe 字段错误卡 / 拒付卡 | `card_binding.py:127-140` / `:183-196` | 字段错误可保持全局（卡数据本身脏），拒付按平台 |

### 写回路径

| 事件 | file:line | 写了什么 |
|---|---|---|
| 支付成功 | `registration.py:229-249` | `card_pool` 标 paid + `valid_cards.record(payment, email)` + `accounts.status='recharged'` + `recharge_logs` success |
| 拒付且**曾成功过** | `registration.py:275-287` | `set_cooldown(24h)`，不判无效 |
| 拒付且**从未成功** | `registration.py:288-292` | `mark_invalid_by_number()` |
| needs_captcha / error / unknown | `registration.py:255-270` | **不消耗卡** |
| 订阅成功 | `app.py:1100-1108` | 同支付成功 |
| 订阅拒付 | `app.py:1109-1121` | **判据与充值链路不一致**：这里用 `valid_card.is_valid(num)`（`app.py:1112`），充值链路用 `recharge_log.last_success_at(num)`（`registration.py:278`） |

## 3. 三个内存注册表

三者都挂在**单例** `AppState` 上（`src/web/app.py:98-100`）。

### `AccountRegistry`（`worker.py:38-103`）
key = `email`（`_claimed: dict[email → worker_id]`，`:51`）。与 `app_state.open_browsers` 双向互斥（`:63,84`，共用一把锁 `:73-87`）。存在理由是硬约束：Chrome profile 目录按 email 命名，`_clear_singleton_locks` 无条件删锁（`:9-11,41-45`）。

多平台冲突取决于 profile 目录怎么分：共用 `data/profiles/<email>` → email 级排他正确且必须；每平台独立 → key 必须变 `(platform, email)`。

关联表 `adspower_profiles`（`database.py:192-199`）：`email TEXT PRIMARY KEY` + `profile_id UNIQUE`，注释明说「一账号一环境是结构性保证」（`database.py:186-190`）。`RECLAIM_STATUS_ORDER`（`adspower_profile.py:21-25`）依赖 `accounts.status`，而 status 本身是单平台语义。

### `PaymentCardRegistry`（`worker.py:106-175`）
key = `card_number`，两级：

- `_in_flight: {card_number → email}`（`:124`）— 此刻正在刷，`release()` 即放（`:158-161`）
- `_used: {card_number → 首个试过它的 email}`（`:135`）— 整轮才清（`release_all()` `:172-175`）

**必然冲突**：`try_acquire` `:151-153` 会让 B 平台被 A 平台正在刷的卡挡住（`registration.py:214-215` 会 continue 跳过）；`_used` 经 `used_numbers()` `:167-170` 喂给 `app.py:506-513`，A 平台试过 → B 平台被排除；`release_all()` 的调用点 `app.py:808,921,1260` 会跨平台清空。

### `ProxyRegistry`（`worker.py:178-225`）
key = `"host:port:username"`（`key_of()` `:192-195`）。**语义上不该加 platform**——代理出口 IP 是全局物理资源。

但有真实 bug 风险：`release_all()` 在 `app.py:922,1261` 被任务收尾无条件调用，两条流水线并发时先结束的会释放另一条正在用的代理。同样问题在 `account_registry.release_all()`（`app.py:920,1259`）。另有兜底破洞：`_acquire_proxy_for` `app.py:707-724` 全忙时按 `account_id % len(usable)` 取模且**不排他**（`:723`）。AdsPower 模式下代理由环境自带，本地代理池不参与（`app.py:714-715`）。

### AppState 本身也是单平台的
`is_running` / `stop_requested` / `success_count` / `fail_count` / `current_action` / `current_card_task_id` 都是单例字段（`app.py:654-658`、`app.py:1152-1157`、`routes.py:36-66`）。

## 4. data/ 目录与导入导出

| 路径 | 说明 |
|---|---|
| `data/openrouter_auto.db`（+ wal/shm） | 主库。默认路径 `database.py:238-244`，创建于 `app.py:1334` |
| `data/openrouter_auto.db.bak-20260726` / `.bak-20260726-import50` / `.bak-verifiedcard` | 三个历史备份 |
| `data/uploads/pool_upload_{group_id}.xlsx` | 上传副本，路径 `routes.py:778-781`。**按 group_id 命名会覆盖** |
| `data/bind_report_*.xlsx`（22 个） | `src/services/card.py:128-151` `export_report()` 生成，**该函数零调用** |
| `credit_cards_template.xlsx` | `src/services/card.py:35-55` |
| `data/profiles/`（65 个） | Chrome profile，按 email 命名 |

**无任何 csv/json 形式的卡池数据。**

导入链路：`POST /api/card-pool/<gid>/upload`（`routes.py:763-799`）→ 存盘 `:778-781` → `card_service.parse_excel()`（`card.py:58-125`，列定义 `:13-17`，必填 `:19-22`，**无 platform 概念**）→ `CardPoolModel.add_cards()`（`card_pool.py:35-70`，去重两层：`find_cards_in_other_groups()` `:22-33` + `UNIQUE` 的 try/except `:68-69`）→ 入库即判过期（`:51-53`）

导出：`GET /api/card/template`（`routes.py:120-125`）、`POST /api/card/history/export`（`:192-270`，18 列）、`GET /api/valid-cards/export`（`:862-919`，22 列，含 `_valid_card_status()` `:822-840` 与 `_attach_recharge_counts()` `:654-661`）

其它端点：merge `:691-718`、move `:721-750`、delete-invalid `:753-760`、delete card `:802-806`、clear `:809-813`、list `:664-688`（逐卡算 is_valid/tds_cooldown/rate_cooldown/bound_email，`:680-684`）

一次性脚本（加 platform 后失效）：`scripts/fix_valid_cards_status.py`、`scripts/fix_failed_accounts_status.py`；另有 8 个 `probe_*.py` 与 `run_subscribe_once.py:82`、`test_opencode_recharge.py:35` 直接调 `get_usable_cards_as_list(group_id)`

## 5. 锁定不变量的测试

共享夹具：`tests/conftest.py:32-45`（`db`，临时库跑全部迁移，新增迁移自动生效）、`:58-70`（`make_cards`）。

### `test_valid_card_invariant.py`
**valid_cards 成员永不被标 invalid**（守卫 `card_pool.py:317-321`）：`:31-41`、`:44-53`、`:56-70`

→ **必须新增**：「卡在 A 平台是 valid，在 B 平台仍能被标 invalid」——这正是当前守卫会挡掉的场景。

### `test_card_pool_bound.py`（改动量最大）
**一卡一账号在卡池留痕，且 bound 不覆盖信息量更大的状态**：`:31-42`（bound 不可选）、`:45-54`（不覆盖 invalid）、`:57-70`（不覆盖 paid）、`:73-81`（不覆盖 expired）、`:84-95`（refresh 不覆盖 bound）、`:98-109`（不进无效桶）、`:112-120`

→ **必须新增**：「卡在 opencode 被 bound，在另一平台仍可选」

### `test_card_claim.py`（12 个用例）
**并发领取的原子性与总量守恒**：`:12-34`（核心）、`:36-46`、`:48-56`、`:58-65`、`:67-79`、`:81-93`、`:95-110`、`:112-116`、`:118-126`、`:128-140`、`:142-150`、`:152-166`（压力版）

同类依赖：`test_reaper.py:19,33,55,76`、`test_pipeline_concurrency.py:83,99,129,146,158,178,194`

### `test_card_move.py`（10 个用例）
**桶口径、重复卡跳过而非删除、非法入参零数据变更**：`:38-47`、`:49-56`、`:58-69`、`:71-82`（**已登记 valid_cards 的卡属 valid 桶** ← 直接依赖 `_bucket_where` `card_pool.py:80,84`）、`:84-107`、`:109-118`、`:120-127`、`:129-138`、`:140-156`

→ 核心问题：**「有效桶按哪个平台算」**。夹具 `:24-35` 用 `group_type='payment'/'bind'` 建组。

### `test_card_fault.py`
**绑卡失败归因白名单**（`utils.py:67-89`），拿不准一律不标 invalid：`:24-45`（参数化）、`:47-49`、`:51-54`（**否定词优先于前缀**，`'mobile phone number'` 那个真实踩过的坑，`utils.py:58-64`）、`:56-`

→ **唯一不需要动 schema 的测试**。但新平台若不用 Stripe，`_CARD_FAULT_PREFIXES`/`_CARD_FAULT_PHRASES`/`_NOT_CARD_PHRASES`（`utils.py:40,47-56,61-64`）需 per-platform 化。

### `test_registry.py`（19 个用例）
**账号与支付卡的运行时排他**：AccountRegistry `:21-42,44-50,52-58,59-70,71-79,80-87,88-113,114-120`；PaymentCardRegistry `:123-128,129-135,136-142,143-164,165-172,174-191`（注释 `:175-190` 记录「硬拦会被误判成卡池耗尽」的坑）、`:192-201,202-210,211-219,220-227`；选卡层 `:236-248`（2026-08-03 实测 5 张卡被双刷）、`:250-259`（**全被试过时必须放行**，不放行会被误判卡池耗尽）、`:261-266`

### 其它
`test_daily_pipeline.py:145-176`（构造 models 字典 `:145-146`、`add_cards` `:150`、`get_usable_cards_as_list` `:176`、裸 SQL `:108-109`）、`test_pipeline_concurrency.py`（`create_batch` 7 处）

## 影响面小结（按风险排序）

1. **`card_pool.status` 必须拆表** — 牵动 13 个方法 + 6 个 API 端点 + 2 个测试文件
2. **`card_payment_state` 主键加 platform** — 改动小但漏了完全没隔离
3. **`valid_cards` UNIQUE 加 platform**，且 `mark_invalid_by_number` 守卫（`card_pool.py:319`）必须同步，否则坏卡在新平台无限循环
4. **`recharge_logs` 加 platform**，尤其 `last_success_at`
5. **`PaymentCardRegistry` key 加 platform**，`ProxyRegistry` 保持全局
6. **`adspower_profiles`**（见 adapter 调研，结论是保持按 email）
7. **`AppState` 按平台切分**才谈得上并发
8. `invoice_payment_state` 是死代码
