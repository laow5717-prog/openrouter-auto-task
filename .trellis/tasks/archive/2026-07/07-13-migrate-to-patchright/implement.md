# 执行计划：Patchright 迁移

**首要原则：稳定性优先（用户明确指令）。改动复杂度不设上限；每个验证门以「连续多次成功」为准，单次跑通不算过。** 详见 design.md §0 / §11。

按「基础设施 → 反检测/PoC 验证 → 分组迁移 → 端到端验证」推进。每个阶段末有验证门，未通过不进入下一阶段。回滚点：`driver_selenium_backup.py`（不可删，直到全部验收通过）。

## 阶段 0：环境与基础设施

- [ ] 0.1 确认 `patchright` 可用：`.venv/bin/python -c "from patchright.sync_api import sync_playwright"`；执行 `.venv/bin/patchright install chrome`（或确认系统 Chrome 可用于 `channel="chrome"`）。
- [ ] 0.2 在 driver.py 顶部新增 Patchright import，暂不删 Selenium import（并存过渡）。
- [ ] 0.3 实现 `BrowserSession` 类（design §2）：`get / get_screenshot_as_png / title / quit / current_url / _cdp() / capture_frame()` + `console_errors` / `net_responses` / `_last_png` / `_png_lock` 字段。
- [ ] 0.4 实现稳健操作封装（design §11.1）：`_safe_click / _safe_fill / _safe_goto / _wait_visible / _wait_gone` + 顶部超时常量。**后续所有函数迁移一律走这些封装，不裸调 locator。**
- [ ] **验证门 0**：`BrowserSession` 可实例化；`_cdp()` 惰性创建不报错；`get_screenshot_as_png()` 在无帧时安全返回 None。

## 阶段 1：create_driver + 反检测 PoC（最高风险，先打通）

- [ ] 1.1 迁移 `create_driver`（design §3）：`launch_persistent_context(channel="chrome", headless=False, ...)`，profile 目录逻辑照搬，随机窗口/locale 迁移。
- [ ] 1.2 迁移 `close_driver` / `type_slowly`（G1）。
- [ ] 1.3 挂载 `page.on("console")` + `page.on("response")` 监听（design §4），迁移 `inject_network_interceptor` / `collect_intercepted_responses`（G2）。
- [ ] 1.4 实现截图缓存模型（design §2，已定方案）：`capture_frame()` 业务线程写、`get_screenshot_as_png()` 截图线程只读缓存。grep 确认无跨线程 Playwright 调用。
- [ ] 1.5 迁移 Turnstile 整页链路 G3（含 CDP shadow DOM 穿透、`page.mouse` 点击）+ `check_and_handle_cf_challenge`；轮询循环内每轮调 `capture_frame()`。
- [ ] **验证门 1（PoC 核心，稳定性统计）**：脚本 `create_driver(profile_id=...)` → `driver.get(".../sign-up")` → 过 Turnstile，**连续跑 ≥5 次**，记录自动通过率与失败原因；确认截图线程与业务线程并行 30s+ 无 greenlet 崩溃；force_stop 中途打断能干净关闭。对比 uc 版通过率。**此门是「是否值得继续」的决策点**——通过率未达 uc 版或出现崩溃，暂停并与用户复盘。

## 阶段 2：注册链路迁移

- [ ] 2.1 迁移 G5（login_cloudflare, fill_signup_form, handle_email_verification, navigate_to_billing, get_bound_card_count 等）。
- [ ] 2.2 迁移 captcha.py（design §7）：`execute_script`→`page.evaluate`、`execute_cdp_cmd`→`_cdp().send`、`find_elements`→`locator.all`、`switch_to`→`frame_locator`。
- [ ] 2.3 迁移 Turnstile 内嵌 G4 中 `_handle_inline_turnstile`。
- [ ] **验证门 2**：Web 端「批量注册」**连续 3 个账号**跑至绑卡前（navigate_to_billing 成功）；邮箱验证码流程稳定通过，无因元素定位/iframe 导致的偶发失败。

## 阶段 3：Stripe 绑卡迁移（iframe 最密集）

