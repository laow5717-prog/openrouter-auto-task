# Implement — 每日订阅编排

## 前置
- 父任务 hCaptcha 攻克已完成并提交（f241661）。原生栈 create_driver_vanilla、subscribe_via_stripe、
  signup_one、login_and_open_own_go、_eligible_cards、account_registry 均已就绪。

## 执行清单（有序）

### 阶段 A：编排核心（src/web/app.py）
- [ ] A1. 加 `_hotmail_by_email(email)`：惰性缓存 read_hotmail_accounts(_DEFAULT_XLSX) → dict，按 email 取 HotmailAccount。
- [ ] A2. 加 `_subscribe_one_account(acct, payment_group_id, captcha_key, worker)`：
      注册分支(signup_one semi_auto=False, Arkose→skip) + 订阅分支(create_driver_vanilla 逐卡试付)。
      返回 ("subscribed"|"registered_only"|"skipped"|"failed", detail)；逐卡消耗镜像 recharge_account。
- [ ] A3. 加 `run_daily_subscribe_pipeline(group_id, captcha_key)`：镜像 run_daily_pipeline 骨架，
      待订阅账号集 = status∉(subscribed,banned)；轮转 + 停止条件（无卡/无待订阅/停止/零进展）+ 收尾。
- [ ] A4. 复用 worker/截图/停止钩子（set_action/_hooked_print/account_registry.claim/release）。

### 阶段 B：API（src/api/routes.py）
- [ ] B1. 加 `POST /api/tasks/daily-subscribe`：校验 group_id 存在、可选卡>0、待订阅账号>0；
      起 daemon 线程跑 run_daily_subscribe_pipeline；返回 {status, usable_cards, accounts, group_name}。
- [ ] B2. 复用现有停止端点（is_running/stop_requested 全局单闸门；订阅与充值互斥）。

### 阶段 C：前端（frontend/src/views/Workbench.vue 或每日任务面板）
- [ ] C1. 加「每日订阅任务」按钮，复用 captcha key / group 选择 / 运行状态 / 日志 / 停止 UI。
- [ ] C2. 调 /api/tasks/daily-subscribe；运行态与现有每日任务共用（互斥）。
- [ ] C3. 前端构建产物（npm build）如需同步 static/。

### 阶段 D：验证（不依赖真实扣款）
- [ ] D1. 单账号函数级 dry 验证：对 leilao40（已 registered）跑 _subscribe_one_account，
      确认走原生栈登录→逐卡→拒付轮转→返回 registered_only（卡源问题下的预期）。
- [ ] D2. 未注册账号：对一个 imported 账号跑，确认触发 signup_one；若弹 Arkose 确认 skip 且转下一个。
- [ ] D3. pipeline 级：Web 触发订阅每日任务，确认账号轮转、subscribed 剔除、停止条件收敛、日志清晰。
- [ ] D4. 回归：现有每日充值任务照跑不受影响。

## 验证命令
- 语法/导入：`python3 -c "import ast; ast.parse(open('src/web/app.py').read())"` + 导入 AppState。
- 单账号：临时脚本或 python -c 调 `_subscribe_one_account`（group=1, leilao40）。
- Web：起服务 → 点「每日订阅任务」→ 观察日志。

## 回滚点
- 每阶段独立可回滚；A/B/C additive，删新增即恢复。任何阶段异常先停 Web 任务（stop_requested）。

## 审查门
- A 完成后 review 状态机（返回码 → status 落库、逐卡消耗规则与 recharge 一致）。
- 合入前确认订阅任务与充值任务互斥闸门有效、不并跑。
