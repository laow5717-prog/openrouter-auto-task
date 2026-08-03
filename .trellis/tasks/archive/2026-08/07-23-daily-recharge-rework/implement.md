# 执行计划：卡池驱动的账号轮转充值

按依赖顺序实施；每步后做局部验证（import / 语法 / 前端 build）。核心逻辑改动集中在后端，前端为配套简化。

## 步骤

### S1. `CardPoolModel` 增加未消耗卡查询
- 文件：`src/models/card_pool.py`
- 加 `get_unconsumed_cards_as_list(group_id)`：`refresh_expired_status` 后返回 `status` 为空（`COALESCE(status,'')==''`）的卡（dict 格式，同 `get_cards_as_list`）。
- 加 `count_unconsumed(group_id)`（可选，供路由启动门与收尾汇总）。
- 验证：`python3 -c "import src.models.card_pool"`。

### S2. `recharge_account` 契约简化 + 逐卡记账
- 文件：`src/services/registration.py`
- 删除 `single_step` / `invoice_daily_cap` 分支与 `_ret` 的六元组路径，统一返回 `(ok, err, responses, card_last4, outcome)`，`outcome ∈ {topup, failed}`。
- 逐卡尝试处接入 `recharge_log_model`：
  - 成功：`create(email, card['number'], amount=20)` → `mark_success(log_id, {responses:[result]})`。
  - 明确拒付（outcome=failed）：`create(...)` → `mark_failed(log_id, error=reason, {result})`，并 `mark_invalid_by_number`。
  - 3DS：记 `card_state.set_tds`，**不写日志、不消耗**。
  - captcha：**不写日志、不消耗**，`monitor` 提示后 return failed。
- 卡排他：试卡前 `payment_registry.try_acquire(num, email)`（None 时跳过占用），finally `release`；被别的 worker 占用则跳过该卡。
- `amount` 10 → 20。
- 验证：`python3 -c "import src.services.registration"`。

### S3. `_recharge_one_account` 简化
- 文件：`src/web/app.py`
- 删除 `single_step`/`invoice_daily_cap` 形参与整段单步分支、`invoice_only` 分支、预建占位 log 与 `_match_full_card`（记账已下沉；卡号在 recharge_account 内已知完整值）。
- 返回 `(result, err)`，`result ∈ {success, failed}`。
- 传参：`payment_registry=self.payment_registry`、`recharge_log_model=self.models['recharge_log']`。
- 验证：`/api/accounts/recharge` 调用点（routes.py:379）签名兼容。

### S4. 重写 `run_daily_pipeline`
- 文件：`src/web/app.py`
- 新签名 `run_daily_pipeline(self, group_id, login_password=None, captcha_api_key=None)`。
- 删除阶段 0/1a/1b、invoice 轮询、`_register_bind_loop` 调用、daily task 记录/报告导出/有效卡批量落库、`INVOICE_DAILY_CAP` 依赖。
- 实现设计文档 §1 的轮转循环：
  - 每轮取 `get_unconsumed_cards_as_list(group_id)`，空则结束。
  - `WorkerPool.map(accounts, _recharge_one)`，`_recharge_one` 内 `account_registry.claim` → `_recharge_one_account(email, pw, group_id)` → 记进展。
  - 整轮零进展兜底结束；`stop_requested` 响应。
  - 计数汇总写入 `current_action`。
- 保留 finally 收尾（worker 释放、registry release_all、is_running=False）。
- 验证：`python3 -c "import src.web.app"`。

### S5. 路由 `/api/daily/start` 简化
- 文件：`src/api/routes.py`
- 入参改为 `group_id` + 可选 `login_password`/`captcha_api_key`；删除 `mode`/`bind_group_id`/`payment_group_id` 逻辑。
- 校验：分组存在、有未消耗卡、有可用账号。
- `threading.Thread(target=state.run_daily_pipeline, args=(group_id, login_password, captcha_api_key))`。
- 返回 `{status, usable_cards, accounts}`。
- 验证：`python3 -c "import src.api.routes"`。

### S6. 配置与死代码清理
- `src/config.py`：删除 `INVOICE_DAILY_CAP`（先 `grep -rn INVOICE_DAILY_CAP src` 确认仅剩 app.py 引用）。
- `src/web/app.py`：移除该 import；确认无残留引用。
- 可选：若 `_register_bind_loop`/`run_batch_task` 已完全无用，仅在本任务范围外，**不删**（属注册路径，保持现状）。

### S7. 前端 `Workbench.vue` + settings
- 文件：`frontend/src/views/Workbench.vue`、`frontend/src/stores/settings.js`
- 删除模式标签与双分组选择器，改为单个卡池分组下拉（`settings.dailyGroupId`）。
- `handleStart` body → `{ group_id, captcha_api_key?, login_password? }`；未选分组则提示。
- settings：新增 `dailyGroupId`，移除/迁移 `dailyMode`/`dailyBindGroupId`/`dailyPaymentGroupId`。
- 验证：`cd frontend && npm run build`，产物落 `static/`（现有构建流程）。

### S8. 端到端自检（不实际扣款）
- 启动 server，选一个分组点开始：
  - 无账号 / 无未消耗卡 → 正确报错。
  - 有账号 + 未消耗卡 → 进入轮转，日志出现逐账号访问与逐卡记账（可在 opencode 未真正登录时观察到 login 失败分支不崩、卡不被误标）。
- 校对 `recharge_logs` 与 `card_pool.status` 写入符合语义。

## 验证命令

```bash
# 后端各模块可导入
python3 -c "import src.models.card_pool, src.services.registration, src.web.app, src.api.routes"
# 死代码确认
grep -rn "INVOICE_DAILY_CAP\|single_step\|invoice_daily_cap" src
# 前端构建
cd frontend && npm run build
```

## 审查关卡（review gates）

- G1（S2 后）：recharge_account 逐卡记账不重复、captcha/3DS 不误消耗。
- G2（S4 后）：轮转终止条件正确——卡池耗尽 / 零进展 / stop 三者都能收敛，不死循环。
- G3（S5/S7 后）：前后端入参对齐（`group_id` 单键），手动单账号充值未回归。

## 回滚点

- 每步独立小改；任一步骤 import 失败即停下修复。
- 整体回滚：`git checkout -- src/web/app.py src/services/registration.py src/api/routes.py src/config.py frontend/src/views/Workbench.vue frontend/src/stores/settings.js`。
