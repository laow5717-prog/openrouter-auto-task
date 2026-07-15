# Design — 每日自动化任务编排

## 架构总览

新增一个编排方法 `AppState.run_daily_pipeline()`,在单个后台线程内串行跑三段,全程持有 `is_running` 锁,复用现有日志/截图/停止机制。三段共享同一个 `card_bindings` 任务的 pending 卡池,消耗顺序:补绑已有账号 → 注册新号 → 充值。

```
POST /api/daily/start
      │ (threading.Thread, daemon)
      ▼
AppState.run_daily_pipeline(bind_group_id, payment_group_id, cf_password,
                            max_bindable_cards, captcha_api_key)
   │
   ├─ 阶段0 准备:取 bind 分组可用卡 → 过滤已绑/Stripe错卡 → create task + create_batch(pending)
   │
   ├─ 阶段1a 补绑已有账号  ── registration.bind_cards_to_existing_account()  【新增】
   │      候选:get_all() + count_by_emails() 筛 真实<2 / 有pw / status≠banned
   │      每账号:分配 pending 卡 → 登录 → 补到 max_bindable → mark_success/failed
   │
   ├─ 阶段1b 注册新号     ── registration.register_and_bind_cards()  【复用】
   │      仅当补绑后仍有 pending;沿用 run_card_driven_task 的循环骨架
   │
   └─ 阶段2 批量充值      ── _recharge_one_account()  【从 routes 抽取复用】
          候选:真实绑卡≥1 且 has_today_record()=False,逐个 Top-up $10
```

## 组件与契约

### 新增 1:`registration.bind_cards_to_existing_account(...)`
补绑单个已有账号。签名对齐现有 `register_and_bind_cards`,但**跳过注册,直接登录**。

```python
def bind_cards_to_existing_account(email, cf_password, card_binding_model, task_id,
                                   batch_records, max_bindable_cards=2,
                                   captcha_api_key=None, monitor_callback=None):
    """登录已有账号并补绑信用卡,补到账号总绑卡数达 max_bindable_cards。
    返回 (bound_count, login_ok)。"""
```
流程:
1. `create_driver(headless=False, profile_id=email)` → `login_cloudflare(driver, email, cf_password)`;失败返回 `(0, False)`(卡不消耗)。
2. `navigate_to_billing(driver)`;`get_bound_card_count(driver)` 读账号页面**真实已绑张数** `current`。
3. `need = max_bindable_cards - current`;`need<=0` 直接返回 `(0, True)`(账号已满,不消耗卡)。
4. 对 `batch_records` 前 `need` 张逐个 `add_credit_card`:成功 `mark_success(binding_id, email)` 且 `bound_count++`;失败 `mark_failed(binding_id, reason)`。每张之间 `navigate_to_billing` 刷新。
5. `finally: close_driver(driver)`。`InterruptedError` 冒泡由上层捕获。

关键:**用 `get_bound_card_count` 的页面真实值决定补几张**,避免 DB 与 CF 实际状态不一致导致超绑。

### 新增 2:`AppState.run_daily_pipeline(...)`(app.py)
编排主体。要点:
- 开头:`is_running=True; stop_requested=False; _patch_prints()`;`finally` 里 `is_running=False; clear_active_driver; _stop_screenshot_loop`(与现有 `run_card_driven_task` 一致)。
- 阶段0:`cards,_ = card_pool.get_usable_cards_as_list(bind_group_id)`;按 `get_successfully_bound_card_numbers()`+`get_stripe_field_error_card_numbers()` 过滤;`task=task.create('daily',...)`;`binding_ids=create_batch(task_id, filtered)`。
- 阶段1a:候选账号排序后遍历;每账号取 `get_pending(task_id)` 头部若干张作 `batch_records`,调补绑函数;`login_ok=False` → `consecutive_failures++` 且卡不消耗;成功绑 >0 → 计数、`consecutive_failures=0`。每账号间检查 `stop_requested` 与阈值。
- 阶段1b:`while get_pending(task_id)`:调 `register_and_bind_cards`,逻辑与 `run_card_driven_task` 主循环相同(可将该循环抽成 `_bind_loop_register(task_id,...)` 供两处复用,或内联)。
- 阶段2:见新增 3。
- 结束:记录 valid_cards(复用现有段)、导出报告、汇总日志。

