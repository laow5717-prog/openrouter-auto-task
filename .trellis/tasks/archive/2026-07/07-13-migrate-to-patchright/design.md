# 技术设计：Patchright 迁移

## 0. 首要原则：稳定性优先（用户明确指令）

**改动复杂度不是约束，稳定性是唯一优化目标。** 凡「省改动」与「更稳」冲突处，一律选更稳。具体贯彻：

- 不为「减少改动」而保留任何可能 flaky 的旧写法；也不为「代码更简洁」而激进删除现有的显式等待/重试/多源兜底。
- 宁可慢、宁可代码更长，也不要偶发失败。每个关键交互（导航/点击/填充/iframe 定位/验证码轮询）都要有**显式超时 + 有界重试 + 失败可诊断日志**。
- 现有的错误检测多层兜底（`_check_stripe_iframe_errors` 的 4 层、Turnstile 三种「已解决」判定）**全部保留冗余**，不合并、不精简。
- 验证门以「**连续多次成功**」为准，而非单次跑通（见 implement.md）。
- 线程模型、超时、重试等稳定性关键约束在本文档中**确定下来**，不留「待 PoC 验证」的悬念（PoC 只用于统计隐蔽性通过率，不用于试探架构可行性）。

## 1. 总体策略

**同步 API + Session 封装对象 + 逐函数 API 映射**，不做兼容适配层。

**为何 sync 而非 async（稳定性论证）**：本项目是 Flask 多线程 WSGI，注册/充值任务跑在后台工作线程。引入 `async_api` 意味着要在工作线程内维护 asyncio event loop，并把 Flask 同步路由、独立截图线程都桥接进该 loop（`call_soon_threadsafe` 等），asyncio 与 Flask 线程模型的交互是**新增的稳定性风险面**。`sync_api` 只要遵守「所有 Playwright 调用限定在创建它的那个工作线程」这一硬约束即可稳定运行（见 §2 线程模型）。因此稳定性优先 → 选 sync + 严格单线程约束。

- 用 `patchright.sync_api`（现有代码全同步，async 会强制重写全部调用链，代价过高）。
- 引入轻量 `BrowserSession` 对象承载 `(playwright, context, page)`，`create_driver` 返回它。50 个内部函数签名保持 `def fn(driver, ...)`，函数体内通过 `driver.page` / `driver.context` 访问 Playwright 对象。
- `BrowserSession` 额外暴露调用层需要的 4 个方法/属性（`.get()` / `.get_screenshot_as_png()` / `.quit()` / `.title`），使 registration/routes/app 调用层零改动或极小改动。

> 澄清：`BrowserSession` 不是 Selenium 兼容层——它只暴露本项目调用层实际用到的 4 个成员，不模拟 Selenium 的 `find_element`/`switch_to` 等 API。内部函数一律用 Playwright 原生风格重写。

## 2. BrowserSession 契约

```python
class BrowserSession:
    def __init__(self, playwright, context, page, temp_profile=None, download_dir=None):
        self.playwright = playwright      # sync_playwright() 句柄
        self.context = context            # BrowserContext（persistent）
        self.page = page                  # 主 Page
        self._temp_profile = temp_profile # 临时 profile 目录，持久化时为 None
        self._download_dir = download_dir
        self.console_errors = []          # page.on("console") 收集（替代 __cfAutoErrors）
        self.net_responses = []           # page.on("response") 收集（替代 __netInterceptResponses）
        self._last_png = None             # 最近一帧截图缓存（业务线程写，截图线程读）
        self._png_lock = threading.Lock()

    # —— 业务线程内部（禁止跨线程） ——
    def capture_frame(self):              # 仅业务线程调用
        png = self.page.screenshot()
        with self._png_lock:
            self._last_png = png

    # —— 调用层外部契约 ——
    def get(self, url):
        self.page.goto(url, wait_until="domcontentloaded")
        self.capture_frame()              # 导航后主动刷新缓存
    def get_screenshot_as_png(self):      # 截图线程调用：只读缓存，不碰 self.page
        with self._png_lock:
            return self._last_png
    @property
    def title(self): return self.page.title()
    def quit(self):
        try: self.context.close()
        finally:
            try: self.playwright.stop()
            finally:
                if self._temp_profile: shutil.rmtree(self._temp_profile, ignore_errors=True)
```

