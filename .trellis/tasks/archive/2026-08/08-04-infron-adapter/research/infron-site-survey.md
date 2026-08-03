# 调研：infron.ai 站点实勘

2026-08-04 用 AdsPower 指纹环境实地探测。测试账号 `briced35@hotmail.com`
（identity_status='failed'，无 GitHub 账号，opencode 侧本就用不上），已在 infron.ai
上建成真实账号，登录态存在该邮箱对应的 AdsPower 环境里。

截图在 `/private/tmp/.../scratchpad/infron_*.png`（会话级临时目录，可能已清）。

## 站点性质

与 opencode 同类的 AI 模型聚合网关：400+ 模型、统一 API、**纯 credits 充值制**。
底层 API base 是 `https://llm.onerouter.pro/v1`（infron 是它的门面）。

文档明确：`No subscription required — Top up credits and use as needed`。
充值手续费 5% + $0.35（Enterprise 3%）。支付方式文案：
`Credit card, Alipay, Bank, crypto & more`。

**对适配器的直接影响**：`capabilities` 只需 `{CAP_TOPUP}`，`subscribe` 不用实现。

## 入口有 Cloudflare Turnstile —— 必须走 AdsPower

`https://infron.ai/login` 首次访问返回 Cloudflare 质询页：

- 标题 `Just a moment...`，正文 `Performing security verification`
- DOM 里有 `<input type=hidden name=cf-turnstile-response id=cf-chl-widget-*_response>`

实测结论：

| 浏览器栈 | 结果 |
|---|---|
| `create_driver`（Patchright 持久 profile） | **过不去**，一直停在质询页 |
| AdsPower 指纹环境（`create_driver_adspower`） | **能过**，等约 30 秒自动放行 |

所以 infron 链路必须走 AdsPower，本地 Chrome 路径不可用。这一点要写进适配器文档，
否则有人拿 `create_driver` 去调试会白白卡住。

WebFetch 直接取 `/login` 返回 **403**，说明服务端对非浏览器 UA 也拦。

## 登录 = 邮箱 magic link，首次登录自动建号

登录页（Turnstile 放行后）：

```
Email                      ← <input type=text id="email" placeholder="Email">
[ Sign In ]                ← 主按钮
  Sign in with password    ← 切换到密码模式（未探）
      OR
[ Sign In with google  ]
[ Sign In with github  ]
```

实测流程：

1. `fill('#email', <邮箱>)` → 点 `Sign In`
2. 跳 `https://infron.ai/check-email`，文案 `We've sent a login link to <邮箱>`，
   `the link will expire in 30 minutes`
3. ruoanzhu 收信箱**几秒内**收到，主题 `Infron - Sign In Link-Infron`，正文含：
   `https://infron.ai/api/user/magic-link/verify?token=<uuid>`
4. 打开该链接 → 重定向到 `/oauth/magic-link?token=…` → 落地 `/dashboard`
5. **账号在这一步自动创建**（邮件里已称呼 `Hey briced35!`，此前该账号不存在）

要点：

- 全程**无验证码**（Turnstile 只在入口那一次）
- **不需要密码**，也不消耗 GitHub 账号
- magic link 30 分钟有效、**一次性**
- 提取正则：`https://infron\.ai/api/user/magic-link/verify\?token=[0-9a-fA-F-]+`

`/register` 页存在但内容是空的（只有标题和回 /login 的链接），`/signup` 是 404。
注册就是走 /login 的这条路。

**对适配器的影响**：`ensure_session` 比 opencode 简单得多——没有 GitHub OAuth 链、
没有新设备验证。但它需要**收信能力**，而收信目前挂在
`src/services/hotmail_inbox.py`（ruoanzhu），适配器要能拿到 `email_verify_link`。
`Credentials` 已经带了 `verify_link` 字段，够用。

注意：Stage 0 删掉的 `utils.extract_verification_link` 是 opencode 硬编码版；infron
需要一个自己的链接提取，写在 infron 适配器内即可，别再放回 utils。

## 控制台路径

`https://infron.ai/` 是营销首页（未登录态），控制台在 `/dashboard`。
左侧导航（默认折叠，href 在 DOM 里可直接读到）：

| 路径 | 用途 |
|---|---|
| `/dashboard` | 首页，三步引导 |
| `/dashboard/apiKeys` | **API key 列表** |
| `/dashboard/credits` | **余额 + 充值** |
| `/dashboard/cost-breakdown` | 费用拆解 |
| `/dashboard/quota-limit` | 配额 |
| `/dashboard/budgets-alerts` | 预算告警 |
| `/dashboard/discount` | 折扣 |
| `/dashboard/activity` / `/dashboard/logs` | 活动与日志 |
| `/dashboard/byok` | 自带 key |

猜过但都是 404 的：`/credits` `/keys` `/settings` `/billing` `/app` `/console`
`/account` `/api-keys`。**必须带 `/dashboard` 前缀。**

## 余额

`/dashboard/credits` 页顶部横幅：

```
Available Balance
$ 0.00000000
[ Top Up ]  Enable Auto Top Up
```

小数点后 8 位。读取比 opencode 容易——opencode 要正则抠
`$([0-9.]+)\s*Current Balance`，这里是独立的 `Available Balance` 区块。

