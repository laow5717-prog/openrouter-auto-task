# 多平台架构改造：邮箱共用、平台账号与卡池占用独立

## Goal

把当前写死 opencode 的单平台流程改造成可承载多平台。同一个邮箱可以在多个平台各自独立注册、各自独立的账号密码与状态；同一张信用卡在 A 平台的占用/绑定/冷却/判废，不影响它在 B 平台被使用。

本次交付**抽象层 + opencode 迁移**，不实现 infron.ai 的具体流程（留作后续任务）。

## Background

现状是单平台硬编码，四处硬约束挡住多平台：

1. `accounts.email UNIQUE`（`src/models/database.py:13-21`）—— 一行同时装邮箱凭据和 opencode 平台数据，同邮箱放不下第二套平台状态。
2. 卡的「已占用」语义分散在 6 个载体且**全部无平台维度**：`card_pool.status='bound'/'invalid'`、`valid_cards.source_email`、`card_payment_state`（主键=卡号）、`recharge_logs` 的实时派生统计、`card_bindings.status='success'`、`PaymentCardRegistry` 内存表。
3. `adspower_profiles.email PRIMARY KEY` + 回收候选按 `accounts.status` 排序（`src/models/adspower_profile.py:22-24,87-93`）—— 单列 status 无法表达 per-platform 状态，多平台下会误删还在用的环境。
4. 浏览器层无平台接口，`opencode_login/billing/subscribe` 被编排层直接 import。

## Requirements

### R1 身份分层：邮箱 / GitHub / 平台账号三层拆分

- **R1.1** 邮箱身份（`email`、`email_password`、`email_verify_link`）独立成一层，多平台共用同一行，任何平台流程都不得修改它。
- **R1.2** GitHub 身份（现 `accounts.login_password` 实为 GitHub 密码、GitHub 用户名）独立成一层。GitHub 账号是跨平台复用的 OAuth 身份供给，不属于任何单一平台。
- **R1.3** 平台账号层按 `(platform, email)` 唯一，各自持有：该平台的登录密码（若平台走独立密码注册）、状态、余额、API key、租户/工作区 id。
- **R1.4** 平台登录密码**每平台独立随机生成**，不跨平台复用。opencode 走 GitHub OAuth，该字段留空。
- **R1.5** 现有全部账号数据迁移后归属 `platform='opencode'`，迁移前后 opencode 流水线行为零变化。

### R2 卡池占用按平台隔离

- **R2.1** 一张卡在 A 平台被标记 `bound`（一卡一账号永久绑定），在 B 平台仍可被选中使用。
- **R2.2** 一张卡在 A 平台被判 `invalid`（拒付判废），在 B 平台仍可被选中使用。
- **R2.3** 一张卡在 A 平台进入 3DS / 速率冷却，不影响它在 B 平台立即可用。
- **R2.4** 一张卡在 A 平台成功支付过（进入 `valid_cards`），**不得**阻止它在 B 平台被判 `invalid`。这是当前守卫子查询（`src/models/card_pool.py:317-321`）的直接反面，不改会让坏卡在新平台无限循环。
- **R2.5** 「本轮已被其它账号试过」的去重（`PaymentCardRegistry._used`）按平台隔离。
- **R2.6** 「此刻正在提交支付」的排他（`PaymentCardRegistry._in_flight`）**保持全局**，不按平台隔离。理由：同一张卡在两处同时向发卡行提交扣款会叠加 velocity 风控，是真实的业务风险，而非并发正确性问题。
- **R2.7** 卡的有效期过期（`expired`）保持全局判定——它与平台无关。

### R3 平台适配器抽象

- **R3.1** 定义 `PlatformAdapter` 接口，覆盖：会话建立、租户 id 提取、余额读取、充值发起与结果判定、订阅（可选能力）。
- **R3.2** opencode 收编为第一个实现，现有 `opencode_login/billing/subscribe` 的行为语义保持不变。
- **R3.3** Stripe Checkout 相关操作（选币种、填卡、提交、3DS/验证码识别）抽为**支付供应商层**，与平台适配器分离——它已在事实上被两条流程共享（`src/browser/opencode_subscribe.py:16-24` 从 billing 模块 import 了 14 个符号）。
- **R3.4** GitHub 注册抽为 `IdentityProvider`，与平台适配器解耦。`github_signup_service.signup_one` 的 `then_opencode` 参数改为由编排层注入。
- **R3.5** 编排层（`registration.recharge_account`、`AppState._recharge_one_account` / `_subscribe_one_account`）改为面向 adapter 编程，不再直接 import opencode 模块。
- **R3.6** `PaymentResult` 的 outcome 语义必须保持不变，尤其 `error` = 不消耗卡、`needs_captcha` = 账号级风控立即停手。

