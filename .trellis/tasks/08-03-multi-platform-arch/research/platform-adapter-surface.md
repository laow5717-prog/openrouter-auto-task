# 调研：浏览器/服务层的平台特化面（PlatformAdapter 接口依据）

调研时间：2026-08-03。

## 1. driver.py（6157 行）—— 通用 vs opencode 专有

**`opencode` 在 driver.py 只出现 3 次，全是注释**（`:8`、`:19`、`:872`），没有任何 opencode URL/选择器。它真正的耦合对象是 Cloudflare，而那批代码已是死代码。

### 1a. 平台无关的通用基建（约 1000 行，`:1-1012`）

| 能力 | 位置 |
|---|---|
| `BrowserSession` 会话封装 | `:107-398`。含 remote 模式（`_remote_browser`/`_remote_stop`，`:120-129`）供 AdsPower CDP 接管复用 |
| 截图缓存（跨线程安全） | `:235-243`、`:260-265` |
| 关闭/进程卫生 | `:269-398`、`:563-598`、`:598-648`、`:649-681` |
| 安全交互原语 | `:413-510`（`_diag`/`_safe_goto`/`_safe_click`/`_safe_fill`/`_wait_visible`/`_wait_gone`） |
| 语言强制英文 | `:505-509`、`:512-562`。刻意走 Chrome 原生 `--lang` 而非 Playwright `locale`（反检测） |
| `create_driver(headless, profile_id, bypass_csp, disable_site_isolation, proxy)` | `:682-866`（Patchright 反检测栈） |
| `create_driver_vanilla(profile_id, proxy)` | `:867-935`。**唯一提到 opencode 的函数注释**（`:872`），但函数体完全通用——存在理由是「Patchright 阉割 add_init_script → hCaptcha token 注不进去」，与站点无关 |
| `close_driver` | `:936-943` |
| `type_slowly` / `inject_network_interceptor` / `collect_intercepted_responses` | `:944-948` / `:949-965` / `:966-1012`（当前无外部调用者） |
| 超时常量 | `:81-92`，被 `adspower_driver.py:25-27` 复用 |
| `US_STATE_ABBR` | `:38-56`，只在本文件内用 |

**外部实际 import 的符号只有 6 个**（这就是通用层的真实公开面）：

- `create_driver` — `github_signup_service.py:21`、`routes.py:437`、`opencode_login.py:286`、`opencode_subscribe.py:429`
- `create_driver_vanilla` — `registration.py:98`、`app.py:1041`
- `close_driver` — 同上
- `_safe_goto`、`_wait_visible` — `github_signup.py:16`
- `BrowserSession`、`DEFAULT_TIMEOUT_MS`、`NAV_TIMEOUT_MS`、`BROWSER_ACCEPT_LANG_HEADER` — `adspower_driver.py:25-27`

### 1b. Cloudflare 专有（`:1013-6157`，约 5100 行 / 占 83%）—— **全部死代码**

模块头 `:1-20` 自述为 `LEGACY Cloudflare-specific（待 opencode.ai 重写）`。逐个 grep 验证，这批函数在 `src/` 内**零外部调用**：`login_cloudflare`/`handle_email_verification`/`navigate_to_billing`/`add_credit_card`/`fill_topup_and_confirm`/`handle_unpaid_invoices`/`check_and_handle_cf_challenge`/`navigate_to_ai_credits`/`read_credits_balance`/`get_bound_card_count`/`dismiss_overdue_dialog`/`fetch_today_invoice_count`。

硬编码站点耦合：

- `dash.cloudflare.com` 硬编码 URL：`:2098`、`:2285-2286`、`:2350`、`:2404-2406`、`:2416`、`:2463`、`:3281`、`:3710`、`:3720`
- `_CREDIT_BALANCE_URL_MARK = 'ai-gateway/billing/credit-balance'`（`:397`）—— Cloudflare AI Gateway 专有接口，却被 `BrowserSession._on_response`（`:180-212`）和 `_capture_credit_balance`（`:213-234`）**引用在通用类里**，是通用层唯一残留的站点污染
- Turnstile 质询群：`:1052-1510`。其中 `_cdp_click_at`(`:1296`)、`_get_viewport_coords`(`:1308`)、`_collect_all_child_frames`(`:4919`)、`_extract_text_from_dom_node`(`:4995`) 是**可复用的通用 CDP 工具**，需保留
- Stripe 绑卡/账单群：`:1511-2082`、`:2612-3706`、`:3942-6157`
- 注册/账号群：`:2084-2611`、`:3707-3941`

