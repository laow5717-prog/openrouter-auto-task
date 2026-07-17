# topup 提交后强制进入账单支付流程

## Goal

只要在 Top-up 弹窗点击了 "Confirm and pay" 发起提交，无论后续付款结果是成功、被拒还是提交步骤本身抛异常，充值流程都必须继续跳转到账单页面执行 Unpaid invoice 支付流程（`handle_unpaid_invoices`），而不是像现在这样在提交失败时直接返回。

## Background / 现状

`recharge_account()`（[src/services/registration.py](../../../src/services/registration.py)）有两条主路径：

- **全量模式**（`invoice_daily_cap is None`）：[registration.py:897-933](../../../src/services/registration.py#L897)
- **单步(round-robin)模式**（`invoice_daily_cap` 为整数）：[registration.py:766-798](../../../src/services/registration.py#L766)

两处都调用 `pay_success, responses, card_last4 = fill_topup_and_confirm(...)`。

- `pay_success` 语义是"**是否成功点击了 Confirm 按钮并收集到响应**"，**不是**"付款成功"。扣款被拒（402/decline）时它仍返回 `True`，此时代码已经会进入 `handle_unpaid_invoices`。真实付款成功/失败由之后的 `_classify_topup`（以 Stripe confirm 为权威）判定。
- 唯一会跳过账单页的缺口：两处都有 `if not pay_success: return (..., "填写金额或确认支付失败", ..., "failed")`——即"点击/收响应"这步抛异常时直接返回，**不进账单支付流程**。

## Requirements

- R1：当 `fill_topup_and_confirm` 返回 `pay_success=False` 时，**不再直接返回**；只要没有勾选 `skip_invoice`，仍继续执行"返回 credits 页读余额 → `handle_unpaid_invoices` 账单支付"流程。
- R2：付款被拒（`pay_success=True` 但 `_classify_topup` 判失败）的既有行为保持不变——仍进账单页（现状已满足，不得回归）。
- R3：`skip_invoice=True`（未选支付卡分组）时行为不变：无账单可处理，提交失败仍按 `failed` 收尾，不去账单页。
- R4：Top-up 本身的成功/失败判定仍由 `_classify_topup` 负责，提交失败时该笔 Top-up 记为失败（余额未增长 → topup_ok=False）；账单支付若成功，其记账走 `_on_invoice_paid` 独立记录，不与 Top-up 的失败混淆、不重复计数。
- R5：两种模式（全量 / 单步）都要覆盖，返回元组结构（5 元组 / 6 元组）和 `outcome` 取值语义对上层 [app.py](../../../src/web/app.py) 编排逻辑保持兼容。

## Acceptance Criteria

- [ ] 全量模式：`fill_topup_and_confirm` 抛异常返回 `pay_success=False` 且 `skip_invoice=False` 时，代码会导航到 credits 页并调用 `handle_unpaid_invoices`，而非提前返回。
- [ ] 单步模式：同上，`pay_success=False` 时不再提前返回，继续走账单处理并正常返回 6 元组（`outcome` 合理、`info` 字段完整）。
- [ ] 付款被拒（`pay_success=True`、confirm 判失败）仍进账单页——回归验证不变。
- [ ] `skip_invoice=True` 且提交失败：仍返回 `failed`，不去账单页。
- [ ] 提交失败但账单支付成功的场景：Top-up 记为失败、账单支付经 `_on_invoice_paid` 记为成功，无重复/误记。
- [ ] 上层 app.py 编排（stepped / failed / cap_reached / topup / invoice_only 分支）无需改动即可正确消费返回值。

## Notes / 非目标

- 不改 `fill_topup_and_confirm` 的付款判定逻辑与 `_classify_topup` 权威判定。
- 不改选卡规则、24h 冷却、3DS 等既有策略。
- 不改前端与 API 层。
