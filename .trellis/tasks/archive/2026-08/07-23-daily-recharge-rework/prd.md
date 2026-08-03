# PRD：每日任务重构——卡池驱动的账号轮转充值

## 背景

现有「每日一键流水线」(`run_daily_pipeline`) 分三阶段：阶段1a 补绑已有账号 → 阶段1b 注册新号 → 阶段2 轮询式充值。其中：

- **阶段 1a/1b 是死代码**：依赖 `registration.bind_cards_to_existing_account` 与 `register_and_bind_cards`，二者均为 `NotImplementedError` 存根（Cloudflare→opencode 改造遗留）。
- **阶段 2 背着 invoice 遗留脚手架**：`single_step` / `INVOICE_DAILY_CAP` / 轮询补生成账单，这套是 Cloudflare 账单模型；opencode 实际充值是 Stripe Checkout 一次性 $20，没有 invoice，机器在空转。
- **唯一真正可用的是 `recharge_account`**：登录 opencode → 取卡池卡 → Stripe Checkout 付款 → 判定并标记卡状态。

## 目标

把每日任务重做为**单阶段、卡池驱动、账号轮转**的充值消耗流程：

> 选定一个卡池分组，用账号列表逐个账号充值；一个账号充值成功后换下一个账号；如此循环轮转，直到把该分组的卡池数据全部消耗完。消耗过程中逐卡记录卡片状态与具体原因，并记录充值记录。

## 核心概念

### 卡「可选/消耗」的定义（已确认，含成功卡复用 + 24h 冷却）
- **成功**（Stripe 付款成功）→ 卡标记 `paid`，**不永久消耗**：好卡可反复被选中给不同账号支付。
- **好卡再被拒**（曾成功过的卡，本次又被拒）→ 视为风控速率限制，进入 **24 小时冷却**（不判无效），到期自动恢复可选。
- **坏卡首次被拒**（从未成功过的卡首次被拒）→ 判 `invalid`，永久剔除，记录原因。
- **过期** → `expired`，永久剔除。
- **3DS** → 24h 冷却（好卡临时拦下）；**hCaptcha / 用户停止** → 不改卡状态，本轮停手，后续可再试。

「可选卡」= 分组内非 `invalid`/`expired`/`bound` 且不处于 24h 冷却中的卡。**选卡顺序：新卡（从未成功过）优先，用尽后再复用好卡**。任务结束 = 当前无任何可选卡（全部无效/过期或在冷却中）。

### 账号轮转（已确认：循环轮转直到无可选卡）
- 账号来源：账号列表中 `login_password` 非空、`status != 'banned'` 的账号，按 id 升序。
- 一个账号完成一次「访问」（在其会话内充成 1 张卡，或该账号本轮无法推进）后，轮转到下一个账号。
- 所有账号轮过一遍后，若仍有可选卡，回到第一个账号继续下一轮。
- 直到：无可选卡 / 用户停止 / 整轮零进展兜底。进展 = 本轮有成功付款，或可选卡数减少（坏卡判无效 / 好卡进冷却 / 过期）。

### 逐卡记录（已确认）
- 每张卡的每次充值尝试都写一条 `recharge_logs`（email、完整卡号、金额、status=success/failed、error=具体原因）。
- 卡片自身状态写回 `card_pool`（paid / invalid / expired）。

## 范围

### 纳入
1. 重写 `AppState.run_daily_pipeline`：删除阶段 0/1a/1b 与 invoice 轮询机器，重建为卡池驱动的账号轮转充值循环。
2. 调整 `registration.recharge_account`：逐卡写 `recharge_logs`；简化契约（去掉 `single_step`/`invoice_daily_cap`）；卡消耗语义（成功=paid，确认失效=invalid/expired）。
3. 修复 `paid` 卡仍被选中的问题：账号轮转循环只取「未消耗卡」。
4. `/api/daily/start` 路由：入参简化为单个 `group_id`（+ 可选 login_password 覆盖、captcha_api_key），去掉 `mode`/`bind_group_id`/`payment_group_id` 三分裂。
5. 前端 `Workbench.vue`：模式标签 + 双分组选择器 → 单个卡池分组选择器；请求体改为 `{ group_id }`。
6. 清理 `INVOICE_DAILY_CAP` 及 `single_step` 相关死代码/配置。

### 不纳入（保持现状）
- `/api/start` → `run_batch_task` → `register_one_account`（注册存根，非「每日任务」，不动）。
- `/api/accounts/recharge` → `_recharge_one_account` 的手动单账号充值路径必须保持可用（可简化实现但契约兼容）。
- opencode_billing 浏览器层 `recharge_via_stripe` 行为不变。
- 并发 worker 框架（WorkerPool/AccountRegistry）保留复用。

## 验收标准

1. 选定一个卡池分组并启动，系统逐账号轮转，在 opencode 上用卡池的卡逐张充值。
2. 充值成功的卡在卡池标记 `paid` 且不再被后续轮次选中；被拒卡标 `invalid`、过期卡标 `expired`，均带原因。
3. `recharge_logs` 出现逐卡记录，成功/失败及原因可在充值记录页查看。
4. 卡池内未消耗卡全部定案后，任务自然结束并给出汇总（成功 N / 失效 M / 剩余不可推进 K）。
5. 用户可随时停止；停止后未定案卡保持未消耗，可再次启动继续。
6. 手动单账号充值 `/api/accounts/recharge` 仍可正常工作。
7. 去掉 mode/双分组后，前端仅需选一个分组即可启动，无回归报错。