### R4 AdsPower 环境：保持按 email 分配

- **R4.1** `adspower_profiles` 主键**保持 `email`**，不按平台拆分。理由：环境的核心资产是 GitHub 授权态（天然跨平台共享），而平台自身 session 本来就活不过浏览器重启；按平台拆等于把 12 个环境的硬配额除以平台数，换取一份短命 session，是负收益。
- **R4.2** 环境回收候选的判据从「单列 `accounts.status`」改为「**该邮箱在所有平台都已进入终态**才可回收」。不改会误删还在用的环境。

### R5 执行模型：单平台串行

- **R5.1** 本次**不支持**两个平台同时跑流水线。`AppState` 保持全局单例互斥，一次只跑一个平台，切换平台即可。
- **R5.2** 流水线启动时必须显式指定目标平台，平台标识贯穿整条链路直到数据落库。
- **R5.3** `ProxyRegistry` 保持全局排他——代理出口 IP 是全局物理资源，跨平台共用同一 IP 一样会被关联。

### R6 前置清理（独立步骤，先于改造）

- **R6.1** 删除 `src/browser/driver.py:1013-6157` 的 Cloudflare 时代遗留（约 5100 行，占该文件 83%，已确认 `src/` 内零调用）。其中的通用 CDP 工具（`_cdp_click_at`、`_get_viewport_coords`、`_collect_all_child_frames`、`_extract_text_from_dom_node`）需保留。
- **R6.2** 删除 `invoice_payment_state` 死表及其模型（全仓仅 import 与注册两处，无任何调用方）。
- **R6.3** 删除 `src/services/email.py` 的 `wait_for_verification_email`（零调用）与 `src/utils.py:184-190` 的 opencode 硬编码链接提取（仅被前者调用）。`wait_for_login_code` 的 opencode 过滤词改为参数化。
- **R6.4** 删除 `AccountModel.get_email_password`（零调用）、`update_bound_cards` 及 `bound_N_cards` 状态编码（零生产调用）。
- **R6.5** 清理必须是独立 commit，与后续改造分离，可单独回滚。

## Out of Scope

- infron.ai 或任何第二个平台的具体注册/登录/支付流程实现。
- 两个平台同时并发执行流水线。
- `AppState` 的按平台切分（`is_running` / 计数器 / 停止旗标）。
- AdsPower 环境配额的扩容或多平台争抢策略。
- 前端的多平台并排展示（本次只需平台切换 + 数据按平台过滤）。

## Acceptance Criteria

### 数据隔离

- [x] AC1 同一邮箱可在两个不同 platform 下各有一条平台账号记录，各自独立的密码、状态、余额、apikey，互不覆盖。
- [x] AC2 一张卡在 platform A 标 `bound` 后，在 platform B 的可选卡列表中仍然出现。
- [x] AC3 一张卡在 platform A 标 `invalid` 后，在 platform B 的可选卡列表中仍然出现。
- [x] AC4 一张卡在 platform A 进入 3DS 冷却后，在 platform B 的 `in_cooldown` 判定为 False。
- [x] AC5 一张卡在 platform A 已进入 `valid_cards`，在 platform B 被拒付时**能够**被标记为 `invalid`（当前守卫的反面）。
- [x] AC6 `PaymentCardRegistry` 在 platform A 记录的 `_used` 不影响 platform B 的选卡；而 `_in_flight` 仍然全局排他（同一卡号在 A 占用时 B 无法获取）。
- [x] AC7 卡的 `expired` 判定不受平台影响，在所有平台一致。

### 迁移安全

