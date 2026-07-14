"""
浏览器自动化模块
使用 Patchright（Playwright 反检测 fork）实现 Cloudflare 注册、
账单页面导航及信用卡添加流程
"""

import os
import re
import time
import random
import logging
import tempfile
import shutil
import threading
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from src.config import cfg
import src.services.captcha as captcha_solver

# US 州名缩写 → 全称映射
US_STATE_ABBR = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
    'PR': 'Puerto Rico', 'VI': 'Virgin Islands', 'GU': 'Guam',
    'AS': 'American Samoa', 'MP': 'Northern Mariana Islands',
}

MAX_WAIT_TIME = cfg.browser.max_wait_time
SHORT_WAIT_TIME = cfg.browser.short_wait_time
ERROR_PAGE_MAX_RETRIES = cfg.retry.error_page_max_retries
BUTTON_CLICK_MAX_RETRIES = cfg.retry.button_click_max_retries


# 常用窗口尺寸（模拟不同设备）
_WINDOW_SIZES = [
    (1280, 800), (1280, 900), (1366, 768), (1440, 900),
    (1536, 864), (1600, 900), (1920, 1080), (1680, 1050),
]

# 常用语言组合
_LANGUAGES = [
    "en-US,en", "en-GB,en", "en-US,en;q=0.9",
    "en-US,en;q=0.9,zh-CN;q=0.8", "en,en-US;q=0.9",
]


# ======================================================================
# Patchright 迁移基础设施（阶段 0）
# ----------------------------------------------------------------------
# 稳定性优先：超时偏保守（偏长），所有交互经 _safe_* 封装（超时+重试+
# 诊断日志），Playwright 对象严格限定在创建它的工作线程内使用。
# ======================================================================

# 超时常量（毫秒）。Playwright 超时单位是 ms；现有 cfg 是秒。
DEFAULT_TIMEOUT_MS = 30_000          # 全局默认（比 MAX_WAIT_TIME=20s 略长，容忍慢网络）
NAV_TIMEOUT_MS = 60_000              # 页面导航（偏长，Cloudflare/Stripe 页面重）
ELEMENT_TIMEOUT_MS = MAX_WAIT_TIME * 1000     # 元素可见/可交互等待 = 20s
SHORT_TIMEOUT_MS = SHORT_WAIT_TIME * 1000     # 短等待 = 5s
CLICK_TIMEOUT_MS = 15_000
FILL_TIMEOUT_MS = 15_000

# 支付/卡错误 console 日志匹配（复刻原 console 拦截器正则，L158-164）
_CARD_ERROR_PATTERNS = [
    re.compile(r'setup.intent.error', re.I),
    re.compile(r'form.error.handler', re.I),
    re.compile(r'payment.intent.failed', re.I),
    re.compile(r'failed.to.save.payment', re.I),
    re.compile(r'card.*(incorrect|invalid|declined|expired|failed)', re.I),
    re.compile(r'security.code.*(incorrect|invalid)', re.I),
    re.compile(r'cvc.*(incorrect|invalid|incomplete)', re.I),
]


class BrowserSession:
    """
    Patchright 浏览器会话封装。承载 (playwright, context, page)，
    对内供 50 个 driver 函数通过 .page/.context 访问 Playwright 原生对象，
    对外暴露调用层依赖的最小契约：get / get_screenshot_as_png / title / quit。

    线程红线（稳定性基石）：所有 Playwright 对象（context/page/CDPSession）
    只能在创建本会话的工作线程内调用。唯一的例外是 get_screenshot_as_png()，
    它只读 _last_png 缓存、绝不触碰 self.page，因此可被独立截图线程安全调用。
    """

    def __init__(self, playwright, context, page, temp_profile=None, download_dir=None):
        self.playwright = playwright        # sync_playwright().start() 句柄
        self.context = context              # 持久化 BrowserContext
        self.page = page                    # 主 Page
        self._temp_profile = temp_profile   # 临时 profile 目录；持久化时为 None
        self._download_dir = download_dir
        self.console_errors = []            # page.on("console") 收集（替代 __cfAutoErrors）
        self.net_responses = []             # page.on("response") 收集（替代 __netInterceptResponses）
        self._net_patterns = []             # 网络拦截关键词模式（子列表内 AND，列表间 OR）
        self._last_png = None               # 最近一帧截图缓存（业务线程写、截图线程读）
        self._png_lock = threading.Lock()
        self._cdp_session = None            # 惰性创建的 CDPSession（缓存）
        self._closed = False
        self.account_banned = False         # 登录时检测到账号被 Cloudflare 封禁则置 True

    # ---- 事件监听（在 create_driver 中挂载，均在业务线程回调） ----
    def _on_console(self, msg):
        try:
            text = msg.text or ''
        except Exception:
            return
        for pat in _CARD_ERROR_PATTERNS:
            if pat.search(text):
                self.console_errors.append(text[:300])
                break

    def _on_response(self, response):
        try:
            url = response.url or ''
        except Exception:
            return
        if not self._net_patterns or not _match_net_url(url, self._net_patterns):
            return
        # 读响应体（sync 回调内允许；失败不影响主流程）
        try:
            data = response.json()
        except Exception:
            try:
                data = response.text()
            except Exception:
                data = None
        try:
            status = response.status
        except Exception:
            status = 0
        self.net_responses.append({'url': url, 'status': status, 'data': data, 'ts': time.time()})

    # ---- 业务线程内部工具（禁止跨线程） ----
    def capture_frame(self):
        """截取当前页面一帧并写入缓存。仅业务线程调用。"""
        try:
            png = self.page.screenshot()
        except Exception:
            return
        with self._png_lock:
            self._last_png = png

    def _cdp(self):
        """惰性创建并缓存 CDPSession（用于 closed shadow DOM 穿透等原生 API 无法覆盖处）。"""
        if self._cdp_session is None:
            self._cdp_session = self.context.new_cdp_session(self.page)
        return self._cdp_session

    @property
    def current_url(self):
        return self.page.url

    # ---- 调用层外部契约 ----
    def get(self, url):
        """等价 Selenium driver.get：导航并刷新截图缓存。"""
        _safe_goto(self, url)
        self.capture_frame()

    def get_screenshot_as_png(self):
        """截图线程调用：只读缓存，绝不触碰 self.page（跨线程安全）。首帧未就绪返回 None。"""
        with self._png_lock:
            return self._last_png

    @property
    def title(self):
        return self.page.title()

    def quit(self):
        """幂等关闭：关 context + 停 playwright + 清理临时 profile。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass
        if self._temp_profile and os.path.exists(self._temp_profile):
            try:
                shutil.rmtree(self._temp_profile, ignore_errors=True)
                print(f"  🧹 已清理临时 profile: ...{os.path.basename(self._temp_profile)}")
            except Exception:
                pass


def _match_net_url(url, patterns):
    """复刻原 matchUrl：patterns 为关键词子列表的列表，url 含某个子列表的全部关键词即匹配。"""
    for keywords in patterns:
        if all(kw in url for kw in keywords):
            return True
    return False


# ======================================================================
# 稳健操作封装（稳定性优先）：统一超时 + 有界重试 + 诊断日志 + 失败截图。
# 所有 50 个 driver 函数的交互一律走这些封装，不裸调 locator。
# ======================================================================

def _diag(session, desc, extra=''):
    """打印诊断上下文（经 _hooked_print 进 Web 日志）。"""
    try:
        url = session.current_url if session else '?'
    except Exception:
        url = '?'
    print(f"    ⚠️ {desc} 失败 [{extra}] @ {url}")


def _safe_goto(session, url, retries=2, timeout=NAV_TIMEOUT_MS):
    """导航，失败重试。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            session.page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return True
        except PWTimeoutError as e:
            last_err = e
            _diag(session, '导航', f'goto {url} 尝试{attempt + 1}/{retries + 1}')
        except Exception as e:
            last_err = e
            _diag(session, '导航', f'{type(e).__name__} 尝试{attempt + 1}/{retries + 1}')
        time.sleep(1)
    if last_err:
        raise last_err
    return False


def _safe_click(locator, session=None, timeout=CLICK_TIMEOUT_MS, retries=2, desc='元素'):
    """等待可交互后点击，失败重试。locator 为 Playwright Locator。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            locator.click(timeout=timeout)
            return True
        except PWTimeoutError as e:
            last_err = e
            _diag(session, f'点击[{desc}]', f'尝试{attempt + 1}/{retries + 1}')
        except Exception as e:
            last_err = e
            _diag(session, f'点击[{desc}]', f'{type(e).__name__} 尝试{attempt + 1}/{retries + 1}')
        if session:
            session.capture_frame()
        time.sleep(0.5)
    if last_err:
        raise last_err
    return False


def _safe_fill(locator, value, session=None, timeout=FILL_TIMEOUT_MS, retries=2,
               verify=True, desc='字段'):
    """填充输入框，可选回读校验（Stripe 字段尤其需要），不一致则重填。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            locator.fill('', timeout=timeout)      # 先清空
            locator.fill(str(value), timeout=timeout)
            if verify:
                actual = locator.input_value(timeout=SHORT_TIMEOUT_MS)
                if actual != str(value):
                    raise ValueError(f'回读不一致 expected={value!r} actual={actual!r}')
            return True
        except PWTimeoutError as e:
            last_err = e
            _diag(session, f'填充[{desc}]', f'尝试{attempt + 1}/{retries + 1}')
        except Exception as e:
            last_err = e
            _diag(session, f'填充[{desc}]', f'{type(e).__name__} 尝试{attempt + 1}/{retries + 1}')
        time.sleep(0.3)
    if last_err:
        raise last_err
    return False


def _wait_visible(locator, timeout=ELEMENT_TIMEOUT_MS):
    """等待元素可见；返回 True/False（不抛）。"""
    try:
        locator.wait_for(state='visible', timeout=timeout)
        return True
    except Exception:
        return False


def _wait_gone(locator, timeout=ELEMENT_TIMEOUT_MS):
    """等待元素消失/隐藏；返回 True/False（不抛）。"""
    try:
        locator.wait_for(state='hidden', timeout=timeout)
        return True
    except Exception:
        return False


def create_driver(headless=False, profile_id=None):
    """
    创建带有反检测的 Chrome 浏览器会话（使用 Patchright）

    参数:
        headless: 是否使用无头模式
        profile_id: 持久化 profile 标识（如 email），传入后复用同一浏览器环境；
                    为 None 时使用全新临时 profile
    返回:
        浏览器驱动实例
    """
    print(f"🌐 正在初始化浏览器 (Headless: {headless})...")

    # 持久化 profile：按 profile_id 复用同一目录；否则用临时目录
    is_persistent = profile_id is not None
    if is_persistent:
        base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'profiles')
        os.makedirs(base_dir, exist_ok=True)
        # 用 profile_id 的安全文件名作为目录名
        safe_name = re.sub(r'[^\w@.\-]', '_', profile_id)
        user_data_dir = os.path.join(base_dir, safe_name)
        os.makedirs(user_data_dir, exist_ok=True)
        print(f"  🔒 使用持久化 profile: {safe_name}")
    else:
        user_data_dir = tempfile.mkdtemp(prefix="cf_chrome_")
        print(f"  🔄 使用全新浏览器 profile: ...{os.path.basename(user_data_dir)}")

    # 下载目录（用于 invoice PDF 下载等）
    download_dir = os.path.join(user_data_dir, 'downloads')
    os.makedirs(download_dir, exist_ok=True)

    # 随机窗口尺寸（反检测）。
    # 注意：不设 Playwright 的 locale 选项——它经 CDP Emulation.setLocaleOverride 生效，
    # 会被 Cloudflare 检测为受控浏览器，导致 Turnstile 报 "problem with verification"。
    # 同理不加 --lang 等可能引入模拟指纹的参数。stealth 全交给 Patchright + channel=chrome。
    w, h = random.choice(_WINDOW_SIZES)

    # 启动参数保持最小（与真人浏览器一致）
    launch_args = [
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={w},{h}",
    ]
    if headless:
        print("  👻 使用伪无头模式 (Off-screen)...")
        launch_args.append("--window-position=-10000,-10000")

    def _clear_singleton_locks():
        # Chrome 异常退出会在 profile 里留下 Singleton 锁，导致下次启动失败
        # （"Mach rendezvous failed / parent died"）。仅在无浏览器占用该 profile 时安全，
        # 本应用通过 open_browsers 保证同一 profile 单实例，可安全清理。
        for _name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
            _p = os.path.join(user_data_dir, _name)
            try:
                if os.path.islink(_p) or os.path.exists(_p):
                    os.remove(_p)
            except Exception:
                pass
        # 修复损坏/臃肿的 Preferences（正常仅几百 KB；曾出现被写成 GB 级导致 Chrome
        # 加载崩溃）。Preferences 只存 UI 配置，不含登录 cookie，重置后 Chrome 自动重建，
        # 登录态（Cookies/Login Data）不受影响。阈值 10MB。
        _pref = os.path.join(user_data_dir, 'Default', 'Preferences')
        try:
            if os.path.exists(_pref) and os.path.getsize(_pref) > 10 * 1024 * 1024:
                os.rename(_pref, _pref + '.corrupt.bak')
                print("  🧹 检测到异常臃肿的 Preferences，已重置（登录态保留）")
        except Exception:
            pass

    playwright = sync_playwright().start()
    context = None
    last_err = None
    for attempt in range(2):          # 启动失败重试一次（清理残留锁后再试），提升稳定性
        _clear_singleton_locks()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="chrome",           # 用系统 Google Chrome（隐蔽性关键，非 bundled chromium）
                headless=False,             # 永不真无头（用户确认仅 headed）
                no_viewport=True,           # 用真实窗口尺寸（由 --window-size 控制）
                args=launch_args,
            )
            break
        except Exception as e:
            last_err = e
            print(f"  ⚠️ 浏览器启动失败(第{attempt + 1}/2次): {str(e)[:120]}")
            time.sleep(1.5)
    if context is None:
        # 两次都失败，停 playwright 并清理临时 profile，避免孤儿资源
        print("  ❌ 浏览器初始化失败，正在清理...")
        try:
            playwright.stop()
        except Exception:
            pass
        if not is_persistent:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        raise last_err

    try:
        page = context.pages[0] if context.pages else context.new_page()

        # 保守的全局默认超时（稳定性优先，容忍慢网络）
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        temp_profile = None if is_persistent else user_data_dir
        session = BrowserSession(playwright, context, page,
                                 temp_profile=temp_profile, download_dir=download_dir)

        # 仅注册网络响应监听（用于充值/支付响应捕获，走 Network 域，Patchright 已适配）。
        # 关键：绝不注册 page.on("console") —— 它会强制启用 CDP Runtime.enable，
        # 而 Runtime.enable 是 Cloudflare/Turnstile 检测「CDP 受控浏览器」的头号信号，
        # 会导致 Turnstile 报 "There was a problem with verification"。
        # 同理，启动阶段不创建任何 CDP 会话（不做 CDP 窗口操作）以免过早泄漏。
        page.on("response", session._on_response)

        print(f"  🖥️ 窗口: {w}x{h}")
        print("✅ 浏览器初始化成功 (Patchright)")
        return session
    except Exception:
        # 后续初始化失败，必须清理避免孤儿进程/资源
        print("  ❌ 浏览器初始化失败，正在清理...")
        try:
            context.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass
        if not is_persistent:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        raise


def close_driver(driver):
    """安全关闭浏览器并清理临时 profile（幂等；quit 内部已含 profile 清理）"""
    try:
        driver.quit()
    except Exception:
        pass


def type_slowly(element, text, delay=0.05):
    """模拟人工缓慢输入。element 为 Playwright Locator。"""
    element.press_sequentially(str(text), delay=int(delay * 1000))


def inject_network_interceptor(driver, patterns):
    """
    注入网络响应拦截器，捕获匹配指定 URL 模式的请求响应

    参数:
        driver: 浏览器驱动
        patterns: URL 关键词列表，每个元素是一个列表，URL 需同时包含所有关键词才匹配
                  例如: [['api.stripe.com', 'confirm'], ['ai-gateway', 'topup']]
    """
    # Patchright 版：不再注入页面 JS。响应由 create_driver 挂载的
    # page.on("response") 监听器在 Python 侧持续收集（更隐蔽，且不受导航清空影响）。
    # 这里只更新过滤模式并清空累积，语义等价于「开始一段新的拦截」。
    driver._net_patterns = patterns
    driver.net_responses = []
    print(f"已注入网络拦截器，监听 {len(patterns)} 个 URL 模式")


def collect_intercepted_responses(driver, timeout=60):
    """
    等待并收集所有被拦截的网络响应

    参数:
        driver: 浏览器驱动
        timeout: 最大等待时间（秒），收到第一个响应后额外等 3 秒收集后续响应
    返回:
        list: 响应列表 [{url, status, data, ts}, ...]
    """
    import json as _json
    first_found_time = None

    for _ in range(timeout):
        # 关键：用 Playwright 的 wait_for_timeout（而非 time.sleep）——sync Playwright 的
        # page.on("response") 事件只在调用 Playwright API 时才派发；裸 time.sleep 不驱动
        # 事件循环，会导致响应永远收不到。
        try:
            driver.page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
        responses = list(driver.net_responses)
        if responses:
            if first_found_time is None:
                first_found_time = time.time()
                print(f"捕获到 {len(responses)} 个响应，等待后续响应...")
            # 收到第一个响应后再等几秒，收集可能的后续请求
            if time.time() - first_found_time >= 3:
                break

    responses = list(driver.net_responses)
    for resp in responses:
        print(f"[网络响应] URL: {resp.get('url', '')}")
        print(f"[网络响应] HTTP Status: {resp.get('status', '')}")
        data = resp.get('data', {})
        if isinstance(data, dict):
            print(f"[网络响应] Body: {_json.dumps(data, indent=2, ensure_ascii=False)}")
        else:
            print(f"[网络响应] Body: {data}")
        print("---")

    if not responses:
        print("未捕获到匹配的网络响应")

    return responses


