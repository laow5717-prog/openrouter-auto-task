# 执行计划 — 每日流水线 top-up 补生成账单（轮询式）

## 顺序清单

### 步骤 1：config 常量
- [ ] `src/config.py` 增 `INVOICE_DAILY_CAP = 30`、`TOPUP_AMOUNT = 10`（模块级或现有 config 类合适位置）。
- 验证：`python3 -c "import src.config"` 无报错，能引用到常量。
- 回滚点：删除两个常量。

### 步骤 2：driver `fetch_today_invoice_count`
- [ ] 在 `src/browser/driver.py` 新增 `fetch_today_invoice_count(driver, account_id) -> int | None`。
  - `page.evaluate` 内 `fetch(url, {credentials:'include'})` 拉 invoice-history，含分页循环。
  - 按**本地当日**边界过滤 `created`；invoices 倒序，翻到某页最早一条 < 今日0点即停。
  - 异常/非 success → 返回 None，打印一行日志。
- 验证：语法 `python3 -m py_compile src/browser/driver.py`；逻辑待实跑（见「验证门」）。
- 回滚点：删除该函数。

### 步骤 3：`handle_unpaid_invoices` 增 `max_invoices`
- [ ] 增可选参 `max_invoices=None`；累计成功付掉（`on_paid` 触发处）计数，达上限跳出主循环。
  - 只在**成功**付掉时计数；`_give_up` 放弃不计。
  - 默认 None 时行为与现状完全一致（不加任何限制分支路径）。
- 验证：`py_compile`；确认默认路径无行为变化（对照 diff，仅新增计数与一个 `if max_invoices and paid>=max_invoices: break`）。
- 回滚点：移除参数与计数分支。

### 步骤 4：`recharge_account` 单步模式
- [ ] 增参 `invoice_daily_cap=None`；返回改 6 元组 `(success, err, responses, card_last4, outcome, info)`。
  - 全量模式（`invoice_daily_cap is None`）：`info={}`，其余逻辑零改动，仅在所有 `return` 补 `{}`。
  - 单步模式：登录后先 `fetch_today_invoice_count`：
    - 达上限 → `return True, "当日账单已达上限", [], '', "cap_reached", {'today_count': n}`。
    - 未达 → 1 次 Top-up + `handle_unpaid_invoices(max_invoices=1)`；结束再读一次 today_count；
      `return ..., "stepped", {'today_count': after, 'generated': after>before, 'paid': <int>, 'topup_ok': <bool>}`。
  - `paid` 数：让 `handle_unpaid_invoices` 返回值里数 `status=='paid'` 的条数，或在 `_on_invoice_paid` 里累加计数器。
- 验证：`py_compile`；确认唯一调用点已同步改 6 元组解包。
- 回滚点：移除参数、还原 5 元组、去掉 cap 分支。

### 步骤 5：`_recharge_one_account` 透传
- [ ] 签名加 `single_step=False, invoice_daily_cap=None`；统一返回 `(result, err, info)`。
  - `single_step=False`：现状逻辑；`info={}`（旧调用点 routes `_do_recharge` 只用副作用/前两元素，需同步）。
  - `single_step=True`：透传 cap，按 6 元组接收，映射 outcome→result（`cap_reached`/`stepped`/`failed`），返回 info。
  - `outcome=="stepped"` 的记账：driver 内 `_on_invoice_paid` 已记 `recharge_logs`；这里对**预建占位 log**
    的处理要对齐——单步模式不应像全量那样把占位 log 记成一次 $10 成功（除非本次 Top-up 真成功）。
    按 `topup_ok` 决定 `mark_success/mark_failed/delete`（复用现有 `topup/failed/invoice_only` 三分支思路）。
- 验证：`py_compile`；routes `/api/recharge` 单账号路径回归（返回值解包不报错）。
- 回滚点：还原签名与返回。

### 步骤 6：阶段2 轮询主循环
- [ ] 用 design §4 的轮询循环替换 `run_daily_pipeline` 中 `for acct in recharge_targets` 段。
  - 支付卡分组无可用卡 → 打印说明并跳过整个轮询（`skip_invoice` 场景无付款意义）。
  - `done` / `no_progress` / `MAX_NOPROG=3` / `MAX_ROUNDS=INVOICE_DAILY_CAP+2` / 全轮无进展 break。
  - 每轮/每账号 `self.stop_requested` 检查；`current_action` 更新含轮次与账号进度。
  - 末尾汇总新增 `topup_generated_total` / `invoice_paid_total`（可选）。
- 验证：`py_compile`；`stop` 中途能退出（`should_stop` 已贯穿 driver）。
- 回滚点：还原为原顺序遍历循环。

### 步骤 7：前端/无
- 无 UI 改动（阶段2 为后台）。若末尾汇总文案变化，仅后端 `current_action` 字符串，无需 rebuild。
  （如需在 Dashboard 展示轮次进度，另议，不在本次范围。）

## 验证门（必须实跑，标注人工）
1. **[人工]** 单账号：某已绑卡账号跑单账号「充值」按钮，确认行为与改动前一致（回归）。
2. **[人工]** 每日流水线小样：2~3 个绑卡账号 + 支付卡分组，观察日志出现轮询（账号交替、每账号每轮 1 生成 1 付），
   且某账号当日累计达 30 后不再被访问。
3. **[人工]** 中途点「停止」，确认能干净退出充值阶段。
4. **[人工]** 支付卡分组置空/无可用卡，确认跳过轮询且不报错。
5. 计数正确性：对照 CF `invoice-history` 页面，确认 `fetch_today_invoice_count` 返回值与当日实际条数一致
   （含跨页场景）。

## 质量检查命令
- `python3 -m py_compile src/config.py src/browser/driver.py src/services/registration.py src/web/app.py`
- 全量语法/导入：`.venv/bin/python3 -c "import src.web.app"`（按项目实际入口调整）。

## 提交
- 全部验证门通过后，按项目约定直接在 `main` 提交（分块提交：config/driver 一提，registration/app 一提）。