## 2. 三个 opencode 模块 → 候选接口方法

### 2a. `opencode_login.py`（306 行）

专有常量：`WORKSPACE_RE = r'opencode\.ai/workspace/(wrk_[A-Za-z0-9]+)'`（`:18`）、`_AUTH_HOST = "auth.opencode.ai"`（`:19`）、`https://opencode.ai/auth`（`:170,227`）、`https://auth.opencode.ai/github/authorize`（`:68`）、`https://opencode.ai/workspace/{wid}/go`（`:259`）

选择器：opencode 侧 `get_by_role("link", name="Continue with GitHub")`（`:60`）；GitHub OAuth 侧（**其实是 GitHub 专有**）`button[name="authorize"][value="1"]` 等（`:86-91,120-122`），文案 `Authorize/Continue/Install/授权`（`:85`）；flag 检测 body 文本 `"account is flagged"`（`:148`）

| 公开函数 | 返回值语义 |
|---|---|
| `login_and_open_own_go(session, monitor=None, timeout=240, open_go=True)` `:151` | `dict{ok, wid, go_url, flagged, detail}`。`wid` = 平台侧租户/工作区 id；`flagged` = 上游 GitHub 被反滥用标记的账号级终态。`open_go` 是 opencode 特有优化（实测多耗 34s） |

私有：`_cur_url:22`、`_extract_wid:29`、`_wait_until:34`、`_step:45`、`_click_continue_github:56`、`_click_authorize_if_present:74`、`_account_flagged:140`。其中 `_extract_wid` 被 `opencode_subscribe.py:25` 跨模块 import。

### 2b. `opencode_billing.py`（1034 行）

专有：`WORKSPACE_RE`（`:18`）、`https://opencode.ai/auth`（`:167`）、`https://opencode.ai/workspace/{wid}/billing`（`:233,254,806`）、余额正则 `_BAL_RE = r'\$([0-9.]+)\s*Current Balance'`（`:214`）、入口按钮 `Add Balance`（`:264`）/`Enable Billing`（`:288`）/`Add`（`:277`）/金额框 `input[type='number']`（`:271`）

| 公开函数 | 返回值 |
|---|---|
| `ensure_opencode_session(session, monitor, login_password, email, verify_link=None)` `:157` | `(wid, detail)`。含完整降级链：已登录复用 → GitHub 登录（`github_signup.login_after_signup`）→ 设备验证收码 → `login_and_open_own_go(open_go=False)` |
| `read_current_balance(session, wid, monitor=None)` `:227` | `float\|None` 实时余额 |
| `start_recharge(session, wid, amount, monitor)` `:243` | `(mode, balance_before)`；`mode ∈ {"first"(Enable Billing→整页跳 Stripe), "reload"(Add Balance→页内 iframe), None}` |
| `recharge_via_stripe(session, card, wid, amount=20, monitor=None, should_stop=None)` `:957` | `dict{ok, outcome, mode, err, last4, balance_after, steps}`；`outcome ∈ success/failed/needs_captcha/unknown/error` |
| `detect_payment_result(session, wid, balance_before, monitor, timeout=120)` `:784` | `dict{outcome, detail, balance_after}`。**权威判据是余额增长**（`:818-824`） |
| `_read_balance(session)` `:217` | 私有，但被 `routes.py:471` **跨模块直接调用** → 必须进接口 |

**Stripe Checkout 通用族（平台无关，应抽 `StripeCheckoutHelper`）**：`_stripe_frame:309`、`_wait_stripe_frame:330`、`pick_currency_usd:347`、`select_card_method:363`、`_gen_us_phone:390`、`fill_phone_if_present:408`、`fill_card_and_address:449`、`uncheck_save_info:525`、`check_ai_agent_consent:541`、`_form_ready_state:578`、`click_pay:598`、`_captcha_challenge_present:623`、`_threeds_challenge_present:670`、`_count_top_layer_overlays:685`、`_threeds_failure_modal:702`、`_close_threeds_modal:731`、`_threeds_challenge_lightbox:752`、`_close_challenge_lightbox:771`、`_DECLINE_HINTS:43`、`_THREEDS_CHALLENGE_GRACE_SEC:749`

