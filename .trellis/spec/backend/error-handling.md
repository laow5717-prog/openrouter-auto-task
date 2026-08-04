# Error Handling

> How errors are handled in this project.

---

## Overview

<!--
Document your project's error handling conventions here.

Questions to answer:
- What error types do you define?
- How are errors propagated?
- How are errors logged?
- How are errors returned to clients?
-->

(To be filled by the team)

---

## Error Types

<!-- Custom error classes/types -->

(To be filled by the team)

---

## Error Handling Patterns

<!-- Try-catch patterns, error propagation -->

(To be filled by the team)

---

## API Error Responses

<!-- Standard error response format -->

(To be filled by the team)

---

## Common Mistakes

<!-- Error handling mistakes your team has made -->

### 按错误前缀归因会误杀好卡

支付失败时会把底料卡在**该平台**标为 `invalid`（`card_platform_state`），此后
`get_usable_cards_as_list(platform, …)` 在那个平台上永远不再选中它。**这个操作不可逆**
（卡在别的平台仍可用，但这个平台上废了），因此归因必须保守。

判废现在还要**连续失败达阈值**才发生（`card_payment_state.fail_streak` ≥
`RechargeConfig.max_fail_streak`，默认 3，成功一次即清零），每次失败另加一段冷却
（默认 24h）。这**不降低**归因的要求：算不算「一次失败」仍由下面这套规则决定，
误判只是把判废从「立刻」推迟到「三次之后」，照样会烧掉好卡。

错误串带分类前缀（`[外部原因]` / `[表单字段错误]` / `[操作失败]` / `[验证超时]` /
`[浏览器中断]` / `[超时]` / `[Stripe字段错误]` / `[控制台表单错误]`），但**前缀不足以定性**：

- `[Stripe字段错误] Please provide a mobile phone number.` —— 这是 Stripe Link
  勾选框要求填手机号，与卡毫无关系。当时该问题导致**每一张卡**都失败；若按前缀
  归因，一整批完好的卡会被永久标成无效。

因此 `utils.is_card_fault()` 的判定顺序是：

1. 命中否定词（`mobile phone number` / `captcha` / `turnstile` / `人机验证` …）→ 一律不归因
2. 环境类前缀（`[操作失败]` `[验证超时]` `[浏览器中断]` `[超时]`）→ 不归因
3. 卡片类前缀（`[外部原因]` `[表单字段错误]`）→ 归因
4. 其余按文案白名单匹配（declined / incorrect cvc / invalid card number / 被拒 …）
5. 都不匹配 → **不归因**

原则：**宁可漏标**。漏标的代价只是下次再试一次这张卡；误标是永久废掉一张好卡。
新增判定规则时先补 `tests/test_card_fault.py`。

### outcome 层面同样有「不消耗卡」的硬约束

上面讲的是错误**文案**的归因；再往上一层，`PaymentResult.outcome` 决定这张卡是否
被消耗。`needs_captcha` / `error` / `unknown` 三者一律**不动卡的任何状态**——分别是
账号级风控、付款前的页面故障、提交后无定论，都不是卡的问题。常量
`platforms.base.OUTCOMES_KEEPING_CARD` 与 `PaymentResult.keeps_card` 把这条固化下来，
新平台的适配器必须按同样语义归类自己的失败，否则一次网络抖动就会废掉一张好卡。

`[Stripe字段错误]` 类的卡则相反——那是卡数据本身填不进表单，换哪个平台都一样，所以
`get_stripe_field_error_card_numbers()` 刻意**不按平台过滤**。

### 客户端表单校验 ≠ 拒付

Stripe 的这几句是**提交给银行之前**的前端校验，与拒付有本质区别：

```
Your card number is incomplete.      Your card number is incorrect.
Your card's security code is incomplete.    Your card's expiration date is incomplete.
```

成因只有两类，两类都不说明「这张卡是坏的」：

1. 我们没把字段填完整（Stripe 的受控输入会在 DOM 重排时吞字符，
   见 `_type_and_verify` 的逐字符输入 + 回读校验）；
2. 该 Stripe 账户没启用这个卡种，位数对不上——实测 14 位 Diners 在 infron 的
   Payment Element 上就报 incomplete。

所以必须归 `error`（不消耗卡），不能归 `failed`。

⚠️ **`_INPUT_INVALID_HINTS` 必须先于 `_DECLINE_HINTS` 判定。** 后者含裸词
`incorrect` 与 `card number is`，会把上面这几句一并吞掉判成拒付。infron 实跑中
两张好 Diners 就是这么被判废的，而判废不可逆。判定顺序有回归测试钉住
（`tests/test_infron_adapter.py::test_input_validation_is_checked_before_decline`）。

同一个坑的另一面：**拒付文案不要扫全部 frame**。`_DECLINE_HINTS` 里的裸词是按
hosted Checkout 主文档那个收敛范围挑的；页面上还挂着 hCaptcha 帧、Stripe 控制帧、
站点自己的 UI，任意一处出现 `expired` 都会被误判。只扫主文档 + 付款表单帧
（`elements-inner-payment*`）。代价是有时退化成 `unknown`——那是安全的一侧。
