# 接入 infron.ai 平台适配器

## Goal

在多平台框架上接入第二个平台 infron.ai：邮箱 magic link 登录（首次登录即建号）、
Top Up 弹窗选金额、Stripe 付款、余额读取。同一个卡池、同一批邮箱身份，与 opencode
各跑各的、互不干扰。

这同时是对 `PlatformAdapter` 抽象的第一次真实检验——上一个任务用虚构的 StubAdapter
证明了「加平台不用改编排层」，这次用真站点验证它。

## Background

站点实勘结论见 `research/infron-site-survey.md`，要点：

- infron.ai 是与 opencode 同类的 AI 模型聚合网关，**纯 credits 充值制、无订阅**
- 登录是**邮箱 magic link**，首次登录自动建号；不需要密码、不需要 GitHub
- 入口挂 **Cloudflare Turnstile**：Patchright 过不去，**AdsPower 指纹环境能过**
- 充值是 `/dashboard/credits` 的 **Top Up 弹窗**：选金额 → 选 Card → `Pay $X`，
  **不需要预先绑卡**，卡在「下一步」填
- 付款走 **Stripe**，且带 **invisible hCaptcha**（与 opencode 同款）

关键的资源发现：库里 26 个 `identity_status='failed'` 的账号（GitHub 注册失败、
无 GitHub 密码）对 opencode 毫无用处，但都有 hotmail 密码与 ruoanzhu 收信链接，
**在 infron 上是完全可用的身份**。

## Requirements

### R1 适配器

- **R1.1** 新增 `src/platforms/infron/`，实现 `PlatformAdapter`，slug 为 `infron`，
  在注册表注册。`capabilities` 只含 `CAP_TOPUP`——infron 无订阅，`subscribe`
  不实现（编排层已按 capabilities 跳过）。
- **R1.2** 平台参数按 infron 自身风控设定，不沿用 opencode 的值。
- **R1.3** 适配器只做站点特有的部分。付款表单操作若与 Stripe 通用形态一致，
  复用 `src/payments/stripe_checkout.py`；确实不同的才在适配器内新写。

### R2 会话：邮箱 magic link

- **R2.1** `ensure_session` 走邮箱 magic link：填邮箱 → 点 Sign In → 从
  `Credentials.verify_link`（ruoanzhu 收信链接）取回登录链接 → 打开 → 落地
  `/dashboard`。
- **R2.2** 已登录时（AdsPower 环境有 cookie）直接复用，不重复走 magic link。
  这是主路径——每轮都发一封信既慢又可能触发发信频控。
- **R2.3** 收到的 magic link **一次性、30 分钟有效**，且必须只认**本次发起之后**
  到达的邮件，不能拿收件箱里的旧链接（同 opencode 收码的时间闸门原则）。
- **R2.4** Cloudflare Turnstile 质询页需要等待放行（实测约 30 秒）。等待期间不得
  误判为登录失败。
- **R2.5** 无 `verify_link` 的账号直接返回失败并说明原因，不静默卡住。

### R3 充值

- **R3.1** `top_up` 流程：进 `/dashboard/credits` → 点 `Top Up` → 等弹窗
  （实测需 15 秒以上）→ 选金额 → 选 `Card` → 点 `Pay $X` → 完成 Stripe 付款。
- **R3.2** `Pay` 按钮文案带的是**含手续费的总额**（如充 $100 显示 `Pay $105.35`），
  定位时不得写死金额。
- **R3.3** hCaptcha hook 必须在**点 Top Up 之前**装好——弹窗一出现 hCaptcha 就挂上了。
- **R3.4** 充值金额可配置，默认取档位值。自定义金额输入框可用但非必需。
- **R3.5** `PaymentResult.outcome` 的六个取值语义与 opencode 完全一致，尤其
  `needs_captcha` / `error` / `unknown` **不消耗卡**。

### R4 余额与 API key

- **R4.1** `read_balance` 从 `/dashboard/credits` 的 `Available Balance` 区块读，
  返回美元浮点。
- **R4.2** `read_balance_from_current_page` 不做导航，只从当前页抠。
- **R4.3** `fetch_apikey` 尽力而为：`/dashboard/apiKeys` 列表页是**脱敏**的
  （`sk-BOK***w8F`），拿不到明文就返回 None，不阻塞主流程。

### R5 必须走 AdsPower

- **R5.1** infron 链路只支持 AdsPower 指纹环境。Patchright 与本地 Chrome 过不了
  入口的 Cloudflare Turnstile。
- **R5.2** 这条约束要在适配器文档里写明，避免有人拿 `create_driver` 调试白白卡住。

### R6 数据隔离（复用既有框架，只需验证）

