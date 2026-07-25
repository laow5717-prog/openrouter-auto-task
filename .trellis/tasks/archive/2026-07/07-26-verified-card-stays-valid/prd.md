# 已验证卡永久有效：曾成功卡失败改冷却不标无效

## Goal

让「曾支付成功过的卡（= 全局 valid_cards / 有效卡）」永久保持有效：即便下次支付失败也不再被标为 invalid，而是进入 24h 临时冷却，到期后可再次使用。并修复历史脏数据——「已验证卡」分组里被误标为 invalid 的有效卡恢复为有效。

## 背景与现状

- 判定口径（用户确认）：**全局 valid_cards 成员**（曾支付成功过的卡）即「有效卡 / 已验证卡」，规则对所有分组一致生效，自然覆盖名为「已验证卡」的分组（id=4）。
- 数据现状：`data/openrouter_auto.db` 中「已验证卡」分组 8 张卡全部在 valid_cards，但 `card_pool.status` 全被标成 `invalid`（is_valid 派生仍显示「有效」，但已落入无效桶 / 不可选）。
- 两条支付链路的失败归因不一致：
  - `src/services/registration.py`（充值 topup）：**已正确**——曾成功卡本次失败 → 打 24h 冷却，不标无效。
  - `src/web/app.py`（订阅试卡）：**有缺陷**——`oc == 'failed'` 时无条件 `mark_invalid_by_number`，会把曾成功的有效卡打成 invalid。这是 8 张卡被误标的元凶。
- 「有效」在本系统是**派生态**（由 valid_cards 成员身份推导，见 `routes.py` 的 `card['is_valid']`），并非 `card_pool.status` 的一个独立取值。因此「设置成有效」= 清除其 invalid/expired 状态，让派生态与桶归类恢复为有效，而非引入新的 status 常量。

## Requirements

- R1：`card_pool.mark_invalid_by_number` 增加底层不变式——**valid_cards 成员永不被标为 invalid**（无论调用方是谁），与 `mark_bound_by_number` 保留 paid 的做法同构。
- R2：`src/web/app.py` 订阅试卡流程的 `failed` 分支对齐 `registration.py`：若该卡是有效卡（in valid_cards），则打 24h 冷却（reason 注明「曾成功卡本次支付失败，速率冷却」）而非标无效；非有效卡才标无效。
- R3：一次性数据修复——把 valid_cards 成员中当前 `status IN ('invalid','expired')` 的卡清为空状态（恢复为有效桶 / 可选），并在主库执行。
- R4：不引入新的 status 常量；「有效」继续走 valid_cards 派生态。
- R5：选卡时有效卡若处于 24h 冷却期内不被选中（现有 `card_state.in_cooldown` 预过滤已覆盖，需确认订阅流程也生效或无回归）。

## Acceptance Criteria

- [ ] `mark_invalid_by_number` 对 valid_cards 成员调用后，其 status 不变为 invalid（单元/集成可验证）。
- [ ] 订阅流程中一张曾成功卡再次 `failed`：status 不变为 invalid，且被写入 24h 冷却；非有效卡 `failed` 仍标 invalid。
- [ ] 运行数据修复后，「已验证卡」分组 8 张卡的无效计数归零、有效(在库)计数为 8。
- [ ] 冷却期内的有效卡不会被订阅/充值流程选中；冷却到期后可再次被选。
- [ ] 现有测试（tests/test_card_pool_bound.py 等）不回归。

## Notes

- 冷却基础设施已存在：`CardPaymentStateModel.set_cooldown/in_cooldown`（24h，物理列 tds_until 共用）。
- 有效卡判定统一用 `valid_card.is_valid(num)`（valid_cards 成员）。
