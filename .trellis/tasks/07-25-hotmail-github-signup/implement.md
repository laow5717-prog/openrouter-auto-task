# Implement — 阶段二执行计划

## 步骤

1. [ ] 重构 `src/services/github_signup_service.py`
   - `_collect_and_fill_code(session, token, since_ts, result)` → `_collect_and_fill_code(session, fetch_code, result)`；
     两处 `wait_for_github_launch_code(token, since_ts)` 改 `fetch_code()`。
   - `_finish_semi_auto(session, token, since_ts, result)` → `_finish_semi_auto(session, fetch_code, result)`。
   - `signup_one(...)` 增参 `account=None`：
     - hotmail 分支跳过 `create_temp_email()`，用 `account.email`，`email_password=account.password`，
       `create_driver(headless, profile_id=account.email)`，`fetch_code=lambda: wait_for_github_launch_code_ruoanzhu(account.link)`。
     - mail.tm 分支 `fetch_code=lambda: wait_for_github_launch_code(token, since_ts)`。
   - import `wait_for_github_launch_code_ruoanzhu`。

2. [ ] 新增 `scripts/run_hotmail_github_signup.py`
   - 读 xlsx（`read_hotmail_accounts`）。
   - `--import`：逐行 `AccountModel.upsert(email, email_password=pw, status='imported')`。
   - `--index/--email/--all`：选账号 → `signup_one(semi_auto=True, headless=False, account=acc)` → 按 outcome 落库。
   - `_persist_result(account_model, acc, result)`：实现状态映射表。

3. [ ] 验证
   - `python3 -m py_compile` 两文件。
   - `--import` 干跑：确认 10 行入 accounts、status='imported'、email_password 落对（查库确认）。
   - mail.tm 老路径回归：`signup_one()` 无参仍可构造 fetch_code（import + 快速静态验证，不实际起浏览器跑满）。
   - 真实注册 1 条：需用户在场过码（有头），确认成功后 accounts 出现该 email、status='registered'、login_password 有值。

## 验证命令

```
python3 -m py_compile src/services/github_signup_service.py scripts/run_hotmail_github_signup.py
python3 scripts/run_hotmail_github_signup.py --import
python3 -c "from src.models.database import Database; from src.models.account import AccountModel; \
  rows=AccountModel(Database()).get_all(); print(len(rows)); [print(r['email'], r['status'], bool(r['email_password'])) for r in rows[:12]]"
# 真实注册（需人在场过码）：
python3 scripts/run_hotmail_github_signup.py --index 1
```

## 回滚点

- 步骤 1 后若回归失败 → git 还原 `github_signup_service.py`。
- DB 仅新增行/改 status，无 schema 变更；如需清理：`DELETE FROM accounts WHERE status='imported'`。

---

## 阶段四执行计划（订阅付款编排）

### 批次 A — 浏览器订阅模块（不扣款，可测试卡验证到提交前）
1. [ ] 新增 `src/browser/opencode_subscribe.py`：
   - `start_subscribe_go(session, wid, monitor)`：`login_and_open_own_go` 后进 /go，点 "Subscribe to Go"，
     轮询等 `checkout.stripe.com` 整页出现；返回 (ok, detail)。
   - `click_subscribe(session, monitor)`：认 role=button name 以 "Subscribe" 开头（排除已在处理态）。
   - `detect_subscribe_result(session, wid, monitor, timeout)`：成功=离开 checkout 跳回 opencode 且 /go 显示已订阅；
     拒付/3DS/hCaptcha 复用 opencode_billing 的文本/弹窗判据（抽用或复制常量）。
   - `subscribe_via_stripe(session, card, wid, monitor, should_stop)`：镜像 `recharge_via_stripe`，
     入口换 start_subscribe_go、提交换 click_subscribe、判定换 detect_subscribe_result，中间填卡步骤复用。
   - 提供 `--dry`（填好卡停在提交前，不点 Subscribe）供无扣款验证。
2. [ ] 用测试卡 `--dry` 实机验证：进 /go→Subscribe→Stripe→选 USD→选 Card→填卡→停在提交前，全链无异常。

### 批次 B — 编排 + web 接入（含真实扣款，需用户显式确认）
3. [ ] 新增 `subscribe_go_account(email, login_password, ...)`：前置注册/登录分支 + 逐卡 subscribe_via_stripe + 记账。
4. [ ] `_recharge_one_account` / routes 增订阅模式分支（additive，不删 Add Balance）。
5. [ ] 用户在场：真实点一次 Subscribe（真扣 $5），标定 detect_subscribe_result 成功信号并回填判定。

## 阶段四验证命令
```
python3 -m py_compile src/browser/opencode_subscribe.py
python3 -m src.browser.opencode_subscribe --email carold030@hotmail.com --dry   # 填卡停在提交前，不扣款
```
