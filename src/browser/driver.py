"""
浏览器自动化模块
使用 Patchright（Playwright 反检测 fork）驱动有头 Chrome。

分两类：
  1) 站点无关的浏览器基建（保留复用）：create_driver / close_driver / profile 卫生
     （进程清理、缓存修剪）、_safe_goto/_safe_click/_safe_fill、语言/下载目录设置等。
  2) LEGACY Cloudflare-specific（待 OpenRouter 重写）：以下方法群紧耦合 Cloudflare
     dash 站点与其内嵌 Stripe/Turnstile，项目改造为 OpenRouter 后已从编排层
     （services/registration.py）剥离、不再被调用，暂保留作接入参考：
       - 登录/账号：login_cloudflare、_detect_account_banned、_extract_account_id、
         navigate_to_ai_credits、fetch_today_invoice_count、read_credits_balance
       - Turnstile 质询：check_and_handle_cf_challenge、_is_challenge_page、
         _try_click_turnstile、_click_turnstile_via_cdp、_handle_inline_turnstile 等
       - Stripe 绑卡/账单：stripe_card_frame、_expand_stripe_card_accordion、
         _wait_for_stripe_fields_ready、_wait_for_billing_form_ready 等
       - 注册/绑卡/充值流程：fill_signup_form、handle_email_verification、
         navigate_to_billing、add_credit_card、fill_topup_and_confirm 等
     接入 OpenRouter 时按其实际页面替换上述实现，再在 registration.py 填充编排。
"""

import os
import re
import time
import random
import logging
import tempfile
import shutil
import threading
from datetime import datetime, timezone
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from src.config import cfg, PROFILE_CACHE_LIMIT_MB
import src.services.captcha as captcha_solver
from src.services.email import get_mail_token, wait_for_login_code

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

# 关闭浏览器的看门狗时限（秒）。Chrome 卡死时 context.close() 会无限期阻塞，
# 整条任务线程随之静默且不报错。超过此时限就按 profile 目录强杀 Chrome 解除阻塞。
# 取值需明显大于 Chrome 正常退出耗时（含 Cookies 落盘），否则会误杀正常关闭流程。
_CLOSE_WATCHDOG_SEC = 30

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

    def __init__(self, playwright, context, page, temp_profile=None, download_dir=None,
                 user_data_dir=None):
        self.playwright = playwright        # sync_playwright().start() 句柄
        self.context = context              # 持久化 BrowserContext
        self.page = page                    # 主 Page
        self._temp_profile = temp_profile   # 临时 profile 目录；持久化时为 None
        # profile 目录（临时/持久化都有值）。与 _temp_profile 职责不同：后者管
        # 「退出时要不要删目录」，本字段管「退出后按目录核查进程是否真的退干净了」。
        self._user_data_dir = user_data_dir
        self._download_dir = download_dir
        self.console_errors = []            # page.on("console") 收集（替代 __cfAutoErrors）
        self.net_responses = []             # page.on("response") 收集（替代 __netInterceptResponses）
        self.failed_responses = []          # 所有 4xx/5xx 响应（URL+状态码），不受拦截模式约束
        self.console_all_errors = []        # 未经卡片模式过滤的 error 级控制台日志（诊断用）
        self._net_patterns = []             # 网络拦截关键词模式（子列表内 AND，列表间 OR）
        self._last_png = None               # 最近一帧截图缓存（业务线程写、截图线程读）
        self._png_lock = threading.Lock()
        self._cdp_session = None            # 惰性创建的 CDPSession（缓存）
        self._closed = False
        self.account_banned = False         # 登录时检测到账号被 Cloudflare 封禁则置 True
        # 本会话内是否出现过欠费/待支付弹窗。该弹窗只对「已产生账单」的账号弹，
        # 因此是「账号已绑过卡」的强信号。dismiss_overdue_dialog 在登录流程里被多处调用，
        # 弹窗关掉后就无从追溯，故在此留痕供上层业务分支判断（见 bind_cards_to_existing_account）。
        self.overdue_dialog_seen = False
        self.credit_balance = None          # 被动监听 credit-balance 接口捕获的最新余额（美元）
        self.credit_balance_ts = None       # 上次捕获余额的时间戳（time.time()）

    # ---- 事件监听（在 create_driver 中挂载，均在业务线程回调） ----
    def _on_console(self, msg):
        try:
            text = msg.text or ''
        except Exception:
            return
        # console_errors 只留匹配卡片错误模式的条目（供 _check_browser_console_for_errors
        # 提取拒卡原因）；诊断「表单卡在加载态」需要的是未经过滤的 error 级日志，
        # 那类报错不含卡片关键词，会被上面的模式全部丢掉，故另存一份。
        try:
            if msg.type == 'error' and len(self.console_all_errors) < 100:
                self.console_all_errors.append(text[:300])
        except Exception:
            pass
        for pat in _CARD_ERROR_PATTERNS:
            if pat.search(text):
                self.console_errors.append(text[:300])
                break

    def _on_response(self, response):
        try:
            url = response.url or ''
        except Exception:
            return
        # 被动捕获 AI Gateway 余额接口：导航到 credits 页时页面会自行请求该接口，
        # 无论当前是否在拦截其它模式，都抓一次最新余额缓存到 session（供落库用）。
        if _CREDIT_BALANCE_URL_MARK in url:
            self._capture_credit_balance(response)
        # 失败响应始终记录（不受 _net_patterns 约束）：排查「表单卡在加载态」这类问题时，
        # 原因常在被拒的接口而非 DOM，而那时没人会预先设置拦截模式。只留 URL+状态码，
        # 不读响应体，开销可忽略；上限 200 条防止长任务累积。
        try:
            if (response.status or 0) >= 400 and len(self.failed_responses) < 200:
                self.failed_responses.append({'url': url, 'status': response.status})
        except Exception:
            pass
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

    def _capture_credit_balance(self, response):
        """解析 credit-balance 接口响应并缓存最新余额（美元）到 session。
        响应结构：{success: true, result: {balance: <分>}}，接口以「分」计
        （4000 == $40.00），换算成美元存入 self.credit_balance；解析失败静默忽略。"""
        try:
            if response.status != 200:
                return
            data = response.json()
        except Exception:
            return
        if not isinstance(data, dict) or data.get('success') is not True:
            return
        result = data.get('result')
        if not isinstance(result, dict):
            return
        bal = result.get('balance')
        if not isinstance(bal, (int, float)) or isinstance(bal, bool):
            return
        self.credit_balance = bal / 100.0
        self.credit_balance_ts = time.time()

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

        # 看门狗：context.close() / playwright.stop() 会等 Chrome 应答，Chrome 卡死时
        # 这两个调用会无限期阻塞，整条任务线程就此静默——现象是日志停在
        # 「正在关闭浏览器...」之后再无任何输出，且不报错（close_driver 吞异常也救不了，
        # 因为根本没抛出）。超时后直接按 profile 目录杀掉 Chrome，阻塞的调用随即解开。
        # 只做 OS 层 os.kill，不碰任何 Playwright 对象，故可安全地从别的线程执行。
        watchdog = None
        if self._user_data_dir:
            def _force_kill():
                print(f"  ⏱️ 关闭浏览器超时 {_CLOSE_WATCHDOG_SEC}s，强制回收 Chrome 进程")
                try:
                    _kill_chrome_for_profile(self._user_data_dir, '关闭超时', grace=0)
                except Exception as e:
                    print(f"  ⚠️ 强制回收失败: {str(e)[:120]}")

            watchdog = threading.Timer(_CLOSE_WATCHDOG_SEC, _force_kill)
            watchdog.daemon = True
            watchdog.start()

        # 这两处异常绝不能静默：close 失败意味着 Chrome 进程还活着，而 playwright.stop()
        # 随后就会拆掉 driver 传输通道，此后再没有任何途径能关掉它。以前吞掉异常的后果是
        # 日志里只剩「初始化 55 次 / 关闭 48 次」这种事后才看得出的差值。
        try:
            self.context.close()
        except Exception as e:
            print(f"  ⚠️ 关闭浏览器 context 失败: {str(e)[:120]}")
        try:
            self.playwright.stop()
        except Exception as e:
            print(f"  ⚠️ 停止 playwright 失败: {str(e)[:120]}")
        finally:
            if watchdog is not None:
                watchdog.cancel()

        # 核查 Chrome 是否真的退了。持久化 profile 才需要——临时 profile 目录随后就被
        # 整个删掉，且下次不会有人复用它。grace 让正常退出的 Chrome 有时间落盘 Cookies，
        # 只有超时还赖着不走的才回收。
        if self._user_data_dir and not self._temp_profile:
            _kill_chrome_for_profile(self._user_data_dir, '关闭后残留', grace=5)

        if self._temp_profile and os.path.exists(self._temp_profile):
            try:
                shutil.rmtree(self._temp_profile, ignore_errors=True)
                print(f"  🧹 已清理临时 profile: ...{os.path.basename(self._temp_profile)}")
            except Exception:
                pass


# credit-balance 接口 URL 标记（credits 页加载时页面会自行请求该接口）。
_CREDIT_BALANCE_URL_MARK = 'ai-gateway/billing/credit-balance'


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


BROWSER_LANG = "en-US"
BROWSER_ACCEPT_LANG = "en-US,en"
# 带 q 值的 Accept-Language 请求头（真人浏览器的标准写法）。
# Stripe 托管支付页、Cloudflare 页面均按此头选择显示语言。
BROWSER_ACCEPT_LANG_HEADER = "en-US,en;q=0.9"


def _write_profile_language(user_data_dir):
    """把英文语言偏好写进 Chrome profile（Local State + Default/Preferences）。

    macOS 上 --lang 不影响 UI 语言（Chrome 读系统 AppleLanguages），但页面语言由
    profile 的 intl.accept_languages 决定，它同时驱动 Accept-Language 头与
    navigator.languages —— 这正是 Cloudflare 页面选择语言的依据。
    合并写入（保留已有键），任何异常都不阻断启动：语言只是锦上添花，不值得让浏览器起不来。
    """
    import json as _json

    def _merge(path, patch):
        data = {}
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = _json.load(f) or {}
        except Exception:
            data = {}          # 读不动/坏了就当空的重建，别把启动搞挂
        for section, values in patch.items():
            node = data.setdefault(section, {})
            if isinstance(node, dict):
                node.update(values)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                _json.dump(data, f)
        except Exception as e:
            print(f"  ⚠️ 写入浏览器语言偏好失败(忽略): {str(e)[:80]}")

    _merge(os.path.join(user_data_dir, 'Default', 'Preferences'),
           {'intl': {'accept_languages': BROWSER_ACCEPT_LANG, 'selected_languages': BROWSER_ACCEPT_LANG}})
    _merge(os.path.join(user_data_dir, 'Local State'),
           {'intl': {'app_locale': BROWSER_LANG}})


# ======================================================================
# 持久化 profile 卫生：孤儿进程回收 + 缓存清理。
#
# 白屏根因：Chrome 异常退出留下的孤儿进程仍占着 user-data-dir，而启动新实例前
# 会无条件删掉 Singleton 锁，于是两个 Chrome 争抢同一份 leveldb，渲染进程起不来；
# 叠加从不清理的 Service Worker 缓存腐坏后返回空响应，页面 URL 正常却全白。
# ======================================================================

# 缓存类目录（均在 profile 的 Default/ 下）。删除它们不影响登录态——
# 登录信息在 Default/Cookies、Default/Login Data、Default/Local Storage
# 和 profile 根的 Local State 里，都不在此列表中。
_PROFILE_CACHE_DIRS = (
    'Cache', 'Code Cache', 'Service Worker', 'GPUCache', 'DawnCache', 'ShaderCache',
)


