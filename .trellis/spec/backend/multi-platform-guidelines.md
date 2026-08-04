# Multi-Platform Guidelines

> How this project runs the same mailbox and the same card pool against several
> target sites without letting them contaminate each other.

---

## The layering

```
src/identity/           who we are        GitHub account supply (reusable OAuth identity)
src/platforms/<slug>/   where we are      site-specific navigation & outcome判定
src/payments/           how we pay        Stripe Checkout form operations
src/browser/            what we drive     browser lifecycle, CDP utils, progress callback
```

Dependencies point downward only. `payments` must never import `platforms` —
that is why the shared progress helper lives in `src/browser/monitor.py` rather
than in the platform layer.

A **platform** is a target site we open accounts on and pay. A platform is
identified by a string slug. The adapter registry in `src/platforms/__init__.py`
is the **single source of truth** for which platforms exist — the database only
ever stores the slug string. There is deliberately no `platforms` table: another
table would be another thing to keep in sync, and the platform list changes with
a code release, not at runtime.

## Adding a platform

1. Write a class satisfying `PlatformAdapter` (`src/platforms/base.py`).
2. Register it in `src/platforms/_bootstrap()`.

That is the whole list. No orchestration code changes — `tests/test_platform_adapter.py`
runs the entire top-up pipeline against a fictional `StubAdapter` to keep it that
way. If someone hardcodes a platform back into the orchestration layer, that file
goes red.

### What belongs in the adapter, and what does not

The interface is deliberately narrow — 7 methods. Earlier drafts had 12; the
extra five (`auth_entry_urls`, `click_oauth_entry`, `balance_url`,
`start_payment`, `detect_payment_outcome`) were only ever called from *inside*
`ensure_session` / `top_up` / `subscribe`. Exposing them would force the next
platform to decompose its flow the way opencode happens to decompose its own —
the opposite of what an abstraction is for. Keep them private.

The one exception is `read_balance_from_current_page`, which the API layer calls
directly while a human has a browser open.

Per-platform tuning lives on the adapter as plain attributes, not in environment
variables: `max_card_attempts`, `recharge_skip_balance`, `default_topup_amount`.
Risk thresholds genuinely differ between sites.

## `PaymentResult.outcome` — a hard contract

| outcome | Card consumed? | Meaning |
|---|---|---|
| `success` | yes (marked `paid`) | Payment went through |
| `failed` | yes | Explicit decline → invalidate if never succeeded **on this platform**, else 24h cooldown |
| `needs_captcha` | **no** | Account-level risk block. Stop immediately, do not try more cards |
| `error` | **no** | Page/infrastructure failure *before* payment |
| `unknown` | **no** | Submitted, no confirmation, no clear signal |
| `dry_ready` | **no** | Rehearsal: card filled, not submitted |

The three "no" rows are non-negotiable, and `OUTCOMES_KEEPING_CARD` /
`PaymentResult.keeps_card` exist to make that checkable. A new adapter that
reports a network blip as `failed` will permanently invalidate good cards, and
that is not reversible.

## What is isolated per platform, and what is not

Not everything should be split. The rule is: **does this describe the card/identity
itself, or what happened at one merchant?**

| Thing | Scope | Why |
|---|---|---|
| Account status | per platform (`platform_accounts.status`) | Recharged on A says nothing about B |
| GitHub signup/ban outcome | **global** (`accounts.identity_status`) | A flagged GitHub account cannot authorize OAuth anywhere |
| Card `bound` / `invalid` / `paid` | per platform (`card_platform_state`) | All three are merchant-specific verdicts |
| Card `expired` | **global** (`card_pool.status`) | Expiry is a property of the card |
| `valid_cards` membership | per platform | "Proved usable" is proved against one merchant |
| 3DS / rate cooldown | per platform | 3DS is decided by merchant + issuer together |
| `PaymentCardRegistry._used` | per platform | Pure round-dedup heuristic; irrelevant across sites |
| `PaymentCardRegistry._in_flight` | **global** | Submitting the same card at two merchants at once stacks issuer velocity risk. The issuer sees the card, not our platform |
| `ProxyRegistry` | **global** | An exit IP is a physical resource |
| AdsPower browser profile | **global, per email** | See below |
| `[Stripe字段错误]` cards | **global** | The card data itself is malformed; it will be malformed everywhere |