### 新增 3:`_recharge_one_account(email, cf_password, payment_group_id)`(抽取)
把 [routes.py:495-566](../../../src/api/routes.py#L495) `_do_recharge` 内的**记账/匹配逻辑**抽成可复用函数(放 app.py 或 registration.py),返回 `(success, err)`。`routes.recharge_account` 与 `run_daily_pipeline` 阶段2 都调用它,避免重复。
- 内部:算 `skip_invoice=not payment_group_id`;`payment_cards=get_usable_cards_as_list(payment_group_id)`;`log_id=recharge_log.create(...)`;调 `registration.recharge_account(...)`;按现有逻辑匹配后四位、`mark_success/mark_failed`。

### 候选账号筛选(阶段1a)
```python
accts = account_model.get_all(order_desc=False)         # 按 id 升序=创建顺序
emails = [a['email'] for a in accts]
counts = card_binding_model.count_by_emails(emails)      # 真实成功绑卡数
candidates = [a for a in accts
              if a.get('cf_password')
              and (a.get('status') or '') != 'banned'
              and counts.get(a['email'], 0) < max_bindable_cards]
```
> `count_by_emails` 只回有绑定记录的 email;缺失即 0 张,天然纳入。

### 充值候选(阶段2)
```python
recharge_targets = [a for a in accts
                    if a.get('cf_password')
                    and counts_after.get(a['email'], 0) >= 1        # 阶段1后重新统计
                    and not recharge_log_model.has_today_record(a['email'])]
```
> 阶段1 会改变绑卡数,阶段2 前需**重新 `count_by_emails`**。

## 数据流与状态
- `card_bindings` 复用:pending→success/failed 天然承载「哪张卡绑到哪个账号」。补绑与注册共用同一 task_id 的池,消耗顺序由遍历先后决定。
- `accounts.status`:补绑成功后可更新为 `bound_{n}_cards`(与现有约定一致),但**判定逻辑不依赖它**。
- 卡池 `card_pool.status`:绑卡阶段不改(维持现有行为,仅充值阶段标 paid);有效卡记 `valid_cards`(source_type=bind/payment)。

## 兼容性 / 复用
- 不改现有 `run_card_driven_task` / `run_batch_task` / 单账号 recharge 路由的行为;新接口独立。
- 抽取 `_recharge_one_account` 时保持 `routes.recharge_account` 外部行为不变(纯重构)。
- 全部受 `is_running` 锁:daily pipeline 运行时,`/api/start`、`/api/card/start*`、`/api/accounts/recharge` 均返回「有任务正在运行」。

## 停止 / 回滚
- 协作式停止:每账号循环首检查 `stop_requested`;补绑/充值函数内 `monitor_callback` 命中即抛 `InterruptedError`,`finally` 各自 `close_driver`。
- 回滚点:新增均为独立函数/接口 + 一段抽取重构。回滚只需移除 `/api/daily/*` 路由、`run_daily_pipeline`、`bind_cards_to_existing_account`,并还原 `_do_recharge`。

## 风险
- R-a 补绑登录:老账号可能已被风控/需二次验证,`login_cloudflare` 失败率未知 → 跳过策略 + 连续失败阈值兜底。
- R-b `get_bound_card_count` 可靠性决定是否超绑 → 以它为准,读取失败时保守按「需补 max_bindable」但每绑一张后重新导航校验。
- R-c 前端参数持久化:localStorage 足够(captcha key 属敏感,存本地不上传第三方)。
