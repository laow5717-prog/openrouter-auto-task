# 执行计划：多平台架构改造

## 基线

改造开始前实测（2026-08-03）：

```
.venv/bin/python -m pytest tests/ -q
→ 210 passed, 1 skipped, 1 warning in 34.38s
```

那条 warning 是 `BrowserSession._force_kill` 的 teardown 竞态（`src/browser/driver.py:298` 读 `_close_finished` 时属性尚未建立），与本次改造无关，但清理 driver.py 时顺手修掉。

**每个步骤结束时都要跑这条命令，通过数只能升不能降。**

---

## Stage 0 — 前置清理（独立 commit，可单独 revert）

目标：把 driver.py 从 6157 行降到约 1000 行，删掉会干扰 schema 迁移的死表死函数。**本阶段不引入任何新行为**。

- [ ] **0.1** 备份生产库
  ```bash
  cp data/openrouter_auto.db data/openrouter_auto.db.bak-20260803-preplatform
  ```

- [ ] **0.2** 逐个确认 `src/browser/driver.py:1013-6157` 的函数确实零调用。对每个待删的顶层函数名执行：
  ```bash
  grep -rn '<函数名>' src/ tests/ scripts/ frontend/src/ --include='*.py' --include='*.js' --include='*.vue'
  ```
  只有定义处一处命中才可删。**先列出完整待删清单并逐条核对，再动手删**——这是本阶段唯一的不可逆风险点。

- [ ] **0.3** 保留其中的通用 CDP 工具，移到 driver.py 前段（约 1012 行之前的通用区）：
  - `_cdp_click_at`（`driver.py:1296`）
  - `_get_viewport_coords`（`driver.py:1308`）
  - `_collect_all_child_frames`（`driver.py:4919`）
  - `_extract_text_from_dom_node`（`driver.py:4995`）

- [ ] **0.4** 删除 `_CREDIT_BALANCE_URL_MARK`（`driver.py:397`）及 `BrowserSession._on_response`（`:180-212`）、`_capture_credit_balance`（`:213-234`）里对它的引用。这是 Cloudflare AI Gateway 专有常量污染了通用 `BrowserSession` 类，删掉后余额读取完全由 adapter 负责。

- [ ] **0.5** 修 `_force_kill` 的属性竞态：在 `BrowserSession.__init__` 里初始化 `_close_finished = False`。

- [ ] **0.6** 删死表 `invoice_payment_state`：
  - 新增 `_SCHEMA_V13 = "DROP TABLE IF EXISTS invoice_payment_state;"`
  - 删 `src/models/invoice_payment_state.py`
  - 删 `src/web/app.py:25` 的 import 与 `app.py:1347` 的 models 注册

- [ ] **0.7** 删死函数：
  - `src/services/email.py` 的 `wait_for_verification_email`（`:331-385`）
  - `src/utils.py:184-190` 的 `extract_verification_link`
  - `AccountModel.get_email_password`（`src/models/account.py:114-122`）
  - `AccountModel.update_bound_cards`（`:52-74`）及测试桩 `tests/test_bind_retry.py:34`
  - `accounts.bound_card_count` / `cards_checked_at` 两列**先不删**（DROP COLUMN 不可逆，且它们不妨碍迁移），只删代码路径；前端 `Accounts.vue:72-80` 对应展示一并删

- [ ] **0.8** `src/services/email.py:148-209` 的 `wait_for_login_code`：把硬编码过滤词 `'opencode'`（`:184`）改为参数 `sender_hints: list[str]`，默认空列表表示不过滤。它当前唯一调用者在死代码里，改完调用点一起删。

**验证**
```bash
.venv/bin/python -m pytest tests/ -q          # ≥ 210 passed
wc -l src/browser/driver.py                   # 应约 1000
grep -rn 'invoice_payment_state' src/ tests/  # 应无输出
.venv/bin/python -c "from src.web.app import AppState; print('import ok')"
```

**Review Gate G0**：确认删除清单里每一项都有 grep 零调用证据；确认 `git revert <commit>` 后测试仍全绿。

**Rollback**：`git revert` 本 commit。数据库侧只多了一个 V13（DROP 死表），revert 代码后 `user_version=13` 高于代码的 12，`_migrate` 的 `if current >= target: return` 会直接跳过，不会报错。

---

## Stage 1 — 数据层平台化

目标：schema 迁移 + 模型方法加 platform 参数。**本阶段结束时 opencode 流程行为必须零变化**（所有调用点传 `platform='opencode'`）。

### 1A 身份分层

- [ ] **1A.1** 加固 `Database._migrate`：`ALTER TABLE ADD COLUMN` 类迁移改为 Python 侧先查 `PRAGMA table_info` 再执行，避免列已存在时整个 `executescript` 失败（迁移幂等性的前提，AC9）。