def dismiss_overdue_dialog(driver):
    """
    检测并关闭 Cloudflare 欠费提示弹窗（点击 'I understand'）
    可在任何页面操作前调用，无弹窗时直接返回
    """
    try:
        page = driver.page
        dialog = page.locator("div[role='alertdialog']")
        if dialog.count() == 0:
            return False
        # 定位含 "I understand" 文本 span 的按钮（等价原 XPath 精确文本匹配）
        btn = dialog.first.locator("button:has(span:text-is('I understand'))")
        if btn.count() > 0:
            _safe_click(btn.first, session=driver, desc="欠费弹窗 I understand")
            print("  已关闭欠费提示弹窗 (I understand)")
            time.sleep(1)
            return True
    except Exception:
        pass
    return False


def check_and_handle_cf_challenge(driver, max_wait=120):
    """
    检测并处理 Cloudflare Turnstile 质询页面

    处理策略（按优先级）：
    1. 等待 selenium-stealth 自动通过（静默通过）
    2. 尝试自动点击 Turnstile checkbox
    3. 如果以上都失败，提示用户在浏览器窗口中手动完成验证

    参数:
        driver: 浏览器驱动
        max_wait: 最大等待时间（秒），默认 120 秒留足手动操作时间
    返回:
        True 表示已通过或无质询，False 表示超时未通过
    """
    # 先检查当前页面是否有质询
    if not _is_challenge_page(driver):
        return True

    print("  🔒 检测到 Cloudflare 人机验证页面")

    start = time.time()
    auto_click_attempted = False
    user_notified = False

    while time.time() - start < max_wait:
        # 轮询期间刷新截图缓存，保证实时截图流不卡顿（业务线程内调用）
        driver.capture_frame()

        # 检查是否已通过质询
        if not _is_challenge_page(driver):
            elapsed = int(time.time() - start)
            print(f"  ✅ Cloudflare 验证已通过！(耗时 {elapsed} 秒)")
            return True

        # 阶段1: 前 10 秒等待自动通过（selenium-stealth 有时能静默通过）
        elapsed = time.time() - start
        if elapsed < 10:
            time.sleep(1)
            continue

        # 阶段2: 尝试自动点击 Turnstile checkbox
        if not auto_click_attempted:
            auto_click_attempted = True
            print("  🤖 尝试自动点击验证框...")
            if _try_click_turnstile(driver):
                # 等待最多 20 秒，每 2 秒检查一次
                for wait_i in range(10):
                    time.sleep(2)
                    if not _is_challenge_page(driver):
                        print("  ✅ 自动点击成功，验证已通过！")
                        return True
                print("  ⚠️ 自动点击未能通过验证")

            # 阶段2.5: 使用 2Captcha 自动解决
            if captcha_solver.is_available():
                print("  🤖 尝试使用 2Captcha 解决 Turnstile...")
                if captcha_solver.solve_turnstile(driver):
                    time.sleep(5)
                    if not _is_challenge_page(driver):
                        print("  ✅ 2Captcha 解决成功，验证已通过！")
                        return True
                    print("  ⚠️ 2Captcha token 注入后仍未通过，等待页面刷新...")
                    time.sleep(10)
                    if not _is_challenge_page(driver):
                        return True

        # 阶段3: 提示用户手动操作（2Captcha 失败时的兜底）
        if not user_notified:
            user_notified = True
            remaining = int(max_wait - (time.time() - start))
            print("")
            print("  " + "=" * 50)
            print("  ⚠️  需要手动完成人机验证！")
            print("  👉 请在浏览器窗口中勾选验证框")
            print(f"  ⏰ 剩余等待时间: {remaining} 秒")
            print("  " + "=" * 50)
            print("")

            pass

        time.sleep(2)

    print("  ❌ Cloudflare 验证超时，未能在规定时间内通过")
    return False


def _is_challenge_page(driver):
    """检测当前页面是否为 Cloudflare 质询页面"""
    try:
        title = driver.title.lower()
        if "just a moment" in title or "attention required" in title or "请稍候" in title:
            return True

        # 有些情况 title 不变，检查页面内容
        try:
            body_text = driver.page.inner_text("body", timeout=SHORT_TIMEOUT_MS).lower()
            challenge_keywords = [
                "checking your browser",
                "verify you are human",
                "needs to review the security",
                "正在验证您是否是真人",
            ]
            for keyword in challenge_keywords:
                if keyword in body_text:
                    return True
        except Exception:
            pass

    except Exception:
        pass
    return False


def _try_click_turnstile(driver):
    """
    尝试自动点击 Turnstile 验证框
    Cloudflare Turnstile 的 iframe 位于 closed shadow DOM 中，
    Selenium 无法直接访问，需要使用 CDP 或坐标点击

    返回 True 表示成功点击（不代表通过验证）
    """
    page = driver.page

    # 注意：不再用 CDP 穿透 shadow DOM 点击 —— 创建 CDP 会话/发送 DOM 命令会破坏
    # Patchright 隐蔽性，触发 Cloudflare "There was a problem with verification"。
    # 仅用 Playwright 原生点击（managed 模式通常无需点击即自动通过，交互式才需要点击）。

    # 方法1: 通过容器元素的坐标偏移点击 checkbox 位置（原生 click）
    try:
        container = page.locator('div[data-testid="challenge-widget-container"]').first
        if container.is_visible():
            # Turnstile checkbox 通常在容器左侧偏上的位置
            # iframe 宽度 300px，高度 65px，checkbox 大约在 (30, 33) 的位置
            container.click(position={"x": 30, "y": 33}, timeout=CLICK_TIMEOUT_MS)
            print("    → 通过坐标偏移点击了 Turnstile 容器")
            time.sleep(2)
            return True
    except Exception:
        pass

    # 方法3: 查找页面中可见的 iframe（非 shadow DOM 场景的回退）
    try:
        frames = page.locator("iframe").all()
        for frame in frames:
            try:
                src = frame.get_attribute('src') or ''
                title_attr = frame.get_attribute('title') or ''
                if 'challenges' in src or 'turnstile' in src or 'widget' in title_attr.lower():
                    if not frame.is_visible():
                        continue
                    try:
                        frame.click(timeout=CLICK_TIMEOUT_MS)
                        print("    → 点击了 Turnstile iframe")
                    except Exception:
                        pass
                    time.sleep(2)

                    # 尝试进入 iframe 查找 checkbox（content_frame 无状态，无需帧切换）
                    try:
                        child = frame.content_frame
                        if child is not None:
                            checkboxes = child.locator(
                                "input[type='checkbox'], .cb-lb, #challenge-stage, .ctp-checkbox-label"
                            ).all()
                            for cb in checkboxes:
                                if cb.is_visible():
                                    try:
                                        cb.click(timeout=CLICK_TIMEOUT_MS)
                                    except Exception:
                                        cb.evaluate("el => el.click()")
                                    print("    → 点击了验证框 checkbox")
                                    return True
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass

    return False


def _click_turnstile_via_cdp(driver):
    """
    使用 Chrome DevTools Protocol (CDP) 穿透 closed shadow DOM
    找到 Turnstile iframe 并模拟点击其 checkbox 区域
    """
    # 通过 CDP 获取整个 DOM 树（包括 closed shadow DOM，原生 API 无法穿透，必须保留 CDP）
    doc = driver._cdp().send('DOM.getDocument', {'depth': -1, 'pierce': True})
    root = doc['root']

    def find_turnstile_iframe(node):
        """递归查找 Turnstile iframe 节点"""
        if node.get('nodeName', '').lower() == 'iframe':
            attrs = node.get('attributes', [])
            attr_dict = dict(zip(attrs[::2], attrs[1::2])) if attrs else {}
            src = attr_dict.get('src', '')
            title = attr_dict.get('title', '')
            if 'challenges.cloudflare.com' in src or 'turnstile' in src.lower() or 'challenge' in title.lower():
                return node
        # 遍历子节点和 shadow roots
        for child in node.get('children', []):
            result = find_turnstile_iframe(child)
            if result:
                return result
        for shadow in node.get('shadowRoots', []):
            for child in shadow.get('children', []):
                result = find_turnstile_iframe(child)
                if result:
                    return result
        return None

    iframe_node = find_turnstile_iframe(root)
    if not iframe_node:
        print("    ⚠️ CDP: 未找到 Turnstile iframe")
        return False

    node_id = iframe_node.get('nodeId')
    backend_node_id = iframe_node.get('backendNodeId')

    # 获取 iframe 元素在视口中的位置
    try:
        # 优先使用 getContentQuads 获取视口坐标（CDP 保留：需精确视口坐标做鼠标点击）
        try:
            quads = driver._cdp().send('DOM.getContentQuads', {'backendNodeId': backend_node_id})
            quad = quads['quads'][0]
            click_x = quad[0] + 30
            click_y = (quad[1] + quad[5]) / 2
        except Exception:
            box_model = driver._cdp().send('DOM.getBoxModel', {'backendNodeId': backend_node_id})
            content = box_model['model']['content']
            click_x = content[0] + 30
            click_y = (content[1] + content[5]) / 2

        print(f"    → CDP: 找到 Turnstile iframe, 点击视口坐标 ({click_x:.0f}, {click_y:.0f})")
        _cdp_click_at(driver, click_x, click_y)
        print("    → CDP: 已发送点击事件到 Turnstile checkbox (等待验证结果...)")
        return True
    except Exception as e:
        print(f"    ⚠️ CDP 点击失败: {e}")
        return False


def _cdp_click_at(driver, x, y):
    """在指定视口坐标处模拟完整鼠标点击（move + down + up）。
    改用 Playwright 原生 page.mouse（比 CDP Input.dispatchMouseEvent 更隐蔽），
    保留原三段时序（移动→按下→释放）。"""
    mouse = driver.page.mouse
    mouse.move(x, y)
    time.sleep(0.1)
    mouse.down()
    time.sleep(0.05)
    mouse.up()


def _get_viewport_coords(driver, element):
    """
    获取元素相对于视口的坐标（而非页面坐标）。
    page.mouse 点击需要视口坐标。element 为 Playwright Locator。
    """
    return element.evaluate(
        "el => { var rect = el.getBoundingClientRect();"
        " return {x: rect.x, y: rect.y, width: rect.width, height: rect.height}; }"
    )


def _click_hcaptcha_via_cdp(driver):
    """
    使用 CDP 找到 hCaptcha iframe 并模拟点击其 checkbox 区域
    hCaptcha 通常出现在 LightboxModal 弹窗中的 .HCaptcha-container 内
    """
    try:
        # 方法1: 通过 locator 找到 hCaptcha iframe 并用 page.mouse 点击
        hcaptcha_iframes = driver.page.locator(
            '#HCaptcha-root iframe[src*="hcaptcha"], '
            '.HCaptcha-container iframe[src*="hcaptcha"], '
            'iframe[data-hcaptcha-widget-id], '
            'iframe[src*="hcaptcha.com"]'
        ).all()

        # 过滤出 checkbox iframe（尺寸较小，通常宽度 < 400px）
        checkbox_iframe = None
        for iframe in hcaptcha_iframes:
            if not iframe.is_visible():
                continue
            bb = iframe.bounding_box()
            width = bb['width'] if bb else 0
            # hCaptcha checkbox iframe 通常尺寸约 302x78 或类似
            # 图片挑战 iframe 尺寸较大 (>400px)
            if width < 400:
                checkbox_iframe = iframe
                break
            elif not checkbox_iframe:
                checkbox_iframe = iframe  # 兜底取第一个可见的

        if checkbox_iframe:
            # 先滚动到 iframe 可见
            checkbox_iframe.evaluate("el => el.scrollIntoView({block: 'center'})")
            time.sleep(0.5)

            # 使用 getBoundingClientRect 获取视口坐标（非页面坐标）
            viewport_rect = _get_viewport_coords(driver, checkbox_iframe)
            # hCaptcha checkbox 在 iframe 左侧约 30px，垂直居中
            click_x = viewport_rect['x'] + 30
            click_y = viewport_rect['y'] + viewport_rect['height'] / 2

            print(f"    → CDP: 找到 hCaptcha iframe ({viewport_rect['width']:.0f}x{viewport_rect['height']:.0f}), "
                  f"点击视口坐标 ({click_x:.0f}, {click_y:.0f})")

            _cdp_click_at(driver, click_x, click_y)
            print("    ✅ CDP: 已模拟点击 hCaptcha checkbox")

            # 等待短暂时间后检查是否需要重试
            time.sleep(2)
            # 检查 checkbox 是否被选中（aria-checked 变化或 iframe 尺寸变化）
            try:
                new_rect = _get_viewport_coords(driver, checkbox_iframe)
                # 如果 iframe 尺寸没有变化，可能点击未生效，尝试微调坐标重试
                if abs(new_rect['width'] - viewport_rect['width']) < 5:
                    print("    → 重试: 微调坐标再次点击...")
                    # 尝试偏移一些坐标
                    click_x2 = viewport_rect['x'] + 25
                    click_y2 = viewport_rect['y'] + viewport_rect['height'] / 2 - 2
                    _cdp_click_at(driver, click_x2, click_y2)
                    time.sleep(0.5)
            except Exception:
                pass

            return True

        # 方法2: CDP DOM 遍历查找（包括 closed shadow DOM，必须保留 CDP 穿透）
        doc = driver._cdp().send('DOM.getDocument', {'depth': -1, 'pierce': True})
        root = doc['root']

        def find_hcaptcha_iframe(node):
            if node.get('nodeName', '').lower() == 'iframe':
                attrs = node.get('attributes', [])
                attr_dict = dict(zip(attrs[::2], attrs[1::2])) if attrs else {}
                src = attr_dict.get('src', '')
                widget_id = attr_dict.get('data-hcaptcha-widget-id', '')
                if 'hcaptcha.com' in src or 'hcaptcha' in src or widget_id:
                    return node
            for child in node.get('children', []):
                result = find_hcaptcha_iframe(child)
                if result:
                    return result
            for shadow in node.get('shadowRoots', []):
                for child in shadow.get('children', []):
                    result = find_hcaptcha_iframe(child)
                    if result:
                        return result
            return None

        iframe_node = find_hcaptcha_iframe(root)
        if iframe_node:
            backend_node_id = iframe_node.get('backendNodeId')
            # 使用 DOM.getContentQuads 获取视口坐标（CDP 保留：比 getBoxModel 更准确）
            try:
                quads = driver._cdp().send('DOM.getContentQuads', {'backendNodeId': backend_node_id})
                quad = quads['quads'][0]
                # quad 是 [x1,y1, x2,y2, x3,y3, x4,y4] 四个角的视口坐标
                click_x = quad[0] + 30
                click_y = (quad[1] + quad[5]) / 2
            except Exception:
                # 回退到 getBoxModel
                box_model = driver._cdp().send('DOM.getBoxModel', {'backendNodeId': backend_node_id})
                content = box_model['model']['content']
                click_x = content[0] + 30
                click_y = (content[1] + content[5]) / 2

            print(f"    → CDP(DOM): 找到 hCaptcha iframe, 点击视口坐标 ({click_x:.0f}, {click_y:.0f})")
            _cdp_click_at(driver, click_x, click_y)
            print("    ✅ CDP(DOM): 已模拟点击 hCaptcha checkbox")
            return True

        print("    ⚠️ CDP: 未找到 hCaptcha iframe")
        return False
    except Exception as e:
        print(f"    ⚠️ CDP hCaptcha 点击失败: {e}")
        return False