def _chrome_pids_for_profile(user_data_dir):
    """查出占用指定 user-data-dir 的 Chrome 进程 pid，主进程排在最前。

    只用标准库（项目未装 psutil）。`ps -Ao command=` 不像 `ps aux` 那样截断长命令行，
    能拿到完整的 --user-data-dir 路径。任何异常都返回空列表——本函数是尽力而为的
    卫生检查，绝不能因为它把浏览器启动搞挂。
    """
    import subprocess
    try:
        out = subprocess.run(
            ['ps', '-Ao', 'pid=,command='],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []

    needle = f'--user-data-dir={user_data_dir}'
    mains, helpers = [], []
    for line in out.splitlines():
        idx = line.find(needle)
        if idx < 0:
            continue
        # 必须匹配到路径边界，否则 /a/b 会误命中 /a/bc 这种兄弟 profile
        tail = line[idx + len(needle):]
        if tail and not tail[0].isspace():
            continue
        try:
            pid = int(line.split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        # 带 --type= 的是 renderer/gpu 等 helper 进程，杀主进程它们会跟着退
        (helpers if '--type=' in line else mains).append(pid)
    return mains + helpers


def _kill_chrome_for_profile(user_data_dir, reason, grace=0):
    """终止占用该 profile 的残留 Chrome，返回回收的进程数。

    grace：先等这么多秒看进程是否自行退出，期间退干净就返回 0（不打日志）。
    context.close() 之后必须留宽限期——close 返回时 Chrome 往往还在优雅退出、
    正把 Cookies 和 Local Storage 落盘，此刻抢着发信号会截断落盘，
    白屏没修成反而把登录态搞丢。启动路径则用 grace=0：那里遇到的是孤儿，等也不会退。

    并发安全性：AccountRegistry（src/web/worker.py）保证同一 email profile 任一时刻
    只被一个 worker 或一个手动会话持有，因此走到这里时占用该目录的进程必然是孤儿。
    这比原先「无条件删 Singleton 锁让新实例与孤儿并存」严格更安全。
    """
    pids = _chrome_pids_for_profile(user_data_dir)
    if not pids:
        return 0

    if grace > 0:
        deadline = time.time() + grace
        while time.time() < deadline:
            pids = _chrome_pids_for_profile(user_data_dir)
            if not pids:
                return 0          # 自己退干净了，正常路径，不该打日志
            time.sleep(0.25)

    import signal
    killed = len(pids)
    # 先一律 SIGTERM，给 Chrome 落盘 Cookies 的机会。主进程排在前面先收到信号，
    # helper 通常跟着主进程退，轮到它们时多半已经不在了（ProcessLookupError 吞掉）。
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.time() + 5
    while time.time() < deadline:
        if not _chrome_pids_for_profile(user_data_dir):
            break
        time.sleep(0.25)
    else:
        # 5 秒还没退干净就强杀，否则新实例照样要跟它抢 leveldb
        for pid in _chrome_pids_for_profile(user_data_dir):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    print(f"  ⚠️ 回收 {reason} 的残留 Chrome 进程 {killed} 个")
    return killed


def _prune_profile_cache(user_data_dir):
    """缓存类目录合计超阈值时整体清理。登录态不受影响，Chrome 会自动重建空缓存。

    必须在 _kill_chrome_for_profile 之后调用——删活着的 Chrome 的 Cache 目录会让它崩溃。
    用总量而非逐目录阈值：这几个目录是联动增长的，单看任一个都可能不触发，
    而白屏由整体缓存腐坏引起。
    """
    default_dir = os.path.join(user_data_dir, 'Default')
    targets = [os.path.join(default_dir, name) for name in _PROFILE_CACHE_DIRS]

    total = 0
    for path in targets:
        if not os.path.isdir(path):
            continue
        for root, _dirs, files in os.walk(path):
            for fname in files:
                try:
                    fpath = os.path.join(root, fname)
                    if not os.path.islink(fpath):
                        total += os.path.getsize(fpath)
                except OSError:
                    pass

    limit = PROFILE_CACHE_LIMIT_MB * 1024 * 1024
    if total <= limit:
        return          # 未超限静默返回，别每次启动都刷一行日志

    for path in targets:
        shutil.rmtree(path, ignore_errors=True)
    print(f"  🧹 profile 缓存 {total // (1024 * 1024)}MB 超过 "
          f"{PROFILE_CACHE_LIMIT_MB}MB，已清理（登录态保留）")


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
        user_data_dir = tempfile.mkdtemp(prefix="openrouter_chrome_")
        print(f"  🔄 使用全新浏览器 profile: ...{os.path.basename(user_data_dir)}")

    # 下载目录（用于 invoice PDF 下载等）
    download_dir = os.path.join(user_data_dir, 'downloads')
    os.makedirs(download_dir, exist_ok=True)

    # 浏览器语言强制英文（页面文案/选择器均按英文编写）。
    # 走 Chrome 原生路径：--lang 开关 + profile 里的 intl.accept_languages，
    # 二者都是真人浏览器的正常配置，navigator.languages 与 Accept-Language 天然一致。
    # 绝不用 Playwright 的 locale 选项——它经 CDP Emulation.setLocaleOverride 生效，
    # 会被 Cloudflare 检测为受控浏览器，导致 Turnstile 报 "problem with verification"。

    # 随机窗口尺寸（反检测）
    w, h = random.choice(_WINDOW_SIZES)

    # 启动参数保持最小（与真人浏览器一致）
    launch_args = [
        "--no-first-run",
        "--no-default-browser-check",
        f"--lang={BROWSER_LANG}",
        f"--accept-lang={BROWSER_ACCEPT_LANG}",
        f"--window-size={w},{h}",
    ]
    if headless:
        print("  👻 使用伪无头模式 (Off-screen)...")
        launch_args.append("--window-position=-10000,-10000")

    def _clear_singleton_locks():
        # 四步顺序不可调换，每一步都依赖前一步的结果：
        #   1) 回收孤儿进程 —— 让第 2 步「删锁是安全的」这个前提真正成立
        #   2) 删 Singleton 锁 —— 孤儿已清，此时删锁不会造成双实例
        #   3) 重置臃肿 Preferences
        #   4) 剪缓存 —— 必须在第 1 步之后，删活着的 Chrome 的 Cache 会让它崩溃
        if is_persistent:
            # 原注释称「本应用通过 open_browsers 保证同一 profile 单实例，可安全清理」，
            # 但该前提在有孤儿进程时并不成立：泄漏的 Chrome 仍占着 user-data-dir，
            # 强删锁后新实例与它争抢同一份 leveldb，渲染进程起不来 → 页面白屏。
            # 现在改为主动验证并回收，而不是假设。
            _kill_chrome_for_profile(user_data_dir, f'profile {safe_name}')

        # Chrome 异常退出会在 profile 里留下 Singleton 锁，导致下次启动失败
        # （"Mach rendezvous failed / parent died"）。
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

        # 缓存腐坏（尤其 Service Worker）会让 dash 的导航请求拿到空响应导致白屏。
        # 临时 profile 每次全新创建，无积累，跳过以省开销。
        if is_persistent:
            _prune_profile_cache(user_data_dir)

    playwright = sync_playwright().start()
    context = None
    last_err = None
    for attempt in range(2):          # 启动失败重试一次（清理残留锁后再试），提升稳定性
        _clear_singleton_locks()
        _write_profile_language(user_data_dir)   # 必须在 _clear 之后：它可能重置 Preferences
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

        # 强制英文页面：给所有请求加 Accept-Language: en-US 头。
        # 这是决定 Stripe 支付页 / Cloudflare 页面显示语言的权威依据——
        # 尤其对已存在的中文持久 profile，仅靠启动前写 Preferences 会被 Chrome
        # 退出时回写覆盖，而请求头不受 profile 影响，能稳定生效。
        # set_extra_http_headers 走 CDP Network 域（项目已用于响应监听），
        # 不触发 Emulation.setLocaleOverride，不引入 Turnstile 检测风险。
        try:
            context.set_extra_http_headers({"Accept-Language": BROWSER_ACCEPT_LANG_HEADER})
        except Exception as e:
            print(f"  ⚠️ 设置 Accept-Language 头失败(忽略): {str(e)[:80]}")

        temp_profile = None if is_persistent else user_data_dir
        session = BrowserSession(playwright, context, page,
                                 temp_profile=temp_profile, download_dir=download_dir,
                                 user_data_dir=user_data_dir)

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
        d0 = dialog.first
        # "I understand" 按钮：文本匹配 → data-kumo-part=close 的 button → role 兜底
        btn = d0.locator("button:has(span:text-is('I understand'))")
        if btn.count() == 0:
            btn = d0.locator("button[data-kumo-part='close']")
        if btn.count() == 0:
            btn = d0.get_by_role("button", name="I understand")
        if btn.count() > 0:
            # 留痕：弹窗出现即视为该账号已有账单（已绑过卡），供上层跳过补绑分支
            try:
                driver.overdue_dialog_seen = True
            except Exception:
                pass
            # 弹窗按钮偶有 actionability 问题，_safe_click 失败则 JS click 兜底
            try:
                _safe_click(btn.first, session=driver, desc="欠费弹窗 I understand", retries=1)
            except Exception:
                try:
                    btn.first.evaluate("el => el.click()")
                except Exception:
                    pass
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


# 卡号/有效期/CVC 字段选择器。命名经本地挂载真实 Payment Element 实测确认：
# name=number/expiry/cvc，id=payment-{number,expiry,cvc}Input（前缀是 element 类型，
# 故用后缀匹配）。旧 Card Element 命名一并保留。详见 spec/backend/stripe-payment-element.md
CARD_NUMBER_SEL = (
    'input[name="number"], input[id$="-numberInput"], '
    'input[name="cardnumber"], input[autocomplete="cc-number"], '
    'input[data-elements-stable-field-name="cardNumber"]'
)
CARD_EXPIRY_SEL = (
    'input[name="expiry"], input[id$="-expiryInput"], '
    'input[name="exp-date"], input[name="cardExpiry"], '
    'input[autocomplete="cc-exp"], '
    'input[data-elements-stable-field-name="cardExpiry"]'
)
CARD_CVC_SEL = (
    'input[name="cvc"], input[id$="-cvcInput"], '
    'input[name="cardCvc"], input[autocomplete="cc-csc"], '
    'input[data-elements-stable-field-name="cardCvc"]'
)

# 卡表单 iframe 的定位锚点，按优先级尝试。
# credit-card-form 下有两个 iframe（payment 与 ach-bank-search-results），
# 泛化选择器必须带 >> nth=0，否则 frame_locator 会因严格模式匹配到多个而报错。
_CARD_IFRAME_SELECTORS = [
    '[data-test-id="credit-card-form"] iframe[title="Secure payment input frame"]',
    'iframe[title="Secure payment input frame"]',
    '[data-test-id="credit-card-form"] iframe >> nth=0',
]


def stripe_card_frame(page):
    """返回卡表单 iframe 的 FrameLocator，找不到返回 None。

    必须用 FrameLocator 而非 page.frames 枚举出的 Frame 对象：Stripe 会反复销毁重建
    这个 iframe（实跑中观察到 name 从 __privateStripeFrame3005 变到 5985），
    缓存下来的 Frame 会指向已失效的旧文档——表现为 body 只剩几 KB、零 input，
    且怎么等都不会变，因为真正的新 frame 从未被看到。
    FrameLocator 是惰性的，每次使用都重新解析，天然免疫重建。
    """
    for sel in _CARD_IFRAME_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return page.frame_locator(sel)
        except Exception:
            continue
    return None


def _card_field_locator(page, selector):
    """在卡表单 iframe 内定位字段，返回 Locator 或 None（每次调用重新解析）。"""
    fl = stripe_card_frame(page)
    if fl is None:
        return None
    try:
        loc = fl.locator(selector).first
        if loc.count() > 0:
            return loc
    except Exception:
        pass
    return None


def _expand_stripe_card_accordion(page):
    """展开 Payment Element 的 "Card" 折叠项，使卡号/有效期/CVC 字段被创建。

    账号有多种支付方式（Card / Bank）时，Stripe 把 Payment Element 渲染成折叠的
    手风琴：界面上看得见 "Card" 和 "Bank" 两栏，但卡片面板 <div id="card"> 是空的，
    输入框在点击展开前根本不存在于 DOM 中。此前误判为「还在加载」并一路加长等待，
    实际上不点击就永远不会出现。

    只有一种支付方式时 Stripe 直接平铺，没有手风琴——此时本函数找不到按钮，
    返回 False，属正常情况，调用方不应据此判定失败。

    返回: 是否执行了展开操作
    """
    fl = stripe_card_frame(page)
    if fl is None:
        return False
    for sel in ('[role="button"][data-value="card"]',
                '[data-testid="payment-accordion-wrapper"] [role="button"]'):
        try:
            btn = fl.locator(sel).first
            if btn.count() == 0:
                continue
            if (btn.get_attribute('aria-expanded') or '').lower() == 'true':
                return False        # 已展开，无需再点
            btn.click(timeout=SHORT_TIMEOUT_MS)
            print("  🖱️ 已展开 Payment Element 的 Card 折叠项")
            time.sleep(2)
            return True
        except Exception:
            continue
    return False


def _uncheck_link_opt_in(page):
    """取消 Stripe Link 的「Save my information for faster checkout」勾选。

    该勾选框默认勾选，勾上后 Link 会要求填手机号，提交时报
    "Please provide a mobile phone number." 导致绑卡失败。我们只需存卡、不需要
    Link 账户，取消即可，无需真的去填手机号。

    DOM 结构（真机确认）：
        <label for="payment-linkOptInInput">
          <input id="payment-linkOptInInput" name="linkOptIn" type="checkbox" checked>
    input 被自定义样式遮住，直接 uncheck() 过不了可操作性检查，必须点 label。

    返回: 是否已处于未勾选状态
    """
    fl = stripe_card_frame(page)
    if fl is None:
        return False
    try:
        cb = fl.locator('input[name="linkOptIn"], input#payment-linkOptInInput').first
        if cb.count() == 0:
            return True          # 无该勾选框（Link 未启用），视为已满足
        if not cb.is_checked():
            return True
    except Exception:
        return False

    for sel in ('label[for="payment-linkOptInInput"]',
                'label#Field-linkOptInCheckbox',
                '.p-LinkOptIn-checkbox label'):
        try:
            lbl = fl.locator(sel).first
            if lbl.count() == 0:
                continue
            lbl.click(timeout=SHORT_TIMEOUT_MS)
            time.sleep(1)
            if not cb.is_checked():
                print("  ☑️ 已取消 Link「保存信息以便快捷结账」勾选")
                return True
        except Exception:
            continue

    # label 点不动时兜底：直接改 checked 并派发事件（Stripe 靠 React 状态渲染，
    # 只改属性不派发事件不会生效）
    try:
        cb.evaluate("""el => {
            if (el.checked) {
                el.checked = false;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""")
        time.sleep(1)
        if not cb.is_checked():
            print("  ☑️ 已取消 Link 勾选（JS 兜底）")
            return True
    except Exception:
        pass
    print("  ⚠️ 未能取消 Link 勾选，提交可能因缺少手机号而失败")
    return False


def _dump_visual_vs_dom(driver):
    """对比「屏幕上渲染出来的」与「DOM 查询看到的」，用于排查两者不一致。

    实测存在这种情况：卡号/有效期/CVC 在界面上完全可见，但对应 frame 的 DOM 查询
    读到零 input。这类问题不是选择器能解决的，必须先确认代码操作的页面是否就是
    显示中的那个页面。这里做三件事：
      1. 存一张截图，肉眼确认代码看到的画面是否与用户看到的一致
      2. 列出浏览器上下文里所有 page，确认没有在操作另一个标签页
      3. 用 elementFromPoint 反查卡表单 iframe 中心点，看命中的到底是什么元素
    """
    page = driver.page
    try:
        shot = os.path.join(tempfile.gettempdir(),
                            f"cf_stripe_debug_{int(time.time())}.png")
        page.screenshot(path=shot)
        print(f"  📸 已保存截图: {shot}")
    except Exception as e:
        print(f"  📸 截图失败: {str(e)[:80]}")

    try:
        pages = driver.context.pages
        print(f"  🔍 上下文内 page 数: {len(pages)}（当前操作的是第 "
              f"{pages.index(page) if page in pages else -1} 个）")
        for i, pg in enumerate(pages):
            mark = " ←当前" if pg is page else ""
            print(f"        [{i}] {str(pg.url)[:110]}{mark}")
    except Exception as e:
        print(f"  🔍 枚举 page 失败: {str(e)[:80]}")

    try:
        info = page.evaluate("""() => {
            const ifr = document.querySelector(
                '[data-test-id="credit-card-form"] iframe[title="Secure payment input frame"]')
                || document.querySelector('[data-test-id="credit-card-form"] iframe');
            if (!ifr) return {found: false};
            const r = ifr.getBoundingClientRect();
            const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            const hit = document.elementFromPoint(cx, cy);
            return {
                found: true,
                rect: {x: Math.round(r.left), y: Math.round(r.top),
                       w: Math.round(r.width), h: Math.round(r.height)},
                hitTag: hit ? hit.tagName.toLowerCase() : null,
                hitTitle: hit ? (hit.getAttribute('title') || '') : '',
                isSameIframe: hit === ifr,
            };
        }""")
        if not info.get('found'):
            print("  🔍 主文档中未找到卡表单 iframe 元素")
        else:
            r = info['rect']
            print(f"  🔍 卡表单 iframe 位置 {r['w']}x{r['h']} @({r['x']},{r['y']})，"
                  f"中心点命中 <{info['hitTag']}> title={info['hitTitle']!r} "
                  f"{'(就是该 iframe)' if info['isSameIframe'] else '(被其它元素遮挡)'}")
            if r['w'] == 0 or r['h'] == 0:
                print("        ❗ iframe 尺寸为 0，DOM 中存在但未占位")
    except Exception as e:
        print(f"  🔍 坐标反查失败: {str(e)[:80]}")


def _wait_for_stripe_fields_ready(driver, timeout=90):
    """
    等待 Stripe 卡号字段真正可定位。弹窗和外层 iframe 会先出现，内部字段延迟加载。

    判定只认「卡号字段可定位且可见」。曾用「弹窗内 iframe ≥2 个」当就绪判据，
    结果把 frame 失效问题伪装成就绪，拖到填写阶段才暴露。
    """
    page = driver.page
    card_inputs_sel = CARD_NUMBER_SEL

    def _vis(loc):
        try:
            return loc.is_visible()
        except Exception:
            return False

    weak_ready = False
    start = time.time()
    next_report = 15          # 首次进度播报时刻（秒），之后每 15s 一次
    while time.time() - start < timeout:
        try:
            # 主路径: 经 FrameLocator 定位（每轮重新解析，跟得上 Stripe 重建 iframe）
            inp = _card_field_locator(page, card_inputs_sel)
            if inp is not None and _vis(inp):
                print("  ✅ Stripe 输入字段已就绪")
                time.sleep(0.5)
                return True

            # 字段不存在时先试着展开 Card 折叠项——多支付方式下 Payment Element 是
            # 折叠手风琴，输入框在展开前不存在于 DOM。每轮都试：Stripe 重建 iframe 后
            # 会退回折叠态，只在首轮点一次不够。
            if _expand_stripe_card_accordion(page):
                inp = _card_field_locator(page, card_inputs_sel)
                if inp is not None and _vis(inp):
                    print("  ✅ Stripe 输入字段已就绪（展开 Card 折叠项后）")
                    time.sleep(0.5)
                    return True

            # 兜底: 遍历 page.frames。卡表单 iframe 锚点变更时仍有机会命中，
            # 但拿到的 Frame 可能已失效，故仅作补充而非主路径。
            main = page.main_frame
            for fr in [main] + [f for f in page.frames if f is not main]:
                try:
                    url = (fr.url or '').lower()
                    name = (fr.name or '').lower()
                except Exception:
                    url = name = ''
                if fr is not main and not (
                    'stripe' in url or 'stripe' in name
                    or name.startswith('__privatestripeframe')
                    or 'elements-inner' in url):
                    continue
                try:
                    inp = fr.locator(card_inputs_sel).first
                    if inp.count() > 0 and _vis(inp):
                        print("  ✅ Stripe 输入字段已就绪 (frames 兜底)")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue

            # 「弹窗内 iframe ≥2 个」只表示容器已渲染，不等于卡号字段可定位，
            # 故仅作状态记录、不能据此返回就绪。
            if not weak_ready:
                inner_frames = page.locator('[role="dialog"] iframe').all()
                if len([f for f in inner_frames if _vis(f)]) >= 2:
                    weak_ready = True

            # 周期性进度：整段等待可长达 90s，若只在开头打一行，看起来会像卡死。
            # 每 15s 报一次已等时长与骨架屏是否仍在，便于区分「在等」和「真卡住」。
            waited = int(time.time() - start)
            if waited >= next_report:
                next_report += 15
                # 注意：loader-ui frame 在场不代表界面真的还在加载——实测存在
                # 「界面已完全渲染可见、DOM 查询却读到零 input」的情况，此时该 frame
                # 仍挂着。故这里只如实报告 frame 状态，不推断界面是否加载完成。
                loader = any('elements-inner-loader-ui' in (f.url or '')
                             for f in page.frames)
                print(f"  ⏳ 等待卡号字段 {waited}s/{timeout}s"
                      f"（loader-ui frame {'在' if loader else '不在'}）")
        except Exception:
            pass
        time.sleep(1)

    if weak_ready:
        # 容器在、字段定位不到：多半是 Stripe 改版导致选择器失配，打印结构便于修选择器
        print("  ⚠️ iframe 已渲染但未定位到卡号字段")
        try:
            # 两条解析路径都打：page.frames 全局枚举 + DOM→content_frame 锚定
            probe = [f for f in page.frames if f is not page.main_frame]
            for f in _card_form_frames(page):
                if f not in probe:
                    probe.append(f)
            _dump_stripe_frame_fields(page, probe)
        except Exception:
            pass
        _dump_visual_vs_dom(driver)
        # Payment Element 卡在加载态时，原因往往不在 DOM 而在被拒的接口或 JS 报错
        try:
            errs = list(driver.console_all_errors or []) or list(driver.console_errors or [])
            if errs:
                print(f"  🔍 控制台错误 (最近 {min(len(errs), 15)} 条):")
                for e in errs[-15:]:
                    print(f"        {e[:200]}")
            else:
                print("  🔍 控制台无错误记录")
        except Exception:
            pass
        try:
            bad = list(driver.failed_responses or [])
            if bad:
                print(f"  🔍 失败的网络请求 (最近 {min(len(bad), 15)} 条):")
                for r in bad[-15:]:
                    print(f"        {r.get('status')} {str(r.get('url'))[:150]}")
            else:
                print("  🔍 无 4xx/5xx 网络请求记录")
        except Exception:
            pass
    else:
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


def login_cloudflare(driver, email: str, password: str, email_password: str = None):
    """
    登录已有的 Cloudflare 账号

    参数:
        driver: 浏览器驱动
        email: 邮箱地址
        password: CF 密码
        email_password: 邮箱密码。传入后可自动通过邮箱二次验证
                        (two-factor?type=email)；不传则遇到 2FA 页会登录失败。
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

        # 时间闸门起点：必须在点击登录按钮「之前」取。若在提交后才取，
        # 邮件可能已先一步到达，导致被当成旧邮件过滤掉而白等到超时。
        login_submit_ts = datetime.now(timezone.utc)

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

        # 邮箱二次验证：清空过 profile 的账号会被 CF 判定为新设备，
        # 跳转到 two-factor?type=email 要求填 7 位邮箱验证码。
        if not _handle_email_two_factor(driver, email, email_password, login_submit_ts):
            return None

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


def _handle_email_two_factor(driver, email, email_password, since_ts, wait_page=15):
    """处理登录后的邮箱二次验证页 (two-factor?type=email)。

    参数:
        since_ts: aware datetime，取值时机须早于登录按钮点击（见调用处注释）
        wait_page: 秒，等待 2FA 页出现的观察窗口
    返回:
        bool: True 表示可以继续后续流程（没出现 2FA 页，或已成功填码）；
              False 表示登录应判定为失败。
    """
    # 2FA 页可能在跳转链路上稍后才出现，短轮询确认
    on_2fa = False
    for _ in range(wait_page):
        if 'two-factor' in (driver.current_url or ''):
            on_2fa = True
            break
        if _extract_account_id(driver):
            return True  # 已直达控制台，无需 2FA
        time.sleep(1)

    if not on_2fa:
        return True

    print("检测到邮箱二次验证页 (two-factor)")

    if not email_password:
        print("  未提供邮箱密码，无法自动获取验证码 —— 登录失败")
        return False

    token = get_mail_token(email, email_password)
    if not token:
        print("  获取邮箱 token 失败 —— 登录失败")
        return False

    code = wait_for_login_code(token, since_ts)
    if not code:
        print("  未收到新的登录验证码 —— 登录失败")
        return False

    if not handle_email_verification(driver, code):
        print("  填入验证码失败 —— 登录失败")
        return False

    print("  验证码已提交，等待跳转...")
    time.sleep(5)
    check_and_handle_cf_challenge(driver)
    time.sleep(3)

    if 'two-factor' in (driver.current_url or ''):
        print("  提交后仍停留在二次验证页（码可能已过期或被拒） —— 登录失败")
        return False

    return True


def _extract_account_id(driver):
    """从当前 URL 中提取 Cloudflare account_id"""
    current_url = driver.current_url
    if 'dash.cloudflare.com' in current_url:
        parts = current_url.replace('https://dash.cloudflare.com/', '').split('/')
        if parts and parts[0] and len(parts[0]) == 32:
            return parts[0]
    return None


def reset_credit_balance(driver):
    """清空被动监听缓存的余额。在导航到 credits 页「之前」调用，
    确保随后 wait_for_credit_balance 拿到的是本次页面新请求的余额，而非上次残留。"""
    try:
        driver.credit_balance = None
        driver.credit_balance_ts = None
    except Exception:
        pass


def wait_for_credit_balance(driver, timeout_ms=15_000, poll_ms=500):
    """等待被动监听捕获 credit-balance 接口的最新余额（美元）。

    credits 页加载后页面会自动请求该接口，其响应由 create_driver 挂载的
    page.on("response") 监听器捕获并缓存到 driver.credit_balance。sync Playwright
    的 response 事件只在调用 Playwright API 时派发，故这里用 page.wait_for_timeout
    （而非 time.sleep）轮询驱动事件循环，直到余额就绪或超时。

    典型用法：reset_credit_balance(driver) → driver.get(credits_url)
              → wait_for_credit_balance(driver)。

    返回:
        float | None: 余额（美元），超时仍未捕获返回 None。
    """
    waited = 0
    while waited < timeout_ms:
        if driver.credit_balance is not None:
            return driver.credit_balance
        try:
            driver.page.wait_for_timeout(poll_ms)
        except Exception:
            break
        waited += poll_ms
    return driver.credit_balance


def read_credits_balance(driver, timeout_ms=30_000):
    """
    读取账号 AI Credits 余额（美元）。

    在已登录的 dash.cloudflare.com 页面上下文用 fetch 调
    ai-gateway/billing/credit-balance 接口（同源带 cookie），以接口返回的
    result.balance 为权威。接口值为"分"（4000 == $40.00），这里换算成美元返回，
    与旧 DOM 读法的口径一致，下游落库/比较逻辑无需变化。

    需当前页面已处于 dash.cloudflare.com（同源）且 URL 含 account_id
    （调用方均在导航到 credits 页后调用，天然满足）。

    返回:
        float | None: 余额（美元），读取失败返回 None
    """
    account_id = _extract_account_id(driver)
    if not account_id:
        print("  读取 Credits 余额失败: 当前 URL 无 account_id")
        return None
    try:
        result = driver.page.evaluate(
            """async ({accountId, timeoutMs}) => {
                const url = `https://dash.cloudflare.com/api/v4/accounts/${accountId}`
                    + `/ai-gateway/billing/credit-balance`;
                const resp = await fetch(url, {
                    credentials: 'include',
                    headers: {'accept': 'application/json'},
                    signal: AbortSignal.timeout(timeoutMs),
                });
                if (!resp.ok) return {ok: false, status: resp.status};
                const body = await resp.json();
                if (!body || body.success !== true || !body.result
                    || typeof body.result.balance !== 'number') {
                    return {ok: false, status: 'bad_body'};
                }
                return {ok: true, balance: body.result.balance};
            }""",
            {"accountId": account_id, "timeoutMs": timeout_ms})
        if not result or not result.get('ok'):
            status = (result or {}).get('status')
            print(f"  读取 Credits 余额失败: credit-balance 接口异常（{status}）")
            return None
        # 接口以"分"计（4000 == $40.00），换算为美元
        return result['balance'] / 100.0
    except Exception as e:
        print(f"  读取 Credits 余额失败: {str(e)[:80]}")
        return None


def fetch_today_invoice_count(driver, account_id):
    """读取指定 CF 账号「当日创建」的账单(invoice)数，以 invoice-history 接口为权威。

    在已登录的 dash.cloudflare.com 页面上下文用 fetch 调 billing/invoice-history 接口
    （同源带 cookie），统计 result.invoices[] 中 created(unix 秒) 落在**本地当日**
    [今天0点, 明天0点) 的条数，paid + open 全部计入。接口分页，invoices 按 created 倒序，
    翻到某页出现早于今日0点的记录即可停止翻页。

    与项目其它「当日」口径（datetime('now','localtime')）一致，以运行机器本地时区为准。

    参数:
        driver: 浏览器驱动
        account_id: Cloudflare 账号 ID
    返回:
        int:  当日账单数
        None: 读取/解析失败（调用方应保守处理，勿据此放行超额生成）
    """
    # 本地当日边界（机器本地时区，DST 交由 mktime 的 isdst=-1 自决）
    lt = time.localtime()
    start_of_today = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    end_of_today = start_of_today + 86400

    # fetch 需同源：确保当前处于 dash.cloudflare.com，否则会被跨源拦截
    try:
        cur = driver.current_url or ''
    except Exception:
        cur = ''
    if 'dash.cloudflare.com' not in cur:
        try:
            driver.get(f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits")
            time.sleep(4)
        except Exception as e:
            print(f"  读取当日账单数前导航失败: {str(e)[:80]}")

    try:
        result = driver.page.evaluate(
            """async ({accountId, startTs, endTs}) => {
                let total = 0;
                for (let page = 1; page <= 20; page++) {
                    const url = `https://dash.cloudflare.com/api/v4/accounts/${accountId}`
                        + `/ai-gateway/billing/invoice-history?page=${page}&per_page=50`;
                    const resp = await fetch(url, {
                        credentials: 'include',
                        headers: {'accept': 'application/json'},
                    });
                    if (!resp.ok) return {ok: false, status: resp.status};
                    const body = await resp.json();
                    if (!body || body.success !== true || !body.result) {
                        return {ok: false, status: 'bad_body'};
                    }
                    const invoices = body.result.invoices || [];
                    let reachedOld = false;
                    for (const inv of invoices) {
                        const c = inv.created;
                        if (typeof c !== 'number') continue;
                        if (c < startTs) { reachedOld = true; continue; }
                        if (c >= startTs && c < endTs) total += 1;
                    }
                    const pg = body.result.pagination || {};
                    if (reachedOld || !pg.has_more) break;
                }
                return {ok: true, count: total};
            }""",
            {"accountId": account_id, "startTs": start_of_today, "endTs": end_of_today},
        )
        if not isinstance(result, dict) or not result.get('ok'):
            print(f"  读取当日账单数失败: {result}")
            return None
        n = int(result.get('count', 0))
        print(f"  当日账单数（CF invoice-history）: {n}")
        return n
    except Exception as e:
        print(f"  读取当日账单数异常: {str(e)[:100]}")
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
    """关闭 Top-up 弹窗。

    直接按 Escape：该弹窗的 Close 按钮长期不满足 Playwright actionability，
    _safe_click 会重试到超时（实测累计 ~45s 全部失败），而 Escape 一次即可关闭，
    且后续账单表格交互本身带 dismiss_overdue_dialog + 滚动/force 兜底，
    即便有残留弹窗也不阻塞。故不再前置点击重试、也不做过宽的弹窗计数校验
    （页面可能同时存在欠费提示弹窗，会导致误判未关闭）。
    """
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


_DECLINE_REASONS = {
    'insufficient_funds': '余额不足',
    'incorrect_number': '卡号错误',
    'invalid_number': '卡号无效',
    'incorrect_cvc': 'CVC 安全码错误',
    'invalid_cvc': 'CVC 安全码无效',
    'incorrect_zip': '邮编/账单地址不匹配',
    'invalid_expiry_month': '有效期月份无效',
    'invalid_expiry_year': '有效期年份无效',
    'expired_card': '卡已过期',
    'card_declined': '银行拒付',
    'do_not_honor': '银行拒绝（do not honor）',
    'generic_decline': '通用拒付',
    'lost_card': '卡已挂失',
    'stolen_card': '卡被盗',
    'pickup_card': '卡被没收',
    'card_not_supported': '卡不支持此交易',
    'currency_not_supported': '货币不支持',
    'fraudulent': '疑似欺诈被拒',
    'processing_error': '处理错误',
    'authentication_required': '需要 3DS 银行验证',
    'try_again_later': '请稍后重试',
}


# 这些 decline code 是临时/环境问题，不代表卡本身无效 → 不把卡标记为无效。
# 注意 authentication_required（3DS）不在此列：本流程无法完成银行验证，
# 需要 3DS 的卡在这里等同于不可用，按卡无效处理。
_TRANSIENT_DECLINE_CODES = {
    'processing_error',
    'try_again_later',
}


def _extract_payment_error(driver):
    """从捕获的支付响应或页面错误文案提取失败原因。

    返回 (reason, card_fault)：reason 为中文说明（无则 None），
    card_fault 表示失败是否归因于卡本身（拒付/卡号/CVC/过期等）——
    只有 card_fault 才应把底料卡标记为无效；临时错误与脚本问题不标记。
    """
    import json as _json
    for r in list(getattr(driver, 'net_responses', []) or []):
        data = r.get('data')
        txt = _json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data or '')
        m = re.search(r'"decline_code"\s*:\s*"([^"]+)"', txt)
        if m:
            code = m.group(1)
            return (_DECLINE_REASONS.get(code, f'拒付（{code}）'),
                    code not in _TRANSIENT_DECLINE_CODES)
        m = re.search(r'"code"\s*:\s*"([a-z_]+)"', txt)
        if m and m.group(1) in _DECLINE_REASONS:
            code = m.group(1)
            return _DECLINE_REASONS[code], code not in _TRANSIENT_DECLINE_CODES
        m = re.search(r'"message"\s*:\s*"([^"]+)"', txt)
        if m and any(k in m.group(1).lower() for k in
                     ['declin', 'insufficient', 'incorrect', 'invalid', 'expired', 'not support', 'fund']):
            return m.group(1)[:120], True
    try:
        for sel in ['[role="alert"]', '.p-Notice-content', '.p-Notice', '.Error']:
            loc = driver.page.locator(sel)
            cnt = loc.count()
            for i in range(min(cnt, 3)):
                t = (loc.nth(i).inner_text(timeout=SHORT_TIMEOUT_MS) or '').strip()
                if t and any(k in t.lower() for k in
                             ['declin', 'insufficient', 'incorrect', 'invalid', 'expired', 'not support', 'fund', 'wrong']):
                    return t[:120], True
    except Exception:
        pass
    return None, False


def extract_decline_from_responses(responses):
    """从**已捕获的响应列表**（元素形如 {url,status,data}）中提取支付拒付原因。

    与 `_extract_payment_error` 的响应扫描部分同源，但作用于调用方冻结保存的 responses，
    不依赖 live `driver.net_responses`——后者会被后续账单支付的 inject_network_interceptor
    清空，导致 Top-up 后再去读拿到过期数据。

    返回 (reason, card_fault)：reason 为中文说明（无则 None）；card_fault 表示是否归因于卡本身
    （拒付/卡号/CVC/过期等，瞬时错误除外）——仅 card_fault 才应把卡标记为无效。
    """
    import json as _json
    for r in (responses or []):
        data = r.get('data')
        txt = _json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data or '')
        m = re.search(r'"decline_code"\s*:\s*"([^"]+)"', txt)
        if m:
            code = m.group(1)
            return (_DECLINE_REASONS.get(code, f'拒付（{code}）'),
                    code not in _TRANSIENT_DECLINE_CODES)
        m = re.search(r'"code"\s*:\s*"([a-z_]+)"', txt)
        if m and m.group(1) in _DECLINE_REASONS:
            code = m.group(1)
            return _DECLINE_REASONS[code], code not in _TRANSIENT_DECLINE_CODES
        m = re.search(r'"message"\s*:\s*"([^"]+)"', txt)
        if m and any(k in m.group(1).lower() for k in
                     ['declin', 'insufficient', 'incorrect', 'invalid', 'expired', 'not support', 'fund']):
            return m.group(1)[:120], True
    return None, False


def _has_3ds_challenge(driver):
    """检测是否出现 Stripe 3DS 银行验证挑战弹窗（无法自动完成）。"""
    try:
        if driver.page.locator('iframe[src*="three-ds"]').count() > 0:
            return True
        overlay = driver.page.locator('div[data-react-aria-top-layer]')
        if overlay.count() > 0 and overlay.locator('iframe[src*="stripe"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _cancel_3ds_challenge(driver):
    """尝试取消 3DS 挑战（点 Cancel/取消/关闭）；失败则由上层导航离开放弃。"""
    try:
        fl = driver.page.frame_locator('iframe[src*="three-ds"]')
        for name in ['Cancel', '取消', 'Close', 'close', '关闭', 'No']:
            try:
                btn = fl.get_by_role('button', name=name)
                if btn.count() > 0:
                    btn.first.click(timeout=SHORT_TIMEOUT_MS)
                    print("    已点击 3DS 取消")
                    return True
            except Exception:
                continue
        driver.page.keyboard.press('Escape')
    except Exception:
        pass
    return False


_CONFIRM_BTN_KEYWORDS = ('确认付款', '确认支付', 'confirm payment', 'confirm')


def _find_confirm_payment_button(driver):
    """
    定位 Stripe 的"确认付款"二次确认按钮（提交卡后出现的 ContentCard 卡片）。

    该按钮与表单的 Pay 按钮共用 data-testid='hosted-payment-submit-button'，
    区别在于它是 type='button'（表单里的是 type='submit'），且 class 带
    'SubmitButton--complete'（就绪待确认态；普通支付按钮是 '--incomplete'）。
    不点它页面会一直停在"确认 US$xx 的付款 / 银行会要求验证身份"卡片上。

    识别用两条互补路径，任一命中即认：
      1) class 含 'submitbutton--complete' —— 权威的"就绪待确认"信号，
         不依赖文案（按钮盖着 Shimmer 流光动画 + 含隐藏的"正在处理"span，
         inner_text 在动画未稳时可能取空/抖动，单靠文案会漏点）。
      2) inner_text 命中"确认付款/confirm"等关键词 —— 兜底覆盖 class 变体。
    返回可点击的 locator；不存在 / 正在处理中 / 已完成时返回 None。
    """
    try:
        btns = driver.page.locator(
            "button[data-testid='hosted-payment-submit-button'][type='button']"
        )
        for i in range(min(btns.count(), 3)):
            btn = btns.nth(i)
            try:
                if not btn.is_visible(timeout=SHORT_TIMEOUT_MS) or not btn.is_enabled(timeout=SHORT_TIMEOUT_MS):
                    continue
                cls = (btn.get_attribute('class', timeout=SHORT_TIMEOUT_MS) or '').lower()
                if 'processing' in cls or 'disabled' in cls:
                    continue
                if 'submitbutton--complete' in cls:
                    return btn                     # 路径 1：就绪态 class 直接认
                txt = (btn.inner_text(timeout=SHORT_TIMEOUT_MS) or '').strip().lower()
                if any(k in txt for k in _CONFIRM_BTN_KEYWORDS):
                    return btn                     # 路径 2：文案兜底
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_confirm_payment(driver, invoice_id=''):
    """点击"确认付款"二次确认按钮；返回是否点击了。"""
    btn = _find_confirm_payment_button(driver)
    if btn is None:
        return False
    print(f"  {invoice_id} 检测到「确认付款」二次确认，点击继续...")
    clicked = False
    try:
        _safe_click(btn, session=driver, desc='确认付款按钮', retries=1)
        clicked = True
    except Exception:
        pass
    if not clicked or _find_confirm_payment_button(driver) is not None:
        # 普通点击失败 / 点后仍停在可确认态（未真正提交）→ JS click 兜底
        try:
            btn.evaluate("el => el.click()")
            clicked = True
        except Exception:
            pass
    driver.page.wait_for_timeout(2000)
    return clicked


# Stripe 账单失效提示的核心短语。完整句是 "This invoice can no longer be paid
# on Stripe. Please contact Cloudflare..."，随文案/版本微调，故只匹配稳定子串防漏判。
_INVOICE_UNPAYABLE_HINT = "can no longer be paid"


def _is_invoice_unpayable(driver):
    """支付页是否出现「此账单已无法在 Stripe 支付」的失效提示（主文档层，非 iframe）。

    出现它代表账单在 Stripe 侧已彻底失效（需联系 Cloudflare 另行处理），此时整个
    支付表单被隐藏（display:none）、Pay 按钮虽在 DOM 却永不可见——换卡或重试都
    毫无意义，应直接跳过该发票转下一张。
    """
    try:
        hint = driver.page.get_by_text(_INVOICE_UNPAYABLE_HINT)
        return hint.count() > 0 and hint.first.is_visible()
    except Exception:
        return False


def _reveal_payment_form_if_saved_card(driver, invoice_id=''):
    """
    支付页若直接停在"确认付款"（已保存支付方式）态、卡表单被隐藏，
    点击"选择新的支付方式"把 InvoicePaymentFormWrapper 展开，好用我们自己的卡填写。
    返回是否做了切换。

    该链接文案随语言/版本变化（实测英文是 "Select a new payment method"，
    也出现过 "Choose/Use a new payment method"），故用公共子串匹配 accessible
    name（get_by_role 的 name 默认子串、不区分大小写），一次覆盖各种说法，
    避免文案微调就漏点导致表单展不开、后续点 Card 选项超时。
    """
    try:
        wrapper = driver.page.locator("div.InvoicePaymentFormWrapper")
        if wrapper.count() == 0 or wrapper.first.is_visible(timeout=SHORT_TIMEOUT_MS):
            return False   # 表单本来就可见 → 正常填卡流程
        for name in ['new payment method', '新的支付方式']:
            link = driver.page.get_by_role('button', name=name)
            if link.count() > 0:
                link.first.click(timeout=CLICK_TIMEOUT_MS)
                # 等表单真正展开（wrapper 变可见）再返回，避免过早去点隐藏的 Card 选项
                try:
                    wrapper.first.wait_for(state="visible", timeout=CLICK_TIMEOUT_MS)
                except Exception:
                    driver.page.wait_for_timeout(2000)
                print(f"  {invoice_id} 页面停在已保存卡的确认态，已切换为新支付方式")
                return True
    except Exception:
        pass
    return False


def _stripe_payment_frame(driver):
    """定位 Stripe Payment Element 的「卡输入」iframe，返回 frame_locator。

    支付页存在两种组件结构：
      - 旧结构：#payment-element 下只有一个 iframe（卡输入表单）。
      - 新结构：#payment-element 下渲染多个 iframe——除卡输入帧外，还有 ACH 银行搜索
        结果帧（title="Bank search results"，aria-hidden、height 4px），页面上方另有
        #express-checkout-element 钱包快捷支付区 + "Or" 分隔。此时
        frame_locator("div#payment-element iframe") 会同时匹配到多个 frame，Playwright
        strict 模式下任何交互都会抛 "resolved to N elements"，导致点 Card / 填卡失败。

    这里优先用稳定的 title 精确锁定卡输入帧（title="Secure payment input frame"），
    命中不了再退回容器内第一个 iframe（.first 兼容旧的单帧结构，也顺带规避 strict）。
    """
    page = driver.page
    titled_sel = "div#payment-element iframe[title='Secure payment input frame']"
    try:
        if page.locator(titled_sel).count() > 0:
            return page.frame_locator(titled_sel).first
    except Exception:
        pass
    return page.frame_locator("div#payment-element iframe").first


def _fill_stripe_payment_and_submit(driver, card_info, invoice_id='', should_stop=None):
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
        # 定位 Stripe Payment Element 卡输入 iframe（兼容新旧两种组件结构，见 _stripe_payment_frame）
        stripe_frame = _stripe_payment_frame(driver)

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

        # 选完国家后地址区会重新渲染，邮编字段随之重挂载——留足时间再定位，避免拿到
        # 正在被替换的旧节点导致后续 click 长时间等待。
        driver.page.wait_for_timeout(1500)

        # 填写 ZIP code（防御：邮编字段对部分卡/表单变体渲染慢或根本不出现——
        # 存在才填、缺失/超时则跳过、不致命。否则 click 默认死等 30s，一张账单会白白
        # 耗掉两次脚本重试仍失败，如 IN-71449467/IN-71450046 的 postalCodeInput 超时。）
        zip_code = card_info.get('zip', '')
        if zip_code:
            try:
                zip_input = stripe_frame.locator("input#payment-postalCodeInput")
                if _wait_visible(zip_input, timeout=15_000):
                    zip_input.click(timeout=SHORT_TIMEOUT_MS)
                    time.sleep(0.3)
                    zip_input.press_sequentially(str(zip_code), delay=50)
                    print(f"  {invoice_id} 已输入 ZIP: {zip_code}")
                    time.sleep(0.5)
                else:
                    print(f"  {invoice_id} 未见邮编字段（等待 15s），跳过 ZIP 填写")
            except Exception as e:
                print(f"  {invoice_id} ZIP 填写失败(继续): {str(e)[:60]}")

        # 取消 "Save my information for faster checkout"（Stripe Link opt-in，默认勾选）——
        # 不把支付信息保存到 Link，避免跨账号关联留存。该 checkbox 未必总出现（取决于邮箱是否
        # 关联 Link 等），存在且已勾选才点、缺失/超时跳过（不致命）。input 常被自定义样式覆盖，
        # 优先点 label 触发 toggle。
        try:
            optin = stripe_frame.locator("input#payment-linkOptInInput")
            if optin.count() > 0 and optin.is_checked(timeout=SHORT_TIMEOUT_MS):
                label = stripe_frame.locator("label[for='payment-linkOptInInput']")
                target = label.first if label.count() > 0 else optin.first
                target.click(timeout=SHORT_TIMEOUT_MS)
                time.sleep(0.3)
                # 校验是否真的取消；若仍勾选，退回直接点 input 兜底
                try:
                    if optin.is_checked(timeout=SHORT_TIMEOUT_MS):
                        optin.first.click(timeout=SHORT_TIMEOUT_MS, force=True)
                        time.sleep(0.3)
                except Exception:
                    pass
                print(f"  {invoice_id} 已取消「保存支付信息」勾选")
        except Exception as e:
            print(f"  {invoice_id} 取消保存信息勾选失败(继续): {str(e)[:60]}")

        driver.capture_frame()
        time.sleep(1)

        import json as _json

        # 注入网络拦截器捕获支付结果（confirm 响应是判定真实成功的依据）
        inject_network_interceptor(driver, [
            ['api.stripe.com', 'confirm'],
            ['stripe.com', 'confirm'],
            ['invoice.stripe.com', 'confirm'],
        ])

        # 点击 Pay 按钮（位于主文档，不在 iframe 内）。
        # 普通 click 可能"假成功"（actionability 通过但未真正提交），故追加 JS click 兜底，
        # 确保真正触发表单提交。
        # 优先取表单里的 type=submit 按钮：页面上若同时存在"确认付款"卡片，
        # 它也带同样的 data-testid（type=button），.first 会误选到它。
        submit_btns = driver.page.locator(
            "button[data-testid='hosted-payment-submit-button'][type='submit']"
        )
        pay_btn = (submit_btns.first if submit_btns.count() > 0
                   else driver.page.locator("button[data-testid='hosted-payment-submit-button']").first)
        _wait_visible(pay_btn, timeout=30_000)
        print(f"  {invoice_id} 正在点击 Pay...")
        try:
            _safe_click(pay_btn, session=driver, desc='Pay 按钮', retries=1)
        except Exception:
            pass
        try:
            pay_btn.evaluate("el => el.click()")   # JS 兜底，确保真正提交
        except Exception:
            pass
        print(f"  {invoice_id} 已点击 Pay，等待支付结果...")

        # 等待支付处理：期间检测 3DS 银行验证弹窗（无法自动完成→取消并判失败），
        # 并提取失败原因（decline_code / 页面错误文案）。是否真正成功由上层按
        # "账单是否仍在 Unpaid 列表" 权威判定；本函数负责取消 3DS + 给出失败原因。
        # 支付是耗时操作（十几秒~几十秒，网络差更久）：观测窗口给足 90s，等到出现
        # 明确结果（3DS / 成功 / 拒付原因）才收手，期间持续等待"处理中"状态。
        is_3ds = False
        reason = None
        card_fault = False
        confirm_clicks = 0
        deadline = time.time() + 90
        while time.time() < deadline:
            # 支付结果等待是本流程最长的一段（最多 90s）：期间响应停止请求，
            # 否则用户点停止后仍要空等一整轮。抛 InterruptedError 由上层 break。
            if should_stop and should_stop():
                raise InterruptedError("用户请求停止")
            driver.page.wait_for_timeout(2000)     # 驱动 Playwright 事件循环 + 等待
            # 0) "确认付款"二次确认卡片：提交卡后 Stripe 可能返回「确认 US$xx 的付款」
            #    卡片（含"银行会要求您验证您的身份"），不点则页面永远停在这里。
            #    点完可能直接成功、也可能转入 3DS（下一步捕获）。点完重新给足观测窗口。
            #    幂等地点击已提交的确认按钮无副作用，故容错上限给到 3。
            if confirm_clicks < 3 and _click_confirm_payment(driver, invoice_id):
                confirm_clicks += 1
                deadline = time.time() + 90
                continue
            # 1) 3DS 挑战弹窗
            if _has_3ds_challenge(driver):
                is_3ds = True
                break
            # 1.5) 支付成功组件（最可靠）：Stripe 支付成功后渲染
            #      data-testid="invoice-summary-post-payment"（"Invoice paid"文案 +
            #      金额 invoice-amount-post-payment + 收据下载按钮）。DOM 命中即真实
            #      成功，优先于网络响应/页面文案判定。
            try:
                if driver.page.locator(
                    "[data-testid='invoice-summary-post-payment'], "
                    "[data-testid='invoice-amount-post-payment'], "
                    "[data-testid='download-invoice-receipt-pdf-button']"
                ).count() > 0:
                    print(f"  {invoice_id} 检测到支付成功组件（Invoice paid）")
                    return {"status": "paid", "responses": list(driver.net_responses)}
            except Exception:
                pass
            # 2) 成功信号（快速返回）
            blob = " ".join(
                (_json.dumps(r.get('data'), ensure_ascii=False)
                 if isinstance(r.get('data'), (dict, list)) else str(r.get('data') or ''))
                for r in list(driver.net_responses)
            )
            if '"status": "succeeded"' in blob or '"status":"succeeded"' in blob \
                    or '"paid": true' in blob or '"paid":true' in blob:
                print(f"  {invoice_id} 检测到支付成功信号")
                return {"status": "paid", "responses": list(driver.net_responses)}
            # 3) 失败原因（响应 decline_code / 页面错误）
            reason, card_fault = _extract_payment_error(driver)
            if reason:
                break
            # 4) 页面成功文案
            try:
                body = driver.page.inner_text('body', timeout=SHORT_TIMEOUT_MS).lower()
            except Exception:
                body = ''
            if any(k in body for k in ['payment received', 'payment complete', 'thanks for your payment',
                                       'you paid', 'receipt from', 'paid on', 'invoice paid']):
                return {"status": "paid", "responses": list(driver.net_responses)}

        collected = list(driver.net_responses)
        if collected:
            print(f"  {invoice_id} 收到 {len(collected)} 条支付响应")
        if is_3ds:
            # 3DS 银行验证无法自动完成，本流程内这张卡不可用（换卡重试同发票）。
            # tds=True 供上层区分：曾成功的卡遇 3DS 走"临时冷却"而非永久作废（R3）。
            print(f"  {invoice_id} 出现 3DS 银行验证，取消并判为失败（本轮不可用）")
            _cancel_3ds_challenge(driver)
            return {"status": "failed", "error": "需要 3DS 银行验证（已取消）",
                    "card_fault": True, "tds": True, "responses": collected}
        if reason:
            print(f"  {invoice_id} 支付失败，原因: {reason}（卡自身问题: {'是' if card_fault else '否'}）")
            return {"status": "failed", "error": reason,
                    "card_fault": card_fault, "responses": collected}
        # 未见明确成功/失败信号 → 交由上层按账单状态权威判定
        print(f"  {invoice_id} 未捕获明确成功/失败信号，交由账单状态判定")
        return {"status": "uncertain", "error": "支付结果未确认",
                "card_fault": False, "responses": collected}

    except InterruptedError:
        # 用户请求停止：不吞掉，向上传递让 handle_unpaid_invoices break、
        # 最终由 recharge_account 的 finally 关闭浏览器。
        raise
    except Exception as e:
        # 填表/定位异常属于脚本侧失败，与卡是否有效无关 → 不标记卡
        print(f"  {invoice_id} Stripe 支付填写失败: {e}")
        return {"status": "failed", "error": f"Stripe 支付填写失败: {e}", "card_fault": False}


def _extract_pdf_pay_url(pdf_path):
    """从 invoice PDF 中提取「Pay online」支付链接。

    账单 PDF 的「Pay online」按钮是个带 /URI 的链接注释，指向 Stripe 托管支付页
    https://invoice.stripe.com/i/acct_.../live_<token>?s=pd（含一次性 token）。
    部分 PDF 还含第二个链接 https://profile.stripe.com/cloudflare?source=invoice_pdf
    （管理账单入口，**非**支付链接），且它排在支付链接之前——它同样含 'stripe'，
    若按宽泛关键词取「第一个命中的」会误取到它，跳转后到不了支付页。
    故这里优先精确匹配 invoice.stripe.com 的支付链接，宽泛关键词仅作兜底且排除 profile 链接。
    """
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        uris = []
        for page in reader.pages:
            if '/Annots' not in page:
                continue
            for annot in page['/Annots']:
                obj = annot.get_object()
                a = obj.get('/A')
                if a and a.get('/URI'):
                    uris.append(a['/URI'])
        # 1) 精确取 Stripe 托管支付页（带 token 的 Pay online 链接）
        for uri in uris:
            if 'invoice.stripe.com' in uri.lower():
                return uri
        # 2) 兜底：宽泛关键词命中，但排除 profile.stripe.com（管理入口，非支付）
        for uri in uris:
            ul = uri.lower()
            if 'profile.stripe.com' in ul:
                continue
            if 'pay' in ul or 'invoice' in ul or 'stripe' in ul:
                return uri
    except Exception as e:
        print(f"解析 PDF 失败: {e}")
    return None


def _invoice_still_unpaid(driver, invoice_id):
    """在 credits 页面判断某 invoice 是否仍未支付（权威判据）。
    发票表格同时显示 Paid/Unpaid 行；支付成功后该行状态会变为 Paid。
    返回 True=仍未支付；False=已支付（该行显示 Paid）。

    重要：必须先等表格加载再判断——否则表格未加载时找不到行会被误判为"已消失=已支付"
    （假阳性会导致把未成功的支付误记为成功）。找不到行 / 表格未加载时一律保守返回 True。
    """
    # 等发票表格加载：出现至少一行带 invoice 链接的行
    try:
        driver.page.locator(
            "xpath=//table//tr[.//a[@role='button']]"
        ).first.wait_for(state="visible", timeout=20_000)
    except Exception:
        return True   # 表格没加载出来，无法确认已支付 → 保守视为仍未支付
    time.sleep(1)
    try:
        rows = driver.page.locator("xpath=//table//tr[.//a[@role='button']]").all()
        seen_ids = []
        for row in rows:
            try:
                iid = (row.locator("xpath=.//a[@role='button']").first
                       .inner_text(timeout=SHORT_TIMEOUT_MS) or '').strip()
                if iid:
                    seen_ids.append(iid)
                if iid == invoice_id:
                    txt = (row.inner_text(timeout=SHORT_TIMEOUT_MS) or '')
                    # 该行明确显示 Paid 且不含 Unpaid → 已支付；否则仍未支付
                    still_unpaid = not ('Paid' in txt and 'Unpaid' not in txt)
                    # 诊断日志：暴露判定依据的原始行文本，便于排查「真实已付却判成未付」
                    one_line = ' | '.join(s.strip() for s in txt.split('\n') if s.strip())
                    print(f"  [判定] {invoice_id} 行文本: {one_line[:160]}"
                          f" → {'仍未支付' if still_unpaid else '已支付(Paid)'}")
                    return still_unpaid
            except Exception:
                continue
        # 表格已加载但找不到该 invoice 行 → 保守视为未支付（避免误记成功）
        print(f"  [判定] {invoice_id} 未在发票表格中找到该发票行"
              f"（表内发票: {seen_ids[:8]}）→ 保守视为仍未支付")
    except Exception as _e:
        print(f"  [判定] {invoice_id} 读取发票表格异常: {str(_e)[:80]} → 保守视为仍未支付")
    return True


def handle_unpaid_invoices(driver, get_card=None, on_paid=None, on_failed=None, account_id=None,
                           should_stop=None, max_invoices=None,
                           skip_invoice_check=None, on_unpayable=None):
    """
    检查 Credits 页面的 Unpaid invoice，逐个下载 PDF、提取支付链接、
    打开 Stripe 支付页面并支付。每处理完一个 invoice 后重新导航回 credits 页面再查找，
    不关闭浏览器、持续支付下一个账单，直到无账单 / get_card 返回 None。

    失败重试语义（重要）：一张发票支付失败时，重试的是「同一张发票」而不是跳到下一张——
    账单要逐张结清，跳过只会把欠费留在账号上。
      - 卡被拒 / 卡号错 / 已过期 / 需要 3DS（card_fault=True）→ 换下一张卡重试同一发票，
        直到付掉或卡池耗尽（get_card 返回 None → 整体停止，剩余发票同样无卡可付）；
      - 页面超时 / PDF 下载失败 / 元素定位失败（card_fault=False）→ 卡是好的，复用同一张卡
        重开支付页重试，最多 2 次；仍失败才放弃该发票并转下一张。

    参数:
        driver: 浏览器驱动
        get_card: 卡提供器 callable，每次需要一张卡时调用（同一发票换卡重试会多次调用），
                  返回一张卡 dict
                  （number/expiry_month/expiry_year/cvc/country/zip...）或 None（无卡→停止）。
                  选卡策略（新卡优先凑满 20 张 / 满 20 或无新卡时复用已付卡 / 避免连续同卡 /
                  跳过 on_failed 标记过的无效卡）由调用方在 get_card 内实现。
                  为 None 时仅打开支付页展开 Card 表单（不支付）。
        on_paid: 每笔支付成功后的回调 on_paid(invoice_id, card, responses, amount)，
                 由调用方负责记账（recharge_log / valid_cards / card_pool 状态）。
        on_failed: 每笔支付失败后的回调 on_failed(invoice_id, card, reason, card_fault)，
                   由调用方记录失败原因。card_fault=True 表示失败归因于卡本身
                   （拒付 / 卡号错 / 已过期 / 需要 3DS），调用方应把该卡标记为无效；
                   False 表示脚本侧失败（页面超时、元素定位失败、结果未确认），不应动卡状态。
        account_id: Cloudflare 账号 ID，用于拼出 credits 页面 URL（返回时整页刷新）。
        should_stop: 无参 callable，返回 True 表示用户请求停止；每轮发票开头检查，
                     命中即中断循环（换卡重试可能持续很久，这是唯一的中途叫停入口）。
        max_invoices: 一次调用最多**成功付掉**的 invoice 数上限。默认 None＝不限（付清全部，
                      现状行为）。用于轮询式（round-robin）场景传 1：付掉 1 张即返回，
                      随后由编排层切换下一个账号。只有成功付掉（on_paid 触发）才计数，
                      重试耗尽放弃的坏账单不占额度；单张 invoice 内部换卡/脚本重试语义不变。
        skip_invoice_check: 无参 callable(invoice_id) -> bool，选发票时对每张待选发票调用；
                      返回 True 表示该发票处于「无法支付」冷却期（24h 内已判定失效）——
                      本轮直接跳过、不下载不开支付页（status='unpayable_cooldown'），
                      转去处理下一张账单。由调用方基于 invoice_payment_state 落库判定。
        on_unpayable: callable(invoice_id, pay_url, amount, permanent=False, reason='')，
                      支付页出现「此账单已无法在 Stripe 支付」失效提示时调用（24h 冷却）；
                      若支付页被重定向到 Stripe Dashboard 登录页（订单已彻底无效），
                      以 permanent=True 调用，调用方应永久标记、以后不再对该发票发起支付。
    返回:
        list: 处理结果列表 [{invoice, status, pay_url, card?, amount?, balance?, error?}, ...]
              status: paid / opened / failed / skipped
    """
    results = []
    download_dir = getattr(driver, '_download_dir', None)
    if not download_dir:
        print("未配置下载目录，跳过 Unpaid invoice 处理")
        return results

    # credits 页面 URL：每轮支付完用整页导航返回并重新加载（不用 go_back——SPA 回退
    # 会复用旧的前端状态，账单表格/余额都可能是支付前的陈旧数据）
    credits_url = None
    if account_id:
        credits_url = f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits"
    elif 'credits' in (driver.current_url or ''):
        credits_url = driver.current_url

    def _return_to_credits():
        """整页导航回 credits 并强制刷新，确保拿到支付后的最新账单状态与余额。"""
        try:
            if credits_url:
                driver.get(credits_url)          # goto 同 URL 即整页重新加载
            else:
                driver.page.reload(wait_until="domcontentloaded")
        except Exception as e:
            print(f"  返回 Credits 页面异常: {str(e)[:80]}")
        time.sleep(3)

    # === 失败重试策略 ===
    # 一张发票支付失败时，换下一张卡重试「同一张发票」，而不是跳到下一张发票——
    # 账单必须逐张结清，跳过只会把欠费留在账号上。
    #  - 卡自身问题（拒付 / 卡号错 / 已过期 / 需要 3DS）→ 换下一张卡重试同一发票。
    #    坏卡由 on_failed 标记为无效，调用方的 get_card 下次自然跳过它；
    #    直到支付成功或卡池耗尽（get_card 返回 None）为止。
    #  - 脚本侧问题（PDF 下载失败 / 支付页超时 / 元素定位失败）→ 卡是好的，不该消耗卡池，
    #    复用同一张卡重开支付页重试，最多 SCRIPT_RETRY_LIMIT 次，仍失败才放弃该发票、转下一张。
    SCRIPT_RETRY_LIMIT = 2
    MAX_ROUNDS = 500              # 死循环兜底（每轮至少消耗一张卡或一次脚本重试，正常远达不到）
    INVOICE_DETECT_RETRIES = 3   # 首次未见账单时的重载重试上限（刚 Top-up 完账单可能尚未渲染/落库）

    done_ids = set()              # 已完结的发票：支付成功、或重试耗尽后永久放弃
    cooldown_recorded = set()     # 已因「无法支付」冷却跳过并记过一条结果的发票（避免重复记录）
    card_tries = {}               # invoice_id -> 该发票已消耗的卡数（日志用）
    script_tries = {}             # invoice_id -> 该发票的脚本侧重试次数
    sticky_cards = {}             # invoice_id -> 脚本侧失败后待复用的同一张卡
    round_num = 0
    paid_count = 0                # 本次调用已成功付掉的 invoice 数（用于 max_invoices 限额）

    def _give_up(iid, reason, **extra):
        """放弃该发票：标记完结并记一条 failed，后续轮次不再选中它。"""
        done_ids.add(iid)
        sticky_cards.pop(iid, None)
        rec = {"invoice": iid, "status": "failed", "error": reason}
        rec.update(extra)
        results.append(rec)

    def _script_fail(iid, reason, **extra):
        """脚本侧失败：未超上限则保留该发票待下轮用同一张卡重试，超限则放弃。"""
        script_tries[iid] = script_tries.get(iid, 0) + 1
        n = script_tries[iid]
        if n > SCRIPT_RETRY_LIMIT:
            print(f"  {iid} 脚本侧失败已达 {SCRIPT_RETRY_LIMIT} 次上限，放弃该发票: {reason}")
            _give_up(iid, f"{reason}（脚本重试 {SCRIPT_RETRY_LIMIT} 次仍失败）", **extra)
        else:
            print(f"  {iid} 脚本侧失败（第 {n}/{SCRIPT_RETRY_LIMIT} 次），将用同一张卡重试该发票: {reason}")

    while round_num < MAX_ROUNDS:
        # 每轮开头检查停止请求：换卡重试会持续换卡，若无此闸门用户无法中途叫停，
        # 只能 kill 进程（force_stop 已 quit driver，后续 driver 调用会抛异常并被
        # try/except 吞成 _script_fail 继续跑，掩盖了停止意图）。
        if should_stop and should_stop():
            print("  收到停止请求，中断账单支付流程")
            break
        round_num += 1
        pending = None   # 本轮是否尝试了支付（用于返回账单页后权威判定成功与否）

        # 等待表格加载 + 清理可能弹出的欠费提示弹窗（会遮挡发票表格导致点击失败）
        time.sleep(3)
        had_overdue = dismiss_overdue_dialog(driver)

        # 查找 Unpaid 行
        def _find_unpaid_rows():
            return driver.page.locator(
                "xpath=//table//tr[.//span[text()='Unpaid']]"
            ).all()

        rows = _find_unpaid_rows()
        # 首次检测（尚未处理过任何发票）查到 0 行时：可能是刚 Top-up 完账单还没渲染/落库，
        # 或页面异步表格未加载完——有限次整页重载重试再定论，不再"查一次就判无账单"。
        # 欠费弹窗存在（had_overdue）却 0 行，几乎可确定账单尚未渲染，同样进入重试。
        if not rows and not results and not done_ids:
            for attempt in range(INVOICE_DETECT_RETRIES):
                print(f"  首次未见 Unpaid 行"
                      f"{'（检测到欠费弹窗，账单应存在）' if had_overdue else ''}"
                      f"，重载重试 {attempt + 1}/{INVOICE_DETECT_RETRIES}")
                _return_to_credits()
                had_overdue = dismiss_overdue_dialog(driver)
                time.sleep(2)
                rows = _find_unpaid_rows()
                if rows:
                    break
        if not rows:
            if not results and not done_ids:
                print("未发现 Unpaid invoice")
            break

        # 找到第一张尚未完结的 invoice。失败过的发票不会进 done_ids，因此仍会被再次选中
        # ——这正是"换卡重试同一张发票"的落点。
        invoice_id = None
        invoice_link = None
        invoice_row = None
        for row in rows:
            try:
                link = row.locator("xpath=.//a[@role='button']").first
                iid = (link.inner_text(timeout=SHORT_TIMEOUT_MS) or '').strip()
                if not iid or iid in done_ids:
                    continue
                # 「无法支付」冷却/永久标记：已判定该账单在 Stripe 侧失效——本轮直接跳过、
                # 不下载不开支付页，转去处理下一张账单（每张只记一条结果）。
                if skip_invoice_check and skip_invoice_check(iid):
                    if iid not in cooldown_recorded:
                        cooldown_recorded.add(iid)
                        print(f"  {iid} 已判定无法在 Stripe 支付（冷却/永久标记中），跳过该发票")
                        results.append({"invoice": iid, "status": "unpayable_cooldown",
                                        "error": "账单已判定无法在 Stripe 支付（冷却/永久跳过）"})
                    done_ids.add(iid)
                    continue
                invoice_id = iid
                invoice_link = link
                invoice_row = row
                break
            except Exception:
                continue

        if not invoice_id:
            print("所有 Unpaid invoice 已处理完毕")
            break

        total_unpaid = len(rows)
        retry_txt = ""
        if card_tries.get(invoice_id) or script_tries.get(invoice_id):
            retry_txt = (f"（重试：已试 {card_tries.get(invoice_id, 0)} 张卡"
                         f"/脚本重试 {script_tries.get(invoice_id, 0)} 次）")
        print(f"[剩余 Unpaid {total_unpaid} 张] 处理 invoice: {invoice_id}{retry_txt}")

        # 提取账单金额（用于记账，取行内第一个美元金额）
        amount = 0.0
        try:
            _m = re.search(r'\$([0-9]+(?:\.[0-9]+)?)', invoice_row.inner_text(timeout=SHORT_TIMEOUT_MS))
            if _m:
                amount = float(_m.group(1))
        except Exception:
            pass

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
                _script_fail(invoice_id, "PDF 下载超时")
                _return_to_credits()
                continue

            # 从 PDF 提取支付链接
            pay_url = _extract_pdf_pay_url(pdf_path)
            if not pay_url:
                print(f"  {invoice_id} 未找到 Pay online 链接")
                _script_fail(invoice_id, "未找到支付链接")
                _return_to_credits()
                continue

            print(f"  找到支付链接: {pay_url}")

            # 在浏览器中打开支付链接
            driver.get(pay_url)
            print(f"  正在等待 {invoice_id} 支付页面加载...")

            # 等待支付页就绪：Pay 按钮可见（正常）、出现失效提示（账单已无法在 Stripe 支付）、
            # 或被重定向到 Stripe Dashboard 登录页（dashboard.stripe.com/login——支付链接已
            # 彻底作废，订单无效）。三者竞态——后两种情况 Pay 按钮永不可见，若只等按钮会白等
            # 满超时再当超时重试，故并行检测以便秒级识别、直接跳过。
            # 新结构（钱包区 + accordion）Stripe 资源加载慢，就绪可达数分钟，故给足 240s。
            pay_btn = driver.page.locator(
                "button[data-testid='hosted-payment-submit-button']"
            ).first
            page_ready = False
            unpayable = False
            login_redirect = False
            deadline = time.time() + 240
            while time.time() < deadline:
                cur_url = driver.current_url or ''
                if 'dashboard.stripe.com/login' in cur_url:
                    login_redirect = True
                    break
                if _is_invoice_unpayable(driver):
                    unpayable = True
                    break
                if pay_btn.is_visible():
                    page_ready = True
                    break
                time.sleep(1)

            if login_redirect:
                # 支付页被重定向到 Stripe Dashboard 登录页——该账单的支付链接已彻底作废
                # （订单无效，非到期可恢复的冷却），永久标记、以后不再对它发起支付。
                # 此刻尚未调用 get_card()，故不消耗卡池额度。
                print(f"  {invoice_id} 支付页跳转到 Stripe 登录页，订单已无效，永久跳过该发票")
                if on_unpayable is not None:
                    try:
                        on_unpayable(invoice_id, pay_url, amount, permanent=True,
                                     reason='支付页跳转 Stripe 登录页（订单无效）')
                    except Exception as _e:
                        print(f"  记录账单永久无效失败: {str(_e)[:80]}")
                done_ids.add(invoice_id)
                results.append({"invoice": invoice_id, "status": "unpayable",
                                "error": "支付页跳转 Stripe 登录页（订单无效，永久跳过）",
                                "pay_url": pay_url, "amount": amount})
                _return_to_credits()
                continue
            if unpayable:
                # 账单本身在 Stripe 侧已作废——换卡/脚本重试都无意义，标记完结直接跳过。
                # 此刻尚未调用 get_card()，故不消耗卡池额度。
                print(f"  {invoice_id} 账单已无法在 Stripe 支付（需联系 Cloudflare），永久跳过该发票")
                # 「can no longer be paid」是 Stripe/Cloudflare 对该发票的终态：作废发票不会复活，
                # 故永久标记（permanent=True），避免 24h 冷却到期后每天重开该页白耗 240s 再重标。
                if on_unpayable is not None:
                    try:
                        on_unpayable(invoice_id, pay_url, amount, permanent=True,
                                     reason='账单已无法在 Stripe 支付（需联系 Cloudflare）')
                    except Exception as _e:
                        print(f"  记录账单永久无法支付失败: {str(_e)[:80]}")
                done_ids.add(invoice_id)
                results.append({"invoice": invoice_id, "status": "unpayable",
                                "error": "账单已无法在 Stripe 支付（需联系 Cloudflare）",
                                "pay_url": pay_url, "amount": amount})
                _return_to_credits()
                continue
            if not page_ready:
                print(f"  {invoice_id} 支付页面加载超时")
                _script_fail(invoice_id, "支付页面加载超时", pay_url=pay_url)
                _return_to_credits()
                continue
            print(f"  {invoice_id} 支付页面已加载完成")

            # 页面可能直接停在"确认付款"（已保存支付方式）态、卡表单被隐藏 → 先切回新支付方式
            _reveal_payment_form_if_saved_card(driver, invoice_id)

            # 切入 Stripe iframe 展开 Card 信用卡表单（frame_locator 无状态）。
            # 新结构下 #payment-element 有多个 iframe，_stripe_payment_frame 精确取卡输入帧。
            #
            # 关键：新结构的 Stripe iframe（钱包区 + accordion 折叠）加载很慢，从支付页
            # 就绪到 Card 折叠项 / 卡表单可交互可能要数分钟——故不用固定短超时，而在一个
            # 较长窗口内轮询，等「卡表单已直接展开（卡号输入框可见）」或「Card 折叠项可点」
            # 二者之一先就绪：前者直接进填卡、后者点开它。期间响应停止请求可中途叫停。
            CARD_READY_TIMEOUT = 300   # 秒，等 Stripe 卡表单/选项就绪的上限（慢加载可达数分钟）
            try:
                stripe_frame = _stripe_payment_frame(driver)
                number_input = stripe_frame.locator("input#payment-numberInput")
                card_btn = stripe_frame.locator("div.p-AccordionButton[data-value='card']")
                deadline_card = time.time() + CARD_READY_TIMEOUT
                state = None   # 'expanded' | 'clicked'
                while time.time() < deadline_card:
                    if should_stop and should_stop():
                        raise InterruptedError("用户请求停止")
                    try:
                        if number_input.is_visible():   # 卡表单已展开（无需点 Card）
                            state = 'expanded'
                            break
                    except Exception:
                        pass
                    try:
                        if card_btn.is_visible():        # Card 折叠项就绪 → 点开展开表单
                            _safe_click(card_btn, session=driver, desc='Card 支付选项')
                            time.sleep(2)
                            state = 'clicked'
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                if state == 'expanded':
                    print(f"  {invoice_id} 卡表单已展开，跳过点击 Card")
                elif state == 'clicked':
                    print(f"  {invoice_id} 已展开 Card 信用卡支付表单")
                else:
                    raise RuntimeError(f"等待 Card 表单/选项就绪超时（{CARD_READY_TIMEOUT}s）")
            except InterruptedError:
                raise
            except Exception as e:
                print(f"  {invoice_id} 点击 Card 选项失败: {e}")
                _script_fail(invoice_id, f"点击 Card 选项失败: {e}", pay_url=pay_url)
                _return_to_credits()
                continue

            # 有卡提供器则填卡并提交（是否真成功在返回 credits 后按"账单是否仍 Unpaid"判定）
            if get_card is not None:
                # 上一轮若是脚本侧失败，卡本身没问题 → 复用同一张卡，不白白消耗卡池额度
                # （一个 CF 账号最多 20 张 distinct 成功支付卡）。
                reuse_card = sticky_cards.pop(invoice_id, None)
                card = reuse_card or get_card()
                if not card:
                    # 卡池耗尽：这张发票付不掉，后面的发票同样无卡可用 → 整体停止
                    print("  无可用支付卡（卡池已耗尽），停止处理剩余账单")
                    results.append({"invoice": invoice_id, "status": "skipped",
                                    "error": f"无可用支付卡（已试 {card_tries.get(invoice_id, 0)} 张卡）"})
                    break
                if not reuse_card:
                    card_tries[invoice_id] = card_tries.get(invoice_id, 0) + 1
                card_last4 = str(card.get('number', ''))[-4:]
                nth = card_tries.get(invoice_id, 1)
                how = "复用同一张卡重试" if reuse_card else f"第 {nth} 张卡"
                print(f"  {invoice_id} 使用卡 ****{card_last4}（{how}）填写并提交...")
                pay_result = _fill_stripe_payment_and_submit(driver, card, invoice_id,
                                                             should_stop=should_stop)
                pending = {"invoice": invoice_id, "card": card, "last4": card_last4,
                           "amount": amount, "pay_url": pay_url,
                           "responses": pay_result.get('responses'),
                           "error": pay_result.get('error'),
                           "card_fault": pay_result.get('card_fault', False),
                           "tds": pay_result.get('tds', False),
                           "fill_status": pay_result.get('status')}
            else:
                results.append({"invoice": invoice_id, "status": "opened", "pay_url": pay_url})

        except InterruptedError:
            # 用户请求停止：立即跳出整个发票循环（不当作脚本失败重试）
            print("  收到停止请求，中断账单支付流程")
            break
        except Exception as e:
            print(f"  处理 {invoice_id} 异常: {e}")
            _script_fail(invoice_id, str(e))

        # 处理完一个 invoice 后，回到 credits 页面重新查找
        print(f"  返回 Credits 页面查找剩余 Unpaid invoices...")
        _return_to_credits()

        # 权威判定：以 Cloudflare 发票表状态为准（不解析 Stripe 响应）。
        # 支付是耗时操作、且 Cloudflare 更新账单状态有延迟，故轮询等待：
        #  - 明确失败（3DS 取消 / 拒付原因）→ 不空等，确认一次后直接判失败；
        #  - 其它（成功/未确认）→ 轮询账单状态最多 ~60s，变 Paid 即成功，否则失败。
        if pending:
            inv = pending["invoice"]
            definite_fail = pending.get("fill_status") == "failed"
            paid_confirmed = False
            if definite_fail:
                print(f"  {inv} 提交结果明确失败（fill_status=failed），确认一次账单状态即判定")
                dismiss_overdue_dialog(driver)
                paid_confirmed = not _invoice_still_unpaid(driver, inv)
            else:
                print(f"  {inv} 提交完成，开始轮询账单状态确认是否已变 Paid（最多 10 轮）...")
                for _rnd in range(10):        # 最多约 10 轮（含刷新与等待，覆盖网络不佳）
                    dismiss_overdue_dialog(driver)
                    if not _invoice_still_unpaid(driver, inv):
                        paid_confirmed = True
                        print(f"  {inv} 第 {_rnd + 1} 轮确认已变 Paid")
                        break
                    print(f"  {inv} 第 {_rnd + 1}/10 轮仍未见 Paid，6s 后刷新重查")
                    time.sleep(6)
                    _return_to_credits()

            if paid_confirmed:
                # 页面已是刷新后的 credits 页 → 此刻的 Credits 卡片即支付后的最新余额
                done_ids.add(inv)
                sticky_cards.pop(inv, None)
                balance = read_credits_balance(driver)
                bal_txt = f"${balance:.2f}" if balance is not None else "未读到"
                print(f"  {inv} 支付成功（账单已变为 Paid，卡 ****{pending['last4']}，"
                      f"${pending['amount']}），当前 Credits 余额: {bal_txt}")
                results.append({"invoice": inv, "status": "paid", "pay_url": pending["pay_url"],
                                "card": pending["last4"], "amount": pending["amount"],
                                "balance": balance,
                                "responses": pending["responses"]})
                if on_paid:
                    try:
                        on_paid(inv, pending["card"], pending["responses"], pending["amount"], balance)
                    except Exception as _e:
                        print(f"  on_paid 回调异常: {_e}")
                paid_count += 1
                if max_invoices is not None and paid_count >= max_invoices:
                    print(f"  已付掉 {paid_count} 张账单，达到本次上限 max_invoices={max_invoices}，返回")
                    break
            else:
                fail_reason = pending.get("error") or "支付未生效（账单仍为 Unpaid）"
                # card_fault：失败是否归因于卡本身（拒付/过期/3DS）。脚本侧失败
                # （页面超时、元素定位失败、结果未确认）为 False，不应把卡判为无效。
                card_fault = bool(pending.get("card_fault"))
                print(f"  {inv} 支付未生效（账单仍为 Unpaid，卡 ****{pending['last4']}），"
                      f"原因: {fail_reason}（卡自身问题: {'是' if card_fault else '否'}）")

                # 先回调：card_fault=True 时调用方会把该卡标为无效，
                # 下一轮 get_card 就自然跳过它、返回下一张卡。
                if on_failed:
                    try:
                        on_failed(inv, pending["card"], fail_reason, card_fault,
                                  bool(pending.get("tds")))
                    except Exception as _e:
                        print(f"  on_failed 回调异常: {_e}")

                if card_fault:
                    # 卡的问题 → 换下一张卡重试同一张发票（不写 done_ids，下一轮会再次选中它）
                    print(f"  {inv} 将改用下一张卡重试该发票"
                          f"（已试 {card_tries.get(inv, 0)} 张卡）")
                    results.append({"invoice": inv, "status": "failed", "pay_url": pending["pay_url"],
                                    "card": pending["last4"], "error": fail_reason,
                                    "card_fault": True, "retrying": True})
                else:
                    # 脚本侧失败 → 卡未判无效，下一轮复用同一张卡重开支付页；超上限才放弃该发票
                    sticky_cards[inv] = pending["card"]
                    _script_fail(inv, fail_reason, pay_url=pending["pay_url"],
                                 card=pending["last4"], card_fault=False)

    if round_num >= MAX_ROUNDS:
        print(f"已达最大处理轮数 {MAX_ROUNDS}，停止（可能存在异常的重试循环）")

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
        # 必须尊重返回值：Turnstile 没过就提交，CF 后端收到空 token 会静默拒绝——
        # 不报错、不发验证邮件，上层只能看到「等待验证邮件超时」，根因被完全掩盖。
        # 宁可在这里失败，也不要制造一次查不出原因的注册失败。
        if not _handle_inline_turnstile(driver):
            print("❌ 人机验证未通过，放弃提交注册表单")
            return False
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

        _report_signup_page_error(driver)

        return True

    except Exception as e:
        print(f"❌ 填写注册表单失败: {e}")
        return False


def _report_signup_page_error(driver):
    """把注册页上 CF 给出的错误文本打进日志。

    存在的理由：注册失败时唯一的信号曾经是「等待验证邮件超时」，而 CF 拒绝的原因
    （token 无效、邮箱域名被拒、密码不合规……）就写在页面上，却从来没人看。
    排查一次要翻半天日志才能确认「表单提交了但什么都没发生」。

    纯诊断用途，任何异常都吞掉，绝不影响注册主流程。
    """
    try:
        if 'sign-up' not in (driver.current_url or ''):
            return          # 已经跳走了，说明提交被接受

        errors = driver.page.evaluate("""() => {
            const out = [];
            const sel = '[role="alert"], [class*="error" i], [class*="Error"], ' +
                        '[data-testid*="error"], [aria-invalid="true"] ~ *';
            document.querySelectorAll(sel).forEach(el => {
                const t = (el.innerText || '').trim();
                if (t && t.length < 300) out.push(t);
            });
            return [...new Set(out)].slice(0, 5);
        }""")
        if errors:
            print("  ⚠️ 注册页错误提示:")
            for text in errors:
                print(f"     • {text}")
        else:
            print("  ℹ️ 仍停留在注册页，但页面未给出错误提示"
                  "（多为后端静默拒绝，检查 Turnstile token 是否真的交付）")
    except Exception:
        pass


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
                # 前三个选择器服务注册流程的邮箱验证页；后两个是登录二次验证
                # (two-factor?type=email) 的输入框——它 name=twofactor_token、
                # autocomplete=off、无 maxlength，前三个一个都不命中。
                code_input = driver.page.locator(
                    'input[name="code"], input[type="text"][maxlength="6"], '
                    'input[autocomplete="one-time-code"], '
                    'input[name="twofactor_token"], '
                    'input[data-testid="email-mfa-login-input-2fa-code"]'
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

        # 方法2: 按「掩码段 + 末四位数字」模式计数（如 "•••• 4673"）。
        # 关键：一张卡的完整掩码号渲染为多段 "•••• •••• •••• 4673"，只有最后一段
        # 跟着真实的 4 位数字。用 /[•*·]{2,}\s*\d{4}/ 锚定这一段，每张卡恰好匹配一次，
        # 避免把前面的每个 "••••" 段各数成一张卡（曾导致 1 张卡被误读为 3 张而跳过补绑）。
        # 优先在 "Billing method" 区域内统计，找不到该区域时退回整页文本。
        count = driver.page.evaluate("""() => {
            var re = /[\\u2022\\*\\u00b7]{2,}\\s*\\d{4}/g;
            var sections = document.querySelectorAll('div, section');
            for (var i = 0; i < sections.length; i++) {
                var header = sections[i].querySelector('span');
                if (header && header.textContent.trim() === 'Billing method') {
                    var m = (sections[i].innerText || '').match(re);
                    return m ? m.length : 0;   // 找到区域：以区域内计数为准（含 0）
                }
            }
            var mb = (document.body.innerText || '').match(re);
            return mb ? mb.length : 0;         // 未找到区域：退回整页计数
        }""")
        if count > 0:
            print(f"  💳 检测到 {count} 张已绑定的信用卡")
        else:
            print("  💳 未检测到已绑定的信用卡")
        return count

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


# 深度查找输入元素：穿透 open shadow root，并把 contenteditable 也算作输入位。
# Stripe 若把字段放进 shadow DOM，普通 querySelectorAll 会返回空——那会与「文档没加载」
# 表现完全一致，必须靠这段区分开。
_DEEP_INPUT_PROBE = """() => {
    const out = [];
    const walk = (root, depth) => {
        if (!root || depth > 6) return;
        let nodes = [];
        try { nodes = Array.from(root.querySelectorAll('input, [contenteditable=""], [contenteditable="true"], select')); }
        catch (e) { return; }
        for (const el of nodes) {
            out.push({
                tag: el.tagName.toLowerCase(),
                name: el.getAttribute('name') || '',
                id: el.id || '',
                ph: el.getAttribute('placeholder') || el.getAttribute('aria-label') || '',
                type: el.getAttribute('type') || '',
                stable: el.getAttribute('data-elements-stable-field-name') || '',
                shadow: depth > 0,
                vis: !!(el.offsetWidth || el.offsetHeight),
            });
        }
        let all = [];
        try { all = Array.from(root.querySelectorAll('*')); } catch (e) { return; }
        for (const el of all) if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    };
    walk(document, 0);
    return {
        fields: out,
        readyState: document.readyState,
        bodyLen: (document.body && document.body.innerHTML.length) || 0,
        iframeCount: document.querySelectorAll('iframe').length,
    };
}"""


def _dump_stripe_frame_fields(page, frames):
    """把各 frame 的文档状态与输入字段结构打进日志，用于定位「字段找不到」的真因。

    上一轮诊断里 27 个 frame 全报 0 个 input，无法区分三种情形，故本函数除字段外
    还打印 readyState / body 长度 / 子 frame 数：
      - body 长度为 0 或 readyState 非 complete → 拿到的不是真实文档（跨域上下文问题）
      - body 有内容但字段为空、且子 frame 数 > 0 → 真实输入框在更深一层 frame
      - 字段带 shadow=True → 字段在 shadow DOM 内，需穿透查询
    """
    print("  🔍 Stripe frame 结构诊断:")
    if not frames:
        print("    (无匹配的 Stripe frame)")
    for idx, fr in enumerate(frames):
        try:
            url = fr.url or ''
        except Exception:
            url = '<url 不可读>'
        # URL 极长（含完整 Stripe 参数），只摘出 componentName 便于辨认 frame 用途
        comp = ''
        m = re.search(r'componentName=([A-Za-z]+)', url)
        if m:
            comp = m.group(1)
        try:
            kids = len(fr.child_frames)
        except Exception:
            kids = -1
        try:
            info = fr.evaluate(_DEEP_INPUT_PROBE)
        except Exception as e:
            print(f"    [{idx}] componentName={comp or '?'} 子frame={kids} url={url[:70]}")
            print(f"        ❗ evaluate 失败: {str(e)[:120]}")
            continue
        print(f"    [{idx}] componentName={comp or '?'} readyState={info['readyState']} "
              f"bodyLen={info['bodyLen']} 内嵌iframe={info['iframeCount']} 子frame={kids} "
              f"url={url[:70]}")
        fields = info['fields']
        if not fields:
            print("        (无 input/contenteditable/select)")
        for f in fields:
            print(f"        {f['tag']} name={f['name']!r} id={f['id']!r} ph={f['ph']!r} "
                  f"type={f['type']!r} stable={f['stable']!r} shadow={f['shadow']} vis={f['vis']}")
        # payment frame 没有字段时，把它的实际内容整段打出来——正常渲染时 body 约 32KB
        # 且含 12 个 input，若只有几 KB 且无字段，内容本身会直接说明是骨架屏、
        # 错误提示还是别的东西，比继续猜选择器有效。
        if comp == 'payment' and not fields:
            try:
                html = fr.evaluate("() => document.body ? document.body.innerHTML : ''")
            except Exception as e:
                print(f"        (读取 payment frame 内容失败: {str(e)[:80]})")
            else:
                print("        ↓↓↓ payment frame 内容全文 ↓↓↓")
                print(html[:4000])
                print("        ↑↑↑ 内容结束 ↑↑↑")


def _type_and_verify_stripe_field(el, value, label, attempts=4):
    """向 Stripe 格式化字段逐字输入并回读校验，不符则清空重填。

    Stripe 字段在输入过程中会重格式化并重建 DOM，与逐字输入形成竞态，表现为随机吞字符：
    实测同一份代码连续跑，有效期 '1230' 会得到 '12 / 30'（对）、'1'、'02 / 30'（月份都错）。
    因此不能靠固定延迟——必须回读校验。两个要点均为实测所得：
      - 清空要逐次 Backspace：Control+a + Delete 清不干净，残留会与重填字符混合成 '10 / 23'
      - 重试要递增延迟：失败几乎都是输入过快被吞，加大间隔后一次即过

    比对时只取数字，因为 Stripe 会插入空格/斜杠（'4242 4242 4242 4242'、'12 / 30'）。
    """
    want = re.sub(r'\D', '', value)
    shown = ''
    for i in range(attempts):
        try:
            el.click()
            time.sleep(0.15)
            el.press('End')
            for _ in range(len((el.input_value() or '')) + 2):
                el.press('Backspace')
            time.sleep(0.2)
            el.press_sequentially(value, delay=60 + i * 120)
            time.sleep(0.5)
            shown = el.input_value() or ''
            if re.sub(r'\D', '', shown) == want:
                return True
            print(f"  ⚠️ {label} 回读不符 (得到 {shown!r})，重填 {i + 2}/{attempts}")
        except Exception as e:
            print(f"  ⚠️ {label} 第 {i + 1} 次填写异常: {str(e)[:60]}")
    print(f"  ❌ {label} 重试 {attempts} 次仍不符，最终值 {shown!r}")
    return False


def _card_form_frames(page):
    """从 credit-card-form 容器内的 iframe 元素解析出 Frame，并递归收集其子 frame。

    与 _stripe_field_frames 的 page.frames 全局枚举是两条独立路径：这里走
    DOM 元素 → content_frame，锚定的是「弹窗里那个信用卡表单」本身，不受
    全局枚举顺序/遗漏影响，也能拿到只挂在该 iframe 下的嵌套层级。
    """
    frames = []

    def _collect(fr, depth=0):
        if fr is None or depth > 4 or fr in frames:
            return
        frames.append(fr)
        try:
            for kid in fr.child_frames:
                _collect(kid, depth + 1)
        except Exception:
            pass

    selectors = [
        '[data-test-id="credit-card-form"] iframe',
        'iframe[title="Secure payment input frame"]',
        '[role="dialog"] iframe',
    ]
    for sel in selectors:
        try:
            for el in page.locator(sel).all():
                try:
                    handle = el.element_handle(timeout=SHORT_TIMEOUT_MS)
                    if handle is None:
                        continue
                    _collect(handle.content_frame())
                except Exception:
                    continue
        except Exception:
            continue
        if frames:
            break
    return frames


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

    # 尝试直接在当前 iframe 中查找输入框。
    # 命名分两套：旧 Card Element 用 cardnumber/exp-date/cvc + cardNumber/cardExpiry/cardCvc；
    # 新 Payment Element 用 number/expiry/cvc + id="Field-{number,expiry,cvc}Input"。
    # 两套必须都覆盖——只写旧的会导致所有 frame 都匹配不上而退化到 Tab 兜底。
    # 以下命名经本地挂载真实 Stripe Payment Element 实测确认（见 stripe-payment-element.md）：
    #   input[name="number"] id="payment-numberInput"
    #   input[name="expiry"] id="payment-expiryInput"
    #   input[name="cvc"]    id="payment-cvcInput"
    # id 前缀是 element 类型而非固定串，故用后缀匹配；旧 Card Element 命名一并保留。
    card_selectors = [
        'input[name="number"]',
        'input[id$="-numberInput"]',
        'input[name="cardnumber"]',
        'input[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]',
        'input[placeholder*="card number"]',
        'input[data-elements-stable-field-name="cardNumber"]',
    ]

    expiry_selectors = [
        'input[name="expiry"]',
        'input[id$="-expiryInput"]',
        'input[name="exp-date"]',
        'input[name="cardExpiry"]',
        'input[autocomplete="cc-exp"]',
        'input[placeholder*="MM"]',
        'input[data-elements-stable-field-name="cardExpiry"]',
    ]

    cvc_selectors = [
        'input[name="cvc"]',
        'input[id$="-cvcInput"]',
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
        # 并入 DOM→content_frame 路径的结果（去重）。两条路径解析机制不同，
        # page.frames 漏掉的嵌套层级可能只有这条能拿到。
        for fr in _card_form_frames(page):
            if fr not in result:
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
                if el.count() == 0:
                    continue
                # 跨域 Stripe frame 内元素可能一时判不出可见（frame 尺寸/合成时机），
                # 不能据此直接跳过——先滚入视口再判一次，仍不可见才放弃。
                if not el.is_visible():
                    try:
                        el.scroll_into_view_if_needed(timeout=SHORT_TIMEOUT_MS)
                    except Exception:
                        pass
                    if not el.is_visible():
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
                elif not _type_and_verify_stripe_field(el, str(value), label):
                    # 回读不符且重试用尽：换下一个选择器，别把错值当成功
                    continue
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

    # 多支付方式下 Payment Element 是折叠手风琴，字段在展开前不存在于 DOM。
    # 等待阶段通常已展开过，这里再确保一次（本函数也可能被单独调用）。
    if _card_field_locator(page, CARD_NUMBER_SEL) is None:
        _expand_stripe_card_accordion(page)

    # 主路径: 经 FrameLocator 填写。每个字段各自重新解析 iframe，Stripe 中途重建
    # 也不会拿到失效的 Frame。
    fl_ok = 0
    for sel, value, label in ((CARD_NUMBER_SEL, number, '卡号'),
                              (CARD_EXPIRY_SEL, expiry, '有效期'),
                              (CARD_CVC_SEL, cvc, 'CVC')):
        if not value:
            continue
        loc = _card_field_locator(page, sel)
        if loc is None:
            continue
        if _type_and_verify_stripe_field(loc, str(value), label):
            print(f"  ✅ 填写 {label}")
            fl_ok += 1
            filled_any = True
        time.sleep(0.3)
    if fl_ok >= 2:
        # Link 勾选框是填完卡号后才出现的，故必须放在填写之后取消
        _uncheck_link_opt_in(page)
        # 至少填成两个字段才认为主路径可用，否则继续走下面的兜底。
        # 账单地址不在这里填——调用方 add_credit_card 有独立步骤负责（driver.py:4148）。
        return True
    if fl_ok:
        print(f"  ⚠️ FrameLocator 路径仅填成 {fl_ok} 个字段，继续尝试其它路径")

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
    # 三个字段各自独立判定，不能用 if/elif 链：新版 Payment Element 把卡号/有效期/CVC
    # 渲染在同一个 frame 内，elif 会让一次循环只填一个字段，单 frame 时后两个永远填不上。
    for fr in _stripe_field_frames():
        try:
            if not card_filled_nested and try_fill_selectors(fr, card_selectors, number, '卡号'):
                card_filled_nested = True
                time.sleep(0.3)
            if not expiry_filled_nested and try_fill_selectors(fr, expiry_selectors, expiry, '有效期'):
                expiry_filled_nested = True
                time.sleep(0.3)
            if not cvc_filled_nested and try_fill_selectors(fr, cvc_selectors, cvc, 'CVC'):
                cvc_filled_nested = True
                time.sleep(0.3)
            for field_name, selectors, value_fn in billing_fields:
                value = value_fn(card_info)
                if value and try_fill_selectors(fr, selectors, value, field_name):
                    billing_filled_in_nested = True
                    time.sleep(0.2)
            time.sleep(0.3)
        except Exception:
            continue

    if card_filled_nested or expiry_filled_nested or cvc_filled_nested:
        _uncheck_link_opt_in(page)
        # 卡信息填了部分，也尝试在主文档层填写账单地址
        if not billing_filled_in_nested:
            try_fill_billing_fields(page)
        return filled_any

    # 策略3: 最后兜底 —— 用 Tab 键在表单字段间切换输入
    # 在首个 Stripe 字段 frame 内点击获焦，然后逐字符输入 + Tab 到下一字段
    # 走到这里说明策略1/2 的选择器全部落空。Tab 兜底是盲打（不校验落点），
    # 成功率低且失败时无从诊断，故先把各 frame 的真实字段结构打出来。
    print("  ⚠️ 未找到独立字段，尝试 Tab 键导航输入...")
    _dump_stripe_frame_fields(page, _stripe_field_frames())
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
