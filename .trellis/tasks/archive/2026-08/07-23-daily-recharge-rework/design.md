# 技术设计：卡池驱动的账号轮转充值

## 1. 总体控制流

单阶段循环，跑在 `AppState.run_daily_pipeline`（保留函数名以复用现有启动/停止/截图/日志机制，签名简化）：

```
入参：group_id, login_password(可选覆盖), captcha_api_key(可选)

准备：
  refresh_expired_status(group_id)          # 过期卡先落 expired（= 已消耗）
  accounts = 账号列表中 login_password 非空且 status != 'banned'，按 id 升序
  若 accounts 为空 → 报错退出（启动门已在路由拦截，这里兜底）

轮转循环（round = 1,2,...）：
  each round:
    unconsumed = 该分组内 status 为空的卡（未定案）
    若 unconsumed 为空 → 卡池耗尽，结束
    round_progressed = False
    for acct in accounts:               # WorkerPool.map 派发；并发下不同账号并行
        if stop_requested or 卡池已空: break
        对 acct 执行一次「访问」：recharge_account 用当前未消耗卡消耗到 1 张成功
        记录该账号本次是否有进展（成功付 1 张 / 消耗掉若干失效卡）
        有进展 → round_progressed = True
    若整轮无任何账号有进展 且 卡池仍非空 → 兜底结束（防死循环）

收尾：释放占用、汇总（paid N / invalid+expired M / 剩余未消耗 K）、is_running=False
```

**为什么「每账号每访问消耗到 1 张成功」而不是「每访问只试 1 张卡」**：`recharge_account` 每次调用都 `create_driver`（有头 Chrome，约 300-500MB / 数秒启动）。若每张卡都重启浏览器，成本不可接受。让一个账号登录一次后在同一会话里连续试卡直到成功一张（沿途把拒付卡标为 invalid 并逐卡记账），是登录成本与「成功即轮转」语义的最佳折中——这也正是 `recharge_account` 现有的内部行为，无需新写循环。

## 2. `registration.recharge_account` 契约变更

### 现状
```
全量: (ok, err, responses, card_last4, outcome)  outcome ∈ {topup, failed}
单步: (ok, err, responses, card_last4, outcome, info)
```
内部：过滤 3DS 冷却卡 → 逐卡 `recharge_via_stripe` → 成功标 paid+记有效卡+账号 recharged → 失败按 outcome 标记（invalid / 3DS 冷却 / captcha 停手）。

### 变更
1. **删除单步模式**：去掉 `invoice_daily_cap` / `single_step` 分支，统一返回全量五元组 `(ok, err, responses, card_last4, outcome)`。
2. **逐卡写 recharge_logs**：新增（复用已传入的 `recharge_log_model`）——每张卡尝试后立即写一条日志：
   - 成功：`create(email, 完整卡号, amount=20)` → `mark_success(log_id, {responses})`
   - 确认失效（outcome=failed 且明确拒付）：`create(...)` → `mark_failed(log_id, error=原因, {result})`
   - 3DS / captcha / 用户停止：**不写卡消耗类日志**（不算消耗）；captcha 直接停手返回。
   为避免与调用方重复记账，**日志记录集中在此**，`_recharge_one_account` 不再预建占位 log。
3. **卡状态语义**（成功卡可复用 + 24h 冷却，已确认）：
   - 成功 → `card_pool.mark_status_by_number(num, 'paid')`；**paid 不永久消耗**（paid ∉ NOT_SELECTABLE），好卡可反复复用支付。
   - 明确拒付：
     - 该卡**曾成功过**（`recharge_log.last_success_at(num) is not None`）→ `card_state.set_cooldown(num, 24h, '曾成功卡本次被拒，速率冷却')`，不判无效。
     - 该卡**从未成功过** → `mark_invalid_by_number(num)`（坏卡，永久剔除）。
   - 过期 → 由 `refresh_expired_status` 前置标记，不进入尝试。
   - 3DS → `card_state.set_cooldown`（24h 临时冷却，好卡拦下）；captcha → 不改状态、停手。
4. **并发下卡排他**：进入尝试某卡前 `payment_registry.try_acquire(num, email)`，成功后不再被其它 worker 选；finally `release`。防两账号并行付同一张卡。串行（max_workers=1）下退化为无害。
5. `amount` 由 10 改为 20（opencode 实际 $20 credits，与 docstring 一致）。

## 3. 卡「可选」集合与选卡顺序（`AppState._eligible_cards`）

成功卡可复用，故选卡集不再是「status 为空」，而是「可选卡」：`get_usable_cards_as_list`（已排除 expired/invalid/bound，**paid 仍在内**）再剔除处于 24h 冷却中的卡。顺序：**新卡优先，再复用好卡**。

`_eligible_cards(group_id)` 实现：
1. `usable, _ = card_pool.get_usable_cards_as_list(group_id)`（含 paid、unverified）
2. `cooldown_map = card_state.get_state_map()`（一次查全部冷却状态，避免 N+1），剔除 `in_cooldown` 的卡
3. `success_nums = recharge_log.all_success_card_numbers()`（一次查全部成功卡号集合）
4. 按是否在 `success_nums` 分成 fresh（新卡）/ good（好卡），返回 `fresh + good`