证据：`opencode_subscribe.py:16-24` 一次性 import 了其中 14 个，**说明这批已在事实上被当作跨流程共享层使用**。

Stripe 侧选择器：`#payment-method-accordion-item-title-card`（`:366`）、`#phoneNumber`（`:414`）、`#enableStripePass`（`:529`）、`[data-testid='hosted-payment-submit-button']`（`:588,601`）、`checkout.stripe.com` 帧判定（`:309-345`）、AI agent 声明 `get_by_label(/AI agent/i)`（`:546`）

`_step(monitor, session, msg)` `:73` 是通用工具（`opencode_subscribe.py:17` 也 import 它），应上移。
`_auto_verify_device:86` / `_wait_github_verified:132` 属 GitHub IdentityProvider。

### 2c. `opencode_subscribe.py`（464 行）

专有：`https://opencode.ai/workspace/{wid}/go`（`:34`）、按钮文案 `"Subscribe to Go"`（`:43`）、订阅结账页币种按钮 `USD`（`:99-101`，区别于充值页的「$金额」块）、提交按钮 `button.SubmitButton[type='submit']`（`:152`）、成功判据 URL 回落 `opencode.ai/workspace/<wid>`（`:257`）

| 公开函数 | 返回值 |
|---|---|
| `start_subscribe_go(session, wid, monitor=None, timeout=60)` `:29` | `(ok, detail)`；ok 表示已进 checkout.stripe.com |
| `select_usd_subscribe(session, monitor=None)` `:75` | 多策略跨 frame 点 USD，失败回退 `pick_currency_usd` |
| `click_subscribe(session, monitor=None, timeout=25)` `:140` | 轮询等 SubmitButton enabled |
| `detect_subscribe_result(session, wid, monitor=None, timeout=200)` `:191` | **成功判据与充值完全不同**：订阅不增余额，改判「离开 checkout 且回落 workspace」（`:200-201` 注释标注此判据尚待真实付费标定） |
| `subscribe_via_stripe(session, card, wid, monitor=None, should_stop=None, dry=False)` `:343` | `dict{ok, outcome, err, last4, steps}`；`outcome` 多一个 `dry_ready` |

`_checkout_frames(session)` `:125` 与 `_stripe_frame` 重复，应合并。

### 2d. 统一返回契约

`PaymentResult{ok, outcome ∈ {success, failed, needs_captcha, unknown, error, dry_ready}, err, last4, mode?, balance_after?, steps[]}`

编排层对每个 outcome 的处置规则已固化在 `registration.py:251-296` 和 `app.py:1096-1130`，抽象时**必须保持语义不变**（尤其 `error` = 不消耗卡、`needs_captcha` = 账号级风控立即停手）。

## 3. GitHub 注册 = 平台无关的身份供给

### `github_signup.py`（619 行）—— 100% 平台无关
模块 docstring 明确写「**不含任何 Cloudflare / opencode 语义**」（`:5`），全文件仅这一处出现 opencode 字样。选择器全是 GitHub 自己的（`:18-29`：`SIGNUP_URL`/`#email`/`#password`/`#login`/`#country-dropdown-panel-button`/`#captcha-container-nux`；`:505-507`：`#login_field`）。

公开函数：`open_signup:64`、`fill_signup_form:75`、`submit:131`、`detect_terminal_state:203`、`wait_for_captcha_cleared:250`、`dump_verification_dom:320`、`submit_email_code:381`、`detect_account_created:465`、`login_after_signup:510`、`detect_signup_complete:565`

### `github_signup_service.py`（370 行）—— 95% 平台无关
唯一耦合：`from src.browser.opencode_login import login_and_open_own_go`（`:26`），只被 `then_opencode=True` 分支使用（`:316-328`），结果写进 `result['opencode']`（`:196`）。

**两个生产调用点都传 `then_opencode=False`**（`app.py:1012`）—— 这条 opencode 耦合当前根本没被走到，只有 CLI/调试路径会用。

`signup_one(headless, semi_auto, keep_open, account, then_opencode, auto_skip_captcha, proxy, browser_factory)`（`:159-161`）返回 `{ok, email, email_password, github_password, username, outcome, reason, screenshot, final_url, opencode}`（`:183-197`），除 `opencode` 键外全部平台无关。

