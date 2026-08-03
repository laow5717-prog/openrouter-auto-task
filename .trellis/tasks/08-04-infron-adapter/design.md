# 技术设计：infron.ai 适配器

## 这个任务的形状

上一个任务把编排层与站点知识切开了，所以这次**只往 `src/platforms/infron/` 里加东西**，
不改任何既有层。AC2 就是这条：git diff 必须只落在新目录 + 注册表一行 + 测试。

如果实现过程中发现「不改编排层就做不到」，那不是绕过去的理由，是抽象漏了东西的信号
——先停下来，把缺口补进 `PlatformAdapter`，而不是在编排层开特例。

## 模块划分

```
src/platforms/infron/
    __init__.py     InfronAdapter：实现协议，组装下面两个模块
    login.py        Turnstile 等待、magic link 全流程
    credits.py      余额读取、Top Up 弹窗、付款结果判定
```

与 opencode 的三模块（login/billing/subscribe）相比少一个——infron 无订阅。

**不新建 `src/payments/` 下的东西**，除非确认 infron 的 Stripe 表单与现有函数不兼容。

## 会话：magic link

### 主路径是「复用已登录」，不是「每次发信」

```
ensure_session(session, creds):
    1. 导航 /dashboard
    2. 等 Turnstile 放行（见下）
    3. 已在 /dashboard 且渲染出内容 → 直接 ok，不发信          ← 主路径
    4. 被弹回 /login → 走 magic link
```

第 3 步是主路径而非优化。AdsPower 环境按 email 持久，cookie 在里面；每轮都发一封信
既慢（收信要等）又可能撞发信频控，还白白消耗一次性链接。

### magic link 的时间闸门

`since` 必须在**点 Sign In 之前**取。infron 的信几秒就到，但收件箱里可能有上一轮的
旧链接——旧链接要么已用过、要么已过期，用了就是一次必然失败的登录，而且失败原因
看起来像「站点抽风」，极难查。

这条与 opencode 收 GitHub 验证码的闸门是同一个道理
（`hotmail_inbox.wait_for_github_launch_code_ruoanzhu` 的 `since` 参数），
但**不复用那个函数**——它按 `github` 过滤主题、只提 6-8 位数字码。
infron 要的是按 `infron` 过滤 + 提 URL。写在 `infron/login.py` 里：

```python
_MAGIC_RE = re.compile(r'https://infron\.ai/api/user/magic-link/verify\?token=[0-9a-fA-F-]+')
```

Stage 0 删掉的 `utils.extract_verification_link` 是 opencode 硬编码版，**不要复活它**
放回 utils——那正是当初删它的原因。链接提取属于平台知识。

### Turnstile 等待

入口质询页特征：标题 `Just a moment...`，正文含 `Performing security verification`，
DOM 里有 `input[name=cf-turnstile-response]`。实测 AdsPower 环境约 30 秒自动放行。

写一个 `wait_past_turnstile(session, timeout=90)`：轮询直到标题不再是 `Just a moment`
且页面渲染出可交互元素。超时返回 False，让 `ensure_session` 返回
`ok=False, detail='Cloudflare 质询未放行'` —— **不要**把它当成登录失败重试，
重试只会再撞一次。

### tenant_id

infron 的 URL 里没有类似 opencode `wrk_xxx` 的租户段（`/dashboard` 就是 `/dashboard`）。
所以 `extract_tenant_id` 返回 None，`SessionResult.tenant_id` 也是 None。

这不影响编排层：`recharge_account` 只是把 `tenant_id` 透传给 `top_up` / `read_balance`
并落库到 `platform_accounts.tenant_id`。infron 那列会是空，正常。

**但要注意**：`registration.recharge_account` 里 `_grab_apikey` 有
`if not (platform_account_model and wid): return` 的守卫——`wid` 为 None 时会跳过抓
API key。若 infron 要抓 key，这个守卫得放宽。这正是「抽象漏了东西」的候选点：
**发现时先改协议契约（把 tenant_id 明确为 Optional 并调整守卫），别在适配器里造假 id。**

## 充值

### 流程

```
top_up(session, tenant_id, card, amount, monitor, should_stop):
    1. 导航 /dashboard/credits，等页面就绪
    2. 读充值前余额（成功判据要用）
    3. 点 Top Up，等弹窗 —— **至少 15 秒**，实测 10 秒不够
    4. 选金额：档位按钮 $50/$100/$300，或填自定义金额输入框
    5. 选支付方式 Card
    6. 点 Pay（按前缀匹配，不认金额）
    7. 完成 Stripe 付款表单
    8. 判定结果
```

### 三个实测得来的约束

**弹窗要等够。** 第一次探测等 10 秒，DOM 里只多了个 hCaptcha iframe，看起来像「点了没反应」，
差点误判成「必须先绑卡」。等到 15 秒以上弹窗才出来。写死一个 sleep 不可靠，
要轮询 `[role=dialog]` 里出现 `Top Up Credits` 文案。

**Pay 按钮不能认金额。** 文案是 `Pay $105.35`——含手续费的总额，不是充值额。
改充值额或 infron 调手续费都会让写死的选择器失效。用 `name=re.compile(r'^Pay ')`。

**hCaptcha hook 要在点 Top Up 之前装。** 弹窗一出现，
`js.stripe.com/v3/hcaptcha-invisible-*` 就挂上了。编排层
（`registration.recharge_account`）已经在建会话后、调 `top_up` 前装好 hook，
所以适配器里不用管——**但别在 `top_up` 内部再重新导航到一个新 context**，
那会让已装的 hook 失效。

