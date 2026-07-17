# 技术设计 — topup 提交后强制进入账单支付流程

## 改动范围

仅 [src/services/registration.py](../../../src/services/registration.py) 内 `recharge_account()` 的两处早返回。不动 driver.py、app.py、routes.py、前端。

## 核心原则

把两处 `if not pay_success: return ...(failed)` 从"提交失败 → 直接返回"改为"提交失败 → 打日志、把 responses 归一为 `[]`，继续向下走既有账单处理分支"。下游 `handle_unpaid_invoices` / `_classify_topup` / `_settle_topup_balance` 均已能安全接受 `responses=[]`（内部都用 `responses or []` / `for r in (responses or [])`），无需额外防护。

`pay_success` 语义边界仍保持："提交失败"＝点击/收响应异常；"提交成功但被拒"＝`pay_success=True`+confirm 判失败，后者行为本来就正确，不触碰。

## 全量模式（[registration.py:897-902](../../../src/services/registration.py#L897)）

现状：
```python
pay_success, responses, card_last4 = fill_topup_and_confirm(driver, amount=10)
_report("topup_confirmed")

if not pay_success:
    print(f"账号 {email} 充值确认失败")
    return False, "填写金额或确认支付失败", responses or [], card_last4, "failed"

print(f"账号 {email} 充值 $10 已提交")
```

改为：
```python
pay_success, responses, card_last4 = fill_topup_and_confirm(driver, amount=10)
_report("topup_confirmed")
responses = responses or []

if not pay_success:
    # 提交步骤异常：不再直接返回。只要选了支付卡分组，仍进账单支付流程
    # （用户要求：提交后无论成败都去账单页）。skip_invoice=True 时无账单可处理，
    # 下方分支会跳过，最终由 _classify_topup 判失败收尾。
    print(f"账号 {email} 充值提交异常，仍按要求继续账单支付流程")
else:
    print(f"账号 {email} 充值 $10 已提交")
```

其后 `if not skip_invoice:`（读余额 → `handle_unpaid_invoices`）和 `_classify_topup` 均无需改动。提交失败时余额不会增长，`_classify_topup` 会返回 `topup_ok=False`，`outcome="failed"`；但账单页已被访问、任何 open invoice 会被支付并经 `_on_invoice_paid` 记账。

## 单步模式（[registration.py:768-772](../../../src/services/registration.py#L768)）

现状：
```python
pay_success, responses, step_card_last4 = fill_topup_and_confirm(driver, amount=TOPUP_AMOUNT)
_report("topup_confirmed")
if not pay_success:
    return (False, "填写金额或确认支付失败", responses or [], step_card_last4, "failed",
            {'today_count': before_count, 'generated': False, 'paid': 0, 'topup_ok': False})
```

改为：
```python
pay_success, responses, step_card_last4 = fill_topup_and_confirm(driver, amount=TOPUP_AMOUNT)
_report("topup_confirmed")
responses = responses or []
if not pay_success:
    # 提交异常：不再直接返回，继续走下方账单处理 + 收尾。generated/paid/topup_ok
    # 由实际结果（after_count 差值 / handle_unpaid_invoices / _classify_topup）如实反映。
    print(f"账号 {email} 单步 Top-up 提交异常，仍按要求继续账单支付流程")
```

删除早返回后，控制流自然进入既有的 `if not skip_invoice:`（读余额 → `handle_unpaid_invoices`）→ `fetch_today_invoice_count` → `_classify_topup` → 组装 `info` → `return True, reason, responses, step_card_last4, "stepped", info`。提交失败时 `generated` 多为 False、`topup_ok` False、`paid` 视账单支付结果而定，均如实。

## 上层兼容性（[app.py](../../../src/web/app.py)）

- 全量：新增路径最终仍走 `outcome ∈ {"topup","failed"}`（提交失败→"failed"），app.py:422/434 分支照常消费。Top-up log 记 failed，账单支付 log 由 registration 内部回调独立写。
- 单步：`outcome` 由 "failed" 改为可能返回 "stepped"（当继续走完账单处理）。app.py:378 的 "stepped" 分支按 `info.topup_ok` 记 success/failed，编排层 `made_progress = generated or paid>0` 逻辑天然兼容——提交失败且没付成任何账单则不计进展，符合预期。这是本改动唯一的返回语义变化，需重点验证不破坏 round-robin 编排。

## 边界与风险

- 提交真失败（按钮从未点上）也会去账单页：这是可接受的鲁棒行为——账单页若无 open invoice，`handle_unpaid_invoices` 首查 0 行后按既有 `INVOICE_DETECT_RETRIES` 重试再返回空，无副作用；若有历史遗留 open invoice 则顺带付掉，符合用户"总是清账单"意图。
- 不引入重复扣款：Top-up 与账单支付是两笔独立事务，各自记账。
- `responses=[]` 传入 `_settle_topup_balance` / `_topup_confirm_requires_action` / `extract_decline_from_responses` 均安全（空迭代）。