### 判定
**GitHub 注册是平台无关的身份供给。** 依据：产出物是通用 OAuth 身份（任何走 GitHub OAuth 的平台都能复用）；`then_opencode` 是纯 opt-in 优化；唯一真实约束是浏览器 profile / AdsPower 环境里的 GitHub 登录态按 email 绑定。

建议：`github_signup_service.signup_one` 提为 `IdentityProvider.provision(account) -> Identity`，`then_opencode` 换成 `post_provision_hook: Optional[PlatformAdapter]`。

## 4. 收码链路的 opencode 硬编码

| 文件 | 位置 | 判定 |
|---|---|---|
| `src/services/email.py:148-209` `wait_for_login_code` | `:182-184` **有 TODO 注释标明「过滤词 opencode 为占位」**；`:187` 主题格式 `Your opencode login token: 1234567` | opencode 硬编码。但 `src/` 内唯一调用者是 `driver.py:2261`，**那是死代码**（Cloudflare LEGACY） |
| `src/services/email.py:331-385` `wait_for_verification_email` | `:346-347` | opencode 硬编码，**完全无调用者** |
| `src/utils.py:184-190` `extract_verification_link` | TODO 注释同样标为占位 | opencode 硬编码，只被上面那条死代码调用 |
| `src/services/email.py` mail.tm API 族 | `:14,17,37,96,269,292,311,131` | 通用 |
| `src/services/email.py:211-267` `wait_for_github_launch_code` | 过滤词 `github`（`:244`） | GitHub 专有，生产路径在用 |
| `src/services/hotmail_inbox.py`（279 行） | 全文件**零 opencode 引用** | 全通用 + GitHub 专有。生产主路径 |
| `src/services/registration.py` | `:99` import ob；`:157,174,221` 三个 `ob.*` 调用点；env `OPENCODE_RECHARGE_SKIP_BALANCE`（`:21-28`）、`OPENCODE_RECHARGE_MAX_ATTEMPTS`（`:195`） | `recharge_account` 骨架（余额预检 → 逐卡试付 → outcome 分派 → 记账）**完全平台无关**，只有 3 个调用点要换。`register_one_account:31`/`register_and_bind_cards:40`/`bind_cards_to_existing_account:51` 全是 `raise NotImplementedError` |

**结论**：收码链路里 opencode 硬编码只有 3 处，**且全部位于死代码路径**。当前活跃的收码链路（GitHub launch code / 设备验证码）100% 平台无关。

## 5. 编排层

### HTTP 入口（`routes.py`）

| 路由 | 位置 | 调用 |
|---|---|---|
| `POST /api/daily/start` | `:921-965` | `Thread(target=state.run_daily_pipeline, args=(group_id, login_password, captcha_api_key, captcha_server))`（`:956-960`） |
| `POST /api/daily/subscribe/start` | `:968-1012` | `Thread(target=state.run_daily_subscribe_pipeline, ...)`（`:1003-1007`）。与充值共用 `is_running` 闸门 |
| `POST /api/accounts/recharge` | `:341-400` | 单账号充值 |
| `POST /api/accounts/open-browser` | `:401-488` | **直接调用 opencode 模块**：`:438` import、`:450` `ensure_opencode_session`、`:471` `ob._read_balance` 轮询落库 |
| `POST /api/start` | `:87-109` | → `state.run_batch_task`（走 registration 存根） |

### 编排实现（`app.py`）

