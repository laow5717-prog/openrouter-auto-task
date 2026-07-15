# 每日自动化任务编排(补绑+批量充值一键流水线)

## Goal

提供一个「一键跑今日任务」的入口,按固定规则串行完成两个阶段:
1. **绑卡阶段**:消耗 bind 分组的可用卡 —— 先登录已有未绑满 2 张卡的账号补绑,数据源还有剩余再注册新账号绑卡,直到 bind 分组无可用卡。
2. **充值阶段**:数据源见底后,逐个账号执行 AI Credits 充值(Top-up $10),配置了支付卡分组时额外处理 Unpaid invoices 在线支付。

用户价值:把现有分散的「注册/绑卡/充值」手动操作合并成每天点一次即可跑完的自动流水线。

## Background / Confirmed Facts(代码与数据核实)

### 数据源与分组
- 「绑卡数据源」= `card_groups.type='bind'` 分组下 `card_pool` 中 `status=''`(可用)的卡。当前唯一 bind 分组 id=2「7-13」,7 张可用。
- 「支付卡」= `card_groups.type='payment'` 分组的可用卡。当前唯一 payment 分组 id=3「7-13-在线支付」,1 张可用。
- 可用卡取数:`card_pool.get_usable_cards_as_list(group_id)`([card_pool.py:112](../../../src/models/card_pool.py#L112)),会先刷新并剔除过期卡。
- 已成功绑定过的卡靠 `card_bindings` 去重,不重复绑:`get_successfully_bound_card_numbers()`;Stripe 字段错误的卡也会被跳过。

### 账号与「绑几张卡」的权威来源
- 账号表 `accounts`,当前 24 个,全部有 `cf_password`。
- **关键**:`accounts.status`(如 `bound_2_cards`/`interrupted`/`registered`)与真实绑卡数不一致。真实成功绑卡数须以 `card_bindings` 中 `status='success'` 计数为准:`count_by_emails()`([card_binding.py:140](../../../src/models/card_binding.py#L140))。
- 当前真实绑卡分布:仅 4 个账号有成功绑定(2 个绑 1 张、2 个绑 2 张),其余 20 个账号 0 张。

### 现有可复用能力
- 注册新号+逐张绑卡编排:`AppState.run_card_driven_task()`([app.py:215](../../../src/web/app.py#L215)),循环「注册新账号→绑满 `max_bindable_cards` 张→剩余卡留给下一账号」,连续失败 3 次停止。**只注册新号,不登录已有账号补绑。**
- 单账号绑卡底层:`add_credit_card()`、`navigate_to_billing()`、`get_bound_card_count()`([driver.py](../../../src/browser/driver.py))。
- 登录已有账号:`login_cloudflare(driver, email, cf_password)`([driver.py:1364](../../../src/browser/driver.py#L1364))。
- 单账号充值:`registration.recharge_account()`([registration.py:292](../../../src/services/registration.py#L292))。是否处理 Unpaid invoices 由是否传 `payment_group_id`/`payment_cards` 决定;Top-up $10 用账号已绑的卡;`has_today_record()` 提供今日幂等;`recharge_log`/`valid_card`/`card_pool` 记账已内置。
- 全局单任务锁 `AppState.is_running` + 协作式停止 `stop_requested`/`force_stop()`([app.py:115](../../../src/web/app.py#L115)),同一时刻只能跑一个自动化任务 → 每日流水线必须串行。

### 缺口(本任务要新增)
1. 「登录已有未绑满账号→补绑」编排(现无)。
2. 「遍历账号逐个充值」批量编排(现有 recharge 仅单账号)。
3. 把绑卡阶段与充值阶段串成一键流水线的入口 + 前端按钮。

## Decisions(已与用户确认)
- 触发方式:**手动一键**(前端按钮/接口触发一次跑完,不引入常驻定时器)。
- 补绑策略:**先补绑已有账号,数据源仍有剩余再注册新号**。
- 阶段关系:**同一次运行内,先绑卡阶段跑到数据源见底,再进充值阶段,串行**。
- 补绑候选:**真实成功绑卡数<2、有 cf_password、status≠banned** 的全部账号(真实数以 `card_bindings` success 计数为准,不看 status 字符串)。
- 失败处理:**单账号失败记录后跳过继续,连续失败达阈值(3 次)才停整条流水线**(与现有 `run_card_driven_task` 一致)。
- 充值范围:**真实成功绑卡数≥1、今日未充值(`has_today_record`)** 的账号,逐个 Top-up $10;配置了 payment 分组则额外处理 Unpaid invoices。
- 参数来源:**前端一键面板填一次并记住上次**(captcha key、新号统一密码可选、bind 分组、payment 分组可选、每账号绑卡数默认 2);分组唯一时自动预选。

## Requirements

### R1 一键入口
- R1.1 新增后端接口 `POST /api/daily/start`,受全局 `is_running` 锁保护,起后台线程跑整条流水线;`POST` 复用现有 `force_stop` 停止。
- R1.2 前端新增「跑今日任务」面板:参数输入 + 启动/停止按钮,参数持久化(localStorage 或后端配置)。
- R1.3 面板参数:bind_group_id(必填,唯一时预选)、payment_group_id(可选)、cf_password(可选,新号统一密码)、max_bindable_cards(默认 2)、captcha_api_key(必填)。

### R2 绑卡阶段(串行两步,消耗同一 bind 分组可用卡池)
- R2.1 取 bind 分组可用卡 `get_usable_cards_as_list`,并按 `card_bindings` 已成功/ Stripe 错误卡号过滤(复用现有过滤逻辑)。
- R2.2 **补绑已有账号**:选出真实绑卡数<2、有 cf_password、status≠banned 的账号,按创建时间升序;逐个 `login_cloudflare` → `navigate_to_billing` → `add_credit_card` 补到 2 张;每绑一张从可用卡池扣减并写 `card_bindings`(success)、`valid_cards`(source_type=bind);账号补满或卡池空则进入下一个/结束。
- R2.3 **注册新号**:补绑做完后若卡池仍有剩余,复用现有 `run_card_driven_task` 的注册+绑卡循环消耗剩余卡,直到卡池空或连续失败达阈值。
- R2.4 登录失败/账号异常:记录并跳过,**不消耗**分配给它的卡(卡退回池给下一个账号/新号);计入连续失败计数。

### R3 充值阶段(绑卡阶段结束后)
- R3.1 选出真实绑卡数≥1 的账号,`has_today_record` 跳过今日已充值的,按创建时间升序。
- R3.2 逐个调用 `recharge_account` 做 Top-up $10;配置了 payment 分组时传 `payment_cards` 处理 Unpaid invoices,否则 `skip_invoice=True`。
- R3.3 记账复用现有 `recharge_log`/`valid_card`/`card_pool` 内置逻辑。

### R4 编排与可观测
- R4.1 整条流水线复用现有 `_hooked_print` 日志 + `/api/status` 轮询 + `_monitor` 截图;每阶段有清晰阶段标题日志。
- R4.2 协作式停止:各账号间检查 `stop_requested`,能在下个检查点安全退出并 `close_driver`。
- R4.3 结束输出汇总:补绑成功数、新号成功数、充值成功/失败/跳过数。

## Acceptance Criteria
- [ ] AC1:点击「跑今日任务」,bind 分组有可用卡时,先对真实绑卡<2 的已有账号补绑,数据源耗尽后再注册新号,全程日志可见阶段切换。
- [ ] AC2:补绑用真实 `card_bindings` success 计数判定「<2 张」,不被 `accounts.status` 字符串误导(如 status=interrupted 但真实 0 张的账号会被纳入)。
- [ ] AC3:banned 账号、无 cf_password 账号不进入补绑;补绑时登录失败的账号被跳过,其卡不被消耗。
- [ ] AC4:bind 分组可用卡耗尽后进入充值阶段;仅真实绑卡≥1 且今日未充值的账号被充值,今日已充值的被 `has_today_record` 跳过。
- [ ] AC5:配置 payment 分组时充值处理 Unpaid invoices,未配置时仅 Top-up($10)。
- [ ] AC6:任一账号失败流水线继续,连续失败达 3 次才停止;中途点停止能在下个账号检查点安全退出。
- [ ] AC7:结束时输出补绑/注册/充值的成功失败汇总;全程受 `is_running` 单任务锁保护,期间其他任务接口返回「有任务正在运行」。

## Out of Scope
- 真正的定时调度器(cron/APScheduler 常驻)。
- 修改现有单账号充值/单分组绑卡的既有 UI 入口。
