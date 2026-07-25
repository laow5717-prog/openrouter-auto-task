# 技术设计：充值 hCaptcha 自动解 + 余额≥$20 归档 + 修复误标 failed

## 参照样板（订阅流程，已验证可用）

| 环节 | 订阅（opencode_subscribe / app._subscribe_one_account） | 充值现状（registration.recharge_account / opencode_billing） |
|---|---|---|
| driver | `create_driver_vanilla(profile_id=email)` | `create_driver(headless=False, profile_id=email)` ❌ |
| solver 初始化 | `init_solver(key, server=captcha_server)` | 无 ❌ |
| hook 安装 | 导航前 `install_hcaptcha_hook(session)` | 无 ❌ |
| 解题调用 | `detect_subscribe_result` 内 `solve_hcaptcha`（≤3 次，非 3DS 阶段才解） | `detect_payment_result` 只提示人工、干等 ❌ |
| server 默认 | `api.multibot.cloud` | — |

结论：把充值三件套补齐、`detect_payment_result` 接入解题即可，**不新增机制、不动订阅**。

## 变更清单（按文件）

### 1. `src/browser/opencode_billing.py`
- 顶部 `from src.services import captcha as captcha_solver`。
- 新增公共函数 `read_current_balance(session, wid, monitor=None)`：导航到 billing 页、`time.sleep`
  等渲染、返回 `_read_balance(session)`（float 美元 | None）。供 R2 归档预检复用。
- 改 `detect_payment_result`：在其"仍在支付 / stripe_fr 存在"分支里，把订阅版的解题逻辑接入到
  **现有** hCaptcha 检测点（当前只 set `saw_captcha=True` 并提示人工那段），规则镜像
  `detect_subscribe_result`：
  - 维持"3DS 优先"：`_threeds_challenge_present` 为真时不解 hCaptcha（3DS 出现=captcha 已过）。
  - 仅在 `not saw_3ds and _captcha_challenge_present(session) is not None` 时：
    - 首次置 `saw_captcha=True` 并 `_step` 提示"检测到 hCaptcha"。
    - `captcha_solver.is_available() and captcha_tries < 3`：`captcha_tries += 1`，调
      `captcha_solver.solve_hcaptcha(session)`，成功则 `time.sleep(4)` 等结果（下一轮靠余额判成功）。
    - `is_available() and captcha_tries >= 3`：若无拒付文案则提前返回 `needs_captcha`（换下一张卡）。
    - `not is_available()`：保留旧行为——`_step` 提示"未配置 solver，请人工点 Verify"（只提示一次）。
  - 新增局部变量 `captcha_tries = 0`（与 `saw_captcha`/`saw_3ds` 并列）。
  - **余额到账判定仍是权威成功信号**（`_balance_grew`），置于每轮最前，保持不变。
- 超时收尾分支保持：`saw_captcha` 为真返回 `needs_captcha`（已有）。

> 注：`detect_payment_result` 的 hCaptcha 分支当前与 `detect_subscribe_result` 结构接近但更简
> （billing 版是 `if _captcha_challenge_present(...) and not saw_captcha`）。改造时把它扩成
> 订阅版那套 `elif not saw_3ds and ...:` 三分支解题块，注意 billing 版 3DS 与 captcha 是两个并列
> `if`，需调整成 captcha 分支受 `not saw_3ds` 守卫，避免 3DS 阶段回头误解常驻 invisible hCaptcha。

### 2. `src/services/registration.py` — `recharge_account`
- 签名加参数：`captcha_api_key=None, captcha_server="api.multibot.cloud"`（末尾追加，向后兼容）。
- import 换：`from src.browser.driver import create_driver_vanilla, close_driver`；
  `from src.services import captcha as captcha_solver`。
- 建 session：`session = create_driver_vanilla(profile_id=email)`（替换 `create_driver(...)`）。
- 建 session 后、`ensure_opencode_session` 前：
  ```python
  if captcha_api_key:
      captcha_solver.init_solver(captcha_api_key, server=captcha_server)
  if captcha_solver.is_available():
      captcha_solver.install_hcaptcha_hook(session)
  ```
- **R2 归档预检**：`ensure_opencode_session` 拿到 wid 后、进入试卡循环前：
  ```python
  bal = ob.read_current_balance(session, wid, monitor_callback)
  if bal is not None and bal >= RECHARGE_SKIP_BALANCE:   # 默认 20.0
      if account_model:
          account_model.update_status(email, "archived")
          account_model.update_balance(email, bal)
      return (False, f"余额 ${bal} ≥ ${RECHARGE_SKIP_BALANCE}，跳过并归档",
              responses, last4, "archived")
  ```
  - `RECHARGE_SKIP_BALANCE = float(os.environ.get("OPENCODE_RECHARGE_SKIP_BALANCE", "20"))`（模块级或函数内）。
  - `last4` 此时可能是 cards[0] 的（无实际扣卡），outcome=`archived` 上层据此不计失败。
- 返回契约新增 outcome 值 `archived`（原 {"topup","failed"} → {"topup","failed","archived"}）。