- [ ] **1A.2** `_SCHEMA_V14`：建 `platform_accounts` 表 + `accounts.identity_status` 列（见 design.md 第一节 schema）。

- [ ] **1A.3** V14 数据搬迁：
  - `accounts.status` → `accounts.identity_status`，按 design.md 第一节对照表映射
  - 平台层状态（`archived`/`recharged`/`subscribed`）或有余额/apikey 的账号 → 建 `platform_accounts` 行，`platform='opencode'`
  - 纯身份层状态的账号**不建**平台行
  - `accounts.status` 列保留不删（回滚保险）

- [ ] **1A.4** 新建 `src/models/platform_account.py`：`PlatformAccountModel`，方法对齐现有 `AccountModel` 的平台相关部分（`update_status` / `update_balance` / `update_apikey` / `get_by_platform` / `upsert`），全部带 `platform` 首参。

- [ ] **1A.5** 瘦身 `AccountModel`：移除已搬走的方法，模块 docstring 写明「本表现在只装邮箱与 GitHub 身份，不装平台状态；邮箱与 GitHub 当前是 1:1，如需一邮箱多 GitHub 账号再拆第三张表」。

- [ ] **1A.6** 在 `src/utils.py` 定义 `IDENTITY_TERMINAL_STATUSES` 与 `PLATFORM_TERMINAL_STATUSES`，替换 `src/web/app.py:689-691` 与 `src/api/routes.py:952` 两处不一致的硬编码集合。

**验证**：`pytest tests/ -q` ≥ 210；新增 `tests/test_platform_account.py` 覆盖 AC1（同邮箱两平台各一行，互不覆盖）。

### 1B 卡池平台化

按依赖顺序做，每小步单独跑测试。

- [ ] **1B.1** `_SCHEMA_V15`：建 `card_platform_state`；把 `card_pool.status ∈ {bound, invalid, paid}` 搬过来（`platform='opencode'`），随后置 `card_pool.status=''`。`expired` 行不动。

- [ ] **1B.2** `src/models/card_pool.py` 全量加 `platform` 参数（13 个方法）：
  `_bucket_where` / `get_by_group` / `count_buckets` / `delete_invalid_by_group` / `move_non_invalid_to_group` / `move_bucket_to_group` / `get_usable_cards_as_list` / `refresh_expired_status` / `mark_status_by_number` / `mark_invalid_by_number` / `mark_bound_by_number` / `get_locations_by_number`

  `get_usable_cards_as_list` 的 SQL 改为 `card_pool LEFT JOIN card_platform_state ON 卡号 AND platform=?`，排除条件见 design.md 第二节。

- [ ] **1B.3** ⚠️ **反转 valid_cards 守卫**（AC5，本次最高风险项）。三处：
  - `mark_invalid_by_number` 的守卫子查询（`card_pool.py:317-321`）
  - `_bucket_where` 的 valid/unverified 桶定义（`card_pool.py:80,84`）
  - `move_non_invalid_to_group(bucket='valid')`（`card_pool.py:157`）

  全部加 `WHERE platform=?`。**先写测试再改代码**：`tests/test_valid_card_invariant.py` 新增「卡在 A 平台是 valid，在 B 平台仍能被标 invalid」，确认它在改前红、改后绿。

- [ ] **1B.4** `_SCHEMA_V16`：
  - 重建 `valid_cards`（`UNIQUE(card_number, source_type, platform)`）：create-new → copy 填 `platform='opencode'` → drop → rename
  - 重建 `card_payment_state`（`PRIMARY KEY(card_number, platform)`）：同上
  - `recharge_logs` / `card_bindings` 加 `platform` 列，既有行填 `'opencode'`

- [ ] **1B.5** 对应模型加 platform 参数：
  - `ValidCardModel.record` / `get_bound_email` / `is_valid` / `get_all_for_export`
  - `CardPaymentStateModel` 全部方法（含 `set_tds`/`in_tds_cooldown` 别名）
  - `RechargeLogModel` 的 6 个派生查询，**尤其 `last_success_at`**——它是「拒付时判冷却还是判废」的判据，漏了会让坏卡在新平台无限循环
  - `CardBindingModel.create_batch` / `claim_batch`（内层 SELECT 加 `AND platform=?`）/ `get_successfully_bound_card_numbers` / `get_declined_card_numbers` / `count_by_emails`
  - `get_stripe_field_error_card_numbers` **保持全局不加 platform**（卡数据本身脏，与平台无关）
  - `reset_all_processing` / `reap_stale` 的跨平台语义：进程重启时应全量重置，因此**不加 platform 过滤**，用新测试用例钉死这个决定

