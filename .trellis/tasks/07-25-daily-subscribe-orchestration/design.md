# Design — 每日订阅编排

## 复用 vs 新增（对照现有 recharge 每日任务）
| 关注点 | 现有 recharge | 订阅编排 |
|---|---|---|
| 后台入口 | `AppState.run_daily_pipeline(group_id,login_pw,captcha_key)` | **新增** `run_daily_subscribe_pipeline(group_id,captcha_key)` |
| 单账号动作 | `_recharge_one_account(email,pw,payment_group_id,worker)` | **新增** `_subscribe_one_account(acct,payment_group_id,captcha_key,worker)` |
| 账号轮转/停止/WorkerPool | run_daily_pipeline 内 | ✅ 同构复用（串行、account_registry、_eligible_cards、零进展兜底） |
| 卡资格/记账/卡状态机 | `_eligible_cards` + recharge_account 内部 | ✅ 复用 `_eligible_cards`；订阅逐卡试付 + 记账见下 |
| 注册 | 无 | **新增**：signup_one(account,semi_auto=False) 跳 Arkose |
| 付款浏览器栈 | Patchright | **原生 Playwright**（create_driver_vanilla） |
| API/前端 | `/api/tasks/daily` + 每日任务按钮 | **新增** `/api/tasks/daily-subscribe` + 前端订阅每日任务按钮 |

## 核心新增

### 1. `_subscribe_one_account(acct, payment_group_id, captcha_key, worker)` （src/web/app.py）
单账号一次推进，跑在 worker 线程。返回 `("subscribed"|"registered_only"|"skipped"|"failed", detail)`。
```
email = acct['email']; status = acct.get('status')
# A. 注册分支：未注册先注册（Patchright）
if status not in ('registered','subscribed'):
    hacc = _hotmail_by_email(email)              # 从 hotmail.xlsx 匹配 link
    if not hacc: return ("skipped","无 hotmail 数据")
    r = signup_one(headless=False, semi_auto=False, account=hacc, then_opencode=False)
    oc = r['outcome']
    if oc == 'reached_captcha': update_status(email,'pending'); return ("skipped","Arkose 跳过")
    if oc == 'account_suspended': upsert(...suspended); return ("skipped","挂起")
    if oc != 'signup_complete': update_status(email,'failed'); return ("failed",oc)
    upsert(email, login_password=r['github_password'], email_password=hacc.password, status='registered')
    # signup_one 用 create_driver（Patchright），返回时其 session 已关；下面另起原生栈
# B. 订阅分支（原生栈）：逐卡试付
session = create_driver_vanilla(profile_id=email)
try:
    if captcha_key: captcha.init_solver(captcha_key); captcha.install_hcaptcha_hook(session)
    lg = login_and_open_own_go(session)
    if not lg['ok']: return ("failed","登录失败:"+lg['detail'])
    wid = lg['wid']
    for card in _eligible_cards(payment_group_id):   # 账号内逐卡，成功即止
        if self.stop_requested or worker...: break
        log_id = recharge_log.create(email, card['number'], amount=5)
        res = subscribe_via_stripe(session, card, wid, should_stop=..., dry=False)
        if res['outcome']=='success':
            card_pool.mark_status_by_number(number,'paid'); valid_card.record(...)
            account.update_status(email,'subscribed'); recharge_log.mark_success(...)
            return ("subscribed", f"****{last4}")
        elif res['outcome']=='failed':
            card_pool.mark_invalid_by_number(number); recharge_log.mark_failed(...)  # 换下一张
        elif res['outcome']=='needs_captcha':
            recharge_log.mark_failed(...); return ("failed","hCaptcha 未过")  # 该账号本轮止
        else: recharge_log.mark_failed(...)  # error/unknown 不耗卡，换下一张
    return ("registered_only","账号内可选卡试尽未成功")
finally:
    close_driver(session)
```
注：逐卡消耗规则镜像 `services.registration.recharge_account`（成功标 paid、拒付标 invalid、
error/unknown 不耗卡）。account_registry.claim(email) 排他在外层轮转里做。

