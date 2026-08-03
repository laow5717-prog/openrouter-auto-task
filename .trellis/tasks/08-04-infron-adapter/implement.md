# 执行计划：infron.ai 适配器

## 基线

```
.venv/bin/python -m pytest tests/ -q
→ 262 passed（2026-08-04，多平台改造合并到 main 后）
```

每步结束都跑，通过数只能升不能降。

## 调试提速的两件事

1. **`briced35@hotmail.com` 已在 infron 建好号**，登录态在它的 AdsPower 环境里。
   调试优先用它，省一次建号、省一次收信。
2. **服务跑在 werkzeug dev 自动重载模式下**。运行期间改任何源码都会重启进程、
   杀掉跑到一半的 worker 线程。上个任务实跑就是这样被打断过一次，
   排查时一度误以为是逻辑回归。要么调试时停掉服务，要么改完再跑。

---

## Stage 1 — 探清填卡形态（**动手写代码之前必须做完**）

design 里说了，`Pay` 之后的页面没探过，它决定 `stripe_checkout.py` 能复用多少。
在这一步之前写填卡代码等于赌。

- [ ] **1.1** 用 `briced35@hotmail.com` 的 AdsPower 环境，走到
  `/dashboard/credits` → `Top Up` → 选 $50 → 选 `Card` → 点 `Pay $X`，
  **停在填卡表单出现的那一刻，不提交**。dump：
  - URL 有没有跳到 `checkout.stripe.com`
  - 所有 frame 的 URL
  - 卡号/有效期/CVC 输入框的选择器与所在 frame
  - 有没有账单地址字段、国家/州下拉
  - hCaptcha 的形态（invisible 还是有交互）

- [ ] **1.2** 对照 `src/payments/stripe_checkout.py` 的现有函数，逐个判定可复用性：
  `_stripe_frame` / `_wait_stripe_frame` / `select_card_method` /
  `fill_card_and_address` / `fill_phone_if_present` / `uncheck_save_info` /
  `click_pay` / `_captcha_challenge_present` / `_threeds_*`。
  产出一张「可直接用 / 要改参数 / 要重写」的表，写进 `research/`。

- [ ] **1.3** 若判定为「要重写」，先想清楚新代码放哪：
  能通用化的补进 `stripe_checkout.py`（加参数而不是加分支），
  确属 infron 特有的才放 `infron/credits.py`。

**Review Gate G1**：1.2 那张表出来之前不开始 Stage 3。

---

## Stage 2 — 骨架与会话

- [ ] **2.1** 建 `src/platforms/infron/{__init__,login,credits}.py`。
  `__init__.py` 的 docstring 里写明「**必须走 AdsPower**，Patchright 过不了入口
  Turnstile」——这条最容易让后来人白白卡住。

- [ ] **2.2** `login.wait_past_turnstile(session, timeout=90)`：轮询到标题不再是
  `Just a moment...` 且渲染出可交互元素。超时返回 False，不重试。

- [ ] **2.3** `login.ensure_session(session, creds, monitor)`：
  - 先导航 `/dashboard`，等 Turnstile → 已登录就直接返回（**主路径**，不发信）
  - 被弹回 `/login` 才走 magic link：**先取 `since`** → `fill('#email')` →
    点 `Sign In` → 轮询 ruoanzhu 取本次之后到达的 `Infron - Sign In Link` →
    正则提链接 → 打开 → 等落地 `/dashboard`
  - 无 `verify_link` → 直接 `ok=False` 并说明
  - Turnstile 超时 → `ok=False`，detail 里提示「可能需要 AdsPower 环境」

- [ ] **2.4** `credits.read_balance(session)`：从 `/dashboard/credits` 的
  `Available Balance` 区块抠美元。**余额 0 返回 `0.0` 不是 `None`**。

- [ ] **2.5** `InfronAdapter` 实现协议，注册进 `platforms._bootstrap()`。
  `capabilities={CAP_TOPUP}`，`extract_tenant_id` 返回 None，
  `fetch_apikey` 第一版不实现。