- [ ] **1B.6** `src/web/worker.py` 的 `PaymentCardRegistry`：
  - `_used` 的 key 改为 `(platform, card_number)`，`used_numbers(platform)` / `release_all(platform)`
  - `_in_flight` 的 key **保持 `card_number`**（R2.6），`try_acquire` 签名不变
  - `AccountRegistry` / `ProxyRegistry` 不动

- [ ] **1B.7** `AppState._eligible_cards(platform, group_id, exclude_used)` 及其 12 个调用点（`app.py:571,728,765,773,928,1084,1171,1190,1205,1245,1266`、`routes.py:944,991`）。

**验证**
```bash
.venv/bin/python -m pytest tests/ -q
```
新增/改写的测试见 design.md 第七节表格。至少覆盖 AC2/AC3/AC4/AC5/AC6/AC7。

**Review Gate G1**：
1. 逐条走查 AC1–AC9，每条指出对应的测试用例。
2. **专项复核 1B.3 的三处守卫**——这是 review 的重点，不是走过场。
3. 在生产库副本上跑一遍迁移，比对迁移前后各表行数：
   ```bash
   cp data/openrouter_auto.db /tmp/migrate_test.db
   .venv/bin/python -c "from src.models.database import Database; Database('/tmp/migrate_test.db')"
   # 比对 accounts / card_pool / valid_cards / recharge_logs / card_bindings 行数
   ```
4. 重复执行第 3 步，确认幂等（AC9）。

**Rollback**：恢复 `data/openrouter_auto.db.bak-20260803-preplatform` + `git revert`。旧列保留意味着即使只回滚代码，项目仍可运行。

---

## Stage 2 — 抽象层

**关键纪律：文件移动与逻辑修改必须分成不同 commit。** 混在一起做，diff 会大到无法 review，而这批 Stripe 代码是踩坑换来的，review 不了就等于没有安全网。

### 2A 纯搬迁（零逻辑变更）

- [ ] **2A.1** 建目录 `src/platforms/`、`src/payments/`、`src/identity/`。

- [ ] **2A.2** `git mv` 三个 opencode 模块到 `src/platforms/opencode/`，只改 import 路径，**一行逻辑不动**。

- [ ] **2A.3** 从 `opencode_billing.py` 抽出 20 个 Stripe 函数到 `src/payments/stripe_checkout.py`：
  `_stripe_frame` / `_wait_stripe_frame` / `pick_currency_usd` / `select_card_method` / `_gen_us_phone` / `fill_phone_if_present` / `fill_card_and_address` / `uncheck_save_info` / `check_ai_agent_consent` / `_form_ready_state` / `click_pay` / `_captcha_challenge_present` / `_threeds_challenge_present` / `_count_top_layer_overlays` / `_threeds_failure_modal` / `_close_threeds_modal` / `_threeds_challenge_lightbox` / `_close_challenge_lightbox` / `_DECLINE_HINTS` / `_THREEDS_CHALLENGE_GRACE_SEC`

  合并 `opencode_subscribe._checkout_frames`（`:125`）与 `_stripe_frame`——两者重复。

- [ ] **2A.4** `_step`（`opencode_billing.py:73`）上移到基础设施层（`src/browser/` 或 `src/platforms/base.py`），两个模块共用。

**验证**：`pytest tests/ -q` ≥ 210，且 `git diff --stat` 显示改动集中在 import 行。这一步跑通即证明搬迁无损。

### 2B 定义协议

- [ ] **2B.1** `src/platforms/base.py`：`PlatformAdapter` Protocol + `SessionResult` / `PaymentResult` dataclass（见 design.md 第三节）。

- [ ] **2B.2** `src/platforms/__init__.py`：注册表 `register` / `get` / `all_slugs`。

- [ ] **2B.3** `OpencodeAdapter`：组装 login/billing/subscribe 三个模块，实现 7 个接口方法。平台配置（`max_card_attempts` / `recharge_skip_balance` / `default_topup_amount`）从 env 变量搬到 adapter 属性，env 保留为覆盖手段。

- [ ] **2B.4** `src/identity/github.py`：`github_signup_service.signup_one` 的 `then_opencode: bool` 改为 `post_provision: PlatformAdapter | None`，删掉 `from src.browser.opencode_login import ...`（`github_signup_service.py:26`）。两个生产调用点本来就传 `then_opencode=False`，改动无风险。

### 2C 编排层接入

- [ ] **2C.1** `src/services/registration.py`：`recharge_account(..., adapter)`，把 3 个 `ob.*` 调用换成 `adapter.*`。骨架逻辑（余额预检 → 逐卡试付 → outcome 分派 → 记账）**一行不动**。

- [ ] **2C.2** `src/web/app.py`：
  - `AppState` 新增 `platform` 字段，`run_daily_pipeline(platform, ...)` / `run_daily_subscribe_pipeline(platform, ...)`
  - `_subscribe_one_account`（`app.py:1041-1044`）改为 `adapter = platforms.get(self.platform)`
  - `_patch_prints`（`app.py:1285-1286`）改为遍历 `adapter.module_names()`

