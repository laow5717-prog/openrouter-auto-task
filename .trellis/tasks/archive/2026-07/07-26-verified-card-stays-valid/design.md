# Design — 已验证卡永久有效

## 核心原则

「有效卡 = valid_cards 成员」是唯一口径。围绕它建立三层防护：
1. **底层不变式**（模型层）：valid_cards 成员永不被写成 invalid。
2. **上层意图正确**（业务流程）：订阅流程失败时对有效卡改打冷却而非标无效，与充值流程一致。
3. **历史数据修复**（一次性）：把已被误标的有效卡恢复。

## 变更点

### 1. `src/models/card_pool.py` — mark_invalid_by_number 加不变式

现状：
```python
def mark_invalid_by_number(self, card_number):
    self.mark_status_by_number(card_number, CARD_STATUS_INVALID)
```

改为带子查询守卫（同库内 valid_cards 可直接子查询，无需注入 valid_card 模型）：
```python
def mark_invalid_by_number(self, card_number):
    """标记为无效卡（支付被拒等卡自身原因）。
    但 valid_cards 成员（曾支付成功过的有效卡）永不被标无效——它已被证明可用，
    再次被拒只应进入临时冷却（见 CardPaymentStateModel.set_cooldown），而非永久作废。"""
    self.db.execute(
        "UPDATE card_pool SET status=? WHERE card_number=? "
        "AND card_number NOT IN (SELECT card_number FROM valid_cards)",
        (CARD_STATUS_INVALID, card_number),
    )
```
- 影响面：这是所有「标无效」的最终收口，registration.py 的 else 分支、app.py 的 failed 分支都经它。即便调用方漏判，有效卡也不会被误标。
- 兼容性：非有效卡行为不变。

### 2. `src/web/app.py` — 订阅试卡 failed 分支对齐 registration.py

现状（约 736–740）：
```python
elif oc == 'failed':
    models['card_pool'].mark_invalid_by_number(num)
    models['recharge_log'].mark_failed(log_id, ...)
    self.add_log(f"{email} 卡 ****{last4} 拒付，标 invalid，换下一张")
```

改为：先判是否有效卡，有效卡打 24h 冷却，非有效卡才标无效。
```python
elif oc == 'failed':
    if models['valid_card'].is_valid(num):
        models['card_state'].set_cooldown(num, hours=24, reason='曾成功卡本次支付失败，速率冷却')
        note = '曾成功有效卡，24h 冷却'
    else:
        models['card_pool'].mark_invalid_by_number(num)
        note = '标 invalid'
    models['recharge_log'].mark_failed(log_id, error=res.get('err', ''), api_response={"result": res})
    self.add_log(f"{email} 卡 ****{last4} 拒付，{note}，换下一张")
```
- `models['card_state']` 即 CardPaymentStateModel（routes.py 已用同名键，确认 app.py 的 models 字典含该键）。
- 有 R1 的不变式兜底，即使这里漏判，有效卡也不会被 mark_invalid 打成无效；此处的价值是**主动打冷却**，让有效卡本轮被跳过、24h 后恢复。

### 3. 数据修复脚本 `scripts/fix_valid_cards_status.py`

一次性把 valid_cards 成员里 status∈(invalid,expired) 的卡清为空状态：
```sql
UPDATE card_pool SET status='' 
WHERE card_number IN (SELECT card_number FROM valid_cards) 
  AND COALESCE(status,'') IN ('invalid','expired');
```
- 清为 `''` 而非 `paid`：valid_cards 可能来自 bind 或 payment，统一置空最安全；「有效」由 is_valid 派生渲染，无需具体 status 值。
- 脚本走项目 Database 连接（`src/models/database.py`），打印修复前后计数。幂等：重复运行无副作用。

## 不做的事

- 不新增 status 常量（R4）。
- 不改 registration.py 的失败归因逻辑（已合规）；仅受益于 R1 不变式的兜底。
- 不改 `_bucket_where` / `count_buckets` / NOT_SELECTABLE 语义。

## 风险与回滚

- 风险：`card_number NOT IN (SELECT ...)` 子查询在超大 card_pool 上的性能——card_number 有索引，valid_cards 极小（8 行量级），可忽略。
- 回滚：三处改动相互独立，可单独 revert。数据修复不可自动逆（但只是把无效恢复为有效，方向安全）。
