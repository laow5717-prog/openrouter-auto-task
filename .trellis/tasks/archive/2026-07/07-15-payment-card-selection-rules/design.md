# 技术设计 — 账单支付选卡规则

## 涉及边界

- `src/services/registration.py`
  - `recharge_account` 内 `_get_card`（选卡）、`_on_invoice_paid`（成功记账）、`_on_invoice_failed`（失败处理）
- `src/browser/driver.py`
  - `handle_unpaid_invoices` 里失败结果 → `on_failed` 的分派；3DS 结果已带专属 reason（[2139-2143](../../../src/browser/driver.py#L2139)）
- `src/models/`
  - 新增 `card_payment_state.py`（3DS 临时状态表模型）
  - `valid_card.py`：新增 `get_bound_email(card_number)`（取该卡 payment 首次成功账号）+ 导出用查询
  - `recharge_log.py`：新增 `success_count_since(card_number, hours)`、`last_success_at(card_number)`
  - `database.py`：建新表 `card_payment_state`
- `src/api/routes.py`：`/api/valid-cards` 响应增补每卡状态；新增 `/api/valid-cards/export`
- `frontend/`：有效卡查看处（`CardPool.vue` 已展示 valid_cards）增"导出"按钮 + 状态列；`npm run build`

## 数据来源策略（尽量复用，减少新增存储）

| 规则 | 数据来源 | 是否需新存储 |
|------|----------|--------------|
| R1 绑定账号 | `valid_cards.source_email`（card_number + source_type='payment' 的首次成功账号，`INSERT OR IGNORE`+`UNIQUE` 保证不被覆盖 = 永久绑定） | 否，复用 |
| R2 次数/冷却 | `recharge_logs`（`card_display`=完整卡号, `status='success'`, `created_at`）实时统计 24h 内成功次数 | 否，实时算 |
| R3 3DS 临时 | 需记录到期时间并覆盖"永久作废"行为 | **是**，新表 `card_payment_state` |

> 绑定与次数均可从既有数据实时派生，故不落冗余；仅 3DS 临时态需要显式持久化。

## 数据模型（新增）

```sql
CREATE TABLE IF NOT EXISTS card_payment_state (
    card_number TEXT PRIMARY KEY,
    tds_until   TEXT,                 -- R3：3DS 临时冷却到期（localtime 字符串），空/过期即可用
    tds_reason  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
```

- 表名用 `card_payment_state` 而非仅 tds，便于后续扩展（如显式 cooldown 缓存）。本期只用 `tds_until`。

## 选卡资格闸门（R1+R2+R3）

在 `recharge_account` 开头（拿到 `email`、`payment_cards` 后）构建一个 `_eligible(card_number)` 判定，并**先过滤** `payment_cards` 再进入既有 `_get_card` 的新卡/复用逻辑：

```
def _eligible(num):
    # R1 一卡绑一账号
    bound = valid_card_model.get_bound_email(num)      # '' 或某账号
    if bound and bound != email: return False, "已绑定其他账号"
    # R2 单卡 24h ≤ 2 次
    if recharge_log_model.success_count_since(num, 24) >= 2:
        return False, "24h内已支付2次(冷却中)"
    # R3 3DS 临时冷却
    if card_state_model.in_tds_cooldown(num):          # now < tds_until
        return False, "临时3DS冷却中"
    return True, ""
```

- 过滤后的集合喂给现有 `_get_card` 的 `_all_cards`/`_new_cards`/reuse 逻辑；`CARD_CAP=20`、避免连续同卡等既有策略保留。
- 被跳过的卡打印原因（AC1 可见性）。日志去重，避免刷屏。
- **R2 冷却语义**：采用"滚动 24h 窗口内成功 ≥ 2 次即排除"。等价于第 2 次成功后，直到最早那次成功滑出 24h 窗口前都不可选——即从命中上限起约冷却 24h。文档化此近似，不单独存 cooldown_until。

## 3DS 临时标记（R3）

- `_fill_stripe_payment_and_submit` 的 3DS 分支已返回专属 `error="需要 3DS 银行验证（已取消）"`、`card_fault=True`（[driver.py:2139](../../../src/browser/driver.py#L2139)）。**新增**在该返回值里带 `tds=True` 标志，`handle_unpaid_invoices` 透传给 `on_failed`。
- `registration._on_invoice_failed(invoice_id, card, reason, card_fault, tds=False)`：
  - 若 `tds` 且该卡**曾支付成功**（`valid_card.get_bound_email(num)` 非空，或 recharge_logs 有 success）→ 调 `card_state_model.set_tds(num, now+24h, reason)`，**不** `card_pool.mark_invalid`（临时，非永久）。
  - 否则维持现状（card_fault=True → mark_invalid；其余不动卡状态）。
- 到期自动恢复：`in_tds_cooldown` 判 `now < tds_until`；到期即视为可用，无需清理任务（可选加惰性清理）。

## R4 有效卡查看 + 导出

- **查看增强**：`/api/valid-cards` 响应对每张卡补充 `bound_email`（=source_email）、`tds_until`/是否 3DS 冷却、24h 成功次数/是否 R2 冷却——供前端展示状态列。
- **导出**：新增 `GET /api/valid-cards/export`，用 `openpyxl` 生成 xlsx（列对齐 `credit_cards_template.xlsx` 的 13 列 + 附加列：`bound_email`、`status`）。`send_file` 返回下载；文件名带日期。
- **前端**：在展示 valid_cards 的位置（`CardPool.vue`）加"导出"按钮，`window.open`/下载该接口；状态列展示绑定账号/冷却/3DS。构建 `npm run build`。

## 数据流

```
账单支付选卡:
  recharge_account(email, payment_cards)
    ├─ eligible = [c for c in payment_cards if _eligible(c.number)]   (R1/R2/R3 闸门)
    └─ _get_card 在 eligible 上做 新卡优先/复用/避免连续同卡 (既有)
         └─ 支付成功 → _on_invoice_paid → valid_card.record(payment, email)  (首次即绑定 email)
         └─ 3DS 失败 → _on_invoice_failed(tds=True) → card_state.set_tds(num, +24h)  (临时, 不永久作废)
```

## 兼容性 / 回滚

- 新表 `card_payment_state` 为增量，无破坏。
- 选卡闸门只做"额外排除"，不改既有新卡/复用算法主体；无可用卡时与现状一致（安全跳过）。
- 3DS 行为由"永久 invalid"改为"曾成功→临时"，回滚=恢复 `_on_invoice_failed` 原逻辑 + 忽略 tds。
- 导出/查看为新增接口与前端按钮，独立可回滚。

## 风险

- R2 滚动窗口近似 vs 严格"第2次成功起冷却24h"：文档化；如需严格，后续可存 cooldown_until。
- 闸门可能把某账号所有支付卡都排除 → 该账号账单当轮无卡可付（安全，留欠费待后续），需日志说明。
- `card_display` 必须是完整卡号才能统计——确认 `_on_invoice_paid` 写入的是完整 number（现状即完整 number）。
- 时区：`recharge_logs.created_at` 用 `localtime`，24h 窗口查询需用 `datetime('now','localtime','-24 hours')` 对齐。
- 前端 valid_cards 展示位置需确认（CardPool.vue 内嵌 vs 独立页），实现前核对。