`close_driver(driver)` 保留为薄封装，内部调用 `driver.quit()`（幂等）。`monitor_callback(driver, step)` 收到的就是 `BrowserSession`，截图循环调 `driver.get_screenshot_as_png()` 不变。

**线程模型（确定方案，硬约束）**：sync_playwright 的对象绑定到创建它的线程，跨线程调用会抛 greenlet 错误。当前 app.py 截图循环在**独立线程**每 0.3s 调 `driver.get_screenshot_as_png()`（见 [app.py:89](../../../src/web/app.py#L89)）。直接让截图线程调 `page.screenshot()` 必然崩溃。**确定采用「业务线程截图 + 缓存帧 + 截图线程只读」模型**：

1. **硬约束**：所有 Playwright 对象（context/page/CDPSession）只在**创建它的那个工作线程**（即 registration/充值任务线程）内调用。这是不可违反的稳定性红线，implement.md 会 grep 检查有无跨线程调用。
2. `BrowserSession` 持有 `self._last_png`（bytes）+ `self._png_lock`（threading.Lock）。
3. `BrowserSession.capture_frame()`（新增，**仅业务线程调用**）：`png = self.page.screenshot()`，加锁写入 `self._last_png`。业务流程在每次 `driver.get()` 后、以及 `monitor_callback` 触发点主动调用它刷新缓存。
4. `get_screenshot_as_png()`（截图线程调用）：加锁**只读返回** `self._last_png`，绝不触碰 `self.page`。首帧未就绪时返回 None（截图循环已容错，见 [app.py:92](../../../src/web/app.py#L92)）。
5. 为让实时截图流足够流畅，在 Turnstile/Stripe 等长等待轮询循环内，业务线程每轮顺带调 `capture_frame()`（这些循环本就在业务线程）。

此方案把「跨线程」彻底消除，是本迁移的稳定性基石，不作为待验证项。

## 3. create_driver 映射

```python
def create_driver(headless=False, profile_id=None):
    p = sync_playwright().start()
    # profile 目录逻辑完全沿用现有（safe_name / data/profiles / tempfile.mkdtemp）
    context = p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",              # 用系统 Chrome，非 bundled chromium（隐蔽性关键）
        headless=False,                # 仅 headed（用户已确认）
        no_viewport=True,              # 用真实窗口尺寸
        locale=lang,                   # 替代 --lang/--accept-lang
        args=["--no-first-run", "--no-default-browser-check", "--disable-popup-blocking",
              "--start-minimized"],
        accept_downloads=True,
        downloads_path=download_dir,   # 替代 Page.setDownloadBehavior
    )
    page = context.pages[0] if context.pages else context.new_page()
    session = BrowserSession(p, context, page, temp_profile=..., download_dir=download_dir)
    _attach_listeners(session)         # console + response 监听（见 §4）
    # 随机窗口尺寸：page.set_viewport_size 或启动 args --window-size
    return session
```

**不要** `add_init_script` 注入 stealth（Patchright 自带 stealth，且注入会暴露）。`channel="chrome"` + Patchright 补丁即反检测主体，替代 uc。

## 4. 网络拦截 & console 错误 → 事件监听（重大简化）

现有两套页面 JS monkey-patch 全部废弃，改 Playwright 事件：

| 现有机制 | 位置 | 替代方案 |
|---|---|---|
| `Page.addScriptToEvaluateOnNewDocument` 注入 console 劫持 → `window.__cfAutoErrors` | driver.py:144 | `page.on("console", handler)`：handler 内用现有正则匹配，命中 push 到 `session.console_errors` |
| `inject_network_interceptor` monkey-patch fetch/XHR → `window.__netInterceptResponses` | driver.py:215 | `page.on("response", handler)`：URL 匹配 patterns 则 `resp.json()` 存 `session.net_responses` |
| `collect_intercepted_responses` 轮询 `window.__netInterceptResponses` | driver.py:285 | 读 `session.net_responses`（附带过滤/等待逻辑保留） |

- `inject_network_interceptor(driver, patterns)` 签名保留，但改为设置 `session._net_patterns = patterns` 并清空 `session.net_responses`（监听器在 create_driver 时已挂，按 patterns 过滤）。
- 事件监听在 Python 侧收集，不注入任何页面 JS，隐蔽性更好，且不受 `driver.get` 导航清空页面变量影响（解决现有「每次 get 后须重注」问题）。

## 5. CDP 用法迁移（15 处）

Patchright 提供 `context.new_cdp_session(page)` → `cdp.send("Domain.method", params)`。分三类处理：

| 现有 CDP | 迁移方案 |
|---|---|
| `Page.setDownloadBehavior` (L134) | 删除，改 `launch_persistent_context(downloads_path=...)` |
| `Page.addScriptToEvaluateOnNewDocument` (L144) | 删除，改 `page.on("console")`（§4） |
| `DOM.getDocument{pierce:true}` + 递归找 Turnstile/hCaptcha iframe (L547/L703/L2833) | **保留 CDP**：`cdp.send("DOM.getDocument", {"depth":-1,"pierce":True})`，Python 递归逻辑不变。closed shadow DOM 无原生替代。 |
| `DOM.getContentQuads` / `DOM.getBoxModel` (L583/L588) | 保留 CDP，取 iframe 视口坐标 |
| `Input.dispatchMouseEvent` 三段点击 (L604-624) | 优先改 `page.mouse.move/down/up`（原生、更隐蔽）；坐标来自上一步 CDP quads |
| `Page.getFrameTree` + `createIsolatedWorld` + `Runtime.evaluate` 查 Stripe 错误 (L2786-2827) | 优先改 `frame_locator` + `page.frames` 遍历读 `.p-FieldError`；跨域读不到时回退 CDP。**注意 `Runtime.evaluate` 是 Patchright 最敏感的检测点，尽量避免。** |

**原则**：能用 `page.mouse` / `frame_locator` / `page.frames` 原生替代的一律替代；只有 closed shadow DOM 穿透（Turnstile/hCaptcha 定位）保留 CDP `DOM.getDocument{pierce}`。

## 6. Selenium API → Playwright 映射表（内部函数体）

| Selenium | Playwright | 备注 |
|---|---|---|
| `driver.find_element(By.CSS, s)` | `page.locator(s).first` | 28 处 |
| `driver.find_elements(By.CSS, s)` | `page.locator(s).all()` | 52 处 |
| `WebDriverWait(d,t).until(EC.presence_of...)` | `page.locator(s).wait_for(state="visible", timeout=t*1000)` | 13 处；多数可删（自动等待） |
| `el.click()` | `locator.click()` | 内置 actionability 等待 |
| `el.send_keys(text)` | `locator.fill(text)` 或 `locator.press_sequentially(text, delay=)` | `type_slowly` → `press_sequentially(delay=50)` |
| `el.send_keys(Keys.ENTER/TAB/ESCAPE)` | `locator.press("Enter"/"Tab"/"Escape")` | |
| `driver.switch_to.frame(f)` | `page.frame_locator(sel)` | 9 处；Stripe 嵌套 iframe 用链式 `frame_locator().frame_locator()` |
| `driver.switch_to.default_content()` | 无需（frame_locator 无状态） | 删除 |
| `driver.execute_script(js, *args)` | `page.evaluate(js, arg)` | 38 处；注意参数传递语义不同（单参数） |
| `ActionChains(d).move_to_element_with_offset(e,x,y).click()` | `locator.click(position={"x":x,"y":y})` 或 `page.mouse` | |
| `driver.current_url` | `page.url` | captcha.py:45/88 |
| `Select(el).select_by_visible_text(t)` | `locator.select_option(label=t)` | L1450/L3604 |
| `driver.get(url)` | `page.goto(url)` | |

`time.sleep(124 处)`（稳定性优先，保守处理）：
- **业务性等待**（Turnstile 轮询、2Captcha 等待、页面稳定观察窗口、Stripe 提交后确认）**全部保留**，不因「自动等待能覆盖」而删。
- **元素定位等待**：改用 Playwright 自动等待 + 显式 `expect(locator).to_be_visible(timeout=)`，但**不裸删 sleep**——凡拿不准的地方保留一个短 sleep 兜底，宁可多等。
- 净效果：等待逻辑只增强不削弱。允许总耗时不降甚至略增，换取更低的 flaky 率。

## 7. captcha.py 迁移

captcha.py 通过传入的 `driver` 调 `driver.current_url` / `driver.execute_script` / `driver.execute_cdp_cmd` / `driver.find_elements` / `driver.switch_to.frame`。迁移后 `driver` 是 `BrowserSession`：

- `driver.current_url` → 新增 `BrowserSession.current_url` property = `self.page.url`（captcha.py 用了 2 处，成本低，加个 property 比改 captcha 调用点省事）。
- `driver.execute_script(...)` → 需改为 `driver.page.evaluate(...)`；captcha.py 里 sitekey 提取 JS（L138/155/174/271/316/343）逐个改。
- `driver.execute_cdp_cmd('DOM.getDocument', {pierce})` (L193/361) → `driver._cdp().send(...)`，新增 `BrowserSession._cdp()` 惰性创建并缓存 CDPSession。
- `driver.find_elements(By.TAG_NAME,'iframe')` (L248/302) → `driver.page.locator('iframe').all()`。
- `driver.switch_to.frame/default_content` (L315/328/338) → `driver.page.frame_locator(...)` 或 `page.frames` 遍历。
- `_inject_turnstile_token` / `_inject_hcaptcha_token`（往隐藏 input 写 2Captcha token 的 JS）→ `page.evaluate`。

新增到 BrowserSession 的辅助成员：`current_url` (property)、`_cdp()` (缓存 CDPSession)。这些是内部函数复用的最小工具，不构成 Selenium 兼容层。

## 8. 数据流不变量

- **错误检测**：console_errors（page.on console）+ Stripe FieldError（frame_locator 读文本）+ 网络响应状态 三源合一，与现有 `_check_stripe_iframe_errors` 的 4 层兜底语义等价。
- **网络响应**：`fill_topup_and_confirm` / `_fill_stripe_payment_and_submit` 依赖捕获 `ai-gateway/topup`、`api.stripe.com/confirm` 响应体判定成功。改 `page.on("response")` 后语义等价，且更可靠（不依赖页面 JS 未被 CSP 拦截）。

## 9. 兼容性与回滚

- `driver_selenium_backup.py` 保留为完整回滚点；迁移在 `driver.py` 原地进行。
- 若 Patchright 版严重回归，`git checkout driver.py` + `cp driver_selenium_backup.py driver.py` 即回退，调用层因签名不变无需改。
- 依赖：`patchright` 已装；需执行 `patchright install chrome` 或确认 `channel="chrome"` 用系统 Chrome。`selenium`/`undetected-chromedriver` 迁移完成后从依赖中移除（保留 backup 文件则暂留）。

## 10. 待迁移函数分组（50 个）

- **G1 生命周期**（3）：create_driver, close_driver, type_slowly
- **G2 网络/console**（3）：inject_network_interceptor, collect_intercepted_responses, （console 监听并入 create_driver）
- **G3 Turnstile 整页**（10）：check_and_handle_cf_challenge, _is_challenge_page, _try_click_turnstile, _click_turnstile_via_cdp, _cdp_click_at, _get_viewport_coords, _click_hcaptcha_via_cdp, _wait_for_turnstile_widget, _is_turnstile_truly_solved, _is_turnstile_solved
- **G4 Turnstile 内嵌/弹窗**（4）：_handle_inline_turnstile, _handle_dialog_turnstile, _is_dialog_turnstile_solved, dismiss_overdue_dialog
- **G5 注册/登录/导航**（8）：login_cloudflare, _extract_account_id, navigate_to_ai_credits, fill_signup_form, handle_email_verification, navigate_to_billing, get_bound_card_count, _find_and_click_add_button
- **G6 充值/发票**（6）：extract_topup_card_last4, close_topup_dialog, fill_topup_and_confirm, _extract_pdf_pay_url, _fill_stripe_payment_and_submit, handle_unpaid_invoices
- **G7 Stripe 绑卡弹窗**（8）：_wait_for_payment_dialog, _wait_for_stripe_iframe, _wait_for_stripe_fields_ready, _wait_for_billing_form_ready, _fill_billing_address_in_dialog, add_credit_card, _find_payment_submit_button, _wait_for_payment_submit_result
- **G8 Stripe 填充/错误检测**（8）：_fill_stripe_payment_element, _fill_stripe_field, _fill_visible_field, _close_payment_dialog, _check_browser_console_for_errors, _check_stripe_iframe_errors, _check_dialog_card_error, _find_stripe_field_errors_in_dom
- **G9 CDP/DOM 工具**（含于上组）：_collect_all_child_frames, _extract_text_from_dom_node

分组用于 implement.md 的迁移顺序与验证门。

## 11. 稳定性加固设计（贯穿全部函数）

在逐函数迁移时统一施加以下加固，作为 check 的验收项：

### 11.1 统一的稳健操作封装
在 driver.py 内提供内部工具（供 50 个函数复用），避免每处手写 try/except：

- `_safe_click(locator, timeout=, retries=2)`：等 actionable → click；失败重试；仍失败抛带上下文的异常。
- `_safe_fill(locator, value, timeout=, verify=True)`：fill 后**回读校验**实际值（Stripe 字段尤其需要），不一致则重填。
- `_safe_goto(page, url, retries=2)`：导航失败/超时重试。
- `_wait_gone(locator, timeout=)` / `_wait_visible(locator, timeout=)`：显式等待包装，统一超时与日志。

所有超时集中在模块顶部常量（`NAV_TIMEOUT`、`CLICK_TIMEOUT`、`TURNSTILE_POLL_TIMEOUT` 等），便于统一调参，不散落魔数。

### 11.2 默认超时策略
- `context.set_default_timeout(...)` / `set_default_navigation_timeout(...)` 设一个**偏保守（偏长）**的全局默认，避免慢网络下误判失败。
- 关键长流程（Turnstile、Stripe 提交结果 `_wait_for_payment_submit_result` 现为 180s）保留其原有长超时。

### 11.3 幂等与清理
- `close_driver` / `quit` 幂等，重复调用不抛（force_stop 与正常结束可能都调）。
- 任一函数中途异常，不得留下半开的 frame 状态（frame_locator 无状态天然满足；不再有 `switch_to` 残留态问题——这本身就是稳定性收益）。

### 11.4 诊断可观测性
- 每个 `_safe_*` 失败时打印当前 URL、目标选择器、已重试次数（经 `_hooked_print` 进 Web 日志）。
- 关键失败点调 `capture_frame()` 保留现场截图，便于事后排查。

### 11.5 反检测稳定性
- `channel="chrome"` 用系统 Chrome。**风险**：Chrome 自动更新可能引入行为漂移。→ 记录为运维注意项；若出现版本相关 flaky，可 pin Chrome 版本或改用 Patchright 托管的 chromium（隐蔽性略降但版本可控）。此权衡在验证门 1 若发现问题时再定。