- **R6.1** infron 的账号状态写 `platform_accounts(platform='infron', email)`，
  与 opencode 那行互不影响。
- **R6.2** 卡的判废/冷却写 `card_platform_state(卡号, 'infron')`，
  同一张卡在 opencode 的遭遇不影响它在 infron 被选中，反之亦然。
- **R6.3** 前端平台选择器能切到 infron，各列表按平台给出对应视角。

## Out of Scope

- **绑卡流程**（`/dashboard/user/payments` 的 Payments Method）。充值不需要它。
- Google / GitHub OAuth 登录、`Sign in with password` 密码模式——邮箱 magic link
  已够用且最省资源。
- Auto Top Up、预算告警、BYOK、折扣码等 infron 的其它功能。
- 两个平台同时并发跑流水线（`AppState` 仍是单例，沿用上一个任务的结论）。
- 为 infron 单独建账号池——直接用现有 `accounts` 表里的邮箱身份。

## Acceptance Criteria

### 适配器与抽象

- [ ] AC1 `platforms.get('infron')` 返回适配器且满足 `PlatformAdapter` 协议，
  `capabilities == {CAP_TOPUP}`。
- [ ] AC2 接入 infron **没有改动任何编排层代码**（`registration.recharge_account`、
  `AppState._recharge_one_account`、`routes.py` 的流水线入口）。用 git diff 证明：
  改动只落在 `src/platforms/infron/`、`src/platforms/__init__.py` 的注册处，
  以及必要的测试。
- [ ] AC3 `subscribe` 未实现时，编排层按 capabilities 跳过而不是抛异常。

### 会话

- [ ] AC4 首次登录（环境无 cookie）能走通 magic link 全程并落地 `/dashboard`，
  `SessionResult.ok=True` 且 `tenant_id` 有值（若 infron 无租户概念则明确返回 None
  并在 design 里说明）。
- [ ] AC5 已登录环境再次 `ensure_session` **不发新邮件**，直接复用。
- [ ] AC6 只接受本次发起之后到达的 magic link；收件箱里的旧链接不被误用。
- [ ] AC7 无 `verify_link` 的账号返回 `ok=False` 且 detail 说明原因。

### 充值

- [ ] AC8 `top_up` 能走到 Stripe 付款环节并返回符合契约的 `PaymentResult`。
- [ ] AC9 真实拒付返回 `outcome='failed'`，且卡按「本平台是否成功过」判冷却或判废。
- [ ] AC10 `needs_captcha` / `error` / `unknown` 三种结果**不消耗卡**（不写平台状态、
  不进冷却）。
- [ ] AC11 `Pay` 按钮的定位不依赖具体金额（改充值额后仍能点中）。

### 余额

- [ ] AC12 `read_balance` 在余额为 0 时返回 `0.0` 而不是 None——两者语义不同，
  None 应只表示「读不到」。
- [ ] AC13 充值成功后余额回写到 `platform_accounts(platform='infron').credits_balance`。

### 跨平台隔离（真实数据验证）

- [ ] AC14 同一邮箱同时有 opencode 与 infron 两行平台账号，状态互不覆盖。
- [ ] AC15 一张卡在 infron 被判废后，opencode 视角的可选卡集合不变；反之亦然。
- [ ] AC16 前端切到 infron 后，账号列表的平台状态列、卡池桶计数都按 infron 给。

### 端到端

- [ ] AC17 用一个 `identity_status='failed'` 的账号跑通完整链路：
  AdsPower 起环境 → 过 Turnstile → magic link 登录建号 → 读余额 → Top Up →
  逐卡试付 → 结果与记账全部落到 `platform='infron'`。
- [ ] AC18 现有测试套件仍全绿（当前基线 262 passed），并新增 infron 适配器的
  契约测试。

## Notes

- 测试账号 `briced35@hotmail.com` 已在 infron 上建好号，登录态在它的 AdsPower
  环境里，可直接用于调试，省一次建号。
- `Pay` 之后的填卡页**尚未探过**（那一步会真实扣款）。实现第一步就是确认它是
  Stripe hosted Checkout 还是嵌入式 Payment Element——这决定
  `stripe_checkout.py` 能复用多少，也是整个任务最大的不确定性。
- AdsPower 环境配额实际可用 11 个（12 减去 AdsPower 自带的 Default Profile）。
  infron 与 opencode **共用**环境（按 email 分配，不按平台拆），所以接入 infron
  不会额外消耗配额，但会让回收判据的第 2 档（「所有开通过的平台都终态」）真正生效——
  这条此前只有单元测试覆盖，届时值得留意。
- 手续费实际显示 3% + ($0.35 + 2%)，与官方文档写的 5% + $0.35 对不上。
  金额校验不要按文档硬算。