用途：账号取卡（传给 recharge_account）、轮次进展/终止判断、路由启动门、收尾汇总。取代原先误加的 `get_unconsumed_cards_as_list/count_unconsumed`（已删）。

新增支撑查询：`RechargeLogModel.all_success_card_numbers()` → 全局成功卡号集合；`CardPaymentStateModel.set_cooldown/in_cooldown`（复用 tds_until 列，3DS 与速率冷却共用；保留 set_tds/in_tds_cooldown 别名）。

## 4. `_recharge_one_account` 简化

保留（`/api/accounts/recharge` 依赖），但：
- 删除 `single_step` / `invoice_daily_cap` 参数与整段单步分支。
- 不再预建占位 recharge_log（记账下沉到 `recharge_account`）。
- 返回简化为 `(result, err)`，`result ∈ {success, failed}`。`invoice_only` 分支随 invoice 模型一并删除。
- 手动路由与轮转循环共用它：`payment_group_id` 参数名保留为「卡片来源分组」，值即选定的 `group_id`。

## 5. `run_daily_pipeline` 重写要点

- 删除：阶段0（卡池准备 / card_bindings 建批 / task 记录）、阶段1a（补绑）、阶段1b（注册）、阶段2 的 invoice 轮询分支与「无支付分组仅 topup」分支。
- 新签名：`run_daily_pipeline(self, group_id, login_password=None, captcha_api_key=None)`。
- **并发度固定为 1（串行，已确认）**：本任务只保证串行正确性。继续用 `WorkerPool`（`is_serial` 走同线程分支，保留截图/停止集成），但强制 `max_workers=1`；`payment_registry` 卡排他退化为无害安全网，并发路径延后到后续任务。
- 复用：`WorkerPool`（串行）、`AccountRegistry`（账号 profile 排他）、`PaymentCardRegistry`（卡排他安全网）、`_hooked_print`、截图/停止/计数收尾。
- 计数：`paid_total` / `invalid_total` / `fail_total`(不确定失败) / 每轮进展标志。
- 兜底：整轮零进展即结束；账号连续失败阈值仍可保留（可选）。
- 收尾：不再有 daily task 记录/报告导出/有效卡批量落库（那些绑卡语义已删）；有效卡记录已在 `recharge_account` 成功分支内逐卡完成。

## 6. 路由 `/api/daily/start`

新入参（`request.json`）：
```
group_id        必填，卡池分组 id
login_password  可选，覆盖账号自身密码（一般留空）
captcha_api_key 可选
```
校验：
- `is_running` 冲突 → 400
- `group_id` 缺失 / 分组不存在 → 400/404
- 分组内无未消耗卡 → 400「卡池无未消耗卡，无事可做」
- 无可充值账号（login_password 非空且非 banned）→ 400
启动 `state.run_daily_pipeline(group_id, login_password, captcha_api_key)`。
返回 `{status:'started', usable_cards: 未消耗卡数, accounts: 可用账号数}`。

## 7. 前端 `Workbench.vue`

- 删除 `MODES` 模式标签、`dailyMode`、`dailyBindGroupId`；双分组选择器合并为**单个卡池分组选择器**（沿用 payment 分组列表接口，或直接列全部分组）。
- `handleStart` 请求体：`{ group_id: settings.dailyGroupId, captcha_api_key?, login_password? }`。
- 校验：未选分组 → 提示。
- `stores/settings.js`：`dailyMode`/`dailyBindGroupId`/`dailyPaymentGroupId` → 收敛为 `dailyGroupId`（保留旧键读取容错或直接迁移）。

## 8. 配置清理

- `src/config.py`：删除 `INVOICE_DAILY_CAP`（确认无其它引用）。
- `src/web/app.py`：移除 `from src.config import ... INVOICE_DAILY_CAP`。

## 9. 兼容性与回滚

- **兼容**：`/api/accounts/recharge`（手动单账号）继续用简化后的 `_recharge_one_account`，契约对齐；`/api/start`+`run_batch_task`+注册存根不动。
- **数据**：不改表结构。`card_pool.status` / `recharge_logs` 沿用现有列。旧 `daily` task 记录不再新增，历史记录只读保留。
- **回滚**：改动集中在 `app.py`(run_daily_pipeline/_recharge_one_account)、`registration.py`(recharge_account)、`routes.py`(daily/start)、`Workbench.vue`、`config.py`。git revert 即可整体回退；无迁移脚本。

## 10. 风险

- **并发下卡/账号排他**：依赖 payment_registry 的 try_acquire；若遗漏会出现两账号付同卡。串行模式（默认 max_workers=1）无此风险，先保证串行正确，再验证并发。
- **卡池极大 + 坏卡多**：`OPENCODE_RECHARGE_MAX_ATTEMPTS`(默认 8) 仍限制单账号单次访问最多试卡数，防 velocity 风控；轮转由外层多轮覆盖剩余卡。
- **无进展死循环**：整轮零进展兜底结束；captcha 停手不算进展但会拦住该账号，需人工过验证码后再启动。