### Why AdsPower profiles stay per-email

Tempting to split, wrong to split. A profile exists to preserve cookies, and the
valuable cookie is the **GitHub authorization**, which is shared across every
OAuth platform by construction. Platform sessions do not survive a browser
restart anyway. Splitting by `(platform, email)` divides a hard quota of 12
profiles by the number of platforms in exchange for a short-lived session — a net
loss.

What *does* need to become platform-aware is the reclaim predicate. See
`AdsPowerProfileModel.reclaim_candidates`: three tiers, and two of the conditions
are easy to get subtly wrong.

- Use `NOT EXISTS (… non-terminal row …)`, not `status IN (terminal set)`. With
  two platforms — one finished, one still running — the `IN` form calls the
  profile reclaimable and deletes a browser someone is using.
- Also require `EXISTS (… any row …)`. Without it, `NOT EXISTS` is vacuously true
  for accounts that have not been onboarded anywhere — precisely the freshly
  registered ones whose GitHub session is about to be used.

## 执行模型：平台之间是并发的

三层结构，越往里越私有：

```
SharedResources   跨平台共享的单例：db / models / open_browsers /
                  三个排他注册表 / AdsPower 客户端与池 / 配额仲裁器
AppState          **每个平台一个**。is_running / stop_requested / 计数 /
                  current_action / 日志缓冲 / workers / _adspower_started
WorkerState       每个浏览器实例的隔离状态
```

`AppState` 保留了类名但语义已变（从「全局唯一」到「每平台一个」）。共享字段用
`@property` 委托给 `self.shared`，所以方法体里 `self.db` / `self.models` /
`self.account_registry` 这些写法一行没改——那是把改动面压到最小的关键。

`create_app` 建 1 个 shared + 每个已注册平台一个 ctx，放进 `app.config['RUN_CONTEXTS']`。
`app.config['APP_STATE']` 仍指向默认平台，供只碰共享资源的老代码使用。

### 哪些必须共享，为什么

不是「看起来像全局」就共享，每一项都有具体的物理理由：

| 资源 | 理由 |
|---|---|
| `account_registry` | Chrome profile 目录**按 email 命名**，排他是 email 级的 |
| `payment_registry` | `_in_flight` 全局是硬要求——同卡在两处同时授权是盗刷特征 |
| `proxy_registry` | 出口 IP 是物理资源，反关联的全部意义就是不重复 |
| `_adspower_client` | `_throttle` 限流状态是**实例级**，两个实例 = 两倍请求速率 |
| `_adspower_pool` | `_lock` 串行化整条建环境链，拆开会活锁（A 刚删出的配额被 B 抢走） |

### owner：共享注册表里必须区分「谁占的」

三个注册表都共享，于是必须能区分归属。**语义分两类，不能一刀切**：

- **排他判定不带 owner**（`is_claimed` / `try_acquire` 的准入）——同一 email
  不能同时在两个平台跑，这是物理约束不是策略。
- **「本轮还有谁在飞」「收尾释放」必须带 owner**（`snapshot(owner)` /
  `release_all(owner)`）。

漏带 owner 的三种后果**全都不报错**：

1. `snapshot()` 不过滤 → A 平台把 B 的在飞账号看成自己这轮在飞 → 永远走 `'wait'`
   → **轮边界永不触发、任务静默不收敛**。表现只是「跑着跑着不动了」。
2. 收尾 `release_all()` 无参 → 把另一个平台正持有的账号/卡/代理全放掉，
   它的排他保护瞬间蒸发。
3. `ProxyRegistry` 只比裸 `worker_id` → 两个平台的 `W1` 互认成自己 →
   **同一出口 IP 发给两边**，反关联白做。持有者身份必须是 `(worker_id, owner)`。