def _wait_for_turnstile_widget(driver, timeout=15):
    """
    等待 Turnstile 人机验证组件加载完成。
    组件可能延迟渲染，需等待其 iframe 或容器实际出现在 DOM 中。
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 检查 Turnstile 容器
            containers = driver.page.locator(
                'div[data-testid="challenge-widget-container"], '
                '[id*="cf-chl-widget"], .cf-turnstile, [data-sitekey]'
            ).all()
            if containers:
                # 容器存在，再检查内部 iframe 是否加载
                has_iframe = driver.page.evaluate("""() => {
                    var widgets = document.querySelectorAll(
                        '[id*="cf-chl-widget"], .cf-turnstile, [data-sitekey], '
                        '[data-testid="challenge-widget-container"]'
                    );
                    for (var i = 0; i < widgets.length; i++) {
                        // 检查 shadow DOM 中的 iframe
                        if (widgets[i].shadowRoot) {
                            var sf = widgets[i].shadowRoot.querySelector('iframe');
                            if (sf) return true;
                        }
                        // 检查普通子元素中的 iframe
                        var f = widgets[i].querySelector('iframe');
                        if (f) return true;
                    }
                    // 检查隐藏 input 是否已存在
                    var cf = document.querySelector('input[name="cf_challenge_response"]');
                    if (cf) return true;
                    return false;
                }""")
                if has_iframe:
                    print("  ✅ Turnstile 组件已加载")
                    time.sleep(1)  # 额外等待渲染稳定
                    return True
        except Exception:
            pass
        time.sleep(1)

    print("  ℹ️ Turnstile 组件等待超时，继续执行")


def _wait_for_stripe_fields_ready(driver, timeout=15):
    """
    等待 Stripe 输入字段或嵌套 iframe 实际渲染完成。
    弹窗和外层 iframe 可能先出现，但内部字段延迟加载。

    Patchright 版：不依赖「已切入 iframe」的状态，直接枚举 page.frames
    （原生穿透所有嵌套层级）查找 Stripe 字段，或统计弹窗内可见 iframe 数量。
    """
    page = driver.page
    card_inputs_sel = (
        'input[name="cardnumber"], input[name="number"], '
        'input[autocomplete="cc-number"], '
        'input[data-elements-stable-field-name="cardNumber"]'
    )

    def _vis(loc):
        try:
            return loc.is_visible()
        except Exception:
            return False

    start = time.time()
    while time.time() - start < timeout:
        try:
            # 检查1: 主文档或 Stripe frame 内的卡号输入框
            main = page.main_frame
            for fr in [main] + [f for f in page.frames if f is not main]:
                try:
                    url = (fr.url or '').lower()
                    name = (fr.name or '').lower()
                except Exception:
                    url = name = ''
                # 主文档也检查一次；其余仅检查 Stripe 相关 frame
                if fr is not main and not (
                    'stripe' in url or 'stripe' in name
                    or name.startswith('__privatestripeframe')
                    or 'elements-inner' in url):
                    continue
                try:
                    inp = fr.locator(card_inputs_sel).first
                    if inp.count() > 0 and _vis(inp):
                        print("  ✅ Stripe 输入字段已就绪")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue

            # 检查2: 嵌套 iframe（Stripe 每个字段一个 iframe）
            inner_frames = page.locator('[role="dialog"] iframe').all()
            visible_frames = [f for f in inner_frames if _vis(f)]
            if len(visible_frames) >= 2:  # 至少有卡号和有效期两个 iframe
                print(f"  ✅ Stripe 嵌套 iframe 已就绪 ({len(visible_frames)} 个)")
                time.sleep(0.5)
                return True
        except Exception:
            pass
        time.sleep(1)

    print("  ⚠️ Stripe 字段加载等待超时，尝试继续")


def _wait_for_billing_form_ready(driver, timeout=15):
    """
    等待账单地址表单字段渲染完成。
    弹窗出现后，地址表单可能延迟加载。
    账单字段在主文档 dialog 内（非 iframe），直接用 page.locator 定位。
    """
    page = driver.page
    fields_sel = (
        '[role="dialog"] input[name="first_name"], '
        '[role="dialog"] input[name="country"], '
        '[data-testid="address-form"] input[name="first_name"]'
    )
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 查找 first_name 或 country 字段（地址表单的标志性字段）
            fields = page.locator(fields_sel).all()
            for f in fields:
                try:
                    if f.is_visible():
                        print("  ✅ 账单地址表单已加载")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(1)

    print("  ⚠️ 账单地址表单加载等待超时，尝试继续")


def _handle_inline_turnstile(driver, max_wait=120):
    """
    处理页面内嵌的 Turnstile 人机验证
    （不是整页质询，而是表单中嵌入的验证组件，如 "Let us know you are human"）

    策略：
    1. 检测页面中是否存在 Turnstile iframe
    2. 尝试自动点击
    3. 如果失败，提示用户手动点击
    4. 等待验证通过（Turnstile iframe 消失或变为已验证状态）
    """
    # 检查是否存在内嵌 Turnstile
    turnstile_found = False
    try:
        # 方法1: 查找 Cloudflare 注册页特有的验证容器
        containers = driver.page.locator(
            'div[data-testid="challenge-widget-container"], '
            'div.c_v'  # Cloudflare 注册页的验证组件 class
        ).all()
        if containers:
            turnstile_found = True

        # 方法2: 查找包含人机验证提示文本（支持中英文）
        if not turnstile_found:
            body_text = driver.page.inner_text("body", timeout=SHORT_TIMEOUT_MS).lower()
            if ('let us know you are human' in body_text or
                'verify you are human' in body_text or
                '确认您是真人' in body_text or
                '证明你是人类' in body_text or
                '请确认您不是机器人' in body_text):
                turnstile_found = True

        # 方法3: 通过 JS 查找 shadow DOM 中的 Turnstile iframe
        if not turnstile_found:
            has_turnstile = driver.page.evaluate("""() => {
                // 查找 cf_challenge_response 隐藏 input
                var cf = document.querySelector('input[name="cf_challenge_response"]');
                if (cf) return true;
                // 查找 id 包含 cf-chl-widget 的元素
                var widget = document.querySelector('[id*="cf-chl-widget"]');
                if (widget) return true;
                return false;
            }""")
            if has_turnstile:
                turnstile_found = True
    except Exception:
        pass

    if not turnstile_found:
        print("  ℹ️ 未检测到内嵌人机验证，继续")
        return True

    print("  🔒 检测到内嵌 Turnstile 人机验证")

    # 阶段1：优先等待自动通过。Patchright 隐蔽性下 managed 模式的 Turnstile
    # 会在几秒内自动出 token（无需任何点击）。绝不在此之前做 CDP 操作。
    print("  ⏳ 等待 Turnstile 自动通过...")
    for i in range(10):
        time.sleep(3)
        driver.capture_frame()
        if _is_turnstile_solved(driver):
            print("  ✅ 人机验证已自动通过！")
            return True
        if i == 3:
            print("  ⏳ 仍在等待验证结果...")

    # 阶段2：仍未通过（可能是交互式 Turnstile），尝试 Playwright 原生点击（不用 CDP）
    print("  🤖 尝试点击验证框（原生）...")
    if _try_click_turnstile(driver):
        for i in range(7):
            time.sleep(3)
            driver.capture_frame()
            if _is_turnstile_solved(driver):
                print("  ✅ 人机验证已通过！")
                return True

    # CDP 点击未能通过，尝试 2Captcha
    if captcha_solver.is_available():
        print("  🤖 尝试使用 2Captcha 解决内嵌 Turnstile...")
        if captcha_solver.solve_turnstile(driver):
            # 注入 token 后需要触发表单提交或等待页面自动响应
            # 不能用 _is_turnstile_solved 验证，因为 token 是我们自己注入的
            # 需要通过页面行为变化来判断：Turnstile widget 消失、验证容器变化、或页面跳转
            time.sleep(8)
            if _is_turnstile_truly_solved(driver):
                print("  ✅ 2Captcha 解决成功！")
                return True
            else:
                print("  ⚠️ 2Captcha token 已注入但验证未通过，可能 token 已过期")

    # 2Captcha 也失败，提示用户手动操作
    print("")
    print("  " + "=" * 50)
    print("  ⚠️  需要手动完成人机验证！")
    print("  👉 请在浏览器窗口中勾选验证框")
    print(f"  ⏰ 等待时间: 最长 {max_wait} 秒")
    print("  " + "=" * 50)
    print("")

    start = time.time()
    while time.time() - start < max_wait:
        driver.capture_frame()
        if _is_turnstile_solved(driver):
            elapsed = int(time.time() - start)
            print(f"  ✅ 人机验证已通过！(耗时 {elapsed} 秒)")
            return True
        time.sleep(2)

    print("  ❌ 人机验证超时")
    return False


def _is_turnstile_truly_solved(driver):
    """
    通过页面行为变化判断 Turnstile 是否真正通过（不依赖 input value）。
    用于 2Captcha 注入 token 后的验证，避免被自己注入的值欺骗。
    """
    try:
        # 检查1: Turnstile widget 是否显示已验证状态（绿色勾 / 成功样式）
        result = driver.page.evaluate("""() => {
            // 检查 Turnstile 容器是否有 success 状态
            var containers = document.querySelectorAll(
                '[data-testid="challenge-widget-container"], .cf-turnstile, [id*="cf-chl-widget"]'
            );
            for (var i = 0; i < containers.length; i++) {
                var c = containers[i];
                // 检查容器的 data-status 属性
                if (c.getAttribute('data-status') === 'solved' ||
                    c.getAttribute('data-status') === 'success') return true;
                // 检查是否有 success class
                if (c.className && (c.className.indexOf('success') >= 0 ||
                    c.className.indexOf('solved') >= 0)) return true;
            }

            // 检查2: 验证组件是否已消失（Turnstile 通过后通常会隐藏）
            var visibleWidgets = document.querySelectorAll(
                '[data-testid="challenge-widget-container"]:not([style*="display: none"]):not([hidden])'
            );
            // 如果之前有容器但现在都隐藏了，说明验证通过
            var allWidgets = document.querySelectorAll('[data-testid="challenge-widget-container"]');
            if (allWidgets.length > 0 && visibleWidgets.length === 0) return true;

            // 检查3: 按钮是否变为可点击状态（验证通过后提交按钮通常会启用）
            var submitBtn = document.querySelector('button[type="submit"]:not([disabled])');
            if (submitBtn) {
                // 同时确认验证容器存在（避免误判）
                if (allWidgets.length > 0) return true;
            }

            return false;
        }""")
        if result:
            return True
    except Exception:
        pass

    # 检查4: 页面 URL 是否已变化（提交成功后可能跳转）
    try:
        current_url = driver.current_url
        if 'sign-up' not in current_url and 'challenge' not in current_url:
            return True
    except Exception:
        pass

    return False


def _is_turnstile_solved(driver):
    """
    检测内嵌 Turnstile 验证是否已完成

    Cloudflare 注册页面的 Turnstile 结构:
    - 隐藏 input: name="cf_challenge_response" (通过后会被填入 token)
    - 容器: data-testid="challenge-widget-container"
    - iframe 在 closed shadow DOM 中，Selenium 无法直接访问
    """
    try:
        # 方法1: 检查隐藏 input 是否有值（最可靠）
        # Cloudflare 注册页使用 name="cf_challenge_response"
        hidden_inputs = driver.page.locator(
            'input[name="cf_challenge_response"], '
            'input[name="cf-turnstile-response"], '
            'input[name*="turnstile"], '
            'input[name*="challenge_response"]'
        ).all()
        for inp in hidden_inputs:
            value = inp.get_attribute('value') or ''
            if len(value) > 10:  # Turnstile token 很长
                return True

        # 方法2: 通过 JS 检查隐藏 input（可能被 shadow DOM 包裹）
        try:
            result = driver.page.evaluate("""() => {
                // 直接查找所有 id 包含 response 的 input
                var inputs = document.querySelectorAll('input[id$="_response"]');
                for (var i = 0; i < inputs.length; i++) {
                    if (inputs[i].value && inputs[i].value.length > 10) return true;
                }
                // 查找 cf_challenge_response
                var cf = document.querySelector('input[name="cf_challenge_response"]');
                if (cf && cf.value && cf.value.length > 10) return true;
                return false;
            }""")
            if result:
                return True
        except Exception:
            pass

    except Exception:
        pass

    return False


def _detect_account_banned(driver):
    """检测当前页面是否出现 Cloudflare 账号封禁提示。命中则置 driver.account_banned=True 并返回 True。
    典型文案："Access to your user is blocked due to suspected malicious use of Cloudflare services."
    """
    try:
        body = driver.page.inner_text("body", timeout=SHORT_TIMEOUT_MS).lower()
    except Exception:
        return False
    signals = [
        "blocked due to suspected malicious",
        "suspected malicious use of cloudflare",
    ]
    if any(s in body for s in signals):
        driver.account_banned = True
        print("  🚫 检测到账号已被 Cloudflare 封禁（suspected malicious use）")
        return True
    return False


def login_cloudflare(driver, email: str, password: str):
    """
    登录已有的 Cloudflare 账号

    参数:
        driver: 浏览器驱动
        email: 邮箱地址
        password: CF 密码
    返回:
        str | None: 成功时返回 account_id，失败返回 None
    """
    try:
        url = "https://dash.cloudflare.com/login"
        print(f"正在打开登录页面 {url}...")
        driver.get(url)
        time.sleep(3)

        check_and_handle_cf_challenge(driver)
        time.sleep(2)
        dismiss_overdue_dialog(driver)

        # 检测页面状态：登录表单 或 已登录控制台，先到者胜出
        print("检测页面状态...")
        login_selector = 'input[type="email"], input[name="email"], input[id="email"], input[autocomplete="email"]'
        timeout = 120
        start = time.time()
        email_input = None

        while time.time() - start < timeout:
            dismiss_overdue_dialog(driver)
            # 检查是否已在控制台
            account_id = _extract_account_id(driver)
            if account_id:
                print(f"已处于登录状态，Account ID: {account_id}")
                return account_id
            # 检查登录表单是否出现
            try:
                loc = driver.page.locator(login_selector).first
                if loc.is_visible():
                    email_input = loc
                    break
            except Exception:
                pass
            time.sleep(2)

        if email_input is None:
            print(f"超时未检测到登录表单或控制台，当前 URL: {driver.current_url}")
            return None
        email_input.clear()
        type_slowly(email_input, email)
        print(f"已输入邮箱: {email}")
        time.sleep(1)

        # 填写密码
        password_input = driver.page.locator(
            'input[type="password"], input[name="password"], input[id="password"]'
        ).first
        password_input.clear()
        type_slowly(password_input, password)
        print("密码已输入")
        time.sleep(1)

        # 处理可能的 Turnstile 验证
        _wait_for_turnstile_widget(driver, timeout=10)
        _handle_inline_turnstile(driver)
        time.sleep(1)

        # 点击登录按钮
        print("正在点击登录按钮...")
        login_selectors = [
            'button[type="submit"]',
            'button[data-testid="login-submit"]',
        ]
        clicked = False
        for selector in login_selectors:
            try:
                btn = driver.page.locator(selector).first
                if btn.is_visible():
                    _safe_click(btn, session=driver, desc='登录按钮')
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            try:
                btns = driver.page.locator('button').all()
                for btn in btns:
                    text = (btn.inner_text() or '').lower()
                    if 'log in' in text or 'sign in' in text or 'login' in text:
                        _safe_click(btn, session=driver, desc='登录按钮(文本)')
                        clicked = True
                        break
            except Exception:
                pass

        if not clicked:
            print("未找到登录按钮")
            return None

        print("登录表单已提交，等待跳转...")
        time.sleep(5)
        check_and_handle_cf_challenge(driver)
        time.sleep(3)

        # 检查是否登录成功：URL 应包含 account_id
        dismiss_overdue_dialog(driver)
        account_id = _extract_account_id(driver)
        if account_id:
            print(f"登录成功！Account ID: {account_id}")
            return account_id

        # 等待更长时间再试（可能有额外验证步骤）
        print("等待仪表盘加载...")
        for _ in range(6):
            time.sleep(5)
            check_and_handle_cf_challenge(driver)
            dismiss_overdue_dialog(driver)
            account_id = _extract_account_id(driver)
            if account_id:
                print(f"登录成功！Account ID: {account_id}")
                return account_id

        _detect_account_banned(driver)
        print(f"登录后未能获取 account_id，当前 URL: {driver.current_url}")
        return None

    except Exception as e:
        print(f"登录失败: {e}")
        return None


def _extract_account_id(driver):
    """从当前 URL 中提取 Cloudflare account_id"""
    current_url = driver.current_url
    if 'dash.cloudflare.com' in current_url:
        parts = current_url.replace('https://dash.cloudflare.com/', '').split('/')
        if parts and parts[0] and len(parts[0]) == 32:
            return parts[0]
    return None


def navigate_to_ai_credits(driver, account_id):
    """
    导航到 AI Gateway Credits 页面并点击 Top-up credits 按钮

    参数:
        driver: 浏览器驱动
        account_id: Cloudflare 账号 ID
    返回:
        bool: 是否成功点击 Top-up credits 按钮
    """
    credits_url = f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits"
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            print(f"正在导航到 AI Credits 页面 (尝试 {attempt}/{max_retries}): {credits_url}")
            driver.get(credits_url)
            time.sleep(5)
            check_and_handle_cf_challenge(driver)
            time.sleep(3)
            dismiss_overdue_dialog(driver)

            print(f"当前 URL: {driver.current_url}")

            # 等待 "Top-up credits" 按钮出现（页面可能加载很慢）
            topup_btn = driver.page.locator(
                "xpath=//button[.//span[text()='Top-up credits']]"
            ).first
            if not _wait_visible(topup_btn, timeout=120_000):
                print(f"第 {attempt} 次未找到 Top-up credits 按钮，页面可能未加载完成")
                if attempt < max_retries:
                    print("刷新页面重试...")
                    continue
                return False

            print("找到 Top-up credits 按钮，正在点击...")
            _safe_click(topup_btn, session=driver, desc='Top-up credits 按钮')
            time.sleep(2)
            print("已点击 Top-up credits 按钮")
            return True

        except Exception as e:
            print(f"第 {attempt} 次导航到 AI Credits 页面失败: {e}")
            if attempt < max_retries:
                print("刷新页面重试...")
                continue
            return False

    return False


def extract_topup_card_last4(driver):
    """
    从 Top-up 弹窗中提取信用卡后四位

    参数:
        driver: 浏览器驱动
    返回:
        str: 卡片后四位，提取失败返回空字符串
    """
    try:
        # 等待弹窗加载
        price_loc = driver.page.locator("input#price").first
        if not _wait_visible(price_loc, timeout=30_000):
            print("未能定位 Top-up 弹窗 input#price")
            return ''
        time.sleep(2)
        # 通过 input#price 向上找到所属的 dialog（避免匹配到 Cookie 弹窗）
        card_last4 = driver.page.evaluate(r"""() => {
            var priceInput = document.querySelector('input#price');
            if (!priceInput) return '';
            var dialog = priceInput.closest('div[role="dialog"]');
            if (!dialog) return '';
            var text = dialog.textContent || dialog.innerText || '';
            var m = text.match(/[\u2022\u00b7*\s]+(\d{4})/);
            return m ? m[1] : '';
        }""")
        if card_last4:
            print(f"检测到支付卡片后四位: {card_last4}")
            return card_last4
        print("未能从弹窗匹配到卡片后四位")
    except Exception as e:
        print(f"提取卡片后四位失败: {e}")
    return ''


def close_topup_dialog(driver):
    """关闭 Top-up 弹窗"""
    try:
        close_btn = driver.page.locator(
            "div[role='dialog'] button[aria-label='Close']"
        ).first
        _safe_click(close_btn, session=driver, desc='关闭 Top-up 弹窗')
        time.sleep(1)
        print("已关闭 Top-up 弹窗")
        return True
    except Exception:
        # 备用：按 Escape 键关闭
        try:
            driver.page.keyboard.press("Escape")
            time.sleep(1)
            print("已通过 Escape 关闭 Top-up 弹窗")
            return True
        except Exception as e:
            print(f"关闭弹窗失败: {e}")
            return False


def fill_topup_and_confirm(driver, amount=10):
    """
    在 Top-up 弹窗中输入金额并点击确认支付（弹窗需已打开）

    参数:
        driver: 浏览器驱动
        amount: 充值金额（美元），默认 10
    返回:
        (bool, list, str): (是否成功点击, API 响应列表, 使用的卡片后四位)
    """
    card_last4 = extract_topup_card_last4(driver)

    try:
        price_input = driver.page.locator("div[role='dialog'] input#price").first

        # 清空并输入金额（回读校验，确保金额正确写入）
        _safe_fill(price_input, amount, session=driver, verify=True, desc='充值金额')
        print(f"已输入充值金额: ${amount}")
        time.sleep(1)

        # 注入网络拦截器，捕获充值相关的接口响应
        inject_network_interceptor(driver, [
            ['api.stripe.com', 'payment_intents', 'confirm'],
            ['ai-gateway', 'billing', 'topup'],
        ])

        # 等待 "Confirm and pay" 按钮变为可点击（输入金额后 disabled 属性会移除）
        confirm_btn = driver.page.locator(
            "xpath=//div[@role='dialog']//button[.//span[text()='Confirm and pay']]"
        ).first
        _wait_visible(confirm_btn, timeout=30_000)

        print("正在点击 Confirm and pay...")
        _safe_click(confirm_btn, session=driver, desc='Confirm and pay')
        print("已点击 Confirm and pay，等待响应...")
        driver.capture_frame()

        # 收集拦截到的响应
        responses = collect_intercepted_responses(driver, timeout=60)
        return True, responses, card_last4

    except Exception as e:
        print(f"充值弹窗操作失败: {e}")
        return False, None, card_last4


def _extract_pdf_pay_url(pdf_path):
    """从 invoice PDF 中提取 Pay online 链接"""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            if '/Annots' in page:
                annots = page['/Annots']
                for annot in annots:
                    obj = annot.get_object()
                    if obj.get('/A') and obj['/A'].get('/URI'):
                        uri = obj['/A']['/URI']
                        if 'pay' in uri.lower() or 'invoice' in uri.lower() or 'stripe' in uri.lower():
                            return uri
    except Exception as e:
        print(f"解析 PDF 失败: {e}")
    return None


_COUNTRY_NAME_TO_CODE = {
    'united states': 'US', 'united states of america': 'US', 'usa': 'US', 'us': 'US',
    'united kingdom': 'GB', 'uk': 'GB', 'great britain': 'GB',
    'canada': 'CA', 'australia': 'AU', 'germany': 'DE', 'france': 'FR',
    'singapore': 'SG', 'japan': 'JP', 'hong kong': 'HK', 'netherlands': 'NL',
}


def _normalize_country_code(val):
    """把国家值规范化为 2 字母代码（Stripe 下拉 option value 用代码）。
    卡库里可能存全称（如 "UNITED STATES"）。"""
    v = (val or '').strip()
    if not v:
        return 'US'
    if len(v) == 2:
        return v.upper()
    return _COUNTRY_NAME_TO_CODE.get(v.lower(), v.upper())


def _fill_stripe_payment_and_submit(driver, card_info, invoice_id=''):
    """
    在 Stripe 支付页面的 iframe 内填写信用卡信息并点击 Pay

    参数:
        driver: 浏览器驱动
        card_info: dict with number, expiry_month, expiry_year, cvc, country, zip
        invoice_id: invoice 编号（用于日志）
    返回:
        dict: {status, error?}
    """
    try:
        # 定位 Stripe Payment Element iframe（frame_locator 无状态，无需手工切帧）
        stripe_frame = driver.page.frame_locator("div#payment-element iframe")

        # 等待 Card 表单渲染完成（accordion 展开后字段需要一点时间挂载）；
        # 用 wait_for_timeout 同时驱动 Playwright 事件循环
        driver.page.wait_for_timeout(2000)

        # 等待卡号输入框出现并填写
        # 跨域 Stripe 字段：用 press_sequentially 模拟真实输入，触发 Stripe
        # 的格式化/校验事件（fill 直接赋值可能不触发，且卡号会被重新格式化）
        number_input = stripe_frame.locator("input#payment-numberInput")
        # Stripe 输入框有时不满足 Playwright 严格 visible 判定，故 visible 失败后
        # 回退到 attached（存在即尝试交互，后续 click 会自动等待可交互）
        if not _wait_visible(number_input, timeout=60_000):
            try:
                number_input.wait_for(state="attached", timeout=10_000)
            except Exception:
                raise RuntimeError("未找到 Stripe 卡号输入框")
        print(f"  {invoice_id} 已定位 Stripe iframe")
        number_input.click()
        time.sleep(0.3)
        number_input.press_sequentially(str(card_info.get('number', '')), delay=50)
        print(f"  {invoice_id} 已输入卡号")
        time.sleep(0.5)

        # 填写有效期 (MM / YY)
        expiry_input = stripe_frame.locator("input#payment-expiryInput")
        expiry_input.click()
        time.sleep(0.3)
        month = str(card_info.get('expiry_month', '')).zfill(2)
        year = str(card_info.get('expiry_year', ''))
        if len(year) == 4:
            year = year[2:]  # 2026 -> 26
        expiry_input.press_sequentially(f"{month}{year}", delay=50)
        print(f"  {invoice_id} 已输入有效期")
        time.sleep(0.5)

        # 填写 CVC
        cvc_input = stripe_frame.locator("input#payment-cvcInput")
        cvc_input.click()
        time.sleep(0.3)
        cvc_input.press_sequentially(str(card_info.get('cvc', '')), delay=50)
        print(f"  {invoice_id} 已输入 CVC")
        time.sleep(0.5)

        # 选择国家：卡库可能存全称（"UNITED STATES"），Stripe 下拉用代码（"US"）。
        # 先按代码选，失败再按 label（全称），仍失败则跳过（不致命，避免整笔支付失败）。
        country_raw = str(card_info.get('country', '') or 'US').strip()
        country_code = _normalize_country_code(country_raw)
        try:
            country_select = stripe_frame.locator("select#payment-countryInput")
            try:
                country_select.select_option(value=country_code, timeout=SHORT_TIMEOUT_MS)
            except Exception:
                country_select.select_option(label=country_raw.title(), timeout=SHORT_TIMEOUT_MS)
            print(f"  {invoice_id} 已选择国家: {country_code}")
            time.sleep(0.5)
        except Exception as e:
            print(f"  {invoice_id} 国家选择失败(继续): {str(e)[:60]}")

        # 填写 ZIP code
        zip_code = card_info.get('zip', '')
        if zip_code:
            zip_input = stripe_frame.locator("input#payment-postalCodeInput")
            zip_input.click()
            time.sleep(0.3)
            zip_input.press_sequentially(str(zip_code), delay=50)
            print(f"  {invoice_id} 已输入 ZIP: {zip_code}")
            time.sleep(0.5)

        driver.capture_frame()
        time.sleep(1)

        # 注入网络拦截器捕获支付结果
        inject_network_interceptor(driver, [
            ['api.stripe.com', 'confirm'],
            ['stripe.com', 'pay'],
        ])

        # 点击 Pay 按钮（位于主文档，不在 iframe 内）
        pay_btn = driver.page.locator(
            "button[data-testid='hosted-payment-submit-button']"
        ).first
        _wait_visible(pay_btn, timeout=30_000)
        print(f"  {invoice_id} 正在点击 Pay...")
        _safe_click(pay_btn, session=driver, desc='Pay 按钮')
        print(f"  {invoice_id} 已点击 Pay，等待支付结果...")

        # 收集支付响应
        responses = collect_intercepted_responses(driver, timeout=60)
        if responses:
            print(f"  {invoice_id} 收到 {len(responses)} 条支付响应")
            for resp in responses:
                print(f"    [{resp.get('status')}] {resp.get('url', '')[:80]}")

        return {"status": "paid", "responses": responses}

    except Exception as e:
        print(f"  {invoice_id} Stripe 支付填写失败: {e}")
        return {"status": "failed", "error": f"Stripe 支付填写失败: {e}"}


def handle_unpaid_invoices(driver, payment_cards=None, max_invoices=None):
    """
    检查 Credits 页面的 Unpaid invoice，逐个下载 PDF、提取支付链接、
    打开 Stripe 支付页面并支付。每处理完一个 invoice 后重新导航回 credits 页面再查找。

    参数:
        driver: 浏览器驱动
        payment_cards: 支付卡列表（dict: number/expiry_month/expiry_year/cvc/country/zip...）。
                       提供时对每个 invoice 用一张卡填写并提交支付（卡按顺序轮换）；
                       为空时仅打开支付页并展开 Card 表单（不支付）。
        max_invoices: 单次最多处理的 invoice 数（安全阀，防止一次性产生过多扣款）；
                      None 表示不限制。
    返回:
        list: 处理结果列表 [{invoice, status, pay_url, card?, error?}, ...]
              status: paid（已提交支付）/ opened（仅打开未支付）/ failed
    """
    results = []
    download_dir = getattr(driver, '_download_dir', None)
    if not download_dir:
        print("未配置下载目录，跳过 Unpaid invoice 处理")
        return results

    payment_cards = payment_cards or []

    # 记录 credits 页面 URL：每轮用直接导航返回（比 go_back 稳定，避免后续 invoice
    # 因页面历史状态错乱导致下载按钮点击超时）
    credits_url = driver.current_url if 'credits' in (driver.current_url or '') else None

    def _return_to_credits():
        if credits_url:
            driver.get(credits_url)
            time.sleep(3)
        else:
            driver.page.go_back(wait_until="domcontentloaded")
            time.sleep(3)
            if 'credits' not in driver.current_url:
                driver.page.go_back(wait_until="domcontentloaded")
                time.sleep(3)

    processed_ids = set()
    round_num = 0

    while True:
        round_num += 1
        # 安全阀：达到本次处理上限则停止
        if max_invoices is not None and len(processed_ids) >= max_invoices:
            print(f"已达单次处理上限 {max_invoices} 个 invoice，停止")
            break

        # 等待表格加载
        time.sleep(3)

        # 查找 Unpaid 行
        rows = driver.page.locator(
            "xpath=//table//tr[.//span[text()='Unpaid']]"
        ).all()
        if not rows:
            if round_num == 1:
                print("未发现 Unpaid invoice")
            break

        # 找到第一个未处理过的 invoice
        invoice_id = None
        invoice_link = None
        for row in rows:
            try:
                link = row.locator("xpath=.//a[@role='button']").first
                iid = (link.inner_text(timeout=SHORT_TIMEOUT_MS) or '').strip()
                if iid and iid not in processed_ids:
                    invoice_id = iid
                    invoice_link = link
                    break
            except Exception:
                continue

        if not invoice_id:
            print("所有 Unpaid invoice 已处理完毕")
            break

        processed_ids.add(invoice_id)
        total_unpaid = len(rows)
        print(f"[{len(processed_ids)}/{total_unpaid}] 处理 invoice: {invoice_id}")

        try:
            # 点击下载并通过 Playwright 下载事件保存 PDF 到下载目录
            safe_iid = re.sub(r'[^\w.\-]', '_', invoice_id)
            pdf_path = os.path.join(download_dir, f"invoice_{safe_iid}.pdf")
            # 下载链接常在视口外/被遮挡导致 click actionability 超时：先滚动进视口并等可见，
            # 再点击；普通点击失败则用 force 兜底（绕过 actionability 检查）。
            try:
                invoice_link.scroll_into_view_if_needed(timeout=SHORT_TIMEOUT_MS)
            except Exception:
                pass
            _wait_visible(invoice_link, timeout=SHORT_TIMEOUT_MS)
            # 下载按钮是无 href 的 <a role=button>，由 React onClick 触发下载。
            # 依次尝试：普通点击 → JS 直接 click（绕过坐标 actionability，直接触发 onClick）→ force。
            downloaded = False
            for _method in ('normal', 'js', 'force'):
                try:
                    with driver.page.expect_download(timeout=30_000) as download_info:
                        if _method == 'js':
                            invoice_link.evaluate("el => el.click()")
                        else:
                            invoice_link.click(timeout=CLICK_TIMEOUT_MS, force=(_method == 'force'))
                    download_info.value.save_as(pdf_path)
                    print(f"  PDF 已下载: {os.path.basename(pdf_path)} (方式={_method})")
                    downloaded = True
                    break
                except Exception as e:
                    print(f"  {invoice_id} PDF 下载失败(方式={_method}): {str(e)[:90]}")
            if not downloaded:
                results.append({"invoice": invoice_id, "status": "failed", "error": "PDF 下载超时"})
                _return_to_credits()
                continue

            # 从 PDF 提取支付链接
            pay_url = _extract_pdf_pay_url(pdf_path)
            if not pay_url:
                print(f"  {invoice_id} 未找到 Pay online 链接")
                results.append({"invoice": invoice_id, "status": "failed", "error": "未找到支付链接"})
                _return_to_credits()
                continue

            print(f"  找到支付链接: {pay_url}")

            # 在浏览器中打开支付链接
            driver.get(pay_url)
            print(f"  正在等待 {invoice_id} 支付页面加载...")

            # 等待 Stripe 支付页面加载完成（Pay 按钮出现）
            pay_btn = driver.page.locator(
                "button[data-testid='hosted-payment-submit-button']"
            ).first
            if not _wait_visible(pay_btn, timeout=120_000):
                print(f"  {invoice_id} 支付页面加载超时")
                results.append({"invoice": invoice_id, "status": "failed", "pay_url": pay_url, "error": "支付页面加载超时"})
                _return_to_credits()
                continue
            print(f"  {invoice_id} 支付页面已加载完成")

            # 切入 Stripe iframe 点击 Card 支付选项（frame_locator 无状态）
            try:
                stripe_frame = driver.page.frame_locator("div#payment-element iframe")
                card_btn = stripe_frame.locator("div.p-AccordionButton[data-value='card']")
                _wait_visible(card_btn, timeout=30_000)
                _safe_click(card_btn, session=driver, desc='Card 支付选项')
                time.sleep(2)
                print(f"  {invoice_id} 已展开 Card 信用卡支付表单")
            except Exception as e:
                print(f"  {invoice_id} 点击 Card 选项失败: {e}")
                results.append({"invoice": invoice_id, "status": "failed", "pay_url": pay_url, "error": f"点击 Card 选项失败: {e}"})
                _return_to_credits()
                continue

            # 有支付卡则填卡并提交支付；否则仅打开表单（保留原行为）
            if payment_cards:
                card = payment_cards[(len(processed_ids) - 1) % len(payment_cards)]
                card_last4 = str(card.get('number', ''))[-4:]
                print(f"  {invoice_id} 使用卡 ****{card_last4} 填写并支付...")
                pay_result = _fill_stripe_payment_and_submit(driver, card, invoice_id)
                if pay_result.get('status') == 'paid':
                    results.append({"invoice": invoice_id, "status": "paid", "pay_url": pay_url,
                                    "card": card_last4, "responses": pay_result.get('responses')})
                    print(f"  {invoice_id} 支付已提交（卡 ****{card_last4}）")
                else:
                    results.append({"invoice": invoice_id, "status": "failed", "pay_url": pay_url,
                                    "card": card_last4, "error": pay_result.get('error', '支付失败')})
            else:
                results.append({"invoice": invoice_id, "status": "opened", "pay_url": pay_url})

        except Exception as e:
            print(f"  处理 {invoice_id} 异常: {e}")
            results.append({"invoice": invoice_id, "status": "failed", "error": str(e)})

        # 处理完一个 invoice 后，回到 credits 页面重新查找
        print(f"  返回 Credits 页面查找剩余 Unpaid invoices...")
        _return_to_credits()

    return results


def fill_signup_form(driver, email: str, password: str):
    """
    填写 Cloudflare 注册表单
    访问 https://dash.cloudflare.com/sign-up 并自动完成注册

    参数:
        driver: 浏览器驱动
        email: 邮箱地址
        password: 密码
    返回:
        bool: 是否成功填写并提交
    """
    try:
        url = "https://dash.cloudflare.com/sign-up"
        print(f"🌐 正在打开 {url}...")
        driver.get(url)
        time.sleep(3)

        # 处理 Cloudflare 质询
        check_and_handle_cf_challenge(driver)
        time.sleep(2)

        print(f"DEBUG: 当前页面标题: {driver.title}")
        print(f"DEBUG: 当前页面URL: {driver.current_url}")

        # 等待邮箱输入框出现
        print("📧 等待邮箱输入框...")
        email_input = driver.page.locator(
            'input[type="email"], input[name="email"], input[id="email"], input[autocomplete="email"]'
        ).first
        if not _wait_visible(email_input):
            raise RuntimeError("邮箱输入框未出现")
        email_input.clear()
        type_slowly(email_input, email)
        print(f"✅ 已输入邮箱: {email}")
        time.sleep(1)

        # 填写密码
        print("🔑 正在填写密码...")
        password_input = driver.page.locator(
            'input[type="password"], input[name="password"], input[id="password"]'
        ).first
        password_input.clear()
        type_slowly(password_input, password)
        print("✅ 密码已输入")
        time.sleep(1)

        # 勾选条款复选框（如果存在）
        try:
            terms_checkbox = driver.page.locator(
                'input[type="checkbox"][name*="terms"], input[type="checkbox"][id*="terms"], '
                'input[type="checkbox"][name*="agree"], input[type="checkbox"][id*="agree"]'
            ).first
            if not terms_checkbox.is_checked(timeout=SHORT_TIMEOUT_MS):
                _safe_click(terms_checkbox, session=driver, desc='服务条款复选框')
                print("✅ 已勾选服务条款")
                time.sleep(0.5)
        except Exception:
            # 尝试通过 label 点击
            try:
                labels = driver.page.locator('label').all()
                for label in labels:
                    text = (label.inner_text() or '').lower()
                    if 'agree' in text or 'terms' in text or 'policy' in text:
                        _safe_click(label, session=driver, desc='服务条款label')
                        print("✅ 已勾选服务条款 (通过 label)")
                        break
            except Exception:
                print("  ℹ️ 未找到条款复选框（可能不需要）")

        # 处理注册页面内嵌的 Turnstile 人机验证（"Let us know you are human"）
        # Turnstile 组件可能延迟加载，需等待其 iframe 或容器实际出现
        print("🔒 等待人机验证组件加载...")
        _wait_for_turnstile_widget(driver, timeout=15)
        print("🔒 检查注册页面内嵌的人机验证...")
        _handle_inline_turnstile(driver)
        time.sleep(2)

        # 点击注册按钮
        print("🔘 正在点击注册按钮...")
        time.sleep(1)

        signup_selectors = [
            'button[type="submit"]',
            'button[data-testid="sign-up-submit"]',
        ]

        clicked = False
        for selector in signup_selectors:
            try:
                btn = driver.page.locator(selector).first
                if btn.is_visible():
                    _safe_click(btn, session=driver, desc='注册按钮')
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # 按文本内容查找按钮
            try:
                btns = driver.page.locator('button').all()
                for btn in btns:
                    text = (btn.inner_text() or '').lower()
                    if 'sign up' in text or 'create' in text or 'register' in text:
                        _safe_click(btn, session=driver, desc='注册按钮(文本)')
                        clicked = True
                        break
            except Exception:
                pass

        if not clicked:
            print("❌ 未找到注册按钮")
            return False

        print("✅ 注册表单已提交")
        time.sleep(3)

        # 提交后处理可能的质询
        check_and_handle_cf_challenge(driver)

        return True

    except Exception as e:
        print(f"❌ 填写注册表单失败: {e}")
        return False


def handle_email_verification(driver, verification_data):
    """
    处理 Cloudflare 邮箱验证
    verification_data 可以是链接（URL）或验证码（数字字符串）

    参数:
        driver: 浏览器驱动
        verification_data: 验证链接或验证码
    返回:
        bool: 是否验证成功
    """
    if not verification_data:
        print("❌ 未提供验证数据")
        return False

    try:
        # 如果是 URL，直接访问
        if verification_data.startswith('http'):
            print(f"🔗 正在打开验证链接...")
            driver.get(verification_data)
            time.sleep(5)

            # 处理验证页面的 CF 质询
            check_and_handle_cf_challenge(driver)
            time.sleep(3)

            print("✅ 验证链接已打开")
            return True

        # 如果是验证码，尝试输入
        else:
            print(f"🔢 正在输入验证码: {verification_data}")
            try:
                code_input = driver.page.locator(
                    'input[name="code"], input[type="text"][maxlength="6"], '
                    'input[autocomplete="one-time-code"]'
                ).first
                if not _wait_visible(code_input, timeout=30_000):
                    raise RuntimeError("验证码输入框未出现")
                code_input.clear()
                type_slowly(code_input, verification_data)
                time.sleep(1)

                # 提交验证码
                try:
                    submit_btn = driver.page.locator('button[type="submit"]').first
                    _safe_click(submit_btn, session=driver, desc='验证码提交')
                except Exception:
                    code_input.press("Enter")

                time.sleep(3)
                return True
            except Exception as e:
                print(f"❌ 输入验证码失败: {e}")
                return False

    except Exception as e:
        print(f"❌ 邮箱验证失败: {e}")
        return False


def navigate_to_billing(driver):
    """
    导航至 Cloudflare 管理账户 > 账单页面

    参数:
        driver: 浏览器驱动
    返回:
        bool: 是否成功导航到账单页面
    """
    try:
        # 等待仪表盘加载
        print("⏳ 等待 Cloudflare 仪表盘加载...")
        time.sleep(5)
        check_and_handle_cf_challenge(driver)
        time.sleep(3)

        current_url = driver.current_url
        print(f"📍 当前 URL: {current_url}")

        # 尝试从 URL 中提取 account ID
        # URL 格式: https://dash.cloudflare.com/<account_id>/...
        account_id = None
        if 'dash.cloudflare.com' in current_url:
            parts = current_url.replace('https://dash.cloudflare.com/', '').split('/')
            if parts and parts[0] and len(parts[0]) == 32:
                account_id = parts[0]

        # 方法1: 通过 URL 直接导航到账单页面
        if account_id:
            billing_url = f"https://dash.cloudflare.com/{account_id}/billing"
            print(f"🌐 直接导航到账单页面: {billing_url}")
            driver.get(billing_url)
            time.sleep(5)
            check_and_handle_cf_challenge(driver)
            if 'billing' in driver.current_url:
                print("✅ 成功导航到账单页面")
                return True

        # 方法2: 通过 UI 点击导航
        print("🔍 尝试通过 UI 导航到账单页面...")

        # 查找 "Manage Account" 或账户菜单
        manage_selectors = [
            '//a[contains(text(), "Manage Account")]',
            '//a[contains(text(), "manage account")]',
            '//span[contains(text(), "Manage Account")]',
            '//a[contains(@href, "billing")]',
            '//div[contains(text(), "Manage Account")]',
        ]

        for xpath in manage_selectors:
            try:
                el = driver.page.locator("xpath=" + xpath).first
                if el.is_visible():
                    _safe_click(el, session=driver, desc='Manage Account')
                    print(f"  🔘 点击了: {el.inner_text()}")
                    time.sleep(3)
                    break
            except Exception:
                continue

        # 查找 Billing 链接
        billing_selectors = [
            '//a[contains(text(), "Billing")]',
            '//a[contains(@href, "/billing")]',
            '//span[contains(text(), "Billing")]',
            '//div[contains(text(), "Billing")]',
        ]

        for xpath in billing_selectors:
            try:
                el = driver.page.locator("xpath=" + xpath).first
                if el.is_visible():
                    _safe_click(el, session=driver, desc='Billing 链接')
                    print(f"  🔘 点击了账单链接: {el.inner_text()}")
                    time.sleep(3)
                    check_and_handle_cf_challenge(driver)
                    if 'billing' in driver.current_url:
                        print("✅ 成功导航到账单页面")
                        return True
                    break
            except Exception:
                continue

        # 方法3: 尝试侧边栏导航
        print("🔍 尝试侧边栏导航...")
        try:
            sidebar_links = driver.page.locator('a[href*="billing"], nav a').all()
            for link in sidebar_links:
                href = link.get_attribute('href') or ''
                text = (link.inner_text() or '').lower()
                if 'billing' in href or 'billing' in text:
                    _safe_click(link, session=driver, desc='侧边栏账单链接')
                    print("  🔘 点击了侧边栏的账单链接")
                    time.sleep(3)
                    if 'billing' in driver.current_url:
                        print("✅ 成功导航到账单页面")
                        return True
                    break
        except Exception:
            pass

        # 方法4: 先回到账户首页再导航
        print("🔍 尝试从账户首页导航...")
        try:
            driver.get("https://dash.cloudflare.com")
            time.sleep(5)
            check_and_handle_cf_challenge(driver)

            # 从当前 URL 提取 account ID
            current = driver.current_url
            parts = current.replace('https://dash.cloudflare.com/', '').split('/')
            if parts and parts[0] and len(parts[0]) >= 20:
                account_id = parts[0]
                billing_url = f"https://dash.cloudflare.com/{account_id}/billing"
                print(f"  🌐 找到 account ID，导航到: {billing_url}")
                driver.get(billing_url)
                time.sleep(5)
                check_and_handle_cf_challenge(driver)
                if 'billing' in driver.current_url:
                    print("✅ 成功导航到账单页面")
                    return True
        except Exception:
            pass

        print("⚠️ 无法确认已导航到账单页面")
        return False

    except Exception as e:
        print(f"❌ 导航到账单页面失败: {e}")
        return False


def get_bound_card_count(driver):
    """
    检测 Cloudflare 账单页面已绑定的信用卡数量

    Cloudflare 账单页面 Billing method 区域结构:
    - 无卡时显示: "No payment method on file" + "Add" 按钮
    - 有卡时显示: 卡号末四位 (如 "•••• 4242") + 卡品牌图标

    参数:
        driver: 浏览器驱动
    返回:
        int: 已绑定的信用卡数量
    """
    try:
        time.sleep(2)

        # 方法1: 检查是否显示 "No payment method on file"（0 张卡）
        try:
            no_payment = driver.page.locator(
                'xpath=//*[contains(text(), "No payment method on file")]'
            ).all()
            visible_no_payment = [el for el in no_payment if el.is_visible()]
            if visible_no_payment:
                print("  💳 检测到 'No payment method on file'，当前无绑定信用卡")
                return 0
        except Exception:
            pass

        # 方法2: 查找 Billing method 区域内的卡号末四位标识
        card_elements = driver.page.locator(
            'xpath=//*[contains(text(), "••••") or contains(text(), "****")]'
        ).all()
        visible_cards = [el for el in card_elements if el.is_visible()]
        if visible_cards:
            count = len(visible_cards)
            print(f"  💳 检测到 {count} 张已绑定的信用卡 (通过卡号标识)")
            return count

        # 方法3: 通过 JS 在 Billing method 区域计数
        count = driver.page.evaluate("""() => {
            // 查找包含 "Billing method" 文本的 section
            var sections = document.querySelectorAll('div, section');
            for (var i = 0; i < sections.length; i++) {
                var header = sections[i].querySelector('span');
                if (header && header.textContent.trim() === 'Billing method') {
                    // 在此区域内查找卡号标识或卡品牌元素
                    var cards = sections[i].querySelectorAll(
                        '[class*="payment"], [class*="card"], [data-testid*="payment"]'
                    );
                    // 过滤掉 "Add" 按钮等非卡片元素，计算实际卡片行数
                    var cardCount = 0;
                    var texts = sections[i].innerText || '';
                    var matches = texts.match(/[•\\*]{4}\\s*\\d{4}/g);
                    if (matches) cardCount = matches.length;
                    return cardCount;
                }
            }
            return -1;  // 未找到 Billing method 区域
        }""")
        if count > 0:
            print(f"  💳 检测到 {count} 张已绑定的信用卡 (通过 Billing method 区域)")
            return count
        if count == 0:
            print("  💳 Billing method 区域未检测到已绑定的信用卡")
            return 0

        print("  💳 未检测到已绑定的信用卡")
        return 0

    except Exception as e:
        print(f"  ⚠️ 检测已绑定信用卡数量失败: {e}")
        return 0


def _find_and_click_add_button(driver):
    """
    在 Cloudflare 账单页面找到 Billing method 区域的 "+ Add" 按钮并点击

    Billing method 区域的 Add 按钮结构:
    <button data-kumo-component="Button" ...>
        <span class="contents">
            <svg ...>+号图标</svg>
            <span>Add</span>
        </span>
    </button>

    返回:
        bool: 是否成功点击
    """
    try:
        # 精确定位: 在 "Billing method" 标题旁边的 "+ Add" 按钮
        # 先找到包含 "Billing method" 文本的容器
        add_handle = driver.page.evaluate_handle("""() => {
            // 查找 "Billing method" 标题所在的卡片容器
            var spans = document.querySelectorAll('span');
            for (var i = 0; i < spans.length; i++) {
                if (spans[i].textContent.trim() === 'Billing method') {
                    // 找到包含此标题的卡片 (ring rounded-lg 的父容器)
                    var card = spans[i].closest('div.w-full');
                    if (!card) card = spans[i].parentElement.parentElement;
                    if (card) {
                        // 在此卡片内查找带 "Add" 文本的按钮
                        var buttons = card.querySelectorAll('button[data-kumo-component="Button"]');
                        for (var j = 0; j < buttons.length; j++) {
                            var btnText = buttons[j].textContent.trim();
                            if (btnText === 'Add' || btnText.indexOf('Add') >= 0) {
                                return buttons[j];
                            }
                        }
                    }
                }
            }
            return null;
        }""")

        add_btn = add_handle.as_element() if add_handle else None
        if add_btn:
            add_btn.scroll_into_view_if_needed()
            time.sleep(0.5)
            _safe_click(add_btn, session=driver, desc="Billing method Add 按钮")
            print("  🔘 点击了 Billing method 区域的 'Add' 按钮")
            return True

    except Exception as e:
        print(f"  ⚠️ JS 方式查找 Add 按钮失败: {e}")

    # 回退: 通过 XPath 查找
    fallback_xpaths = [
        '//span[text()="Billing method"]/ancestor::div[contains(@class, "w-full")]//button[.//span[text()="Add"]]',
        '//button[.//span[text()="Add"]]',
    ]
    for xpath in fallback_xpaths:
        try:
            btns = driver.page.locator("xpath=" + xpath).all()
            for btn in btns:
                if btn.is_visible():
                    btn.scroll_into_view_if_needed()
                    time.sleep(0.5)
                    _safe_click(btn, session=driver, desc="Add 按钮(xpath)")
                    print(f"  🔘 点击了: {(btn.inner_text() or '').strip()}")
                    return True
        except Exception:
            continue

    return False


def _wait_for_payment_dialog(driver, timeout=30):
    """
    等待 "Add a payment method" 弹窗出现

    弹窗特征: role="dialog" 且包含 "Add a payment method" 标题
    内部包含 Stripe iframe (data-test-id="credit-card-form")
    和账单地址表单 (data-testid="address-form")

    返回:
        bool: 弹窗是否已出现
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            dialogs = driver.page.locator('[role="dialog"]').all()
            for dialog in dialogs:
                try:
                    if dialog.is_visible():
                        text = (dialog.inner_text() or '').lower()
                        if 'add a payment method' in text or 'payment' in text:
                            print("  ✅ 检测到 Add a payment method 弹窗")
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(1)

    print("  ❌ 等待 payment 弹窗超时")
    return False