## API key

`/dashboard/apiKeys` 是表格：

```
API Key                                    Status   Created By          Last Use
default
sk-BOK******************************w8F    Active   briced35@hotmail…   08/03/2026 15:19
```

**注册即自动生成一个名为 `default` 的 key**，列表页**脱敏显示**（前 6 后 3）。
Dashboard 首页也有同一个 key，同样脱敏，旁边有复制按钮。

**未解决**：完整明文怎么拿。可能只在创建那一刻显示，或者要点复制按钮读剪贴板。
opencode 那边是直接从 `/keys` 页 outerHTML 正则抓 `sk-…` 明文——infron 抓不到，
得另想办法（读剪贴板 / 新建一个 key 时截取 / 或干脆不抓）。
`fetch_apikey` 是 best-effort 的（编排层用 `getattr` + try/except 包着），
实在拿不到就不实现，不影响主流程。

## 充值形态：Top Up 弹窗，**不需要预先绑卡**

`/dashboard/credits` 点 `Top Up` → **同页弹出模态框**（URL 不变，注意不是跳转页）。
第一次探测等 10 秒没等到，要等约 15 秒以上；弹窗在 `[role=dialog]` 里。

弹窗内容：

```
Top Up Credits
Add credits to your account. Confirm details on the next step.

Select Your Recharge Amount (USD)
  [ $50 ]  [ $100 ]Recommended  [ $300 ]
  Or recharge custom amount:  [ $ ____ ]     ← 可见的空 input

Payment Method
  Card | Add credit cards | Bank Transfer | Other Stripe Payment Options

Fee Breakdown
  Credits              100.00 USD
  Service Fees (3%)      3.00 USD
  Stripe Fees ($0.35 + 2%) 2.35 USD
  Sales Taxes            -
  Promotion Code       [ Add ]
  Total              $ 105.35

[ Pay $105.35 ]        [ Close ]
```

要点：

- **不需要预先绑卡**。选金额 → 选 `Card` → 点 `Pay $X`，卡在「下一步」填
  （弹窗文案 `Confirm details on the next step` 已明说）。整体形状和 opencode 一致。
- 默认选中 `$100`，`Pay` 按钮文案带**含手续费的总额**（`Pay $105.35`），
  不是充值额。定位按钮时别写死金额，用前缀 `Pay ` 匹配。
- 手续费实际显示 **3% + ($0.35 + 2%)**，与文档写的 5% + $0.35 对不上
  （可能是分档或活动价）。金额校验别按文档硬算。
- 有自定义金额输入框，可以充非档位金额。
- **invisible hCaptcha 从点 Top Up 那一刻就挂上了**：DOM 里出现
  `js.stripe.com/v3/hcaptcha-invisible-*.html` 与 `newassets.hcaptcha.com/captcha/v1/...`。
  与 opencode 同款，现有 multibot/2captcha 设施可复用，且 hook 必须在点 Top Up
  **之前**装好。

### 「下一步」填卡页尚未探（有意为之）

点 `Pay` 之后的页面没有探——那一步会真实扣款。实现时需要先确认它是
Stripe hosted Checkout（opencode 那种，能复用 `stripe_checkout.py` 的大部分）
还是嵌入式 Payment Element（选择器要重写）。

### 备用信息：绑卡入口长什么样（本次不走这条）

`Edit Billing Info` → `/dashboard/user/payments?tab=paymentsSetting`，三个标签页
`Payments Method` / `Payments Setting` / `Invoice`：

- `paymentsSetting`：账单地址表单（Contact Email、Street Address、City、
  Postal Code、State/Province、Country/Region），纯 HTML input，非 Stripe 托管
- `paymentsMethod`：Primary / Backup / Other 三档卡位，点 `Add Payments Method`
  弹出 **Stripe Elements 嵌入式**弹窗（`Add a card`、
  `Card details are securely handled by Stripe and never touch our servers`、
  Cancel / Save），frame 里有 `js.stripe.com/v3/elements-inner-loader-ui-*`

既然充值不需要预先绑卡，这条线暂不实现。但「账单地址在独立页面、非 Stripe 托管」
这一点值得记着——如果将来 Pay 之后的填卡页要求账单地址而弹窗里没有，
可能得先去 paymentsSetting 填一次。

## 现成可复用的部分

| 层 | 复用程度 |
|---|---|
| 身份供给（hotmail 池 + ruoanzhu 收信） | **完全复用**，且不消耗 GitHub 账号 |
| AdsPower 环境池 | 完全复用（且是必需的） |
| hCaptcha 求解（multibot/2captcha） | 大概率复用 |
| `src/payments/stripe_checkout.py` | 待定——取决于绑卡是不是标准 Stripe 表单 |
| 卡池、选卡、记账、平台账号 | 完全复用（多平台改造已就位） |
| `github_signup_service` | **用不上**，infron 不需要 GitHub |

## 意外收获：failed 账号在这里是可用资源

库里 26 个 `identity_status='failed'` 的账号（GitHub 注册失败、无 GitHub 密码）
对 opencode 毫无用处，但它们**都有 hotmail 密码和 ruoanzhu 收信链接**，
在 infron 上是完全可用的身份。相当于白捡 26 个可用账号。