- [x] AC8 迁移脚本执行后，全部既有 accounts / card_bindings / valid_cards / recharge_logs / card_payment_state 数据归属 `platform='opencode'`，无数据丢失。
- [x] AC9 迁移可在生产库副本上重复执行且幂等。
- [x] AC10 迁移后 opencode 每日充值流水线与订阅流水线端到端行为与迁移前一致（至少各跑通一个账号）。

  **2026-08-03 20:30 在生产环境实跑验证通过。** AdsPower 在线、真实账号、真实信用卡。

  充值链路（`cunninghamh22@hotmail.com`，分组 6）：AdsPower 环境接管并复用登录态 →
  `adapter.ensure_session` 检出已登录 → `adapter.read_balance` 未达归档阈值 →
  逐卡 `adapter.top_up` 走 Stripe Checkout。试满 8 张停手（上限来自 adapter 而非硬编码），
  7 张真实拒付判废、1 张 `unknown`（120s 未确认）**未被消耗**。

  订阅链路（同账号 + `jot763@hotmail.com`）：`adapter.ensure_session` → capabilities
  含 subscribe → 逐卡 `adapter.subscribe`，5 张全部 `unknown`（200s 未确认订阅结果），
  **一张都没被消耗**，账号转 `registered_only` 后正常轮转到下一个；手动停止后
  worker、代理、AdsPower 环境全部干净释放。

  逐条核对结果：
  - 判废写的是 `card_platform_state(卡号,'opencode')`，不是全局 `card_pool.status`
  - `unknown` 既不写平台状态也不进冷却（AC13 在真实数据上成立，两条链路都验了）
  - 身份层 `identity_status` 全程未被平台流程改动
  - 付款未成功则不建 `platform_accounts` 行
  - **跨平台隔离**：两轮共判废 9 张卡后，opencode 视角分组 6 可选 432 张，
    另一平台视角仍是 971 张——这 9 张判废对它零影响
  - 启动门与流水线的账号筛选数一致（都是 33），旧的「启动说 N 个跑起来 M 个」已消失

  未覆盖：没有一笔**成功**付款（卡池里的卡当前全部拒付或超时），所以
  `outcome='success'` 的分支——标 `paid`、写 `valid_cards`、置 `recharged`、
  回写余额——只有单元测试覆盖，没有真实付款印证。有能付通的卡时值得再跑一次确认。

  另：`adspower.reclaim_batch` 本次未调低观察，因为两轮都没触发配额回收
  （环境是复用的）。首次真正撞配额时仍建议按 Stage 4.4 小步观察。

### 抽象层

- [x] AC11 `src/` 生产代码中不再存在 `from src.browser.opencode_*` 的直接 import（探针脚本除外），改为经 adapter 注册表解析。
- [x] AC12 新增一个平台只需实现 `PlatformAdapter` 接口并注册，无需改动编排层代码。以一个最小的 stub adapter 验证这一点。
- [x] AC13 `PaymentResult` 的 5 个 outcome 在编排层的处置分支与改造前逐一对应，`error` 仍不消耗卡。

### AdsPower

- [x] AC14 `adspower_profiles` 仍以 email 为主键，一邮箱一环境。
- [x] AC15 回收候选判定为「该邮箱在所有已注册平台均为终态」；存在任一平台处于非终态时该环境不被回收。

### 清理与回归

- [x] AC16 前置清理为独立 commit，`git revert` 该 commit 后项目仍可运行。
- [x] AC17 清理后 `src/browser/driver.py` 行数降至约 1000 行，且现有测试全绿。
- [x] AC18 现有测试套件（`tests/`）全部通过。涉及卡池不变量的测试（`test_valid_card_invariant` / `test_card_pool_bound` / `test_card_claim` / `test_card_move` / `test_registry`）需补充跨平台对照用例。
- [x] AC19 `test_registry.py:250-259` 的「全被试过时必须放行」兜底用例在 per-platform 化后仍然通过——这条兜底不放行会导致卡池被误判耗尽。

## Notes

- 迁移前必须备份 `data/openrouter_auto.db`（`data/` 下已有三个历史备份，沿用同样的命名习惯）。
- `card_groups` 不适合承载平台维度：一张卡物理上只能属于一个分组（`src/models/card_pool.py:22-33` 主动阻止跨组同号），用分组表达平台等于「一张卡只能给一个平台用」，与需求相反。
- 两处 status 过滤集合本就不一致（`src/web/app.py:689-691` 四元组 vs `src/api/routes.py:952` 二元组），改造时抽成共享常量顺手修掉。
- `accounts.id` 被业务消费（`src/web/app.py:779,783` 的代理取模兜底），拆表时需明确该 id 归属哪张表。