- [ ] 3.1 迁移 G7（add_credit_card, _wait_for_stripe_iframe, _fill_billing_address_in_dialog 等）。
- [ ] 3.2 迁移 G8（_fill_stripe_payment_element 用链式 `frame_locator`、_fill_stripe_field, _check_stripe_iframe_errors, _check_dialog_card_error）。
- [ ] 3.3 迁移 `_handle_dialog_turnstile` / `_is_dialog_turnstile_solved`（G4 剩余）。
- [ ] **验证门 3**：完整跑注册+绑卡**连续 3 次**，成功绑定卡；故意用错误卡验证 `_check_stripe_iframe_errors` / `_check_dialog_card_error` 稳定识别 declined/invalid（不漏报、不误报）；`_safe_fill` 回读校验确认 Stripe 字段值正确写入。

## 阶段 4：充值链路迁移

- [ ] 4.1 迁移 G6（navigate_to_ai_credits, fill_topup_and_confirm, _fill_stripe_payment_and_submit, handle_unpaid_invoices, _extract_pdf_pay_url 等）。
- [ ] 4.2 验证 `page.on("response")` 正确捕获 `ai-gateway/topup`、`api.stripe.com/confirm` 响应体。
- [ ] **验证门 4**：Web 端「充值」对已绑卡账号**连续 2 次**跑通；`page.on("response")` 稳定捕获目标响应体，数据与 uc 版一致，无丢包/时序错乱。

## 阶段 5：清理与全量验收

- [ ] 5.1 删除 driver.py / captcha.py 中所有 `import undetected_chromedriver` / `from selenium...`（backup 除外）。
- [ ] 5.2 grep 确认无残留 Selenium API：`grep -n "By\.\|WebDriverWait\|execute_cdp_cmd\|switch_to\|ActionChains\|find_element" src/browser/driver.py src/services/captcha.py`。
- [ ] 5.3 调用层核对：registration.py / routes.py / app.py 除 create_driver 返回值语义外无需改；确认 force_stop 的 `driver.quit()`、截图循环正常。
- [ ] 5.4 依赖清理：从 requirements/环境移除 selenium 相关（若保留 backup 文件则暂缓，记录 TODO）。
- [ ] **验证门 5（全量验收，对应 prd §6）**：注册+绑卡、充值、登录截图、force_stop、三类 Turnstile 场景、卡错误检测 全部通过；**关键链路各连续多次成功、无偶发失败**；Turnstile 通过率 ≥ uc 版；连续运行下无内存/进程泄漏（孤儿 Chrome 进程）。

## 验证命令

```bash
# 语法/import 检查
.venv/bin/python -c "import ast,sys; ast.parse(open('src/browser/driver.py').read())"
.venv/bin/python -c "from src.browser import driver"          # 导入不报错
# Selenium 残留检查（阶段 5）
grep -nE "undetected_chromedriver|from selenium|By\.|WebDriverWait|execute_cdp_cmd|switch_to\.|ActionChains" src/browser/driver.py src/services/captcha.py
# 端到端：通过 Web UI 触发对应任务并观察实时截图与日志
```

## 回滚点

- 任一验证门失败且无法在合理时间内修复 → `git checkout src/browser/driver.py src/services/captcha.py`（回到 uc 版），或 `cp src/browser/driver_selenium_backup.py src/browser/driver.py`。
- 调用层签名不变，回滚无连带改动。

## 审查门（review gates）

- 验证门 1（PoC）后：与用户确认隐蔽性提升，决定是否继续全量。
- 验证门 3 / 5 后：`trellis-check` 做 spec 合规与跨层数据流检查。

## 备注

- **不改** 业务逻辑、DB、前端。所有函数保持外部签名。
- **稳定性优先**：不裸删 `time.sleep`。元素定位改自动等待 + `expect`，但保留短 sleep 兜底；业务性等待（Turnstile 轮询、2Captcha、Stripe 提交确认）全部保留。等待逻辑只增强不削弱（design §6 / §11）。
- 每个交互走 `_safe_*` 封装（超时+重试+诊断日志+失败截图），不裸调 locator。
- Patchright 敏感点：避免 `add_init_script`、`expose_function`、频繁 `Runtime.evaluate`；`page.evaluate` 少量使用可接受，但错误检测优先走 `page.on` 事件与 `frame_locator`。
