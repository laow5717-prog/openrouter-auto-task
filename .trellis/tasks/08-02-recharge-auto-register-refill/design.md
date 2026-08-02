# 技术设计 — 充值任务账号耗尽自动注册补号 + 充值成功去重

## 现状锚点(app.py)

- `run_daily_pipeline(group_id, login_password, captcha_api_key, captcha_server)`
  - 账号集:`[a for a in accts if (login_password or a.login_password) and status ∉ ('banned','archived','flagged')]`
  - 主循环:`while round_num < MAX_ROUNDS`,每轮 `pool.map(payable_accounts, _recharge_one)`,
    `_recharge_one` → `_recharge_one_account` → `registration.recharge_account`。
  - `MAX_ROUNDS = len(accounts)*50 + 5`;收尾统计 paid/fail/archived/flagged。
- `_subscribe_one_account` 的 A 段(707–733)= 注册逻辑;B 段 = 订阅。
- `_hotmail_by_email(email)` 从 hotmail.xlsx 取 HotmailAccount。
- `signup_one(headless, semi_auto, account, then_opencode, auto_skip_captcha)` 返回
  `{outcome: reached_captcha|account_suspended|signup_complete|..., github_password}`。

## 变更点

### 1. 充值成功去重(R1)

账号筛选排除集加 `'recharged'`:

```python
and (a.get('status') or '') not in ('banned', 'archived', 'flagged', 'recharged')
```

一处修改。docstring 同步(「账号选取排除 banned、archived、flagged 与 recharged」)。

### 2. 抽取共享注册函数(R3)

新增 `_register_one_account(self, acct, worker=None) -> (result, detail)`,
result ∈ {"registered","skipped","failed"}。函数体 = 现 `_subscribe_one_account` 707–733:

- 无 hotmail 数据 → ("skipped","无 hotmail 数据")
- signup_one:
  - reached_captcha → update_status(email,'pending');("skipped","碰 Arkose")
  - account_suspended → upsert status='suspended';("skipped","注册即挂起")
  - 非 signup_complete → update_status('failed');("failed", f"注册失败: {oc}")
  - signup_complete → upsert status='registered' + login_password;("registered", "GitHub 注册成功")

`_subscribe_one_account` 改为:
```python
if status not in ('registered', 'subscribed'):
    rr, rdetail = self._register_one_account(acct, worker)
    if rr != "registered":
        return ("skipped" if rr=="skipped" else "failed"), rdetail
    # 继续 B 段订阅(此时 login_password 已在库,重取 acct 或用返回值)
```
注意:订阅 B 段登录用 `login_and_open_own_go(session)`,不依赖 login_password 变量,
故抽取后行为不变(仅把注册内联段换成函数调用)。

### 3. 充值补号阶段(R2, R4)

`run_daily_pipeline` 主循环改造为「优先充值现有可充账号,耗尽则注册一个 imported 补充」。

每轮开始重算 `payable`(排除 recharged 后的可充集,含本轮新注册转正的账号):

```
while round_num < MAX_ROUNDS:
    if stop_requested: break
    if not eligible_cards: break          # 无可选卡,原逻辑
    payable = _payable_accounts()          # 有密码 且 status ∉ 终态(含 recharged)
    if not payable:
        # —— 账号耗尽:尝试注册一个 imported 补号 ——
        cand = _next_registerable_imported()   # status='imported' 且 hotmail 有数据
        if not cand:
            self._hooked_print("无可充账号且无 imported 可注册,任务结束")
            break
        if not self.account_registry.claim(cand['email']): continue
        try:
            rr, rdetail = self._register_one_account(cand, self.primary_worker)
        finally:
            self.account_registry.release(cand['email'])
        registered_total += 1 if rr == "registered" else 0
        self._hooked_print(f"补号 {cand['email']}: {rr}（{rdetail}）")
        continue        # 下一轮:注册成功者已在 payable;失败者已离开 imported
    # —— 正常充值 payable(现有 pool.map 逻辑不变)——
    round_num += 1
    ...
```

关键点:
- **`payable` 每轮实时从 DB 取**(`account_model.get_all`),故补号转正的账号无需手工塞进列表。
- **补号迭代不占 round_num**(round_num 只在真正充值轮递增),避免 50 次注册把兜底上限吃光;
  另将 `MAX_ROUNDS` 基数改为 `(len(payable_initial) + imported_count) * 50 + 5` 或对补号单独计数上限。
- **防死循环**:`_register_one_account` 必改 cand 状态(registered/pending/suspended/failed),
  下次 `_next_registerable_imported()` 取不到同一个;imported 耗尽 → cand=None → break。
- **闭环**:注册成功账号下一轮进入 `pool.map`→`_recharge_one_account`→`recharge_account`,
  其中 `ensure_opencode_session` 完成 opencode 登录,即「注册→登录→充值」连贯。

`done_emails` 语义保持(archived/flagged 退出);补号账号不进 done_emails。

### 4. 收尾统计(A4)

新增 `registered_total`,收尾行加「注册补号 {registered_total} 个」。

## 备选与取舍

- **补号即时充值 vs 下轮充值**:选「下轮」——主循环每轮实时重算 payable,注册成功者自然于
  下一轮被充值,代码最简、无重复登录;"注册→(下一迭代)登录充值"对用户仍是全自动连贯,
  不牺牲体验。即时充值需在补号分支内联一次 `_recharge_one_account`,分支复杂度更高,收益低。
- **imported 一次注册一个 vs 批量**:一次一个。注册重(Arkose、浏览器),串行下逐个更稳,
  且每个注册后立即有机会充值,失败也能尽快暴露。

## 影响面

- 改动集中在 app.py（`run_daily_pipeline`、新增 `_register_one_account`、`_next_registerable_imported`、
  `_payable_accounts`、`_subscribe_one_account` 微调）。
- 不改 registration.py / 浏览器层 / 前端 / DB schema。
- 兼容:充值对外 API、订阅任务对外行为不变。