def _wait_for_stripe_iframe(driver, timeout=60):
    """
    等待 Stripe 信用卡表单外层 iframe 出现/就绪。

    Stripe 表单位于:
    div[data-test-id="credit-card-form"] > .StripeElement > .__PrivateStripeElement > iframe

    Patchright 版角色变化：不再返回供 switch_to 用的 iframe 元素——实际填卡的
    _fill_stripe_payment_element 会自行从 page.frames 定位 Stripe frame。本函数
    仅负责「等待外层 Stripe iframe 出现」并保留原 4 策略的判定意图，返回 bool。

    返回:
        bool: Stripe iframe 是否已就绪
    """
    page = driver.page
    start = time.time()
    debug_logged = False

    def _attrs(iframe):
        try:
            title = (iframe.get_attribute('title') or '')
        except Exception:
            title = ''
        try:
            name = (iframe.get_attribute('name') or '')
        except Exception:
            name = ''
        try:
            src = (iframe.get_attribute('src') or '')
        except Exception:
            src = ''
        try:
            visible = iframe.is_visible()
        except Exception:
            visible = False
        return title, name, src, visible

    while time.time() - start < timeout:
        try:
            # 弹窗 dialog 内的所有 iframe
            dialog_iframes = page.locator('[role="dialog"] iframe').all()

            # 每 10 秒输出一次调试信息
            elapsed = int(time.time() - start)
            if elapsed > 0 and elapsed % 10 == 0 and not debug_logged:
                debug_logged = True
                print(f"  🔍 DEBUG: 弹窗内找到 {len(dialog_iframes)} 个 iframe:")
                for f in dialog_iframes:
                    title, name, src, visible = _attrs(f)
                    print(f"    - title='{title}' name='{name}' visible={visible} src='{src[:80]}...'")
            elif elapsed % 10 != 0:
                debug_logged = False

            # 策略1: 精确匹配 data-test-id="credit-card-form" 内的 iframe
            for iframe in page.locator('[data-test-id="credit-card-form"] iframe').all():
                title, name, src, visible = _attrs(iframe)
                if visible:
                    print(f"  ✅ 找到 credit-card-form 内的 iframe (title='{title}')")
                    return True

            # 策略2: 按 title 属性匹配
            for iframe in dialog_iframes:
                title, name, src, visible = _attrs(iframe)
                if not visible:
                    continue
                if 'secure payment' in title.lower() or 'payment input' in title.lower():
                    print("  ✅ 找到 Stripe payment iframe (title 匹配)")
                    return True

            # 策略3: 按 src 属性匹配 (stripe.com 且是 payment 类型)
            for iframe in dialog_iframes:
                title, name, src, visible = _attrs(iframe)
                if not visible:
                    continue
                src_l = src.lower()
                name_l = name.lower()
                if 'stripe.com' in src_l and ('payment' in src_l or 'elements-inner' in src_l):
                    # 排除 express checkout iframe
                    if 'express' not in src_l and 'express' not in name_l:
                        print("  ✅ 找到 Stripe iframe (src 匹配)")
                        return True

            # 策略4: 按 iframe name 匹配 (__privateStripeFrame)
            for iframe in dialog_iframes:
                title, name, src, visible = _attrs(iframe)
                if not visible:
                    continue
                if name.startswith('__privateStripeFrame') and 'express' not in src.lower():
                    print(f"  ✅ 找到 Stripe iframe (name='{name}')")
                    return True

        except Exception as e:
            print(f"  ⚠️ 查找 iframe 异常: {e}")

        time.sleep(2)

    # 超时，输出最终的 iframe 调试信息
    print("  ❌ 等待 Stripe iframe 超时 (60秒)")
    try:
        all_iframes = page.locator('[role="dialog"] iframe').all()
        print(f"  🔍 最终状态: 弹窗内共 {len(all_iframes)} 个 iframe:")
        for f in all_iframes:
            title, name, src, visible = _attrs(f)
            print(f"    - title='{title}' name='{name}' visible={visible}")
            print(f"      src={src[:100]}")
    except Exception:
        pass

    return False