### 成功判据

沿用 opencode 的做法：**以余额增长为准**，不信页面文案。
`detect_payment_result` 那套（轮询余额 + 识别拒付文案 + 3DS 弹窗 + hCaptcha 挑战）
是踩坑换来的，infron 这边的判定应当照同一套骨架写：

| 信号 | outcome |
|---|---|
| 余额增加 | `success` |
| 明确拒付文案（`declined` / `unable to authenticate` …） | `failed` |
| hCaptcha 挑战出现且解不掉 | `needs_captcha` |
| 付款前的页面故障（弹窗没出来 / 找不到 Pay / 填卡失败） | `error` |
| 已点 Pay 但超时未确认 | `unknown` |

后三者**不消耗卡**（AC10）。`platforms.base.OUTCOMES_KEEPING_CARD` 已固化这条。

### Stripe 层复用边界 —— 本任务最大的不确定性

点 `Pay` 之后的填卡页**没探过**（那一步真实扣款）。两种可能：

| 形态 | 复用程度 |
|---|---|
| Stripe hosted Checkout（跳 `checkout.stripe.com`，同 opencode） | `stripe_checkout.py` 几乎全套可用 |
| 嵌入式 Payment Element（留在 infron 页面内的 iframe） | `_stripe_frame` 的定位逻辑可能可用，`fill_card_and_address` 的选择器要重写 |

**实现第一步就是确认它**（见 implement.md 的 1.1）。在确认之前不要动手写填卡代码。

值得注意的是绑卡弹窗（本任务不走）用的是 `js.stripe.com/v3/elements-inner-loader-ui-*`
即 Payment Element，所以充值那条路**也可能**是 Element 而非 hosted Checkout。
先探清再说。

## 余额

`/dashboard/credits` 顶部横幅：

```
Available Balance
$ 0.00000000
```

比 opencode 好读——opencode 要从整页文本里正则抠 `$([0-9.]+)\s*Current Balance`，
这里 `Available Balance` 是独立区块。

**返回 0.0 与返回 None 不是一回事**（AC12）：0.0 是「读到了，余额为零」，
None 是「没读到」。编排层的归档预检拿 None 会跳过归档判断继续充值，
拿 0.0 会正确判定「未达阈值，该充」。把 0 当成 None 返回会让逻辑微妙地错。

## API key

`/dashboard/apiKeys` 列表页是**脱敏**的（`sk-BOK***w8F`），
opencode 那种「从 outerHTML 正则抓 `sk-` 明文」的做法在这里拿不到东西。

`fetch_apikey` 是 best-effort 的（编排层用 `getattr` + `try/except` 包着），
所以**第一版直接不实现**，让编排层跳过。想抓明文再另开任务探
（复制按钮读剪贴板 / 新建 key 时截取）。

不实现比返回一个脱敏串好——脱敏串落库会看起来像成功抓到了，实际不可用。

## 平台参数

```python
class InfronAdapter:
    slug = 'infron'
    display_name = 'infron.ai'
    capabilities = frozenset({CAP_TOPUP})

    max_card_attempts = 5        # 比 opencode(8) 保守：新平台风控未知，先小步
    recharge_skip_balance = 20.0
    default_topup_amount = 50.0  # infron 最低档位是 $50
```

`default_topup_amount` 取 50 是因为档位如此。若要充 $100 走 Recommended 档，
或用自定义输入框充别的数，都由调用方传 `amount` 决定。

## 必须走 AdsPower

Patchright 与本地 Chrome 过不了入口 Turnstile（实测）。这条要写在
`infron/__init__.py` 的模块 docstring 里——否则有人拿 `create_driver` 调试会
卡在质询页找不到原因，而现象（页面一直是 "Just a moment..."）看起来像网络问题。

编排层的 `browser_factory` 在 `cfg.adspower.enabled=false` 时返回 None，
`recharge_account` 会回落到 `create_driver_vanilla`。对 infron 这等于必然失败。
适配器不该去管配置，但**可以在 `ensure_session` 检出 Turnstile 超时时，
在 detail 里提示「infron 需要 AdsPower 环境」**，让失败可诊断。

## 数据隔离：不用做，只需验证

`platform_accounts` / `card_platform_state` / `recharge_logs.platform` 等等在上一个
任务已经就位。infron 接入时唯一要做的是**把 slug 传对**，其余自动生效。

AC14/AC15/AC16 是验证题不是实现题：同一邮箱两行平台账号、同一张卡两个平台各自判废、
前端切平台看到对应视角。

## 已知风险

| 风险 | 缓解 |
|---|---|
| **Pay 之后的填卡页形态未知**，可能要重写填卡逻辑 | implement 第一步先探清再动手；探测时用 dry 思路，走到表单出现即停 |
| 编排层「不用改」这个前提被打破 | 一旦发现，先补协议而不是开特例。tenant_id=None 对 `_grab_apikey` 守卫的影响是已知的第一个候选 |
| magic link 一次性 + 30 分钟，调试时反复登录会频繁发信 | 优先复用已登录环境；`briced35@hotmail.com` 的环境已有登录态可直接用 |
| Turnstile 未来收紧，AdsPower 也过不去 | 目前无预案。真发生了整条 infron 链路就断，要在 detail 里让它可诊断而不是静默重试 |
| infron 反欺诈未知，连续拒付可能封号 | `max_card_attempts` 先设 5（比 opencode 保守），跑通后再调 |