| 函数 | 位置 | 角色 |
|---|---|---|
| `run_daily_pipeline(group_id, login_password, captcha_api_key, captcha_server)` | `:624-941` | **每日充值主入口**。`_payable_now:685`、`_registerable_imported:695`、`_acquire_proxy_for:707`、`_card_keys_now:762`、`_try_claim:769`、`_produce:816`、`_do:838`（按 kind 分派 `:846-880`） |
| `_recharge_one_account(...)` | `:549-623` | 调 `registration.recharge_account`（`:578-593`），按 outcome ∈ topup/archived/flagged/failed 分派（`:595-620`） |
| `run_daily_subscribe_pipeline(group_id, captcha_api_key, captcha_server)` | `:1142-1274` | **订阅主入口**。串行 `WorkerPool(self, 1)`（`:1160`）；`_needing:1180`；`_do:1203` |
| `_subscribe_one_account(...)` | `:1032-1141` | **opencode 耦合最集中**。`:1041-1044` import；A 分支未注册先注册，B 分支登录 + 逐卡 `subscribe_via_stripe` |
| `_register_one_account(acct, worker, proxy)` | `:984-1031` | `signup_one(..., then_opencode=False, ...)`（`:1011-1013`），两条管线共用 |
| `browser_factory()` | `:140-160` | 返回 `callable(email)->BrowserSession`；未启用 AdsPower 返回 None |
| `_eligible_cards(group_id, exclude_used)` | `:516-548` | 选卡 |
| `_hotmail_by_email` / `_hotmail_for_account` | `:944-983` | 收码数据供给 |
| `_patch_prints()` | `:1275-1303` | **按模块名字符串劫持 print**，硬编码 opencode 模块列表（`:1285-1286`） |

### 全部 opencode import（生产代码只有 6 处）

```
src/web/app.py:1042              from src.browser.opencode_login import login_and_open_own_go
src/web/app.py:1043              from src.browser.opencode_subscribe import subscribe_via_stripe
src/web/app.py:1285-1286         importlib 字符串: opencode_subscribe / opencode_login / opencode_billing
src/api/routes.py:438            from src.browser import opencode_billing as ob
src/services/registration.py:99  from src.browser import opencode_billing as ob
src/services/github_signup_service.py:26  from src.browser.opencode_login import login_and_open_own_go
```

模块内部：`opencode_billing.py:201`、`opencode_subscribe.py:16-24,25`、`opencode_login.py:286`、`opencode_subscribe.py:429`（后两个是 `__main__`）

测试：`tests/test_captcha_detection.py:13-14`
脚本（14 个探针，不影响生产）：`probe_click_dom.py:8-11`、`probe_cdp_inject.py:22-25`、`probe_cdp_inject2.py:19-22`、`probe_no_isolation.py:18-21`、`probe_vanilla_inject.py:25-28`、`probe_hcaptcha_frames.py:18-21`、`probe_hcaptcha_obj.py:21-23`、`probe_bypass_csp.py:18-21`、`probe_click_solver.py:20-24`、`probe_go_page.py:13`、`probe_opencode_login.py`、`probe_opencode_onboarding.py:7`、`run_subscribe_once.py:18-19`、`test_opencode_recharge.py:20`、`probe_pay_result.py:18`

## 6. AdsPower：为什么不该按平台拆

### 生命周期

数据模型（`database.py:185-200`）：`email TEXT PRIMARY KEY`、`profile_id TEXT NOT NULL UNIQUE`、`profile_no`、`proxy_id`、`created_at`、`last_used_at`。注释明说「email 作主键是『一账号一环境』的结构性保证，而不是靠调用方自觉」。

**配额硬上限 = 12**（`src/services/adspower.py:17-19`；`adspower_driver.py:8-9`：「本机实测环境上限 12，而账号成百上千；AdsPower 代理列表里有 100 个代理。**瓶颈永远在环境配额，回收逻辑是主线而非兜底**」）。配额满的信号是 msg 含 `exceeds the limit` → `AdsPowerQuotaExceeded`（`adspower.py:59-60,74-75`）。

创建/复用（`adspower_driver.py:139-175`）：
1. `ensure_profile(email)` `:139` 持 `_lock` → `profiles.get_by_email(email)`（`adspower_profile.py:31`）
2. 命中 → `touch(email)` 刷 `last_used_at`，返回 `(profile_id, proxy_id, created=False)` `:147-148` —— **这就是登录态复用，环境里的 GitHub + opencode cookie 全在这**
3. 未命中 → `_create_profile(email)` `:151`：`pick_free_proxy()` `:106`（按服务端 `profile_count==0` 挑空闲代理；全忙则复用占用最少的并告警 `:131-135`）→ `client.create_profile(payload)`，payload 含 `name=auto-{email}`、`proxyid`、`fingerprint_config` `:154-159`
4. 撞配额 → `reclaim(exclude={email})` → 成功则**重试一次**；仍无可回收则抛出 `:163-170`
5. 成功 → `profiles.upsert(...)` `:172`（`INSERT OR REPLACE`，`adspower_profile.py:36-51`）

