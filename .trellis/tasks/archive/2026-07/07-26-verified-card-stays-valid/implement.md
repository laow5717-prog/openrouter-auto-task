# Implement — 已验证卡永久有效

## 执行顺序

### Step 1 — 模型层不变式（R1）
- 文件：`src/models/card_pool.py`
- 改 `mark_invalid_by_number`：改为带 `AND card_number NOT IN (SELECT card_number FROM valid_cards)` 的 UPDATE，补 docstring 说明「valid_cards 成员永不标无效」。
- 校验点：非 valid_cards 卡仍会被标 invalid；valid_cards 卡调用后 status 不变。

### Step 2 — 订阅流程 failed 分支（R2）
- 文件：`src/web/app.py`（约 736–740，`elif oc == 'failed':`）
- 有效卡（`models['valid_card'].is_valid(num)`）→ `models['card_state'].set_cooldown(num, hours=24, reason='曾成功卡本次支付失败，速率冷却')`；否则 `mark_invalid_by_number(num)`。日志文案区分两种情况。
- 保留 `recharge_log.mark_failed` 记账不变。

### Step 3 — 数据修复脚本（R3）
- 新建 `scripts/fix_valid_cards_status.py`：用项目 Database 连接，
  1) 打印修复前：valid_cards 成员中 status∈(invalid,expired) 的计数；
  2) 执行 `UPDATE card_pool SET status='' WHERE card_number IN (SELECT card_number FROM valid_cards) AND COALESCE(status,'') IN ('invalid','expired')`；
  3) 打印修复行数 + 「已验证卡」分组桶计数（invalid 应为 0，valid 应为 8）。
- 运行：`python3 scripts/fix_valid_cards_status.py`

### Step 4 — 测试
- 新增/补 `tests/` 覆盖：
  - `mark_invalid_by_number` 对 valid_cards 成员是 no-op；对普通卡生效。
  - （可选）订阅 failed 分支的分流逻辑（有效卡→冷却，普通卡→invalid）。
- 复用现有 conftest / db fixture 风格（参考 tests/test_card_pool_bound.py）。

## 验证命令

```bash
# 单测
python3 -m pytest tests/test_card_pool_bound.py tests/test_valid_card_invariant.py -q
# 语法/导入自检
python3 -c "import src.web.app, src.models.card_pool"
# 数据修复（对主库执行一次）
python3 scripts/fix_valid_cards_status.py
# 修复后核对（应 invalid=0）
sqlite3 data/openrouter_auto.db "SELECT COALESCE(NULLIF(status,''),'(空)'),COUNT(*) FROM card_pool WHERE group_id=4 GROUP BY status;"
```

## Review Gate / Rollback
- 三处代码改动 + 一个脚本相互独立，可分别 revert。
- 数据修复方向安全（invalid→有效），无需逆向脚本；如需回退可依 valid_cards 重新推导。