def _fill_billing_address_in_dialog(driver, card_info):
    """
    在 "Add a payment method" 弹窗中填写账单地址

    弹窗中的地址表单字段 (直接在主文档 DOM 中，不在 iframe 内):
    - input[name="first_name"] - First name
    - input[name="last_name"] - Last name
    - input[name="country"] (combobox) - Country
    - input[name="address"] - Address line 1
    - input[name="address2"] - Address line 2 (optional)
    - input[name="city"] - City
    - input[name="state"] - State
    - input[name="zipcode"] - ZIP code
    - input[name="company"] - Organization name (optional)

    card_info 字段:
    - first_name, last_name, country, address, address2, city, state, zip, company
    """
    page = driver.page
    time.sleep(1)

    def fill_input(name_attr, value, label=""):
        if not value:
            return False
        selectors = [
            f'[data-testid="address-form"] input[name="{name_attr}"]',
            f'[role="dialog"] input[name="{name_attr}"]',
            f'input[name="{name_attr}"]',
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() == 0 or not el.is_visible():
                    continue
                # 账单文本字段在主文档 dialog（非跨域 Stripe iframe），用 _safe_fill 回读校验
                _safe_fill(el, value, session=driver, verify=True, desc=label or name_attr)
                print(f"  ✅ 填写 {label or name_attr}: {value}")
                time.sleep(0.3)
                return True
            except Exception:
                continue
        print(f"  ❌ 未找到 {label or name_attr} 输入框")
        return False

    # First name / Last name
    fill_input('first_name', card_info.get('first_name', ''), 'First name')
    fill_input('last_name', card_info.get('last_name', ''), 'Last name')

    # Country (combobox: 需要特殊处理下拉选择)
    country = card_info.get('country', '')
    if country:
        try:
            country_input = None
            for sel in [
                '[data-testid="address-form"] input[name="country"]',
                '[role="dialog"] input[name="country"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible():
                        country_input = el
                        break
                except Exception:
                    continue

            if country_input is not None:
                country_input.click()
                time.sleep(0.3)
                # 清除已有内容：用 Control+a + Backspace（Playwright press 组合键不会
                # 像 Windows 下 send_keys(CONTROL+'a') 那样泄漏字母 'a'）
                country_input.press("Control+a")
                time.sleep(0.1)
                country_input.press("Backspace")
                time.sleep(0.3)
                # 输入国家名称
                country_input.press_sequentially(str(country), delay=50)
                time.sleep(1.5)
                # 从下拉列表中找到精确匹配的选项并点击
                exact_matched = False
                try:
                    options = page.locator('[role="option"], [role="listbox"] li, ul[id] li').all()
                    for opt in options:
                        try:
                            opt_text = (opt.inner_text() or '').strip()
                        except Exception:
                            continue
                        if opt_text.lower() == country.lower():
                            opt.click()
                            exact_matched = True
                            break
                except Exception:
                    pass
                if not exact_matched:
                    # 回退：选择第一项
                    country_input.press("ArrowDown")
                    time.sleep(0.3)
                    country_input.press("Enter")
                print(f"  ✅ 填写 Country: {country}")
                time.sleep(0.5)
            else:
                print("  ❌ 未找到 Country 输入框")
        except Exception as e:
            print(f"  ⚠️ 填写 Country 失败: {e}")

    # Address
    fill_input('address', card_info.get('address', ''), 'Address line 1')
    fill_input('address2', card_info.get('address2', ''), 'Address line 2')
    fill_input('city', card_info.get('city', ''), 'City')
    # State: 如果是缩写（如 MD），转为全称（Maryland）
    state_val = card_info.get('state', '')
    if state_val and state_val.upper() in US_STATE_ABBR:
        state_full = US_STATE_ABBR[state_val.upper()]
        print(f"  📍 State 缩写 '{state_val}' → '{state_full}'")
        state_val = state_full
    fill_input('state', state_val, 'State')
    fill_input('zipcode', card_info.get('zip', ''), 'ZIP code')
    fill_input('company', card_info.get('company', ''), 'Organization')


def add_credit_card(driver, card_info):
    """
    在 Cloudflare 账单页面添加信用卡

    流程:
    1. 点击 Billing method 区域的 "+ Add" 按钮
    2. 等待 "Add a payment method" 弹窗出现
    3. 在 Stripe iframe 中填写卡号/有效期/CVC
    4. 在弹窗主文档中填写账单地址
    5. 处理弹窗内 Turnstile 人机验证（如果出现）
    6. 点击 "Add payment method" 提交按钮

    参数:
        driver: 浏览器驱动
        card_info: 信用卡信息字典
    返回:
        tuple[bool, str]: (是否成功添加, 错误原因字符串)
    """
    try:
        print("\n" + "=" * 50)
        print("💳 开始添加信用卡")
        print("=" * 50)

        # 1. 点击 "+ Add" 按钮
        print("🔍 查找添加付款方式按钮...")
        time.sleep(2)

        if not _find_and_click_add_button(driver):
            print("  ❌ 未找到添加付款方式按钮")
            return False, "[操作失败] 未找到添加付款方式按钮"

        time.sleep(3)

        # 2. 等待弹窗出现
        print("⏳ 等待 Add a payment method 弹窗...")
        if not _wait_for_payment_dialog(driver):
            return False, "[操作失败] 支付弹窗未出现"

        # 3. 等待 Stripe iframe 加载
        print("⏳ 等待 Stripe 信用卡表单加载...")
        stripe_ready = _wait_for_stripe_iframe(driver)
        if not stripe_ready:
            return False, "[操作失败] Stripe表单未加载"

        time.sleep(2)

        # 4. 填写信用卡信息
        # Patchright 版：_fill_stripe_payment_element 自行从 page.frames 定位 Stripe
        # frame（frame_locator/page.frames 无状态、原生穿透跨域嵌套），无需先 switch_to.frame。
        print("💳 正在填写信用卡信息 (Stripe iframe)...")
        time.sleep(1)

        # 等待 Stripe 内部组件（输入字段/嵌套 iframe）实际渲染完成
        # 弹窗和外层 iframe 出现后，内部字段可能仍在加载中
        _wait_for_stripe_fields_ready(driver)

        # Stripe Payment Element 使用统一的表单，字段可能在嵌套 iframe 中
        card_filled = _fill_stripe_payment_element(driver, card_info)
        driver.capture_frame()

        if not card_filled:
            print("  ❌ 填写信用卡信息失败")
            return False, "[操作失败] 填写信用卡信息失败"

        time.sleep(1)

        # 5. 填写账单地址（在弹窗主文档中）
        # 等待地址表单字段渲染完成
        print("🏠 等待账单地址表单加载...")
        _wait_for_billing_form_ready(driver)
        print("🏠 正在填写账单地址...")
        _fill_billing_address_in_dialog(driver, card_info)

        time.sleep(2)

        # 5.5 处理弹窗内的 Turnstile 验证（"Let us know you're human"）
        # 同时检查卡片错误，如果卡信息有误则直接跳过
        print("🔒 检查弹窗内是否有 Turnstile 验证...")
        if not _handle_dialog_turnstile(driver):
            # 卡片错误导致的 False，或 Turnstile 超时；先检测具体原因
            _card_err = _check_dialog_card_error(driver)
            _close_payment_dialog(driver)
            return False, (_card_err if _card_err else "[验证超时] Turnstile人机验证超时")

        time.sleep(1)

        # 6. 清空旧错误日志（确保只检测本次提交产生的错误）
        # Patchright 版：错误检测已改事件驱动（page.on("console") → driver.console_errors），
        # 清空累积错误列表，避免跨重试的旧错误导致误报。
        try:
            driver.console_errors.clear()
        except Exception:
            pass

        # 7. 点击 "Add payment method" 提交按钮
        print("🔘 查找提交按钮...")
        submitted = False

        # 精确匹配弹窗中的 "Add payment method" 按钮
        submit_btn = _find_payment_submit_button(driver)

        if not submit_btn:
            print("  ❌ 未找到提交按钮")
            return False, "[操作失败] 未找到提交按钮"

        # 先滚动到按钮可见
        try:
            submit_btn.scroll_into_view_if_needed()
        except Exception:
            pass
        time.sleep(0.5)

        # 优先使用原生点击（更好地触发 React/Stripe 事件），失败回退 JS 点击
        try:
            submit_btn.click()
            print("  🔘 已点击 'Add payment method' 按钮 (原生点击)")
            submitted = True
        except Exception as e1:
            print(f"  ⚠️ 原生点击失败: {e1}, 尝试 JS 点击...")
            try:
                submit_btn.evaluate("el => el.click()")
                print("  🔘 已点击 'Add payment method' 按钮 (JS 点击)")
                submitted = True
            except Exception as e2:
                print(f"  ⚠️ JS 点击也失败: {e2}")

        if not submitted:
            print("  ❌ 点击提交按钮失败")
            return False, "[浏览器中断] 点击提交按钮失败"

        driver.capture_frame()

        # 8. 等待提交结果（含人机验证检测）
        return _wait_for_payment_submit_result(driver)

    except Exception as e:
        print(f"❌ 添加信用卡失败: {e}")
        return False, f"[浏览器中断] {str(e)[:150]}"


def _handle_dialog_turnstile(driver, max_wait=120):
    """
    处理 Add payment method 弹窗内的 Turnstile 验证
    弹窗中可能出现 "Let us know you're human" + Turnstile checkbox
    需要在点击提交按钮之前完成验证

    注意: 卡片错误可能和 Turnstile 同时出现，优先检测卡片错误
    """
    # 优先检查是否已有卡片错误（卡信息有误时过验证码也没用）
    card_error = _check_dialog_card_error(driver)
    if card_error:
        print(f"  ❌ 检测到卡片错误，跳过 Turnstile: {card_error}")
        return False

    # 检查弹窗内是否存在 Turnstile
    has_turnstile = False
    try:
        dialog = driver.page.locator('[role="dialog"]').first
        if not dialog.is_visible():
            return True

        dialog_text = dialog.inner_text().lower()
        if ("let us know you" in dialog_text or
            "verify you are human" in dialog_text or
            "captcha is required" in dialog_text or
            '确认您是真人' in dialog_text or
            '证明你是人类' in dialog_text):
            has_turnstile = True

        # 也检查弹窗内是否有 Turnstile iframe 或容器
        if not has_turnstile:
            turnstile_els = dialog.locator(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '[data-testid="challenge-widget-container"], '
                'iframe[title*="challenge"], '
                'iframe[title*="Turnstile"], '
                '[id*="cf-chl-widget"]').all()
            if any(el.is_visible() for el in turnstile_els):
                has_turnstile = True
    except Exception:
        return True

    if not has_turnstile:
        print("  ℹ️ 弹窗内无 Turnstile 验证，继续")
        return True

    print("  🔒 检测到弹窗内 Turnstile 验证，开始处理...")

    # 尝试 CDP 点击 Turnstile checkbox
    print("  🤖 尝试自动点击验证框...")
    _try_click_turnstile(driver)

    # 等待验证完成（轮询检查）
    print("  ⏳ 等待 Turnstile 后台验证...")
    for i in range(10):
        time.sleep(3)
        driver.capture_frame()
        if _is_dialog_turnstile_solved(driver):
            print("  ✅ 弹窗内 Turnstile 验证已通过！")
            return True
        if i == 3:
            print("  ⏳ 仍在等待验证结果...")

    # CDP 未通过，尝试 2Captcha
    if captcha_solver.is_available():
        print("  🤖 尝试使用 2Captcha 解决弹窗内 Turnstile...")
        if captcha_solver.solve_turnstile(driver):
            time.sleep(8)
            if _is_turnstile_truly_solved(driver):
                print("  ✅ 2Captcha 解决成功！")
                return True
            else:
                print("  ⚠️ 2Captcha token 已注入但验证未通过，可能 token 已过期")

    # 提示用户手动操作
    print("")
    print("  " + "=" * 50)
    print("  ⚠️  需要手动完成弹窗内人机验证！")
    print("  👉 请在浏览器弹窗中勾选 Turnstile 验证框")
    print(f"  ⏰ 等待时间: 最长 {max_wait} 秒")
    print("  " + "=" * 50)
    print("")

    start = time.time()
    while time.time() - start < max_wait:
        driver.capture_frame()
        if _is_dialog_turnstile_solved(driver):
            elapsed = int(time.time() - start)
            print(f"  ✅ 弹窗内 Turnstile 验证已通过！(耗时 {elapsed} 秒)")
            return True
        time.sleep(2)

    print("  ⚠️ 弹窗内 Turnstile 验证超时，尝试继续提交...")
    return False


def _is_dialog_turnstile_solved(driver):
    """检测弹窗内 Turnstile 验证是否已完成"""
    try:
        # 方法1: 检查隐藏 input 是否有 token 值
        hidden_inputs = driver.page.locator(
            'input[name="cf_challenge_response"], '
            'input[name="cf-turnstile-response"], '
            'input[name*="turnstile"], '
            'input[name*="challenge_response"]'
        ).all()
        for inp in hidden_inputs:
            value = inp.get_attribute('value') or ''
            if len(value) > 10:
                return True

        # 方法2: 通过 JS 查找（包括弹窗内部）
        try:
            result = driver.page.evaluate("""() => {
                var dialog = document.querySelector('[role="dialog"]');
                if (!dialog) return false;
                // 弹窗内查找 token input
                var inputs = dialog.querySelectorAll('input[type="hidden"]');
                for (var i = 0; i < inputs.length; i++) {
                    var name = inputs[i].name || '';
                    if ((name.indexOf('turnstile') >= 0 || name.indexOf('challenge') >= 0)
                        && inputs[i].value && inputs[i].value.length > 10) {
                        return true;
                    }
                }
                // 全局查找
                var cf = document.querySelector('input[name="cf-turnstile-response"]');
                if (cf && cf.value && cf.value.length > 10) return true;
                var cf2 = document.querySelector('input[name="cf_challenge_response"]');
                if (cf2 && cf2.value && cf2.value.length > 10) return true;
                return false;
            }""")
            if result:
                return True
        except Exception:
            pass

        # 方法3: 检查弹窗中的验证提示文字是否消失
        try:
            dialog = driver.page.locator('[role="dialog"]').first
            if dialog.is_visible():
                dialog_text = dialog.inner_text().lower()
                # 如果 "captcha is required" 和 "verify you are human" 都不在了
                if ('captcha is required' not in dialog_text and
                    'let us know you' not in dialog_text and
                    'verify you are human' not in dialog_text):
                    return True
        except Exception:
            pass

        # 方法4: 检查 Turnstile iframe 中的 checkbox 是否已勾选（通过 aria-checked）
        try:
            result = driver.page.evaluate("""() => {
                var iframes = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
                for (var i = 0; i < iframes.length; i++) {
                    if (!iframes[i].offsetParent) continue;
                    var rect = iframes[i].getBoundingClientRect();
                    // 已验证的 Turnstile iframe 通常会显示绿色勾选标记
                    // 检查 data-* 属性变化
                    var parent = iframes[i].parentElement;
                    if (parent) {
                        var response = parent.querySelector('input[type="hidden"]');
                        if (response && response.value && response.value.length > 10) return true;
                    }
                }
                return false;
            }""")
            if result:
                return True
        except Exception:
            pass

    except Exception:
        pass

    return False


def _check_browser_console_for_errors(driver):
    """
    从事件驱动收集的控制台日志中检测 Stripe/支付错误。
    Patchright 版：不再注入页面 JS（避免暴露自动化），改读 create_driver 挂载的
    page.on("console") 监听器收集到的 driver.console_errors（已按 _CARD_ERROR_PATTERNS 过滤）。
    捕获 Cloudflare 输出的如:
      ⛔️ Setup intent error: Your card's CVC is incorrect.
      ⚠️ Form error handler [There was an error processing your card...]
    """
    try:
        errors = list(driver.console_errors or [])
        if not errors:
            return None
        for err in errors:
            if not isinstance(err, str) or len(err) <= 3:
                continue
            # 尝试提取具体错误描述
            m = re.search(r'Setup intent error[:\s]+(.+)', err, re.IGNORECASE)
            if m:
                detail = m.group(1).strip().rstrip('.')
                print(f"  [控制台拦截] {detail[:100]}")
                return detail[:200]
            m = re.search(r'Form error handler\s*\[(.+?)\]', err, re.IGNORECASE)
            if m:
                detail = m.group(1).strip()
                print(f"  [控制台拦截] {detail[:100]}")
                return detail[:200]
            # 其他错误直接返回原始消息
            print(f"  [控制台拦截] {err[:100]}")
            return err[:200]
    except Exception:
        pass
    return None


def _check_stripe_iframe_errors(driver):
    """
    检查 Stripe iframe 内的表单字段错误
    Stripe 错误元素格式: <p id="Field-cvcError" class="p-FieldError Error" role="alert">...</p>
    返回错误文本或 None
    """
    # 方法1: 从浏览器控制台日志中读取错误（最可靠）
    # Chrome 原生日志 API，不受 JS 引用缓存影响
    console_err = _check_browser_console_for_errors(driver)
    if console_err:
        return console_err

    # 方法2: 原生 frame 枚举（优先，隐蔽）——Patchright 的 page.frames 原生支持
    # 跨域 Stripe iframe，可直接在其内读取 .p-FieldError 文本，无需 CDP 隔离世界。
    err_selector = ('.p-FieldError, [role="alert"][id*="Error"], '
                    '[role="alert"].Error, [id*="Error"].Error')
    try:
        for fr in driver.page.frames:
            try:
                field_errors = fr.locator(err_selector).all()
            except Exception:
                continue
            for fe in field_errors:
                try:
                    if not fe.is_visible():
                        continue
                    err_text = (fe.inner_text() or '').strip()
                    if err_text and len(err_text) > 3:
                        try:
                            furl = (fr.url or '?')[:50]
                        except Exception:
                            furl = '?'
                        print(f"  [frame FieldError] 在 frame {furl} 中发现错误")
                        return err_text[:200]
                except Exception:
                    continue
    except Exception:
        pass

    # 方法3: CDP - 在每个 iframe 的独立执行上下文中查询 DOM（跨域 + closed shadow 兜底）
    try:
        frame_tree = driver._cdp().send('Page.getFrameTree', {})
        all_frames = _collect_all_child_frames(frame_tree.get('frameTree', {}))

        for frame_info in all_frames:
            try:
                world = driver._cdp().send('Page.createIsolatedWorld', {
                    'frameId': frame_info['id'],
                    'worldName': 'err_chk_' + str(int(time.time() * 1000)),
                    'grantUniversalAccess': True,
                })
                ctx_id = world.get('executionContextId')
                if not ctx_id:
                    continue

                result = driver._cdp().send('Runtime.evaluate', {
                    'expression': '''
                        (function() {
                            var sels = [
                                '.p-FieldError',
                                '[role="alert"]',
                                '.Error[id*="Error"]',
                                '[id*="Error"][role="alert"]'
                            ];
                            for (var s = 0; s < sels.length; s++) {
                                var els = document.querySelectorAll(sels[s]);
                                for (var i = 0; i < els.length; i++) {
                                    var t = (els[i].textContent || '').trim();
                                    if (t && t.length > 3) return t;
                                }
                            }
                            return null;
                        })()
                    ''',
                    'returnByValue': True,
                    'contextId': ctx_id,
                })
                val = result.get('result', {}).get('value')
                if val:
                    print(f"  [CDP isolated world] 在 frame {frame_info.get('url', '?')[:50]} 中发现错误")
                    return val[:200]
            except Exception:
                continue
    except Exception:
        pass

    # 方法4: CDP DOM 穿透遍历（closed shadow DOM 最终兜底）
    try:
        doc = driver._cdp().send('DOM.getDocument', {'depth': -1, 'pierce': True})
        error_text = _find_stripe_field_errors_in_dom(doc['root'])
        if error_text:
            return error_text
    except Exception:
        pass

    return None


def _collect_all_child_frames(frame_tree_node):
    """从 Page.getFrameTree 结果中收集所有子 frame 信息"""
    frames = []
    for child in frame_tree_node.get('childFrames', []):
        frame = child.get('frame', {})
        frame_id = frame.get('id')
        if frame_id:
            frames.append({
                'id': frame_id,
                'url': frame.get('url', ''),
                'name': frame.get('name', ''),
            })
        # 递归收集嵌套的子 frame
        frames.extend(_collect_all_child_frames(child))
    return frames


def _find_stripe_field_errors_in_dom(node):
    """
    递归遍历 CDP DOM 树，查找 Stripe 的 FieldError 元素
    匹配: class 包含 'p-FieldError' 或 id 包含 'Error' 且 role='alert' 的元素
    """
    node_name = node.get('nodeName', '').lower()

    # 检查当前节点是否是错误元素（p, div, span 等）
    if node_name in ('p', 'div', 'span'):
        attrs = node.get('attributes', [])
        attr_dict = dict(zip(attrs[::2], attrs[1::2])) if attrs else {}

        cls = attr_dict.get('class', '')
        node_id = attr_dict.get('id', '')
        role = attr_dict.get('role', '')

        # 匹配 Stripe FieldError: class 包含 "p-FieldError" 或 "FieldError"
        is_field_error = ('FieldError' in cls or 'p-FieldError' in cls)
        # 匹配 role="alert" 且 id 包含 "Error"
        is_alert_error = (role == 'alert' and 'Error' in node_id)

        if is_field_error or is_alert_error:
            # 提取文本内容
            text = _extract_text_from_dom_node(node)
            if text:
                return text

    # 递归子节点
    for child in node.get('children', []):
        result = _find_stripe_field_errors_in_dom(child)
        if result:
            return result

    # 递归 shadow roots（Stripe 不常用但以防万一）
    for shadow in node.get('shadowRoots', []):
        for child in shadow.get('children', []):
            result = _find_stripe_field_errors_in_dom(child)
            if result:
                return result

    # 递归 iframe 的 contentDocument
    if node_name == 'iframe':
        for child in node.get('children', []):
            # iframe 的 children 中可能包含 contentDocument
            if child.get('nodeName', '') == '#document':
                result = _find_stripe_field_errors_in_dom(child)
                if result:
                    return result

    # contentDocument 节点
    content_doc = node.get('contentDocument')
    if content_doc:
        result = _find_stripe_field_errors_in_dom(content_doc)
        if result:
            return result

    return None


def _extract_text_from_dom_node(node):
    """从 CDP DOM 节点提取文本内容"""
    # 直接文本节点
    if node.get('nodeType') == 3:  # TEXT_NODE
        return (node.get('nodeValue', '') or '').strip()

    text_parts = []
    for child in node.get('children', []):
        if child.get('nodeType') == 3:
            val = (child.get('nodeValue', '') or '').strip()
            if val:
                text_parts.append(val)
        else:
            sub = _extract_text_from_dom_node(child)
            if sub:
                text_parts.append(sub)

    return ' '.join(text_parts) if text_parts else None


def _check_dialog_card_error(driver):
    """
    检查弹窗内是否出现信用卡/表单错误信息
    返回带分类前缀的错误字符串（如果有错误），否则返回 None
    分类前缀: [控制台表单错误] [外部原因] [表单字段错误] [支付处理错误] [页面错误] [Stripe字段错误]
    """
    # 最优先: 从浏览器控制台日志读取 Stripe 错误（最可靠的信号）
    console_err = _check_browser_console_for_errors(driver)
    if console_err:
        return f"[控制台表单错误] {console_err}"

    try:
        dialog = None
        dialogs = driver.page.locator('[role="dialog"]').all()
        for d in dialogs:
            try:
                if d.is_visible():
                    dialog = d
                    break
            except Exception:
                continue
        if not dialog:
            return None

        dialog_text = dialog.inner_text()

        # 中文错误信息检测 —— 按分类分组
        cn_declined = ['银行卡被拒', '交易被拒绝', '卡片被拒绝', '信用卡被拒', '资金不足', '余额不足']
        cn_form = [
            '安全码错误', '安全码不正确', '安全码无效', 'CVC错误',
            '卡号错误', '卡号无效', '卡号不正确', '卡号不完整',
            '有效期错误', '有效期无效', '有效期已过', '卡已过期',
            '地址错误', '邮编错误', '邮政编码',
        ]
        cn_payment = ['付款失败', '支付失败', '处理失败', '无法处理', '请检查您的', '信息不正确', '发生错误', '请重试', '不支持']
        for kw in cn_declined:
            if kw in dialog_text:
                return f"[外部原因] {kw}"
        for kw in cn_form:
            if kw in dialog_text:
                return f"[表单字段错误] {kw}"
        for kw in cn_payment:
            if kw in dialog_text:
                return f"[支付处理错误] {kw}"

        # 英文错误信息检测（精确匹配优先，避免误报）—— 按分类分组
        dialog_text_lower = dialog_text.lower()

        # 外部原因：银行/发卡机构拒绝
        en_declined = [
            'card was declined', 'card has been declined', 'transaction declined',
            'do not honor', 'insufficient funds', 'lost or stolen', 'pickup card',
            'generic decline', 'call issuer', 'restricted card', 'not permitted',
        ]
        # 表单字段验证错误
        en_form = [
            "your card's security code is incorrect", 'security code is incorrect',
            'incorrect cvc', 'incorrect security code', "your card's cvc is invalid",
            'cvc is incomplete', 'card number is invalid', 'card number is incomplete',
            'is not a valid card number', 'invalid card number',
            'expiration date is incomplete', 'expiry date is incomplete',
            "your card's expiration date is in the past",
            "your card's expiration year is invalid",
            "your card's expiration month is invalid",
            'expiration date is invalid', 'expiry date is invalid', 'card has expired',
            'zip code is incomplete', 'postal code is incomplete',
            'address is incomplete', 'invalid zip', 'invalid postal',
            'billing address is invalid', 'billing address is incomplete',
            'address verification failed',
        ]
        # 通用支付处理错误
        en_payment = [
            'payment failed', 'unable to process', 'processing error',
            'try again later', 'try again', 'an error occurred', 'something went wrong',
            'could not be processed', 'please try again', 'not supported', 'not accepted',
        ]
        for phrase in en_declined:
            if phrase in dialog_text_lower:
                return f"[外部原因] {phrase}"
        for phrase in en_form:
            if phrase in dialog_text_lower:
                return f"[表单字段错误] {phrase}"
        for phrase in en_payment:
            if phrase in dialog_text_lower:
                return f"[支付处理错误] {phrase}"

        # 检测弹窗内的错误提示元素（role="alert" 等）
        # 排除 captcha 相关的提示（如 "Captcha is required."）
        try:
            error_els = dialog.locator(
                '[role="alert"], [data-error], '
                '.field-error, .form-error, .validation-error').all()
            for err_el in error_els:
                if not err_el.is_visible():
                    continue
                err_text = (err_el.inner_text() or '').strip()
                if not err_text:
                    continue
                # 排除 captcha 相关提示
                err_lower = err_text.lower()
                if 'captcha' in err_lower or 'human' in err_lower:
                    continue
                return f"[页面错误] {err_text[:100]}"
        except Exception:
            pass

        # Stripe iframe 内部错误检测
        # Stripe 的错误元素（如 p.p-FieldError）在 iframe 内，主文档读不到
        stripe_err = _check_stripe_iframe_errors(driver)
        if stripe_err:
            return f"[Stripe字段错误] {stripe_err}"

    except Exception:
        pass

    return None


def _find_payment_submit_button(driver):
    """在弹窗中查找 'Add payment method' 提交按钮。返回 Playwright Locator 或 None。"""
    page = driver.page

    # 方法1: 弹窗内 data-kumo-component 按钮中精确匹配 "Add payment method"
    try:
        dialog = page.locator('[role="dialog"]').first
        if dialog.count() > 0:
            buttons = dialog.locator('button[data-kumo-component="Button"]').all()
            for btn in buttons:
                try:
                    if (btn.inner_text() or '').strip() == 'Add payment method' and btn.is_visible():
                        return btn
                except Exception:
                    continue
    except Exception:
        pass

    # 方法2: XPath 回退
    submit_xpaths = [
        '//div[@role="dialog"]//button[contains(., "Add payment method")]',
        '//div[@role="dialog"]//button[contains(., "Add")]',
        '//button[contains(., "Add payment method")]',
    ]
    for xpath in submit_xpaths:
        try:
            btns = page.locator("xpath=" + xpath).all()
            for btn in btns:
                try:
                    if btn.is_visible() and 'Cancel' not in (btn.inner_text() or ''):
                        return btn
                except Exception:
                    continue
        except Exception:
            continue

    return None


def _wait_for_payment_submit_result(driver, max_wait=180):
    """
    等待添加信用卡提交的结果

    提交后可能出现:
    1. 弹窗关闭 → 添加成功
    2. 弹窗显示错误信息 → 添加失败
    3. 出现人机验证（Cloudflare Turnstile / Stripe 3DS 验证）→ 等待用户手动操作
    4. 页面跳转到 3DS 验证页面 → 等待用户完成

    参数:
        driver: 浏览器驱动（BrowserSession）
        max_wait: 最大等待时间（秒），默认 180 秒
    返回:
        tuple[bool, str]: (是否成功添加, 错误原因字符串)
    """
    page = driver.page

    def _vis(loc):
        try:
            return loc.is_visible()
        except Exception:
            return False

    def _no_visible_dialog():
        try:
            dialogs = page.locator('[role="dialog"]').all()
            return not any(_vis(d) for d in dialogs)
        except Exception:
            return False

    print("⏳ 等待提交结果...")
    time.sleep(5)

    # 提交后先检查是否已有错误（Stripe 返回快的话 5 秒内就有结果）
    card_error = _check_dialog_card_error(driver)
    if card_error:
        print(f"  ❌ 添加失败: {card_error}")
        _close_payment_dialog(driver)
        return False, card_error

    # 检查弹窗是否已关闭（提交成功）
    if _no_visible_dialog():
        print("🎉 信用卡添加成功！(弹窗已关闭)")
        return True, ""

    print("  ⏳ 开始检测结果...")

    user_notified_captcha = False
    last_retry_click_time = time.time() - 5  # 首次重试等待5秒，后续重试等待10秒
    retry_click_count = 0
    max_retry_clicks = 3
    loading_stuck_since = None  # 按钮进入 loading 状态的时间
    start = time.time()

    while time.time() - start < max_wait:
        # 每轮刷新截图缓存，让实时截图流保持流畅（业务线程内调用）
        driver.capture_frame()

        # 检查1: 弹窗是否已关闭（成功标志）
        if _no_visible_dialog():
            print("🎉 信用卡添加成功！(弹窗已关闭)")
            return True, ""

        # 检查2: 弹窗内是否有表单/卡片错误（优先于 captcha 检测）
        card_error = _check_dialog_card_error(driver)
        if card_error:
            print(f"  ❌ 添加失败: {card_error}")
            _close_payment_dialog(driver)
            return False, card_error

        # 检查3: 是否出现人机验证
        captcha_type = None  # 'turnstile', 'hcaptcha', 'unknown'
        try:
            # Cloudflare Turnstile
            turnstile = page.locator(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '[data-testid="challenge-widget-container"], '
                'iframe[title*="challenge"], '
                'iframe[title*="Turnstile"]'
            ).all()
            if any(_vis(el) for el in turnstile):
                captcha_type = 'turnstile'

            # hCaptcha (主文档 + 所有 iframe 内部)
            if not captcha_type:
                hcaptcha = page.locator(
                    'iframe[src*="hcaptcha.com"], '
                    'iframe[src*="hcaptcha"], '
                    '.HCaptcha-container, '
                    '.h-captcha, '
                    '#HCaptcha-root, '
                    'iframe[title*="hCaptcha"], '
                    'iframe[title*="hcaptcha"], '
                    'iframe[data-hcaptcha-widget-id], '
                    '[data-hcaptcha-widget-id]'
                ).all()
                if any(_vis(el) for el in hcaptcha):
                    captcha_type = 'hcaptcha'

                # 在嵌套 iframe 中查找 hCaptcha（page.frames 已含全部嵌套层级，
                # 原生穿透跨域，替代旧的 switch_to.frame 逐层遍历）
                if not captcha_type:
                    try:
                        main = page.main_frame
                        for fr in page.frames:
                            if fr is main:
                                continue
                            try:
                                inner_hcaptcha = fr.locator(
                                    'iframe[src*="hcaptcha.com"], '
                                    'iframe[src*="hcaptcha-inner"], '
                                    'iframe[src*="hcaptcha"], '
                                    '.h-captcha, '
                                    '[data-hcaptcha-widget-id]'
                                ).all()
                                if any(_vis(el) for el in inner_hcaptcha):
                                    captcha_type = 'hcaptcha'
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

            # Stripe 3DS 验证弹窗
            if not captcha_type:
                threed_frames = page.locator(
                    'iframe[name*="__stripeJSAuth"], '
                    'iframe[src*="3ds"], '
                    'iframe[title*="3D Secure"]'
                ).all()
                if any(_vis(el) for el in threed_frames):
                    captcha_type = 'unknown'

            # 页面文本检测
            if not captcha_type:
                try:
                    body_text = (page.inner_text('body', timeout=SHORT_TIMEOUT_MS) or '').lower()
                except Exception:
                    body_text = ''
                captcha_keywords = [
                    'verify you are human', 'human verification',
                    '确认您是真人', '验证您不是机器人',
                    'complete the security check', 'security verification',
                    '还需一步即可完成', '选择下方的选框',
                ]
                for kw in captcha_keywords:
                    if kw in body_text:
                        # 再判断具体类型
                        try:
                            hc = page.locator('iframe[src*="hcaptcha.com"]').all()
                            if any(_vis(el) for el in hc):
                                captcha_type = 'hcaptcha'
                            else:
                                captcha_type = 'unknown'
                        except Exception:
                            captcha_type = 'unknown'
                        break
        except Exception:
            pass

        if captcha_type:
            if not user_notified_captcha:
                user_notified_captcha = True
                print(f"  🔒 检测到人机验证 (类型: {captcha_type})")

                # 处理策略：CDP 点击 checkbox → 等待图片挑战加载 → 2Captcha 解决
                solved = False
                if captcha_type == 'hcaptcha':
                    # 第一步：CDP 点击 hCaptcha checkbox
                    print("  🤖 尝试 CDP 点击 hCaptcha checkbox...")
                    cdp_clicked = _click_hcaptcha_via_cdp(driver)
                    if cdp_clicked:
                        # 等待图片挑战页面加载（通常会出现新的大 iframe）
                        print("  ⏳ 等待 hCaptcha 图片挑战加载...")
                        challenge_loaded = False
                        for _ in range(15):
                            time.sleep(2)
                            driver.capture_frame()
                            # 先检查是否意外直接通过了
                            if _no_visible_dialog():
                                print("  🎉 hCaptcha 直接通过，信用卡添加成功！")
                                return True, ""
                            try:
                                modals = page.locator(
                                    '.LightboxModal-open, .HCaptcha-container').all()
                                if not any(_vis(m) for m in modals):
                                    print("  ✅ hCaptcha 验证已通过！")
                                    solved = True
                                    break
                            except Exception:
                                pass
                            # 检测图片挑战 iframe 是否已加载
                            try:
                                challenge_iframes = page.locator(
                                    'iframe[src*="hcaptcha.com/challenge"], '
                                    'iframe[src*="hcaptcha.com/getcaptcha"], '
                                    'iframe[src*="newassets.hcaptcha.com"][style*="position"]').all()
                                # 图片挑战 iframe 通常尺寸较大（宽>300px）
                                for cf in challenge_iframes:
                                    if not _vis(cf):
                                        continue
                                    try:
                                        bb = cf.bounding_box()
                                    except Exception:
                                        bb = None
                                    if bb and bb.get('width', 0) > 300:
                                        challenge_loaded = True
                                        break
                            except Exception:
                                pass
                            if challenge_loaded:
                                print("  📸 hCaptcha 图片挑战已加载")
                                break

                    # 第二步：用 2Captcha 解决图片挑战
                    if not solved and captcha_solver.is_available():
                        print("  🤖 尝试使用 2Captcha 解决 hCaptcha 图片挑战...")
                        solved = captcha_solver.solve_hcaptcha(driver)

                elif captcha_type == 'turnstile':
                    # Turnstile: 先 CDP 点击再 2Captcha
                    print("  🤖 尝试 CDP 点击 Turnstile...")
                    if _click_turnstile_via_cdp(driver):
                        for _ in range(8):
                            time.sleep(2)
                            driver.capture_frame()
                            if _no_visible_dialog():
                                print("  🎉 Turnstile 通过，信用卡添加成功！")
                                return True, ""
                    if captcha_solver.is_available():
                        print("  🤖 尝试使用 2Captcha 解决 Turnstile...")
                        solved = captcha_solver.solve_turnstile(driver)
                else:
                    if captcha_solver.is_available():
                        print("  🤖 尝试使用 2Captcha 自动解决...")
                        solved = captcha_solver.solve_hcaptcha(driver) or captcha_solver.solve_turnstile(driver)

                if solved:
                    time.sleep(5)
                    driver.capture_frame()
                    if _no_visible_dialog():
                        print("  🎉 验证解决成功，信用卡添加成功！")
                        return True, ""
                    # 检查 LightboxModal 是否关闭
                    try:
                        modals = page.locator(
                            '.LightboxModal-open, .HCaptcha-container').all()
                        if not any(_vis(m) for m in modals):
                            print("  ✅ 验证已通过，等待页面响应...")
                    except Exception:
                        pass
                    # 验证完成后弹窗仍在 → 重新点击提交按钮
                    try:
                        resubmit_btn = _find_payment_submit_button(driver)
                        if resubmit_btn is not None and resubmit_btn.is_enabled():
                            print("  🔄 验证解决后重新点击提交按钮...")
                            try:
                                resubmit_btn.click()
                            except Exception:
                                try:
                                    resubmit_btn.evaluate("el => el.click()")
                                except Exception:
                                    pass
                            last_retry_click_time = time.time()
                            time.sleep(3)
                    except Exception:
                        pass

                # CDP/2Captcha 都失败，提示用户手动操作
                remaining = int(max_wait - (time.time() - start))
                print("")
                print("  " + "=" * 50)
                print("  ⚠️  需要手动完成人机验证！")
                print("  👉 请在浏览器窗口中完成验证")
                print(f"  ⏰ 等待时间: 最长 {remaining} 秒")
                print("  " + "=" * 50)
                print("")
            time.sleep(3)
            continue

        # (卡片错误检测已在循环开头的 _check_dialog_card_error 中处理)

        # 检查4: 提交按钮状态检测 → 可点击则重试，loading 超时则放弃
        if time.time() - last_retry_click_time > 7:
            try:
                retry_btn = _find_payment_submit_button(driver)
                if retry_btn is not None:
                    try:
                        btn_enabled = retry_btn.is_enabled()
                    except Exception:
                        btn_enabled = False
                    aria_disabled = (retry_btn.get_attribute('aria-disabled') or '') == 'true'
                    # 只依赖原生 disabled 属性和 aria-disabled 判断，避免 class 名误判
                    is_loading = (not btn_enabled or aria_disabled)
                    btn_classes = retry_btn.get_attribute('class') or ''
                    print(f"  🔍 按钮状态: enabled={btn_enabled} aria-disabled={aria_disabled} class={btn_classes[:80]}")
                    if is_loading:
                        # 记录按钮首次进入 loading 状态的时间
                        if loading_stuck_since is None:
                            loading_stuck_since = time.time()
                        elif time.time() - loading_stuck_since > 45:
                            print(f"  ⚠️ 提交按钮已持续 loading {int(time.time() - loading_stuck_since)}s，判定为卡死，关闭弹窗重试")
                            _close_payment_dialog(driver)
                            return False, "[提交超时] 提交按钮持续loading超过45秒"
                    else:
                        # 按钮恢复可点击，重置 loading 计时
                        loading_stuck_since = None
                        if retry_click_count < max_retry_clicks:
                            retry_click_count += 1
                            last_retry_click_time = time.time()
                            print(f"  🔄 提交按钮可点击，重试第 {retry_click_count} 次...")
                            try:
                                retry_btn.click()
                            except Exception:
                                try:
                                    retry_btn.evaluate("el => el.click()")
                                except Exception:
                                    pass
                            time.sleep(3)
                            continue
            except Exception:
                pass

        # 继续等待 - 每30秒输出一次状态
        elapsed = int(time.time() - start)
        if elapsed % 30 < 4:
            try:
                iframe_count = len(page.locator('iframe').all())
                dialog_count = len([d for d in page.locator('[role="dialog"]').all() if _vis(d)])
                print(f"  ⏳ 等待中... ({elapsed}s) 弹窗:{dialog_count} iframe:{iframe_count}")
            except Exception:
                pass
        time.sleep(3)

    # 超时
    print(f"  ⚠️ 等待提交结果超时 ({max_wait}秒)")
    # 最后检查一次弹窗状态
    if _no_visible_dialog():
        print("🎉 信用卡添加成功！(弹窗已关闭)")
        return True, ""

    _close_payment_dialog(driver)
    return False, f"[超时] 等待提交结果超过{max_wait}秒"


def _fill_stripe_payment_element(driver, card_info):
    """
    在 Stripe Payment Element iframe 内填写信用卡信息

    Stripe Payment Element 是一个统一的支付表单组件，
    包含卡号、有效期、CVC 等字段，可能使用 div[contenteditable]
    或嵌套 iframe 的方式渲染

    参数:
        driver: 浏览器驱动（BrowserSession）。Patchright 版不依赖「已切入 iframe」的
                状态——frame_locator/page.frames 无状态，函数自行从页面根定位 Stripe 字段。
        card_info: 信用卡信息
    返回:
        bool: 是否成功填写
    """
    filled_any = False
    page = driver.page

    # 尝试直接在当前 iframe 中查找输入框
    card_selectors = [
        'input[name="cardnumber"]',
        'input[name="number"]',
        'input[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]',
        'input[placeholder*="card number"]',
        'input[data-elements-stable-field-name="cardNumber"]',
    ]

    expiry_selectors = [
        'input[name="exp-date"]',
        'input[name="cardExpiry"]',
        'input[autocomplete="cc-exp"]',
        'input[placeholder*="MM"]',
        'input[data-elements-stable-field-name="cardExpiry"]',
    ]

    cvc_selectors = [
        'input[name="cvc"]',
        'input[name="cardCvc"]',
        'input[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]',
        'input[data-elements-stable-field-name="cardCvc"]',
    ]

    # Stripe Payment Element 内的账单地址字段选择器
    billing_fields = [
        ('billingName', [
            'input[name="billingName"]',
            'input[autocomplete="name"]',
            'input[autocomplete="cc-name"]',
            'input[data-elements-stable-field-name="billingName"]',
            'input[placeholder*="Name on card"]',
            'input[placeholder*="name on card"]',
            'input[placeholder*="Full name"]',
        ], lambda ci: f"{ci.get('first_name', '')} {ci.get('last_name', '')}".strip()),
        ('billingAddress_line1', [
            'input[name="addressLine1"]',
            'input[name="billingAddress-line1"]',
            'input[autocomplete="address-line1"]',
            'input[data-elements-stable-field-name="addressLine1"]',
            'input[placeholder*="Address"]',
        ], lambda ci: ci.get('address', '')),
        ('billingAddress_line2', [
            'input[name="addressLine2"]',
            'input[name="billingAddress-line2"]',
            'input[autocomplete="address-line2"]',
            'input[data-elements-stable-field-name="addressLine2"]',
        ], lambda ci: ci.get('address2', '')),
        ('billingAddress_city', [
            'input[name="addressCity"]',
            'input[name="billingAddress-city"]',
            'input[autocomplete="address-level2"]',
            'input[data-elements-stable-field-name="addressCity"]',
            'input[placeholder*="City"]',
        ], lambda ci: ci.get('city', '')),
        ('billingAddress_state', [
            'input[name="addressState"]',
            'input[name="billingAddress-state"]',
            'input[autocomplete="address-level1"]',
            'input[data-elements-stable-field-name="addressState"]',
            'select[name="addressState"]',
            'input[placeholder*="State"]',
        ], lambda ci: ci.get('state', '')),
        ('billingAddress_zip', [
            'input[name="addressZip"]',
            'input[name="billingAddress-postalCode"]',
            'input[autocomplete="postal-code"]',
            'input[data-elements-stable-field-name="addressZip"]',
            'input[placeholder*="ZIP"]',
        ], lambda ci: ci.get('zip', '')),
        ('billingAddress_country', [
            'select[name="addressCountry"]',
            'select[name="billingAddress-country"]',
            'select[autocomplete="country"]',
            'input[name="addressCountry"]',
            'input[data-elements-stable-field-name="addressCountry"]',
        ], lambda ci: ci.get('country', '')),
    ]

    def _stripe_field_frames():
        """收集与 Stripe 支付表单相关的 frame（page.frames 已含所有嵌套层级，
        原生穿透跨域，替代旧的手工逐层切帧遍历）。排除 express checkout。"""
        result = []
        try:
            main = page.main_frame
        except Exception:
            main = None
        for fr in page.frames:
            if main is not None and fr == main:
                continue
            try:
                url = (fr.url or '').lower()
                name = (fr.name or '').lower()
            except Exception:
                continue
            if 'express' in url or 'express' in name:
                continue
            if ('stripe' in url or 'stripe' in name
                    or name.startswith('__privatestripeframe')
                    or 'elements-inner' in url):
                result.append(fr)
        return result

    def try_fill_selectors(ctx, selectors, value, label):
        """在给定上下文 ctx（Page 或 Frame）内按选择器列表填写。
        跨域 Stripe 字段用 press_sequentially 逐字输入（触发 Stripe 格式化/校验事件，
        不用 fill+回读——Stripe 会重格式化导致失配）。"""
        nonlocal filled_any
        if not value:
            return False
        for sel in selectors:
            try:
                el = ctx.locator(sel).first
                if el.count() == 0 or not el.is_visible():
                    continue
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == 'select':
                    # 下拉选择框：先按可见文本，再按 value，最后模糊匹配
                    try:
                        el.select_option(label=value)
                    except Exception:
                        try:
                            el.select_option(value=value)
                        except Exception:
                            try:
                                opts = el.locator('option').all()
                                for opt in opts:
                                    ot = (opt.inner_text() or '')
                                    if value.lower() in ot.lower():
                                        ov = opt.get_attribute('value') or ot
                                        el.select_option(value=ov)
                                        break
                            except Exception:
                                pass
                else:
                    el.click()
                    time.sleep(0.3)
                    el.press_sequentially(str(value), delay=50)
                print(f"  ✅ 填写 {label}")
                filled_any = True
                return True
            except Exception:
                continue
        return False

    number = card_info.get('number', '')
    exp_month = card_info.get('expiry_month', '')
    exp_year = card_info.get('expiry_year', '')
    expiry = f"{exp_month}{exp_year[-2:]}" if exp_year else exp_month
    cvc = card_info.get('cvc', '')

    def try_fill_billing_fields(ctx):
        """尝试在指定上下文内填写 Stripe 表单账单地址字段"""
        for field_name, selectors, value_fn in billing_fields:
            value = value_fn(card_info)
            if value:
                try_fill_selectors(ctx, selectors, value, field_name)
                time.sleep(0.3)

    # 策略1: 主文档直接查找（Stripe 字段通常在 iframe 内，此层多为空跑，成本低）
    if try_fill_selectors(page, card_selectors, number, '卡号'):
        time.sleep(0.5)
        try_fill_selectors(page, expiry_selectors, expiry, '有效期')
        time.sleep(0.5)
        try_fill_selectors(page, cvc_selectors, cvc, 'CVC')
        time.sleep(0.5)
        try_fill_billing_fields(page)
        return filled_any

    # 策略2: 枚举 Stripe 相关 frame（每字段各自在独立嵌套 iframe 中）
    card_filled_nested = False
    expiry_filled_nested = False
    cvc_filled_nested = False
    billing_filled_in_nested = False
    for fr in _stripe_field_frames():
        try:
            if not card_filled_nested and try_fill_selectors(fr, card_selectors, number, '卡号'):
                card_filled_nested = True
            elif not expiry_filled_nested and try_fill_selectors(fr, expiry_selectors, expiry, '有效期'):
                expiry_filled_nested = True
            elif not cvc_filled_nested and try_fill_selectors(fr, cvc_selectors, cvc, 'CVC'):
                cvc_filled_nested = True
            else:
                for field_name, selectors, value_fn in billing_fields:
                    value = value_fn(card_info)
                    if value and try_fill_selectors(fr, selectors, value, field_name):
                        billing_filled_in_nested = True
                        time.sleep(0.2)
            time.sleep(0.3)
        except Exception:
            continue

    if card_filled_nested or expiry_filled_nested or cvc_filled_nested:
        # 卡信息填了部分，也尝试在主文档层填写账单地址
        if not billing_filled_in_nested:
            try_fill_billing_fields(page)
        return filled_any

    # 策略3: 最后兜底 —— 用 Tab 键在表单字段间切换输入
    # 在首个 Stripe 字段 frame 内点击获焦，然后逐字符输入 + Tab 到下一字段
    print("  ⚠️ 未找到独立字段，尝试 Tab 键导航输入...")
    try:
        target_frames = _stripe_field_frames()
        focus_frame = target_frames[0] if target_frames else None

        # 获取焦点：优先点 frame 内任意可见输入/元素，失败则点 frame body
        if focus_frame is not None:
            try:
                any_input = focus_frame.locator('input, div[contenteditable], span, label, p').first
                if any_input.count() > 0:
                    any_input.click(timeout=SHORT_TIMEOUT_MS)
                else:
                    focus_frame.locator('body').first.click(timeout=SHORT_TIMEOUT_MS)
            except Exception:
                try:
                    focus_frame.locator('body').first.click(timeout=SHORT_TIMEOUT_MS)
                except Exception:
                    pass
        time.sleep(0.5)

        kb = page.keyboard

        # 输入卡号
        kb.type(str(number), delay=50)
        print("  ✅ 输入卡号 (Tab 方式)")
        filled_any = True
        time.sleep(0.5)

        # Tab 到有效期
        kb.press("Tab")
        time.sleep(0.3)
        kb.type(str(expiry), delay=50)
        print("  ✅ 输入有效期 (Tab 方式)")
        time.sleep(0.5)

        # Tab 到 CVC
        kb.press("Tab")
        time.sleep(0.3)
        kb.type(str(cvc), delay=50)
        print("  ✅ 输入 CVC (Tab 方式)")

    except Exception as e:
        print(f"  ❌ Tab 方式输入失败: {e}")

    return filled_any


def _close_payment_dialog(driver):
    """关闭 Add a payment method 弹窗"""
    try:
        dialog = driver.page.locator('[role="dialog"]').first
        if dialog.count() == 0:
            return

        # 1) Cancel 按钮（精确文本匹配）
        try:
            cancel_btn = dialog.get_by_role("button", name="Cancel", exact=True)
            if cancel_btn.count() > 0 and cancel_btn.first.is_visible():
                _safe_click(cancel_btn.first, session=driver, desc='Cancel 按钮')
                print("  🔘 已关闭弹窗")
                time.sleep(2)
                return
        except Exception:
            pass

        # 2) 关闭按钮 (aria-label="Close")
        try:
            close_btn = dialog.locator('button[aria-label="Close"]').first
            if close_btn.count() > 0 and close_btn.is_visible():
                _safe_click(close_btn, session=driver, desc='关闭按钮')
                print("  🔘 已关闭弹窗")
                time.sleep(2)
                return
        except Exception:
            pass

        # 3) 兜底：ESC 关闭
        try:
            driver.page.keyboard.press("Escape")
            time.sleep(1)
        except Exception:
            pass
    except Exception:
        pass


def _fill_stripe_field(driver, field_name, selectors_str, value):
    """
    填写 Stripe 表单字段
    先在主文档查找，找不到则遍历所有 frame（page.frames 已含全部嵌套层级）
    """
    selectors = [s.strip() for s in selectors_str.split(',')]
    page = driver.page

    def try_fill(ctx):
        for selector in selectors:
            try:
                el = ctx.locator(selector).first
                if el.count() == 0 or not el.is_visible():
                    continue
                try:
                    el.scroll_into_view_if_needed(timeout=SHORT_TIMEOUT_MS)
                except Exception:
                    pass
                el.click()
                el.press_sequentially(str(value), delay=50)
                return True
            except Exception:
                continue
        return False

    # 在主文档中查找
    if try_fill(page):
        print(f"  ✅ 在主文档找到 {field_name}")
        return True

    # 遍历所有 frame（page.frames 原生穿透跨域，等价原 2 层嵌套切帧遍历）
    try:
        main = page.main_frame
    except Exception:
        main = None
    for fr in page.frames:
        if main is not None and fr == main:
            continue
        try:
            if try_fill(fr):
                try:
                    furl = (fr.url or '?')[:50]
                except Exception:
                    furl = '?'
                print(f"  ✅ 在 iframe ({furl}) 中找到 {field_name}")
                return True
        except Exception:
            continue

    print(f"  ❌ 未找到 {field_name} 输入框")
    return False


def _fill_visible_field(driver, field_name, selectors_str, value):
    """填写主文档或 iframe 中的可见字段"""
    selectors = [s.strip() for s in selectors_str.split(',')]
    page = driver.page

    def try_ctx(ctx, in_iframe=False):
        for selector in selectors:
            try:
                el = ctx.locator(selector).first
                if el.count() == 0 or not el.is_visible():
                    continue
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == 'select':
                    try:
                        el.select_option(label=value)
                    except Exception:
                        try:
                            el.select_option(value=value)
                        except Exception:
                            pass
                else:
                    try:
                        el.fill('')
                    except Exception:
                        pass
                    el.press_sequentially(str(value), delay=50)
                suffix = ' (iframe)' if in_iframe else ''
                print(f"  ✅ 填写 {field_name}: {value}{suffix}")
                return True
            except Exception:
                continue
        return False

    # 在主文档中查找
    if try_ctx(page):
        return True

    # 在 iframe 中查找（page.frames 已含全部嵌套 frame）
    try:
        main = page.main_frame
    except Exception:
        main = None
    for fr in page.frames:
        if main is not None and fr == main:
            continue
        try:
            if try_ctx(fr, in_iframe=True):
                return True
        except Exception:
            continue

    return False
