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

- [x] **1.1** 实探完成，结论见 `research/infron-payment-form.md`。**决定性事实：
  嵌入式 Stripe Payment Element，页面不跳转**（URL 始终 `/dashboard/credits`），
  不是 opencode 那种 hosted Checkout。

- [x] **1.2** 逐函数复用判定表已写进同一份文档。大意：**验证码与 3DS 那半
  （最难、踩坑最多）能复用，表单定位那半要按 Payment Element 重写。**

- [x] **1.3** 定位相关的新代码放 `infron/credits.py`；`_stripe_frame` 的 frame 匹配
  建议**加参数**支持 `elements-inner-payment-*`，而不是在里面加站点分支。

### 1.1 没能拿到的部分（Stage 3 第一件事补上）

**卡号/有效期/CVC 的精确选择器没拿到。** 原因：探针没装 captcha hook，
hCaptcha 显示 `Please try again ⚠️` 并且**把 Payment Element 卡在了加载之前**
（frame 根本不出现）。

Stage 3 写填卡代码前，**先带着 captcha hook 再探一次**把选择器补进
`research/infron-payment-form.md`。在此之前不要照抄 opencode 的选择器——
两边表单结构不同。

这条也是个通用教训：**任何绕过编排层的 infron 调试脚本都会卡在 hCaptcha**，
而现象（Element 不出现）看起来像页面加载慢，极易误判成别的问题。

**Review Gate G1**：✅ 已通过。

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

- [ ] **3.4a** **带 captcha hook 重探一次**，把卡号/有效期/CVC 的选择器补进
  `research/infron-payment-form.md`。这是 Stage 1 因 hCaptcha 阻断没拿到的部分。

- [ ] **3.4b** 接填卡：先点 Element 里的 `Card` tab（**默认选中的是 Alipay，不是
  Card**），再填字段。`select_card_method` 不能复用（accordion vs tab）。
  `_stripe_frame` 要能匹配 `elements-inner-payment-*` frame。

- [ ] **3.4c** 注意两步弹窗的 `Pay $X` 按钮**同名**。用弹窗文案区分当前步骤：
  第一步是 `Confirm details on the next step`，第二步是
  `Enter your card or another Stripe payment method`。

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