**验证**
```bash
.venv/bin/python -c "
import src.platforms as P
from src.platforms.base import PlatformAdapter
a = P.get('infron'); print(isinstance(a, PlatformAdapter), sorted(a.capabilities))"
.venv/bin/python -m pytest tests/ -q        # ≥ 262
```

- [ ] **2.6** 实跑验证会话：用 `briced35@hotmail.com`（已登录环境）确认
  **不发新邮件**直接复用（AC5）；再用另一个 `identity_status='failed'` 的账号
  跑一次全新 magic link 建号（AC4）。

---

## Stage 3 — 充值

- [ ] **3.1** `credits.open_topup_modal(session, monitor)`：进
  `/dashboard/credits` → 点 `Top Up` → **轮询等 `[role=dialog]` 里出现
  `Top Up Credits`，至少给 20 秒**。等不到返回 None（上层转 `outcome='error'`，
  不消耗卡）。

- [ ] **3.2** `credits.select_amount(session, amount)`：优先点档位按钮
  （`$50`/`$100`/`$300`），非档位金额填自定义输入框。

- [ ] **3.3** `credits.click_pay(session, monitor)`：按 `^Pay ` 前缀匹配，
  **不认金额**（AC11）。

- [ ] **3.4** 按 Stage 1 的结论接填卡：能复用就调 `stripe_checkout.py`，
  不能就在 `credits.py` 里写。

- [ ] **3.5** `credits.detect_payment_result(session, balance_before, ...)`：
  骨架照 opencode 那套写——轮询余额增长为成功判据，识别拒付文案 / 3DS / hCaptcha，
  按 design 里那张表映射 outcome。

- [ ] **3.6** `InfronAdapter.top_up` 串起来，返回 `PaymentResult`。

**验证**
```bash
.venv/bin/python -m pytest tests/ -q
```

- [ ] **3.7** 新增 `tests/test_infron_adapter.py`：协议契约 + outcome 语义
  （needs_captcha/error/unknown 不消耗卡）。可仿
  `tests/test_platform_adapter.py` 的 StubAdapter 写法，用假 session。

---

## Stage 4 — 端到端实跑

- [ ] **4.1** 前端切到 infron，确认账号列表、卡池桶计数都按 infron 视角给（AC16）。

- [ ] **4.2** 单账号充值实跑：`/api/accounts/recharge` 传
  `platform=infron`。核对：
  - 结果落 `platform_accounts(platform='infron')`
  - 卡判废落 `card_platform_state(卡号,'infron')`
  - `recharge_logs.platform='infron'`
  - **opencode 视角的可选卡集合不变**（AC15）

- [ ] **4.3** 同一邮箱在两个平台各有一行平台账号且互不覆盖（AC14）。

- [ ] **4.4** `git diff` 确认改动只落在 `src/platforms/infron/`、
  `src/platforms/__init__.py` 的注册处、以及测试（**AC2，本任务的核心命题**）。

- [ ] **4.5** 全量 AC 走查。

---

## 提交划分

| commit | 内容 |
|---|---|
| 1 | Stage 1 的探测结论（只有 research 文档，无代码） |
| 2 | Stage 2 骨架 + 会话 |
| 3 | Stage 3 充值 |
| 4 | Stage 4 的修正与文档 |

Stage 1 单独成一个「只有文档」的 commit 是有意的：它记录的是**站点当时的样子**，
将来站点改版时能对照着看变了什么。

---

## 留意但不在本任务范围

- `_grab_apikey` 的 `if not (platform_account_model and wid)` 守卫会因
  infron 的 `tenant_id=None` 而永远跳过。若要给 infron 抓 key，这个守卫要放宽——
  那属于**改协议契约**，不是在适配器里造个假 id 绕过去。
- infron 接入后，AdsPower 回收判据的第 2 档（「所有开通过的平台都终态」）才会真正
  被触发（此前只有单元测试覆盖）。第一次撞配额时值得盯一眼，
  `config.yaml` 的 `reclaim_batch` 可临时降到 1。
- 手续费实际是 3% + ($0.35 + 2%)，与官方文档的 5% + $0.35 对不上。不要按文档硬算金额。