回收（`adspower_driver.py:177-216` + `adspower_profile.py:22-24,77-95`）：
- SQL 按 `accounts.status` 优先级给候选：`RECLAIM_STATUS_ORDER = ('failed','pending','rejected','recharged','archived','subscribed','flagged','banned','suspended')`，同优先级按 `last_used_at ASC`
- **`registered` 绝不可回收**（`adspower_profile.py:20-21`）
- 再剔除 `exclude` 与 `_is_busy(email)`（注入自 `AccountRegistry.is_claimed`，`app.py:135`）
- `_stop_all(ids)` `:218-234`（AdsPower 拒删运行中环境）→ `delete_profiles(ids)` → **成功后才** `profiles.delete_by_emails(emails)` `:213`（顺序反了会留孤儿环境永久吃配额）
- `reclaim_batch=3`（`config.yaml:56`、`adspower_driver.py:47`）
- 整条「挑代理→建环境→撞配额→回收→重试」由 `_lock` 串行化防活锁 `:84-87`

其它：`drop_mapping(email)` `:236`、`release(email)` `:241`、`create_driver_adspower(...)` `:276-348`（`connect_over_cdp` 接管 + `_pick_page` 过滤 devtools + `_TAKEOVER_SETTLE_SEC=2.0`）、收尾 `AppState._stop_started_adspower()` `app.py:162-179`

### 改成 (platform, email) 的七个问题

| # | 问题 | 依据 |
|---|---|---|
| **P1 致命** | **配额 12 被平台数直接除**。2 平台 → 6 个账号可并行，3 平台 → 4 个。而 `reclaim_batch=3`、`max_workers` clamp 1-4（`app.py:642`）—— 3 平台下光并发 worker 就可能占满全部配额 | `adspower.py:17-19`、`adspower_driver.py:8-9`、`config.yaml:56` |
| **P2 致命** | **回收候选判据是单列 `accounts.status`，无法表达 per-platform 状态**。「A 平台已 recharged 但 B 平台还 registered」的账号，`JOIN accounts a ON a.email = p.email`（`adspower_profile.py:88`）会给出错误答案 → **误删还在用的环境** | `adspower_profile.py:22-24,77-95`、`database.py:13-21` |
| P3 | 主键改造 + UNIQUE 冲突。`upsert` 的 `INSERT OR REPLACE ... COALESCE(SELECT created_at ... WHERE email=?)` 子查询（`adspower_profile.py:43-51`）要全部加 platform | `database.py:192-200` |
| P4 | 代理成倍消耗。`pick_free_proxy` 挑 `profile_count==0`（`:127`），池 100 个。但**同一 GitHub 身份在不同平台用不同 IP** 反而是异常信号 | `adspower_driver.py:106-135` |
| **P5 核心矛盾** | **登录态复用的价值被稀释**。环境存在的唯一理由是保住 cookie（`adspower_profile.py:3-4`）。但 **GitHub 登录态跨平台共享**，只有平台自己的 session 是 per-platform 的。而 `opencode_login.py:12` 明确记录：「**opencode 会话不跨浏览器重启持久，但 GitHub 授权持久 → 重登时授权页无感自动跳过**」。per-platform 环境保住的那点平台 session 本来就活不久，真正值钱的 GitHub 授权反被拆成 N 份重复存储 —— **负收益** | |
| P6 | `AccountRegistry.claim(email)` 按 email 排他（`app.py:1068,1206`）。放宽成 `(platform,email)` → 同一 GitHub 账号被两平台 worker 同时登录，Chrome profile 单实例约束和 GitHub 并发会话都会出问题；保持按 email 排他 → per-platform 环境永远不会被并行使用，P1 的配额浪费白付 | `app.py:97,135`、`adspower_driver.py:99-101` |
| P7 | `_profile_name(email)` = `auto-{email}`（`:56-58`）会在客户端产生同名重复环境 | `app.py:162-179` |

### 建议方案

**保持环境按 `email` 分配（per-identity，不是 per-platform）。** 理由是 P5。真正需要 per-platform 化的是 **`accounts.status`（P2）**：拆成平台状态后，`reclaim_candidates` 改成「所有平台都已终态的账号才可回收」，既不动 12 的配额，也不会误删。
