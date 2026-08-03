# Stage 1 结论：infron 的填卡形态

2026-08-04 实探，账号 `briced35@hotmail.com`，AdsPower 环境。**未提交任何付款。**

## 决定性事实：嵌入式 Payment Element，不是 hosted Checkout

点 `Pay $X` 后**页面不跳转**——主 URL 始终是
`https://infron.ai/dashboard/credits`。付款表单出现在

```
https://js.stripe.com/v3/elements-inner-payment-*.html
```

这是 **Stripe Payment Element**。opencode 用的是 hosted Checkout
（整页跳 `checkout.stripe.com`），两者的 DOM 结构与定位方式不同。

**对 `stripe_checkout.py` 复用性的直接影响见文末那张表。**

## Top Up 是两步弹窗

同一个模态框里分两步，`Back` 按钮可退回：

**第一步**（点 Top Up 后）
```
Top Up Credits
Add credits to your account. Confirm details on the next step.
Select Your Recharge Amount (USD):  [$50] [$100]Recommended [$300]  + 自定义输入
Payment Method:  Card | Add credit cards | Bank Transfer | Other Stripe Payment Options
Fee Breakdown …  Total $52.85（选 $50 时）
[ Pay $52.85 ]  [ Close ]
```

**第二步**（点 Pay 后，同一弹窗内容替换）
```
Top Up Credits
Enter your card or another Stripe payment method to complete the top-up.
  ← 这里嵌 Stripe Payment Element
[ Back ]  [ Pay $52.85 ]  [ Close ]
```

注意第二步**还有一个 `Pay $X` 按钮**——两步的按钮同名。实现时要用弹窗内的文案
（`Confirm details on the next step` vs `Enter your card or another Stripe payment method`）
区分当前处在哪一步，不能只靠按钮名。

## Payment Element 里的支付方式是 tab，且默认不是 Card

Element frame 内的 tab：

```
Alipay | Card | Afterpay | US bank account | Cash App Pay | More
```

**默认选中 Alipay**（文案 `After submission, you will be redirected to securely
complete next steps.`）。必须先点 `Card` tab，卡号字段才会出现。

这与 opencode 不同：opencode 的 hosted Checkout 用的是 accordion
（`#payment-method-accordion-item-title-card`），`stripe_checkout.select_card_method`
就是按那个写的，**在 Payment Element 上不适用**，要另写。

## hCaptcha 是硬门槛，且卡在 Element 初始化之前

点 Top Up 那一刻起，DOM 里就挂上

```
https://js.stripe.com/v3/hcaptcha-invisible-*.html
https://newassets.hcaptcha.com/captcha/v1/*/static/hcaptcha.html#frame=challenge
```

**未装解题 hook 时**，hCaptcha frame 显示 `Please try again. ⚠️` + `Verify` 按钮，
且 Payment Element **加载不出来**（第二次探测时 `elements-inner-payment` frame
根本没出现，弹窗停在 Back/Pay/Close）。

结论：**hook 必须在点 Top Up 之前装好**。编排层
（`registration.recharge_account`）已经在建会话后、调 `top_up` 前装 hook，
所以生产链路没问题；但**任何绕过编排层的调试脚本都会卡在这里**，
而现象（Element 不出现）看起来像页面加载慢，极易误判。

## 没能拿到的：卡号字段的精确选择器

因为上述 hCaptcha 阻断，探针没能让 Element 渲染出卡号/有效期/CVC 字段，
所以**没有拿到它们的选择器**。

这不是阻塞项，但要如实记着：Stage 3 写填卡代码时，第一件事是**带着 captcha hook**
再跑一次探测，把字段选择器补进这份文档。在此之前不要照抄 opencode 的选择器——
两边表单结构不同。

## `stripe_checkout.py` 逐函数复用判定

| 函数 | 判定 | 说明 |
|---|---|---|
| `_stripe_frame` / `_wait_stripe_frame` | **要改** | 现在按 `checkout.stripe.com` 找 frame；Payment Element 的 frame 是 `js.stripe.com/v3/elements-inner-payment-*`。建议加参数而不是加分支 |
| `select_card_method` | **重写** | accordion vs tab，结构不同 |
| `pick_currency_usd` | **不需要** | infron 弹窗自己选金额，币种固定 USD |
| `fill_card_and_address` | **待定** | 拿到字段选择器后才能判；Stripe 的卡字段命名（`cardNumber`/`cardExpiry`/`cardCvc`）在 Element 与 Checkout 里通常一致，有希望复用 |
| `fill_phone_if_present` | **待定** | Element 里有没有手机号字段未知 |
| `uncheck_save_info` | **待定** | 未见 `#enableStripePass`，可能不适用 |
| `check_ai_agent_consent` | **不需要** | opencode 专有 |
| `click_pay` | **重写** | infron 的 Pay 按钮在 infron 自己的弹窗里，不在 Stripe frame 内 |
| `_captcha_challenge_present` / `_captcha_frames_debug` | **可复用** | hCaptcha frame 的判定逻辑与站点无关 |
| `_threeds_*` 一族 | **大概率可复用** | 3DS 挑战是 Stripe 侧行为，与集成方式关系不大 |
| `_DECLINE_HINTS` | **可复用** | 拒付文案来自 Stripe/发卡行 |

**大意**：验证码与 3DS 那半（最难、踩坑最多的部分）能复用；表单定位那半要按
Payment Element 重写。

## 账单地址

第二步弹窗里没看到账单地址字段（Element 未渲染，无法确认）。
infron 另有独立的账单地址页 `/dashboard/user/payments?tab=paymentsSetting`
（纯 HTML input，非 Stripe 托管）。

若 Stage 3 发现 Element 要求账单地址而弹窗里没有，可能需要**先去那个页面填一次**。
届时那会变成 `ensure_session` 之后、`top_up` 之前的一次性准备步骤。
