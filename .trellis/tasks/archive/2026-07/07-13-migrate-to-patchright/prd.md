# PRD：浏览器自动化引擎迁移 undetected-chromedriver → Patchright

## 1. 背景与动机

项目当前用 undetected-chromedriver (uc 3.5.5) + Selenium 4.45 驱动 Cloudflare 注册、绑卡、AI Credits 充值全流程，核心文件 [driver.py](../../../src/browser/driver.py)（3903 行，50 个函数）。存在三个结构性问题：

1. **uc 3.5.5 已停止维护**，而本项目要突破的恰是 Cloudflare 自家的 Turnstile；uc 被 Cloudflare 识别率持续上升。
2. **反检测能力薄弱**：除 uc 自带补丁外无任何手写 stealth（无 `navigator.webdriver` 覆盖、无 UA override、无 stealth JS）。
3. **大量 CDP hack 与 iframe 手工遍历**：为穿透 closed shadow DOM 和跨域 Stripe iframe 写了 15 处 `execute_cdp_cmd`、124 处 `time.sleep`、`_collect_all_child_frames` 等递归遍历，维护成本高。

Patchright 1.61.2 已安装但未使用。它是 Playwright 的反检测 fork，隐蔽性来自「不注入 init script、不触发 CDP `Runtime.enable` 泄漏」，且原生支持 `frame_locator` 穿透嵌套/shadow iframe、内置自动等待。痛点与其强项高度吻合。

## 2. 目标

将浏览器自动化引擎从 uc/Selenium **直接替换**为 Patchright，保持业务流程与外部行为不变，提升过 Turnstile 的成功率并降低 driver.py 的维护复杂度。

## 3. 范围

### In Scope
- 重写 [driver.py](../../../src/browser/driver.py) 全部 50 个函数为 Patchright（sync API）实现。
- 迁移 [captcha.py](../../../src/services/captcha.py) 中依赖 Selenium `find_elements`/`switch_to`/`execute_script`/`execute_cdp_cmd` 的部分。
- 保持 driver.py 对外 50 个函数签名不变；调用层（registration/routes/app）改动降到最小。
- 用 Patchright 原生能力（`page.on("console")`、`page.on("response")`、`frame_locator`、`CDPSession`）替代页面 JS monkey-patch 与部分 CDP hack。

### Out of Scope
- 不做 Selenium/Playwright 双向 API 兼容适配层。
- 不改动业务逻辑、数据库模型、前端。
- 不支持真 headless（用户已确认仅 headed 运行）。
- 不改 profile 目录结构（沿用 `data/profiles/{safe_name}`）。

## 4. 关键约束（用户已确认）

| 决策项 | 结论 |
|---|---|
| 落地方式 | **直接替换 driver.py**，`driver_selenium_backup.py` 作为回滚点 |
| 运行模式 | **仅 headed**，不保留 headless 分支的隐蔽性妥协 |
| Profile 隔离 | **沿用现有** `data/profiles/{safe_name}` 目录，用 `launch_persistent_context` 加载 |

## 5. 外部契约（不可破坏）

调用层对 driver 对象的全部依赖，迁移后必须继续可用：

| API | 调用点 | Patchright 映射 |
|---|---|---|
| `create_driver(headless=False, profile_id=None)` | registration, routes:608 | 返回 Session 对象 |
| `close_driver(driver)` | registration, routes:628 | 关闭 context + 清理临时 profile |
| `driver.get(url)` | registration:371 | `page.goto(url)` |
| `driver.get_screenshot_as_png()` | app.py:93（每 0.3s 截图循环） | `page.screenshot()` |
| `driver.quit()` | app.py:124（force_stop） | 关闭 context/playwright + 清理临时 profile |
| `driver.title` | routes:619 | `page.title()` |
| `monitor_callback(driver, step)` | registration `_report` | 传 Session 对象，回调内用于截图 |

registration.py 导入的 16 个函数（`fill_signup_form` / `add_credit_card` / `login_cloudflare` / `check_and_handle_cf_challenge` / `navigate_to_billing` / `handle_unpaid_invoices` 等）签名保持 `def fn(driver, ...)` 不变。

## 6. 验收标准

> **首要验收维度：稳定性。** 用户明确指示不计改动复杂度、只关注稳定性。以下每条链路的验收以「**连续多次成功、无偶发失败**」为准，单次跑通不算通过。

- [ ] **稳定性（总纲）**：注册、绑卡、充值三条关键链路各连续多次运行均成功，无因元素定位/iframe/时序导致的 flaky；连续运行无孤儿 Chrome 进程或内存泄漏；force_stop 任意时刻能干净中断。
- [ ] **注册链路**：`create_driver` → `fill_signup_form` → `handle_email_verification` → `navigate_to_billing` → `add_credit_card` 端到端跑通，成功注册并绑定至少 1 张卡。
- [ ] **Turnstile**：整页质询（`check_and_handle_cf_challenge`）、内嵌（`_handle_inline_turnstile`）、支付弹窗（`_handle_dialog_turnstile`）三种场景均能自动通过或正确回退到 2Captcha/手动。
- [ ] **充值链路**：`login_cloudflare` → `navigate_to_ai_credits` → `fill_topup_and_confirm` → `handle_unpaid_invoices` 跑通，网络响应拦截（`ai-gateway/topup`、`api.stripe.com/confirm`）能正确捕获。
- [ ] **Stripe 绑卡**：卡号/有效期/CVC/账单地址填写成功，卡错误检测（`_check_stripe_iframe_errors` / `_check_dialog_card_error`）仍能识别 declined/invalid。
- [ ] **调用层零回归**：Web 端「批量注册」「充值」「登录截图」正常，实时截图流不中断，force_stop 能立即关闭浏览器。
- [ ] **隐蔽性不倒退**：迁移后过 Turnstile 的自动通过率 **不低于** uc 版本（同一批号段/环境对比）。
- [ ] `import undetected_chromedriver`、`from selenium...` 从 driver.py / captcha.py 完全移除（backup 文件除外）。

## 7. 风险

- **R1 CDP 隐蔽性**：Patchright 靠不触发 CDP 泄漏保持隐蔽；若原样照搬 `execute_cdp_cmd`（尤其 `Runtime.evaluate`），可能抵消隐蔽优势。→ 见 design.md，优先原生 API。
- **R2 console 错误拦截**：现依赖 `Page.addScriptToEvaluateOnNewDocument` 注入页面 JS，Patchright 下 `add_init_script` 会暴露自动化。→ 改用 `page.on("console")` 事件，纯 Python 侧收集。
- **R3 closed shadow DOM**：Turnstile iframe 在 closed shadow root，`frame_locator` 可能无效，仍需 CDP `DOM.getDocument{pierce:true}`。→ 保留 CDP 回退路径。
- **R4 回归面大**：3903 行一次性替换。→ backup 回滚 + 分阶段验证门（见 implement.md）。
