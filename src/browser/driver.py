"""
浏览器自动化模块
使用 Patchright（Playwright 反检测 fork）驱动有头 Chrome。

本模块只放**站点无关**的浏览器基建：create_driver / create_driver_vanilla /
close_driver、profile 卫生（进程清理、缓存修剪）、_safe_goto/_safe_click/_safe_fill、
语言与下载目录设置、通用 CDP / DOM 工具。

任何带具体站点 URL、DOM 选择器或业务流程的代码都不属于这里——它们归各平台的
适配器（src/platforms/<平台>/）或支付供应商层。此前这里曾有约 5100 行 Cloudflare
dash 时代的登录/绑卡/账单实现，编排层剥离后长期无人调用，已整体删除。
"""

import os
import re
import time
import random
import tempfile
import shutil
import threading
from datetime import datetime
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from src.config import cfg, PROFILE_CACHE_LIMIT_MB

MAX_WAIT_TIME = cfg.browser.max_wait_time
SHORT_WAIT_TIME = cfg.browser.short_wait_time


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
                 user_data_dir=None, remote_browser=None, remote_stop=None):
        self.playwright = playwright        # sync_playwright().start() 句柄
        self.context = context              # 持久化 BrowserContext
        self.page = page                    # 主 Page
        # remote 模式（AdsPower 等外部指纹浏览器，经 connect_over_cdp 接管）。
        # 与本地模式的区别全在 quit()：浏览器进程不归我们管，既不能按 profile 目录
        # 杀进程（会误伤 AdsPower 自己的实例），也不能删目录（目录在 AdsPower 内部）。
        # remote_browser 是 connect_over_cdp 返回的 Browser（close() = 断开连接，不关浏览器），
        # remote_stop 是「请外部管理器关掉这个浏览器」的回调。两者同时为 None 即本地模式。
        self._remote_browser = remote_browser
        self._remote_stop = remote_stop
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
        # Playwright/Patchright node driver 进程 pid（cli.js run-driver）。quit() 看门狗
        # 杀完 Chrome 后 close 仍可能阻塞在 node 侧（实测 Chrome 已退、close 干等 ~300s
        # 才自解），需要它做二段回收。取不到（内部结构变动）则为 None，降级为只杀 Chrome。
        self._node_pid = None
        self._close_finished = False        # close 是否已走完（看门狗二段回收的判据）
        try:
            self._node_pid = playwright._impl_obj._connection._transport._proc.pid
        except Exception:
            pass

    # ---- 事件监听（在 create_driver 中挂载，均在业务线程回调） ----
    def _on_console(self, msg):
        try:
            text = msg.text or ''
        except Exception:
            return
        # console_errors 只留匹配卡片错误模式的条目（供上层提取拒卡原因）；诊断
        # 「表单卡在加载态」需要的是未经过滤的 error 级日志，那类报错不含卡片
        # 关键词，会被上面的模式全部丢掉，故另存一份。
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

        if self._remote_stop is not None:
            self._quit_remote()
            return

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
                # 二段回收：Chrome 已退（或刚被杀）后 close 仍可能阻塞在 node driver
                # （实测 fernandezr701：Chrome 早已退出、context.close() 仍干等 ~300s
                # 才自解，期间任务静默如挂死）。再等 10s，仍没走完就直接杀 node——
                # 阻塞的调用随连接断开立刻抛错解开。同样只做 os.kill，不碰 Playwright 对象。
                import signal
                for _ in range(20):
                    if self._close_finished:
                        return
                    time.sleep(0.5)
                if self._node_pid:
                    print("  ⏱️ close 仍阻塞，强制回收 Playwright node driver 进程")
                    try:
                        os.kill(self._node_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

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
            # 标志位先于 cancel：Timer 已触发时 cancel 是空操作，二段回收只认这个标志。
            self._close_finished = True
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

    def _quit_remote(self):
        """remote 模式关闭：断开 CDP → 停 playwright → 请外部管理器关浏览器。

        与本地模式的关键差异：
          - 用 remote_browser.close() 断开连接，**不**调 context.close()。CDP 接管到的
            context 是浏览器里那个真实的默认上下文，关掉它会连带关掉整个浏览器，
            外部管理器（AdsPower）随后就收不到自己那份关闭流程，环境状态会停在
            "Active" 而实际进程已死，下次启动直接失败。
          - 不按 user_data_dir 杀进程：那个目录属于 AdsPower，用 pkill 匹配会连它自己
            管理的其它环境一起杀掉。

        看门狗仍然保留，但只做一件事：超时后直接调 remote_stop。CDP 断连不像本地
        Chrome 那样会把线程钉死，真正可能卡住的是对端无响应，而那正是 remote_stop
        （走 HTTP 接口）能解决的。
        """
        stopped = threading.Event()

        def _force_stop():
            if stopped.is_set():
                return
            print(f"  ⏱️ 断开远程浏览器超时 {_CLOSE_WATCHDOG_SEC}s，直接请求关闭环境")
            self._call_remote_stop()

        watchdog = threading.Timer(_CLOSE_WATCHDOG_SEC, _force_stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            if self._remote_browser is not None:
                try:
                    self._remote_browser.close()
                except Exception as e:
                    print(f"  ⚠️ 断开远程浏览器连接失败: {str(e)[:120]}")
            try:
                self.playwright.stop()
            except Exception as e:
                print(f"  ⚠️ 停止 playwright 失败: {str(e)[:120]}")
        finally:
            self._close_finished = True
            stopped.set()
            watchdog.cancel()

        self._call_remote_stop()

    def _call_remote_stop(self):
        """幂等地请求外部管理器关闭浏览器（看门狗与正常路径都可能调到）。"""
        cb, self._remote_stop = self._remote_stop, None
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            print(f"  ⚠️ 关闭远程浏览器环境失败: {str(e)[:150]}")


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


def create_driver(headless=False, profile_id=None, bypass_csp=False,
                  disable_site_isolation=False, proxy=None):
    """
    创建带有反检测的 Chrome 浏览器会话（使用 Patchright）

    参数:
        headless: 是否使用无头模式
        profile_id: 持久化 profile 标识（如 email），传入后复用同一浏览器环境；
                    为 None 时使用全新临时 profile
        bypass_csp: 关闭页面 CSP 强制。Patchright 把 add_init_script 作为**内联 <script>**
                    重写进 HTML 响应注入，而 Stripe/hCaptcha 帧的严格 CSP 会拦掉内联脚本
                    导致 hook 不执行。开启后（Page.setBypassCSP）内联注入才能在这些 OOPIF 帧
                    生效——这是 2captcha token 交付进 Stripe enterprise hCaptcha 的前提。
                    仅订阅付款流程需要，默认关闭以最小化对其它流程的检测面。
        proxy: HTTP 代理 dict {"server","username","password"}，None 则直连。
               每账号一个出口 IP（反关联）；透传给 launch_persistent_context。
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
    if disable_site_isolation:
        # 关站点隔离：让跨域 iframe（Stripe/hCaptcha 的 OOPIF）变成同目标进程内子帧，
        # 于是主目标一条 CDP Page.addScriptToEvaluateOnNewDocument 即可前置注入到 hcaptcha 帧
        # （Patchright 的 add_init_script 在本构建失效，OOPIF 又抢不过 Playwright 的 resume）。
        launch_args += ["--disable-site-isolation-trials",
                        "--disable-features=IsolateOrigins,site-per-process"]
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
                bypass_csp=bypass_csp,      # 见 create_driver docstring：让内联 hook 注入不被 CSP 拦
                args=launch_args,
                **({"proxy": proxy} if proxy else {}),   # 每账号一个出口 IP；None 则直连
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


def create_driver_vanilla(profile_id, proxy=None):
    """用**原生 Playwright**（非 Patchright）创建持久 context + BrowserSession。

    proxy: HTTP 代理 dict {"server","username","password"}，None 直连；每账号一个出口 IP。

    专用于 opencode 订阅付款：Patchright 为反检测**阉割了 add_init_script / CDP 脚本前置注入**，
    导致无法在 Stripe enterprise hCaptcha 的跨域 OOPIF 帧脚本加载前 hook，2captcha token 交付不进去
    （见 07-25 task design.md 第一~五轮）。原生 Playwright 作主调试器能暂停 OOPIF 并原生前置注入
    add_init_script（第六轮实测：hcaptcha 帧 ec=2/rs=2，execute 被成功拦截）——故付款走原生栈。

    代价：原生栈隐蔽性弱于 Patchright，仅用于付款这一步；注册/登录仍可用 Patchright 主栈。
    profile 与 create_driver 同构（data/profiles/<safe>），复用已登录态。
    """
    from playwright.sync_api import sync_playwright as _vanilla_sync_playwright

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    safe_name = re.sub(r'[^\w@.\-]', '_', profile_id)
    user_data_dir = os.path.join(root, 'data', 'profiles', safe_name)
    os.makedirs(user_data_dir, exist_ok=True)
    download_dir = os.path.join(user_data_dir, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    print(f"🌐 初始化浏览器 (原生 Playwright, profile: {safe_name})...")

    # profile 清理（对齐 create_driver：回收孤儿 Chrome + 删 Singleton 锁 + 写语言）
    _kill_chrome_for_profile(user_data_dir, f'vanilla {safe_name}')
    for _name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        _p = os.path.join(user_data_dir, _name)
        try:
            if os.path.islink(_p) or os.path.exists(_p):
                os.remove(_p)
        except Exception:
            pass
    _write_profile_language(user_data_dir)

    w, h = random.choice(_WINDOW_SIZES)
    playwright = _vanilla_sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--no-first-run", "--no-default-browser-check",
                  f"--lang={BROWSER_LANG}", f"--accept-lang={BROWSER_ACCEPT_LANG}",
                  f"--window-size={w},{h}"],
            **({"proxy": proxy} if proxy else {}),   # 每账号一个出口 IP；None 则直连
        )
        page = context.pages[0] if context.pages else context.new_page()
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        try:
            context.set_extra_http_headers({"Accept-Language": BROWSER_ACCEPT_LANG_HEADER})
        except Exception:
            pass
        session = BrowserSession(playwright, context, page, temp_profile=None,
                                 download_dir=download_dir, user_data_dir=user_data_dir)
        page.on("response", session._on_response)
        print(f"  🖥️ 窗口: {w}x{h}")
        print("✅ 浏览器初始化成功 (原生 Playwright)")
        return session
    except Exception:
        print("  ❌ 原生浏览器初始化失败，正在清理...")
        try:
            playwright.stop()
        except Exception:
            pass
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


# ======================================================================
# 通用 CDP / DOM 工具
# ----------------------------------------------------------------------
# 从已删除的 Cloudflare 实现里保留下来的站点无关工具，供后续平台适配器复用。
# ======================================================================

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

