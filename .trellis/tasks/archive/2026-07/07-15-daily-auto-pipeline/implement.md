# Implement — 每日自动化任务编排

## 前置
- 后端改动无需迁移(不新增表/列);`task.type='daily'` 直接写入现有 `tasks` 表。
- 前端改动后须 `cd frontend && npm run build`(产物进 `static/`)再提交。

## 执行清单(按依赖顺序)

### Step 1 — 抽取充值复用函数(纯重构,先做以隔离风险)
- [ ] 1.1 在 [src/api/routes.py](../../../src/api/routes.py) 把 `_do_recharge` 内的记账/匹配逻辑抽成模块级 `_recharge_one_account(state, models, email, cf_password, payment_group_id) -> (success, err)`;`recharge_account` 路由改为调用它。
- [ ] 1.2 验证:重构前后 `POST /api/accounts/recharge` 行为不变(单账号充值路径不回归)。
- 回滚点:此步独立,可单独 revert。

### Step 2 — 补绑单账号函数
- [ ] 2.1 在 [src/services/registration.py](../../../src/services/registration.py) 新增 `bind_cards_to_existing_account(email, cf_password, card_binding_model, task_id, batch_records, max_bindable_cards=2, captcha_api_key=None, monitor_callback=None) -> (bound_count, login_ok)`。
- [ ] 2.2 实现:`create_driver(profile_id=email)` → `login_cloudflare` → `navigate_to_billing` → `get_bound_card_count` 得 `current` → `need=max_bindable-current` → 逐张 `add_credit_card` + `mark_success/mark_failed`;`finally close_driver`;`InterruptedError` 向上抛。
- [ ] 2.3 登录失败返回 `(0, False)`;账号已满返回 `(0, True)`;绑卡后 `account_model.update_status(email, f"bound_{n}_cards")`(需把 account_model 传入或复用 models)。

### Step 3 — 编排主体
- [ ] 3.1 在 [src/web/app.py](../../../src/web/app.py) `AppState` 新增 `run_daily_pipeline(bind_group_id, payment_group_id, cf_password, max_bindable_cards, captcha_api_key)`。
- [ ] 3.2 阶段0:取可用卡 + 过滤 + `task.create('daily')` + `create_batch`;无可用卡则直接跳到阶段2(仍可能有账号要充值)。
- [ ] 3.3 阶段1a 补绑循环:候选账号筛选(见 design)→ 遍历,每账号取 pending 头部 `need` 张 → 调 Step 2 函数 → 连续失败阈值(3)与 `stop_requested` 检查。
- [ ] 3.4 阶段1b 注册循环:`while get_pending`:调 `register_and_bind_cards`(骨架同 `run_card_driven_task` 主循环);可抽 `_register_bind_loop(task_id, ...)` 供复用。
- [ ] 3.5 阶段2 充值循环:阶段1后**重新 `count_by_emails`** → 筛 真实≥1 且 `has_today_record`=False → 逐个调 Step 1 的 `_recharge_one_account`;单账号失败跳过,连续失败阈值停。
- [ ] 3.6 结束:valid_cards 记录(复用现有段)、报告导出、汇总日志(补绑/注册/充值 成功失败数)。
- [ ] 3.7 `finally` 与现有一致:`is_running=False`、清 driver、停截图、`task.finish`。

### Step 4 — 接口
- [ ] 4.1 在 [src/api/routes.py](../../../src/api/routes.py) 新增 `POST /api/daily/start`:校验 `is_running`、取参、校验 bind 分组存在且有可用卡或有可充账号、`threading.Thread(target=state.run_daily_pipeline,...)`。
- [ ] 4.2 停止复用现有 `POST /api/stop`(`force_stop`);无需新增。

### Step 5 — 前端面板
- [ ] 5.1 在 frontend 新增「跑今日任务」入口(参照现有卡驱动/充值面板组件结构):参数 bind 分组下拉、payment 分组下拉(可选)、cf_password、max_bindable、captcha key。
- [ ] 5.2 参数存 localStorage,下次自动带出;分组下拉唯一项时默认选中。
- [ ] 5.3 启动调 `/api/daily/start`,状态/日志复用现有 `/api/status` 轮询与视频流。
- [ ] 5.4 `cd frontend && npm run build`。

## 验证命令
```bash
# 语法/导入检查
.venv/bin/python3 -c "import src.web.app, src.services.registration, src.api.routes; print('import ok')"

# 候选账号筛选逻辑单点验证(临时脚本,跑完删)
.venv/bin/python3 - <<'PY'
# 连 data/cloudflare_auto.db,打印 真实<2 且非banned 的补绑候选 与 真实>=1 的充值候选
PY

# 前端构建
cd frontend && npm run build
```

## 端到端验收(须用户实跑,不可仅静态判断)
- [ ] E1 bind 分组有可用卡 + 存在真实<2 账号:点击运行,观察日志阶段切换(补绑→注册→充值),浏览器画面正常。
- [ ] E2 中途点停止:在下一个账号检查点安全退出,浏览器关闭,`is_running` 复位。
- [ ] E3 payment 分组配置/不配置两种路径:充值分别走「处理欠费发票」与「仅 Top-up」。

## 风险文件 / 回滚
- 风险:[src/api/routes.py](../../../src/api/routes.py) `_do_recharge` 抽取若破坏原路由 → Step 1 单独验证后再继续。
- 风险:[src/web/app.py](../../../src/web/app.py) `AppState` 是全局单例,`run_daily_pipeline` 的 `finally` 必须完整复位锁,否则卡死后续任务。
- 回滚:移除 `/api/daily/*`、`run_daily_pipeline`、`bind_cards_to_existing_account`,还原 `_do_recharge` 即可,无数据结构变更。
```