### 2. `run_daily_subscribe_pipeline(group_id, captcha_key)` （src/web/app.py）
镜像 run_daily_pipeline 的账号轮转骨架，但**待订阅账号集**和**单账号动作**不同：
```
accounts = [a for a in account_model.get_all(order_desc=False)
            if (a.get('status') or '') not in ('subscribed','banned')]
while not stop and eligible_cards>0 and 有"本轮未定案"账号:
    round: pool.map(accounts_needing, _subscribe_one_account)
    # 账号一旦 subscribed 从后续轮次剔除；一轮零进展（无 subscribed 且卡数没减）兜底结束
```
停止：`_eligible_cards(group_id)` 为 0 / 待订阅账号集空 / stop_requested / 零进展。

### 3. `_hotmail_by_email(email)` 辅助（src/web/app.py 或 services）
`read_hotmail_accounts(hotmail.xlsx)` 缓存成 dict{email→HotmailAccount}，供注册取 ruoanzhu link。
xlsx 路径同 run_hotmail_github_signup.py 的 `_DEFAULT_XLSX`。

### 4. Web 接线
- API：`src/api/routes.py` 新增 `POST /api/tasks/daily-subscribe`，body `{group_id, captcha_api_key}`，
  启动门校验（可选卡>0、待订阅账号>0），起线程跑 `run_daily_subscribe_pipeline`。
- 前端：复用现有每日任务面板，加「每日订阅任务」按钮（或模式切换），复用 captcha key / group 选择、
  运行状态/日志/停止（sate.is_running / stop_requested 已是全局单任务闸门——订阅与充值互斥，不并跑）。

## 驱动栈切换（关键）
- 注册：signup_one 内部用 `create_driver`（Patchright），函数返回时已关闭其 session。
- 订阅：`create_driver_vanilla(profile_id=email)` 另起原生栈，复用同一 `data/profiles/<email>`（登录态持久）。
- 两栈**不同时**打开同一 profile（串行 + 注册先关后订阅），无 Singleton 锁冲突。

## 数据流 / 状态机
accounts.status 流转：`imported/pending/failed` --注册成功--> `registered` --订阅成功--> `subscribed`；
Arkose→`pending`、挂起→`suspended`、封禁→`banned`（不选）。卡：成功 paid / 拒付 invalid（同 recharge）。

## 兼容 / 回滚
- 全 additive：新 pipeline/account 动作/API/按钮，不改 run_daily_pipeline 与 _recharge_one_account。
- 回滚：删新增函数 + API + 前端按钮即可；无 schema 变更。
- 互斥：沿用 AppState 单任务闸门（is_running/stop_requested），订阅任务与充值任务不并跑。

## 风险 / 实测约束（2026-07-25 实跑发现）
- **注册模式**：最初用 signup_one(semi_auto=False) 错误——它从不完成注册（不弹 Arkose 也停在
  邮箱验证页 reached_verify_email → 误标 failed）。已修：新增 `auto_skip_captcha=True`——不弹 Arkose
  自动收码完成、弹了立即跳过（fernandezr701 实测自动收码建号成功）。
- **【硬约束】新注册 GitHub 账号被 flag**：刚自动注册的账号在 opencode OAuth 授权页报
  「This account is flagged … cannot authorize a third party application」，拿不到 workspace、无法订阅。
  已处理：login_and_open_own_go 检测 flagged → 返回 flagged=True；_subscribe_one_account 标账号
  `flagged` 并从待订阅集永久排除。**但这意味着「新号→订阅」大多被 GitHub flag 挡住**，能订阅的主要是
  早前注册的非 flagged 账号（carold030/leilao40）。绕 flag 需账号养号/手机验证/申诉，非本任务代码可解。
- **卡源**：group 1 全部过不了 Stripe 认证（父任务第六轮）。
- 综合：端到端「新号→subscribed」被 GitHub flag + 卡源双重阻挡；编排/状态机本身已正确（注册自动完成、
  flagged 跳过、逐卡拒付轮转、停止收敛均实测可见）。
- signup_one 有头 Patchright 在 Web 后台线程运行，已用 worker.make_monitor + 截图/停止钩子集成。