> 登录：保留 `ensure_opencode_session`（driver 无关，靠 BrowserSession 接口）。其 GitHub 自动登录
> 兜底极少触发（profile 已手动登录），vanilla 下也只在 profile 未登录时才走，风险低。

### 3. `src/web/app.py`
- `_recharge_one_account(self, email, login_password, payment_group_id=None, worker=None,
  captcha_api_key=None, captcha_server="api.multibot.cloud")`：
  - 透传 `captcha_api_key=captcha_api_key, captcha_server=captcha_server` 给 `recharge_account`。
  - 处理新 outcome：`recharge_account` 返回 outcome=`archived` 时（success=False 但非失败），
    映射为一个不计入 fail 的结果。方案：`_recharge_one_account` 返回三态 `("success"|"failed"|"archived", err)`；
    `run_daily_pipeline._recharge_one` 里 `archived` 单独计数（`round_stats['archived']`），
    **不计入 fail_count、不阻碍进展判定**（视为该账号已完成/退出轮转）。
  - 日志：`{email} 余额≥$20，已归档跳过`。
- `run_daily_pipeline(self, group_id, login_password=None, captcha_api_key=None,
  captcha_server="api.multibot.cloud")`：
  - 账号筛选排除 `archived`：`(a.get('status') or '') not in ('banned', 'archived')`。
  - 把 `captcha_api_key` / `captcha_server` 传进 `_recharge_one_account`。
  - `round_stats` 增 `'archived': 0`；进展判定 `progressed` 语义不变（archived 视为已消耗账号，
    不算失败也不算卡消耗，但不应导致"整轮零进展"误判——若一轮全是 archived，说明账号在收敛，
    应允许继续/正常收尾。实现：`progressed = paid>0 or after<remaining or archived>0`）。
  - 收尾统计串新增 archived 计数。

### 4. `src/api/routes.py`
- daily 充值启动端点（run_daily_pipeline 的那个）：
  - 加 `captcha_server = data.get('captcha_server') or 'api.multibot.cloud'`。
  - 启动门账号计数排除 `archived`：`(a.get('status') or '') not in ('banned', 'archived')`。
  - `args=(group_id, login_password, captcha_api_key, captcha_server)`。
- 单账号充值端点 `/api/accounts/recharge`：
  - 读 `captcha_api_key = data.get('captcha_api_key')`、`captcha_server = data.get('captcha_server') or 'api.multibot.cloud'`。
  - `_recharge_one_account(email, login_password, payment_group_id, captcha_api_key=..., captcha_server=...)`。

### 5. `src/models/account.py`（可选增强）
- 增 `reset_failed_to_registered(self)` 方法：`UPDATE accounts SET status='registered',
  updated_at=... WHERE status='failed'`，返回受影响行数。供 R3 脚本调用（比脚本内裸写 SQL 更内聚）。

### 6. `scripts/fix_failed_accounts_status.py`（新增，R3）
- 对齐 `scripts/fix_valid_cards_status.py`：定位 DB → 统计 `status='failed'` 条数 → 执行
  `reset_failed_to_registered` → 打印"before/after / 修改 N 条"。可重复执行（幂等）。
- 本任务执行阶段运行一次（先确认真实 DB 路径；根目录 `openrouter_auto.db` 为 0 字节，需查
  `config.yaml` / `data/` 下实际库）。

## 数据流

```
routes.py (captcha_api_key, captcha_server)
  → app.run_daily_pipeline(..., captcha_api_key, captcha_server)
      → app._recharge_one_account(..., captcha_api_key, captcha_server)
          → registration.recharge_account(..., captcha_api_key, captcha_server)
              ├─ create_driver_vanilla(profile_id=email)
              ├─ init_solver(key, server) + install_hcaptcha_hook(session)
              ├─ ensure_opencode_session → wid
              ├─ read_current_balance ≥ 20 → status='archived' + return "archived"
              └─ 逐卡 recharge_via_stripe → detect_payment_result
                     └─ 非 3DS 阶段遇 hCaptcha → solve_hcaptcha (≤3) [multibot]
```

## 兼容性 / 风险

- **driver 切 vanilla**：同 profile 目录，登录态复用（订阅流程已验证）。vanilla 隐蔽性弱但充值仅
  需已登录 profile + Stripe 支付，风险可控。回滚点：单文件 revert `recharge_account` 的 driver 行。
- **新增 outcome `archived`**：所有消费 outcome 的分支需显式处理，避免落入 else 被当失败。已在
  registration/app 两层明确分流。
- **测试**：`tests/test_daily_pipeline.py:125` monkeypatch `recharge_account`；新增参数带默认值，
  stub 签名兼容。运行全量 recharge/pipeline 相关测试确认不回归。
- **不动订阅**：所有改动限于充值链路与新增脚本/模型方法，`opencode_subscribe.py` 与
  `_subscribe_one_account` 零改动。

## 回滚

- R1/R2：git revert 对应文件改动（opencode_billing / registration / app / routes）。
- R3：数据修正不可逆，但为用户明确要求；如需回退需另行按 recharge_logs 重建，不在本任务范围。