- [ ] **2C.3** `src/api/routes.py:438`：`open_account_browser` 改为按请求参数解析 adapter；余额轮询（`:471`）改用 `adapter.read_balance_from_current_page`。

- [ ] **2C.4** `src/models/adspower_profile.py:87-93` 的回收候选查询改为两级判定 + `NOT EXISTS`（design.md 第四节）。

- [ ] **2C.5** stub adapter（AC12）：`tests/` 下实现 `StubAdapter`，用它跑通一遍充值编排，证明新增平台无需改编排层。

**验证**
```bash
.venv/bin/python -m pytest tests/ -q
grep -rn 'from src.browser.opencode' src/     # 应无输出（AC11）
grep -rn 'from src.platforms.opencode' src/   # 只应出现在注册处
```

**Review Gate G2**：
1. 逐一比对 `PaymentResult` 六个 outcome 在编排层的处置分支与改造前是否一一对应（AC13）。**重点确认 `error`/`unknown`/`needs_captcha` 仍不消耗卡**。
2. 确认 2A 的 Stripe 搬迁 commit 里没有夹带逻辑修改。
3. AdsPower 回收判据用 `NOT EXISTS` 而非 `IN`（AC15）。

---

## Stage 3 — API 与前端

- [ ] **3.1** `POST /api/daily/start`、`POST /api/daily/subscribe/start` 新增必填 `platform` 参数。
- [ ] **3.2** 卡池类读接口的 `platform` 参数**设为必填，缺失返回 400**，不做默认值兜底——猜错平台会返回混合数据，比报错糟糕得多。
- [ ] **3.3** 账号列表接口：不传 platform 时返回全部身份，各平台状态以紧凑结构展开。
- [ ] **3.4** 前端顶栏平台选择器，选中值存 `frontend/src/stores/app.js`，所有请求自动带上。
- [ ] **3.5** 重建 `Accounts.vue` 的 status 筛选：拆成「身份状态」与「平台状态」两个下拉，取值对齐实际写入点（现有下拉缺 6 个真实状态、多 5 个死状态）。
- [ ] **3.6** `npm run build` 重新生成 `static/assets/`。

**验证**：手动过一遍 Web UI——平台切换、账号列表、卡池列表、启动流水线。

---

## Stage 4 — 端到端回归（AC10）

- [ ] **4.1** 在生产库上执行迁移（已备份）。
- [ ] **4.2** 跑通一个账号的 opencode 每日充值流水线，比对行为与改造前一致。
- [ ] **4.3** 跑通一个账号的 opencode 订阅流水线。
- [ ] **4.4** 首次上线时把 `config.yaml` 的 `adspower.reclaim_batch` 临时降到 1，观察一轮回收行为是否符合预期，确认无误后改回 3。
- [ ] **4.5** 全量 AC 走查，勾选 prd.md 的验收清单。

---

## 提交划分

| commit | 内容 | 可独立 revert |
|---|---|---|
| 1 | Stage 0 前置清理 | ✅ |
| 2 | Stage 1A 身份分层（schema + 模型） | ✅（配合备份） |
| 3 | Stage 1B 卡池平台化 | ✅（配合备份） |
| 4 | Stage 2A 纯文件搬迁 | ✅ |
| 5 | Stage 2B+2C 抽象层与编排接入 | ✅ |
| 6 | Stage 3 API 与前端 | ✅ |

Stage 4 不产生 commit，只做验证与勾选。

---

## 需要在实施中留意的既有偏差

调研中发现的、与本次改造相邻但不属于本次范围的问题，遇到时记一笔即可，不要顺手改（会污染 diff）：

- `src/web/app.py:707-724` 的 `_acquire_proxy_for` 全忙兜底按 `account_id % len(usable)` 取模且**不排他**，多平台下走到这个分支的概率会上升。
- `ProxyRegistry.release_all()` / `AccountRegistry.release_all()` 在任务收尾被无条件调用（`app.py:920-922`、`app.py:1259-1261`），若将来支持跨平台并发，先结束的流水线会误清另一条的占用。本次单平台串行不受影响。
- 订阅链路与充值链路的「是否曾成功」判据不一致：`app.py:1112` 用 `valid_card.is_valid`，`registration.py:278` 用 `recharge_log.last_success_at`。两处都要加 platform，届时可考虑统一，但统一本身是行为变更，需单独评估。
- `data/uploads/pool_upload_{group_id}.xlsx`（`routes.py:778-781`）按 group_id 命名会互相覆盖。
- `src/services/card.py:128-151` 的 `export_report` 零调用，`data/` 下有 22 个它生成的历史文件。