回归测试见 `tests/test_registry_owner.py`，含调用点检查——注册表支持 owner
不等于流水线传了，缺陷正是出在调用点。

### 日志归属靠 contextvar，不靠绑定实例

被劫持的 `print` 指向**模块级** `dispatch_print`（进程内只装一次），归属在运行时
从 `_RUN_CTX` 这个 contextvar 解析。

不能再像单平台时代那样把某个实例的绑定方法塞进模块 globals：两个平台各装一次，
后装的直接覆盖先装的，所有平台的 print 都会进同一个日志流。
`src.payments.stripe_checkout` 尤其致命——两个平台的 `module_names()` 都声明了它。

⚠️ **contextvars 不跨线程继承**，每个会跑业务代码的线程入口都要 `bind_logs()`：
三条流水线、`WorkerPool._run_in_worker`、`routes.py` 的 `_do_recharge`。
漏一处的表现是那条链路的日志跑到另一个平台去，不报错。

无归属时如实退化成 `builtins.print`，**不猜平台**——猜错比丢掉更难查。

### AdsPower 环境配额由仲裁器管

`AdsPowerQuota`（`src/browser/adspower_quota.py`）：总上限 **11 而不是 12**
（AdsPower 自带的 Default Profile 也占一个名额，实测卡在 11/12），
各平台自有额度 opencode 7 / infron 4，对方空闲时可借。

归还是**协作式**的：原主发 `request_recall`，借用方在下一个账号边界还清，
期间原主等待。**不做强制抢占**——中断正在跑的会话可能打断一笔已提交待授权的
支付，留下「钱可能扣了但状态不明」，那正是 `PaymentResult.unknown` 存在的原因。

两个接入要害：

- 配额必须在**进池之前**取。池锁串行化整条建环境链，持着池锁等配额会让释放方
  拿不到池锁删环境——直接死锁。
- 释放挂在 `close_driver` 的 `_on_closed` 回调（所有关闭路径的唯一收口）。
  建会话失败、`quit` 抛异常两条路径也要还，收尾再对账一次兜住泄漏。
  额度只出不进的话，几个账号之后就再也起不来浏览器。

配额耗尽**不再置全局 stop**。那条老逻辑的理由是「配额是全局的，下一个账号
必然撞同一堵墙」；按平台拆分后置 stop 只停自己、配额却仍是共用的，
结果会是一个平台饿死在等待、另一个反复抛错自杀。

### 仍然成立的物理约束

**同一个 email 不能同时在两个平台跑。** Chrome profile 目录与 AdsPower 环境都按
email 分配。所以「两平台并发」实际是「**两平台各跑不同账号**」，这不是能优化掉的。

## Passing the platform around

- API layer: `_req_platform()` in `src/api/routes.py`. Read endpoints fall back to
  the **default platform constant** — not `AppState.platform`, which used to mean
  "whatever ran last" and gets overwritten by whichever platform started later.
  Pipeline-start endpoints pass `required=True` and return 400 when it is missing.
  Guessing is worse than failing — a wrong guess writes data to the wrong platform.
- 与「某次运行」有关的端点（启停 / 状态 / 日志 / 视频流）一律用 `get_ctx(platform)`，
  不能用 `get_app_state()`——后者永远是默认平台。只碰共享资源的端点可以继续用它，
  但要在 docstring 里写明「不是漏改」。
- `/video_feed` 与 `/api/workers/<id>/logs` 必须带 `platform`：两个平台各有一套
  同名的 `W1..W4`，而 `get_worker` 对未知 id 会**回落到主 worker**——取错 ctx
  不会 404，只会安静地给错数据。
- Frontend: injected centrally in `frontend/src/api/index.js`, never by individual
  call sites. A missed call site does not error; it quietly shows another
  platform's data.
- Models: `platform` is a required positional parameter on every card/account
  state method. No defaults — a default is how a missed call site becomes silent
  cross-platform contamination.
