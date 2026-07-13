"""
浏览器自动化模块
使用 undetected-chromedriver 实现 Cloudflare 注册、
账单页面导航及信用卡添加流程
"""

import os
import re
import time
import random
import logging
import tempfile
import shutil
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

# 抑制 urllib3 连接池警告（undetected-chromedriver 高频操作时触发）
logging.getLogger('urllib3.connectionpool').setLevel(logging.ERROR)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

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


def create_driver(headless=False, profile_id=None):
    """
    创建带有反检测的 Chrome 浏览器驱动（使用 undetected-chromedriver）

    参数:
        headless: 是否使用无头模式
        profile_id: 持久化 profile 标识（如 email），传入后复用同一浏览器环境；
                    为 None 时使用全新临时 profile
    返回:
        浏览器驱动实例
    """
    print(f"🌐 正在初始化浏览器 (Headless: {headless})...")

    options = uc.ChromeOptions()

    # 最小化启动，不抢占用户焦点；需要干预时点 Dock 图标即可还原
    options.add_argument("--start-minimized")

    if headless:
        print("  👻 使用伪无头模式 (Off-screen)...")
        options.add_argument("--window-position=-10000,-10000")

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

    # 随机语言
    lang = random.choice(_LANGUAGES)
    options.add_argument(f"--lang={lang.split(',')[0]}")
    options.add_argument(f"--accept-lang={lang}")

    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")

    driver = uc.Chrome(
        options=options,
        use_subprocess=True,
        user_data_dir=user_data_dir,
    )

    try:
        # 随机窗口尺寸
        w, h = random.choice(_WINDOW_SIZES)
        driver.set_window_size(w, h)
        print(f"  🖥️ 窗口: {w}x{h}, 语言: {lang.split(',')[0]}")

        # macOS 上 --start-minimized 不可靠，直接发 WebDriver 命令最小化
        if not headless:
            driver.minimize_window()

        # 记录 profile 目录，临时 profile 关闭时清理，持久化 profile 保留
        driver._cf_temp_profile = None if is_persistent else user_data_dir

        # 设置下载目录（用于 invoice PDF 下载等）
        download_dir = os.path.join(user_data_dir, 'downloads')
        os.makedirs(download_dir, exist_ok=True)
        driver._cf_download_dir = download_dir
        try:
            driver.execute_cdp_cmd('Page.setDownloadBehavior', {
                'behavior': 'allow',
                'downloadPath': download_dir,
            })
        except Exception:
            pass

        # 注入控制台拦截器（在所有页面 JS 执行之前生效）
        # 用于捕获 Cloudflare/Stripe 的错误日志
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    window.__cfAutoErrors = [];
                    (function() {
                        var methods = ['log', 'error', 'warn'];
                        methods.forEach(function(method) {
                            var orig = console[method].bind(console);
                            console[method] = function() {
                                var msg = '';
                                try {
                                    msg = Array.from(arguments).map(function(a) {
                                        return typeof a === 'string' ? a : String(a);
                                    }).join(' ');
                                } catch(e) {}
                                if (/setup.intent.error/i.test(msg) ||
                                    /form.error.handler/i.test(msg) ||
                                    /payment.intent.failed/i.test(msg) ||
                                    /failed.to.save.payment/i.test(msg) ||
                                    /card.*(incorrect|invalid|declined|expired|failed)/i.test(msg) ||
                                    /security.code.*(incorrect|invalid)/i.test(msg) ||
                                    /cvc.*(incorrect|invalid|incomplete)/i.test(msg)) {
                                    window.__cfAutoErrors.push(msg.substring(0, 300));
                                }
                                orig.apply(null, arguments);
                            };
                        });
                    })();
                '''
            })
        except Exception:
            pass

        print("✅ 浏览器初始化成功 (undetected-chromedriver)")
        return driver
    except Exception:
        # Chrome 进程已启动但后续初始化失败，必须清理避免孤儿进程
        print("  ❌ 浏览器初始化失败，正在清理...")
        try:
            driver.quit()
        except Exception:
            pass
        try:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass
        raise


def close_driver(driver):
    """安全关闭浏览器并清理临时 profile"""
    try:
        driver.quit()
    except Exception:
        pass
    # 清理临时 profile 目录
    temp_dir = getattr(driver, '_cf_temp_profile', None)
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"  🧹 已清理临时 profile: ...{os.path.basename(temp_dir)}")
        except Exception:
            pass


def type_slowly(element, text, delay=0.05):
    """模拟人工缓慢输入"""
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


def inject_network_interceptor(driver, patterns):
    """
    注入网络响应拦截器，捕获匹配指定 URL 模式的请求响应

    参数:
        driver: 浏览器驱动
        patterns: URL 关键词列表，每个元素是一个列表，URL 需同时包含所有关键词才匹配
                  例如: [['api.stripe.com', 'confirm'], ['ai-gateway', 'topup']]
    """
    import json as _json
    patterns_js = _json.dumps(patterns)
    driver.execute_script('''
        window.__netInterceptResponses = [];
        var patterns = ''' + patterns_js + ''';
        function matchUrl(url) {
            for (var i = 0; i < patterns.length; i++) {
                var keywords = patterns[i];
                var matched = true;
                for (var j = 0; j < keywords.length; j++) {
                    if (url.indexOf(keywords[j]) === -1) { matched = false; break; }
                }
                if (matched) return true;
            }
            return false;
        }
        // 拦截 fetch
        (function() {
            var origFetch = window.fetch;
            window.fetch = function() {
                var url = typeof arguments[0] === 'string' ? arguments[0] : (arguments[0].url || '');
                if (matchUrl(url)) {
                    return origFetch.apply(this, arguments).then(function(response) {
                        var clone = response.clone();
                        clone.text().then(function(text) {
                            try { var data = JSON.parse(text); } catch(e) { var data = text; }
                            window.__netInterceptResponses.push({
                                url: url, status: response.status, data: data, ts: Date.now()
                            });
                        }).catch(function() {});
                        return response;
                    });
                }
                return origFetch.apply(this, arguments);
            };
        })();
        // 拦截 XMLHttpRequest
        (function() {
            var origOpen = XMLHttpRequest.prototype.open;
            var origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this._cfInterceptUrl = url;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function() {
                var xhr = this;
                if (xhr._cfInterceptUrl && matchUrl(xhr._cfInterceptUrl)) {
                    xhr.addEventListener('load', function() {
                        try { var data = JSON.parse(xhr.responseText); } catch(e) { var data = xhr.responseText; }
                        window.__netInterceptResponses.push({
                            url: xhr._cfInterceptUrl, status: xhr.status, data: data, ts: Date.now()
                        });
                    });
                }
                return origSend.apply(this, arguments);
            };
        })();
    ''')
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
        time.sleep(1)
        responses = driver.execute_script('return window.__netInterceptResponses || [];')
        if responses:
            if first_found_time is None:
                first_found_time = time.time()
                print(f"捕获到 {len(responses)} 个响应，等待后续响应...")
            # 收到第一个响应后再等几秒，收集可能的后续请求
            if time.time() - first_found_time >= 3:
                break

    responses = driver.execute_script('return window.__netInterceptResponses || [];')
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
        dialog = driver.find_elements(By.CSS_SELECTOR, "div[role='alertdialog']")
        if not dialog:
            return False
        btn = dialog[0].find_elements(By.XPATH, ".//button[.//span[text()='I understand']]")
        if btn:
            btn[0].click()
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
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
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
    driver.switch_to.default_content()

    # 方法1: 通过 CDP 穿透 closed shadow DOM 找到 iframe 并点击
    try:
        clicked = _click_turnstile_via_cdp(driver)
        if clicked:
            return True
    except Exception as e:
        print(f"    ⚠️ CDP 方式失败: {e}")

    # 方法2: 通过容器元素的坐标偏移点击 checkbox 位置
    try:
        container = driver.find_element(
            By.CSS_SELECTOR,
            'div[data-testid="challenge-widget-container"]'
        )
        if container.is_displayed():
            # Turnstile checkbox 通常在容器左侧偏上的位置
            # iframe 宽度 300px，高度 65px，checkbox 大约在 (30, 33) 的位置
            actions = ActionChains(driver)
            actions.move_to_element_with_offset(container, 30, 33).click().perform()
            print("    → 通过坐标偏移点击了 Turnstile 容器")
            time.sleep(2)
            return True
    except Exception:
        pass

    # 方法3: 查找页面中可见的 iframe（非 shadow DOM 场景的回退）
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in frames:
            try:
                src = frame.get_attribute('src') or ''
                title_attr = frame.get_attribute('title') or ''
                if 'challenges' in src or 'turnstile' in src or 'widget' in title_attr.lower():
                    if not frame.is_displayed():
                        continue
                    actions = ActionChains(driver)
                    actions.move_to_element(frame).click().perform()
                    print("    → 点击了 Turnstile iframe")
                    time.sleep(2)

                    # 尝试切入 iframe 查找 checkbox
                    try:
                        driver.switch_to.frame(frame)
                        checkboxes = driver.find_elements(
                            By.CSS_SELECTOR,
                            "input[type='checkbox'], .cb-lb, #challenge-stage, .ctp-checkbox-label"
                        )
                        for cb in checkboxes:
                            if cb.is_displayed():
                                try:
                                    cb.click()
                                except Exception:
                                    driver.execute_script("arguments[0].click();", cb)
                                print("    → 点击了验证框 checkbox")
                                driver.switch_to.default_content()
                                return True
                        driver.switch_to.default_content()
                    except Exception:
                        driver.switch_to.default_content()
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                continue
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    return False


def _click_turnstile_via_cdp(driver):
    """
    使用 Chrome DevTools Protocol (CDP) 穿透 closed shadow DOM
    找到 Turnstile iframe 并模拟点击其 checkbox 区域
    """
    # 通过 CDP 获取整个 DOM 树（包括 shadow DOM）
    doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1, 'pierce': True})
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
        # 优先使用 getContentQuads 获取视口坐标
        try:
            quads = driver.execute_cdp_cmd('DOM.getContentQuads', {'backendNodeId': backend_node_id})
            quad = quads['quads'][0]
            click_x = quad[0] + 30
            click_y = (quad[1] + quad[5]) / 2
        except Exception:
            box_model = driver.execute_cdp_cmd('DOM.getBoxModel', {'backendNodeId': backend_node_id})
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
    """使用 CDP 在指定视口坐标处模拟完整的鼠标点击（mouseMoved + mousePressed + mouseReleased）"""
    driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
        'type': 'mouseMoved',
        'x': x,
        'y': y,
    })
    time.sleep(0.1)
    driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
        'type': 'mousePressed',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1,
    })
    time.sleep(0.05)
    driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
        'type': 'mouseReleased',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1,
    })


def _get_viewport_coords(driver, element):
    """
    获取元素相对于视口的坐标（而非页面坐标）。
    CDP Input.dispatchMouseEvent 需要视口坐标。
    """
    return driver.execute_script("""
        var rect = arguments[0].getBoundingClientRect();
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    """, element)


def _click_hcaptcha_via_cdp(driver):
    """
    使用 CDP 找到 hCaptcha iframe 并模拟点击其 checkbox 区域
    hCaptcha 通常出现在 LightboxModal 弹窗中的 .HCaptcha-container 内
    """
    try:
        # 方法1: 直接通过 Selenium 找到 hCaptcha iframe 并用 CDP 点击
        hcaptcha_iframes = driver.find_elements(
            By.CSS_SELECTOR,
            '#HCaptcha-root iframe[src*="hcaptcha"], '
            '.HCaptcha-container iframe[src*="hcaptcha"], '
            'iframe[data-hcaptcha-widget-id], '
            'iframe[src*="hcaptcha.com"]'
        )

        # 过滤出 checkbox iframe（尺寸较小，通常宽度 < 400px）
        checkbox_iframe = None
        for iframe in hcaptcha_iframes:
            if not iframe.is_displayed():
                continue
            size = iframe.size
            # hCaptcha checkbox iframe 通常尺寸约 302x78 或类似
            # 图片挑战 iframe 尺寸较大 (>400px)
            if size.get('width', 0) < 400:
                checkbox_iframe = iframe
                break
            elif not checkbox_iframe:
                checkbox_iframe = iframe  # 兜底取第一个可见的

        if checkbox_iframe:
            # 先滚动到 iframe 可见
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox_iframe)
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

        # 方法2: CDP DOM 遍历查找（包括 shadow DOM）
        doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1, 'pierce': True})
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
            # 使用 DOM.getContentQuads 获取视口坐标（比 getBoxModel 更准确）
            try:
                quads = driver.execute_cdp_cmd('DOM.getContentQuads', {'backendNodeId': backend_node_id})
                quad = quads['quads'][0]
                # quad 是 [x1,y1, x2,y2, x3,y3, x4,y4] 四个角的视口坐标
                click_x = quad[0] + 30
                click_y = (quad[1] + quad[5]) / 2
            except Exception:
                # 回退到 getBoxModel
                box_model = driver.execute_cdp_cmd('DOM.getBoxModel', {'backendNodeId': backend_node_id})
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
            containers = driver.find_elements(
                By.CSS_SELECTOR,
                'div[data-testid="challenge-widget-container"], '
                '[id*="cf-chl-widget"], .cf-turnstile, [data-sitekey]'
            )
            if containers:
                # 容器存在，再检查内部 iframe 是否加载
                has_iframe = driver.execute_script("""
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
                """)
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
    在已切入的 Stripe iframe 内，等待输入字段或嵌套 iframe 实际渲染完成。
    弹窗和外层 iframe 可能先出现，但内部字段延迟加载。
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 检查1: 直接输入框
            inputs = driver.find_elements(By.CSS_SELECTOR,
                'input[name="cardnumber"], input[name="number"], '
                'input[autocomplete="cc-number"], '
                'input[data-elements-stable-field-name="cardNumber"]'
            )
            for inp in inputs:
                if inp.is_displayed():
                    print("  ✅ Stripe 输入字段已就绪")
                    time.sleep(0.5)
                    return True

            # 检查2: 嵌套 iframe（Stripe 每个字段一个 iframe）
            inner_frames = driver.find_elements(By.TAG_NAME, 'iframe')
            visible_frames = [f for f in inner_frames if f.is_displayed()]
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
    """
    driver.switch_to.default_content()
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 查找 first_name 或 country 字段（地址表单的标志性字段）
            fields = driver.find_elements(By.CSS_SELECTOR,
                '[role="dialog"] input[name="first_name"], '
                '[role="dialog"] input[name="country"], '
                '[data-testid="address-form"] input[name="first_name"]'
            )
            for f in fields:
                if f.is_displayed():
                    print("  ✅ 账单地址表单已加载")
                    time.sleep(0.5)
                    return True
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
        containers = driver.find_elements(
            By.CSS_SELECTOR,
            'div[data-testid="challenge-widget-container"], '
            'div.c_v'  # Cloudflare 注册页的验证组件 class
        )
        if containers:
            turnstile_found = True

        # 方法2: 查找包含人机验证提示文本（支持中英文）
        if not turnstile_found:
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if ('let us know you are human' in body_text or
                'verify you are human' in body_text or
                '确认您是真人' in body_text or
                '证明你是人类' in body_text or
                '请确认您不是机器人' in body_text):
                turnstile_found = True

        # 方法3: 通过 JS 查找 shadow DOM 中的 Turnstile iframe
        if not turnstile_found:
            has_turnstile = driver.execute_script("""
                // 查找 cf_challenge_response 隐藏 input
                var cf = document.querySelector('input[name="cf_challenge_response"]');
                if (cf) return true;
                // 查找 id 包含 cf-chl-widget 的元素
                var widget = document.querySelector('[id*="cf-chl-widget"]');
                if (widget) return true;
                return false;
            """)
            if has_turnstile:
                turnstile_found = True
    except Exception:
        pass

    if not turnstile_found:
        print("  ℹ️ 未检测到内嵌人机验证，继续")
        return True

    print("  🔒 检测到内嵌 Turnstile 人机验证")

    # 尝试自动点击
    print("  🤖 尝试自动点击验证框...")
    _try_click_turnstile(driver)

    # CDP 点击后需要等待 Turnstile 后台验证完成（通常 5~20 秒）
    # 不能过早进入 2Captcha，否则会干扰正在进行的验证导致组件刷新
    print("  ⏳ 等待 Turnstile 后台验证...")
    for i in range(10):
        time.sleep(3)
        if _is_turnstile_solved(driver):
            print("  ✅ 人机验证已自动通过！")
            return True
        if i == 3:
            print("  ⏳ 仍在等待验证结果...")

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
        result = driver.execute_script("""
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
        """)
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
        hidden_inputs = driver.find_elements(
            By.CSS_SELECTOR,
            'input[name="cf_challenge_response"], '
            'input[name="cf-turnstile-response"], '
            'input[name*="turnstile"], '
            'input[name*="challenge_response"]'
        )
        for inp in hidden_inputs:
            value = inp.get_attribute('value') or ''
            if len(value) > 10:  # Turnstile token 很长
                return True

        # 方法2: 通过 JS 检查隐藏 input（可能被 shadow DOM 包裹）
        try:
            result = driver.execute_script("""
                // 直接查找所有 id 包含 response 的 input
                var inputs = document.querySelectorAll('input[id$="_response"]');
                for (var i = 0; i < inputs.length; i++) {
                    if (inputs[i].value && inputs[i].value.length > 10) return true;
                }
                // 查找 cf_challenge_response
                var cf = document.querySelector('input[name="cf_challenge_response"]');
                if (cf && cf.value && cf.value.length > 10) return true;
                return false;
            """)
            if result:
                return True
        except Exception:
            pass

    except Exception:
        pass

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
    wait = WebDriverWait(driver, MAX_WAIT_TIME)

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
                els = driver.find_elements(By.CSS_SELECTOR, login_selector)
                if els and els[0].is_displayed():
                    email_input = els[0]
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
        password_input = driver.find_element(
            By.CSS_SELECTOR,
            'input[type="password"], input[name="password"], input[id="password"]'
        )
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
                btn = driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            try:
                btns = driver.find_elements(By.TAG_NAME, 'button')
                for btn in btns:
                    text = btn.text.lower()
                    if 'log in' in text or 'sign in' in text or 'login' in text:
                        driver.execute_script("arguments[0].click();", btn)
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
            wait = WebDriverWait(driver, 120)
            try:
                topup_btn = wait.until(EC.element_to_be_clickable((
                    By.XPATH, "//button[.//span[text()='Top-up credits']]"
                )))
            except Exception:
                print(f"第 {attempt} 次未找到 Top-up credits 按钮，页面可能未加载完成")
                if attempt < max_retries:
                    print("刷新页面重试...")
                    continue
                return False

            print("找到 Top-up credits 按钮，正在点击...")
            topup_btn.click()
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


def fill_topup_and_confirm(driver, amount=10):
    """
    在 Top-up 弹窗中输入金额并点击确认支付

    参数:
        driver: 浏览器驱动
        amount: 充值金额（美元），默认 10
    返回:
        (bool, list, str): (是否成功点击, API 响应列表, 使用的卡片后四位)
    """
    wait = WebDriverWait(driver, 30)
    card_last4 = ''

    try:
        # 等待弹窗中的金额输入框出现
        print(f"等待充值弹窗加载...")
        price_input = wait.until(EC.visibility_of_element_located((
            By.CSS_SELECTOR, "div[role='dialog'] input#price"
        )))

        # 提取弹窗中显示的支付卡片后四位
        try:
            dialog = driver.find_element(By.CSS_SELECTOR, "div[role='dialog']")
            card_text = dialog.text
            match = re.search(r'(\d{4})\s*$', card_text.replace('\n', ' ').strip())
            if not match:
                # 尝试匹配 •••• •••• •••• 1234 格式
                match = re.search(r'[•·*\s]+(\d{4})', card_text)
            if match:
                card_last4 = match.group(1)
                print(f"检测到支付卡片后四位: {card_last4}")
        except Exception:
            pass

        # 清空并输入金额
        price_input.click()
        price_input.clear()
        time.sleep(0.5)
        price_input.send_keys(str(amount))
        print(f"已输入充值金额: ${amount}")
        time.sleep(1)

        # 注入网络拦截器，捕获充值相关的接口响应
        inject_network_interceptor(driver, [
            ['api.stripe.com', 'payment_intents', 'confirm'],
            ['ai-gateway', 'billing', 'topup'],
        ])

        # 等待 "Confirm and pay" 按钮变为可点击（输入金额后 disabled 属性会移除）
        confirm_btn = wait.until(EC.element_to_be_clickable((
            By.XPATH, "//div[@role='dialog']//button[.//span[text()='Confirm and pay']]"
        )))

        print("正在点击 Confirm and pay...")
        confirm_btn.click()
        print("已点击 Confirm and pay，等待响应...")

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


def handle_unpaid_invoices(driver):
    """
    检查 Credits 页面的 Unpaid invoice，下载 PDF 并打开 Pay online 链接

    参数:
        driver: 浏览器驱动
    返回:
        list: 处理结果列表 [{invoice, status, pay_url, error}, ...]
    """
    results = []
    download_dir = getattr(driver, '_cf_download_dir', None)
    if not download_dir:
        print("未配置下载目录，跳过 Unpaid invoice 处理")
        return results

    try:
        # 等待 top-up history 表格加载
        time.sleep(3)

        # 查找所有包含 Unpaid 的行
        rows = driver.find_elements(By.XPATH, "//table//tr[.//span[text()='Unpaid']]")
        if not rows:
            print("未发现 Unpaid invoice")
            return results

        print(f"发现 {len(rows)} 条 Unpaid invoice，开始处理...")

        for i, row in enumerate(rows):
            invoice_id = ''
            try:
                # 提取 invoice 编号
                invoice_link = row.find_element(By.XPATH, ".//a[@role='button']")
                invoice_id = invoice_link.text.strip()
                print(f"[{i+1}/{len(rows)}] 处理 invoice: {invoice_id}")

                # 清空下载目录中的旧 PDF
                for f in os.listdir(download_dir):
                    if f.endswith('.pdf'):
                        os.remove(os.path.join(download_dir, f))

                # 点击下载
                invoice_link.click()
                print(f"  已点击下载 {invoice_id}...")

                # 等待 PDF 下载完成
                pdf_path = None
                for _ in range(30):
                    time.sleep(1)
                    pdfs = [f for f in os.listdir(download_dir) if f.endswith('.pdf') and not f.endswith('.crdownload')]
                    if pdfs:
                        pdf_path = os.path.join(download_dir, pdfs[0])
                        break

                if not pdf_path:
                    print(f"  {invoice_id} PDF 下载超时")
                    results.append({"invoice": invoice_id, "status": "failed", "error": "PDF 下载超时"})
                    continue

                print(f"  PDF 已下载: {os.path.basename(pdf_path)}")

                # 从 PDF 提取支付链接
                pay_url = _extract_pdf_pay_url(pdf_path)
                if not pay_url:
                    print(f"  {invoice_id} 未找到 Pay online 链接")
                    results.append({"invoice": invoice_id, "status": "failed", "error": "未找到支付链接"})
                    continue

                print(f"  找到支付链接: {pay_url}")

                # 在浏览器中打开支付链接
                driver.get(pay_url)
                time.sleep(5)
                print(f"  已打开 {invoice_id} 的在线支付页面")
                results.append({"invoice": invoice_id, "status": "opened", "pay_url": pay_url})

                # 等待支付页面加载完成后再处理下一条
                time.sleep(3)

            except Exception as e:
                print(f"  处理 {invoice_id} 异常: {e}")
                results.append({"invoice": invoice_id, "status": "failed", "error": str(e)})
                continue

    except Exception as e:
        print(f"处理 Unpaid invoices 异常: {e}")

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
    wait = WebDriverWait(driver, MAX_WAIT_TIME)

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
        email_input = wait.until(EC.visibility_of_element_located((
            By.CSS_SELECTOR,
            'input[type="email"], input[name="email"], input[id="email"], input[autocomplete="email"]'
        )))
        email_input.clear()
        type_slowly(email_input, email)
        print(f"✅ 已输入邮箱: {email}")
        time.sleep(1)

        # 填写密码
        print("🔑 正在填写密码...")
        password_input = driver.find_element(
            By.CSS_SELECTOR,
            'input[type="password"], input[name="password"], input[id="password"]'
        )
        password_input.clear()
        type_slowly(password_input, password)
        print("✅ 密码已输入")
        time.sleep(1)

        # 勾选条款复选框（如果存在）
        try:
            terms_checkbox = driver.find_element(
                By.CSS_SELECTOR,
                'input[type="checkbox"][name*="terms"], input[type="checkbox"][id*="terms"], '
                'input[type="checkbox"][name*="agree"], input[type="checkbox"][id*="agree"]'
            )
            if not terms_checkbox.is_selected():
                try:
                    terms_checkbox.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", terms_checkbox)
                print("✅ 已勾选服务条款")
                time.sleep(0.5)
        except Exception:
            # 尝试通过 label 点击
            try:
                labels = driver.find_elements(By.CSS_SELECTOR, 'label')
                for label in labels:
                    text = label.text.lower()
                    if 'agree' in text or 'terms' in text or 'policy' in text:
                        label.click()
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
                btn = driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # 按文本内容查找按钮
            try:
                btns = driver.find_elements(By.TAG_NAME, 'button')
                for btn in btns:
                    text = btn.text.lower()
                    if 'sign up' in text or 'create' in text or 'register' in text:
                        driver.execute_script("arguments[0].click();", btn)
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
                code_input = WebDriverWait(driver, 30).until(
                    EC.visibility_of_element_located((
                        By.CSS_SELECTOR,
                        'input[name="code"], input[type="text"][maxlength="6"], '
                        'input[autocomplete="one-time-code"]'
                    ))
                )
                code_input.clear()
                type_slowly(code_input, verification_data)
                time.sleep(1)

                # 提交验证码
                try:
                    submit_btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
                    submit_btn.click()
                except Exception:
                    code_input.send_keys(Keys.ENTER)

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
    wait = WebDriverWait(driver, 30)

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
                el = driver.find_element(By.XPATH, xpath)
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    print(f"  🔘 点击了: {el.text}")
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
                el = driver.find_element(By.XPATH, xpath)
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    print(f"  🔘 点击了账单链接: {el.text}")
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
            sidebar_links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="billing"], nav a')
            for link in sidebar_links:
                href = link.get_attribute('href') or ''
                text = link.text.lower()
                if 'billing' in href or 'billing' in text:
                    driver.execute_script("arguments[0].click();", link)
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
            no_payment = driver.find_elements(
                By.XPATH,
                '//*[contains(text(), "No payment method on file")]'
            )
            visible_no_payment = [el for el in no_payment if el.is_displayed()]
            if visible_no_payment:
                print("  💳 检测到 'No payment method on file'，当前无绑定信用卡")
                return 0
        except Exception:
            pass

        # 方法2: 查找 Billing method 区域内的卡号末四位标识
        card_elements = driver.find_elements(
            By.XPATH,
            '//*[contains(text(), "••••") or contains(text(), "****")]'
        )
        visible_cards = [el for el in card_elements if el.is_displayed()]
        if visible_cards:
            count = len(visible_cards)
            print(f"  💳 检测到 {count} 张已绑定的信用卡 (通过卡号标识)")
            return count

        # 方法3: 通过 JS 在 Billing method 区域计数
        count = driver.execute_script("""
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
        """)
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
        add_btn = driver.execute_script("""
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
        """)

        if add_btn:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", add_btn)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", add_btn)
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
            btns = driver.find_elements(By.XPATH, xpath)
            for btn in btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", btn)
                    print(f"  🔘 点击了: {btn.text.strip()}")
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
            dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
            for dialog in dialogs:
                if dialog.is_displayed():
                    text = dialog.text.lower()
                    if 'add a payment method' in text or 'payment' in text:
                        print("  ✅ 检测到 Add a payment method 弹窗")
                        return True
        except Exception:
            pass
        time.sleep(1)

    print("  ❌ 等待 payment 弹窗超时")
    return False


def _wait_for_stripe_iframe(driver, timeout=60):
    """
    等待 Stripe 信用卡表单 iframe 加载完成

    Stripe 表单位于:
    div[data-test-id="credit-card-form"] > .StripeElement > .__PrivateStripeElement > iframe

    返回:
        WebElement or None: Stripe iframe 元素
    """
    start = time.time()
    debug_logged = False

    while time.time() - start < timeout:
        try:
            # 确保在主文档上下文
            driver.switch_to.default_content()

            # 先检查弹窗 dialog 内的所有 iframe
            dialog_iframes = driver.find_elements(
                By.CSS_SELECTOR,
                '[role="dialog"] iframe'
            )

            # 每 10 秒输出一次调试信息
            elapsed = int(time.time() - start)
            if elapsed > 0 and elapsed % 10 == 0 and not debug_logged:
                debug_logged = True
                iframe_info = []
                for f in dialog_iframes:
                    try:
                        title = f.get_attribute('title') or ''
                        name = f.get_attribute('name') or ''
                        src = (f.get_attribute('src') or '')[:80]
                        visible = f.is_displayed()
                        iframe_info.append(f"title='{title}' name='{name}' visible={visible} src='{src}...'")
                    except Exception:
                        pass
                print(f"  🔍 DEBUG: 弹窗内找到 {len(dialog_iframes)} 个 iframe:")
                for info in iframe_info:
                    print(f"    - {info}")
            elif elapsed % 10 != 0:
                debug_logged = False

            # 策略1: 精确匹配 data-test-id="credit-card-form" 内的 iframe
            stripe_iframes = driver.find_elements(
                By.CSS_SELECTOR,
                '[data-test-id="credit-card-form"] iframe'
            )
            for iframe in stripe_iframes:
                if iframe.is_displayed():
                    title = iframe.get_attribute('title') or ''
                    print(f"  ✅ 找到 credit-card-form 内的 iframe (title='{title}')")
                    return iframe

            # 策略2: 按 title 属性匹配
            for iframe in dialog_iframes:
                try:
                    if not iframe.is_displayed():
                        continue
                    title = (iframe.get_attribute('title') or '').lower()
                    if 'secure payment' in title or 'payment input' in title:
                        print(f"  ✅ 找到 Stripe payment iframe (title 匹配)")
                        return iframe
                except Exception:
                    continue

            # 策略3: 按 src 属性匹配 (stripe.com 且是 payment 类型)
            for iframe in dialog_iframes:
                try:
                    if not iframe.is_displayed():
                        continue
                    src = (iframe.get_attribute('src') or '').lower()
                    name = (iframe.get_attribute('name') or '').lower()
                    if 'stripe.com' in src and ('payment' in src or 'elements-inner' in src):
                        # 排除 express checkout iframe
                        if 'express' not in src and 'express' not in name:
                            print(f"  ✅ 找到 Stripe iframe (src 匹配)")
                            return iframe
                except Exception:
                    continue

            # 策略4: 按 iframe name 匹配 (__privateStripeFrame)
            for iframe in dialog_iframes:
                try:
                    if not iframe.is_displayed():
                        continue
                    name = iframe.get_attribute('name') or ''
                    src = (iframe.get_attribute('src') or '').lower()
                    if name.startswith('__privateStripeFrame') and 'express' not in src:
                        print(f"  ✅ 找到 Stripe iframe (name='{name}')")
                        return iframe
                except Exception:
                    continue

        except Exception as e:
            print(f"  ⚠️ 查找 iframe 异常: {e}")

        time.sleep(2)

    # 超时，输出最终的 iframe 调试信息
    print("  ❌ 等待 Stripe iframe 超时 (60秒)")
    try:
        all_iframes = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"] iframe')
        print(f"  🔍 最终状态: 弹窗内共 {len(all_iframes)} 个 iframe:")
        for f in all_iframes:
            try:
                title = f.get_attribute('title') or ''
                name = f.get_attribute('name') or ''
                src = (f.get_attribute('src') or '')[:100]
                visible = f.is_displayed()
                print(f"    - title='{title}' name='{name}' visible={visible}")
                print(f"      src={src}")
            except Exception:
                pass
    except Exception:
        pass

    return None


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
    driver.switch_to.default_content()
    time.sleep(1)

    def fill_input(name_attr, value, label=""):
        if not value:
            return False
        try:
            selectors = [
                f'[data-testid="address-form"] input[name="{name_attr}"]',
                f'[role="dialog"] input[name="{name_attr}"]',
                f'input[name="{name_attr}"]',
            ]
            for sel in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.2)
                        el.clear()
                        type_slowly(el, value)
                        print(f"  ✅ 填写 {label or name_attr}: {value}")
                        time.sleep(0.3)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
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
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el.is_displayed():
                        country_input = el
                        break
                except Exception:
                    continue

            if country_input:
                country_input.click()
                time.sleep(0.3)
                # 清除已有内容: 用 ActionChains 确保修饰键正确按住
                # send_keys(Keys.CONTROL + 'a') 在 Windows 上可能泄漏字母 'a'
                actions = ActionChains(driver)
                actions.click(country_input)
                actions.key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL)
                actions.pause(0.1)
                actions.send_keys(Keys.BACKSPACE)
                actions.perform()
                time.sleep(0.3)
                # 输入国家名称
                type_slowly(country_input, country)
                time.sleep(1.5)
                # 从下拉列表中找到精确匹配的选项并点击
                exact_matched = False
                try:
                    options = driver.find_elements(By.CSS_SELECTOR, '[role="option"], [role="listbox"] li, ul[id] li')
                    for opt in options:
                        opt_text = opt.text.strip()
                        if opt_text.lower() == country.lower():
                            opt.click()
                            exact_matched = True
                            break
                except Exception:
                    pass
                if not exact_matched:
                    # 回退：选择第一项
                    country_input.send_keys(Keys.ARROW_DOWN)
                    time.sleep(0.3)
                    country_input.send_keys(Keys.ENTER)
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
        stripe_iframe = _wait_for_stripe_iframe(driver)
        if not stripe_iframe:
            return False, "[操作失败] Stripe表单未加载"

        time.sleep(2)

        # 4. 切入 Stripe iframe 填写信用卡信息
        print("💳 正在填写信用卡信息 (Stripe iframe)...")
        driver.switch_to.default_content()
        driver.switch_to.frame(stripe_iframe)
        time.sleep(1)

        # 等待 Stripe iframe 内部组件（输入字段/嵌套 iframe）实际渲染完成
        # 弹窗和外层 iframe 出现后，内部字段可能仍在加载中
        _wait_for_stripe_fields_ready(driver)

        # Stripe Payment Element 使用统一的表单，字段可能在嵌套 iframe 中
        # 尝试在当前 iframe 内直接查找并填写
        card_filled = _fill_stripe_payment_element(driver, card_info)

        driver.switch_to.default_content()

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
        try:
            driver.execute_script("window.__cfAutoErrors = [];")
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
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_btn)
        time.sleep(0.5)

        # 优先使用原生 Selenium 点击（更好地触发 React/Stripe 事件）
        try:
            submit_btn.click()
            print("  🔘 已点击 'Add payment method' 按钮 (原生点击)")
            submitted = True
        except Exception as e1:
            print(f"  ⚠️ 原生点击失败: {e1}, 尝试 JS 点击...")
            try:
                driver.execute_script("arguments[0].click();", submit_btn)
                print("  🔘 已点击 'Add payment method' 按钮 (JS 点击)")
                submitted = True
            except Exception as e2:
                print(f"  ⚠️ JS 点击也失败: {e2}")

        if not submitted:
            print("  ❌ 点击提交按钮失败")
            return False, "[浏览器中断] 点击提交按钮失败"

        # 8. 等待提交结果（含人机验证检测）
        return _wait_for_payment_submit_result(driver)

    except Exception as e:
        print(f"❌ 添加信用卡失败: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False, f"[浏览器中断] {str(e)[:150]}"


def _handle_dialog_turnstile(driver, max_wait=120):
    """
    处理 Add payment method 弹窗内的 Turnstile 验证
    弹窗中可能出现 "Let us know you're human" + Turnstile checkbox
    需要在点击提交按钮之前完成验证

    注意: 卡片错误可能和 Turnstile 同时出现，优先检测卡片错误
    """
    driver.switch_to.default_content()

    # 优先检查是否已有卡片错误（卡信息有误时过验证码也没用）
    card_error = _check_dialog_card_error(driver)
    if card_error:
        print(f"  ❌ 检测到卡片错误，跳过 Turnstile: {card_error}")
        return False

    # 检查弹窗内是否存在 Turnstile
    has_turnstile = False
    try:
        dialog = driver.find_element(By.CSS_SELECTOR, '[role="dialog"]')
        if not dialog.is_displayed():
            return True

        dialog_text = dialog.text.lower()
        if ("let us know you" in dialog_text or
            "verify you are human" in dialog_text or
            "captcha is required" in dialog_text or
            '确认您是真人' in dialog_text or
            '证明你是人类' in dialog_text):
            has_turnstile = True

        # 也检查弹窗内是否有 Turnstile iframe 或容器
        if not has_turnstile:
            turnstile_els = dialog.find_elements(By.CSS_SELECTOR,
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '[data-testid="challenge-widget-container"], '
                'iframe[title*="challenge"], '
                'iframe[title*="Turnstile"], '
                '[id*="cf-chl-widget"]')
            if any(el.is_displayed() for el in turnstile_els):
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
        driver.switch_to.default_content()

        # 方法1: 检查隐藏 input 是否有 token 值
        hidden_inputs = driver.find_elements(
            By.CSS_SELECTOR,
            'input[name="cf_challenge_response"], '
            'input[name="cf-turnstile-response"], '
            'input[name*="turnstile"], '
            'input[name*="challenge_response"]'
        )
        for inp in hidden_inputs:
            value = inp.get_attribute('value') or ''
            if len(value) > 10:
                return True

        # 方法2: 通过 JS 查找（包括弹窗内部）
        try:
            result = driver.execute_script("""
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
            """)
            if result:
                return True
        except Exception:
            pass

        # 方法3: 检查弹窗中的验证提示文字是否消失
        try:
            dialog = driver.find_element(By.CSS_SELECTOR, '[role="dialog"]')
            if dialog.is_displayed():
                dialog_text = dialog.text.lower()
                # 如果 "captcha is required" 和 "verify you are human" 都不在了
                if ('captcha is required' not in dialog_text and
                    'let us know you' not in dialog_text and
                    'verify you are human' not in dialog_text):
                    return True
        except Exception:
            pass

        # 方法4: 检查 Turnstile iframe 中的 checkbox 是否已勾选（通过 aria-checked）
        try:
            result = driver.execute_script("""
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
            """)
            if result:
                return True
        except Exception:
            pass

    except Exception:
        pass

    return False


def _check_browser_console_for_errors(driver):
    """
    从拦截的控制台日志中检测 Stripe/支付错误
    通过 Page.addScriptToEvaluateOnNewDocument 在页面 JS 执行前注入拦截器，
    捕获 Cloudflare 输出的如:
      ⛔️ Setup intent error: Your card's CVC is incorrect.
      ⚠️ Form error handler [There was an error processing your card...]
    """
    try:
        errors = driver.execute_script("return window.__cfAutoErrors || [];")
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

    # 方法2: CDP - 在每个 iframe 的独立执行上下文中查询 DOM
    try:
        frame_tree = driver.execute_cdp_cmd('Page.getFrameTree', {})
        all_frames = _collect_all_child_frames(frame_tree.get('frameTree', {}))

        for frame_info in all_frames:
            try:
                world = driver.execute_cdp_cmd('Page.createIsolatedWorld', {
                    'frameId': frame_info['id'],
                    'worldName': 'err_chk_' + str(int(time.time() * 1000)),
                    'grantUniversalAccess': True,
                })
                ctx_id = world.get('executionContextId')
                if not ctx_id:
                    continue

                result = driver.execute_cdp_cmd('Runtime.evaluate', {
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

    # 方法3: CDP DOM 穿透遍历
    try:
        doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1, 'pierce': True})
        error_text = _find_stripe_field_errors_in_dom(doc['root'])
        if error_text:
            return error_text
    except Exception:
        pass

    # 方法4: Selenium frame 切换（最后兜底）
    try:
        driver.switch_to.default_content()
        target_iframes = []
        all_dialog_iframes = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"] iframe')
        for iframe in all_dialog_iframes:
            try:
                if not iframe.is_displayed():
                    continue
                name = iframe.get_attribute('name') or ''
                src = (iframe.get_attribute('src') or '').lower()
                title = (iframe.get_attribute('title') or '').lower()
                if ('stripe' in src or 'payment' in title.lower() or
                    name.startswith('__privateStripeFrame')):
                    if 'express' not in src:
                        target_iframes.append(iframe)
            except Exception:
                continue

        for sf in target_iframes:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(sf)
                field_errors = driver.find_elements(By.CSS_SELECTOR,
                    '.p-FieldError, [role="alert"][id*="Error"], '
                    '[role="alert"].Error, [id*="Error"].Error')
                for fe in field_errors:
                    if fe.is_displayed():
                        err_text = fe.text.strip()
                        if err_text:
                            driver.switch_to.default_content()
                            return err_text[:200]
            except Exception:
                pass

        driver.switch_to.default_content()
    except Exception:
        try:
            driver.switch_to.default_content()
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
        dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
        for d in dialogs:
            if d.is_displayed():
                dialog = d
                break
        if not dialog:
            return None

        dialog_text = dialog.text

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
            error_els = dialog.find_elements(By.CSS_SELECTOR,
                '[role="alert"], [data-error], '
                '.field-error, .form-error, .validation-error')
            for err_el in error_els:
                if not err_el.is_displayed():
                    continue
                err_text = err_el.text.strip()
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
    """在弹窗中查找 'Add payment method' 提交按钮"""
    # 方法1: 精确匹配 data-kumo-component 按钮
    try:
        submit_btn = driver.execute_script("""
            var dialog = document.querySelector('[role="dialog"]');
            if (!dialog) return null;
            var buttons = dialog.querySelectorAll('button[data-kumo-component="Button"]');
            for (var i = 0; i < buttons.length; i++) {
                var text = buttons[i].textContent.trim();
                if (text === 'Add payment method') {
                    return buttons[i];
                }
            }
            return null;
        """)
        if submit_btn and submit_btn.is_displayed():
            return submit_btn
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
            btns = driver.find_elements(By.XPATH, xpath)
            for btn in btns:
                if btn.is_displayed() and 'Cancel' not in btn.text:
                    return btn
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
        driver: 浏览器驱动
        max_wait: 最大等待时间（秒），默认 180 秒
    返回:
        tuple[bool, str]: (是否成功添加, 错误原因字符串)
    """
    print("⏳ 等待提交结果...")
    time.sleep(5)

    # 提交后先检查是否已有错误（Stripe 返回快的话 5 秒内就有结果）
    card_error = _check_dialog_card_error(driver)
    if card_error:
        print(f"  ❌ 添加失败: {card_error}")
        _close_payment_dialog(driver)
        return False, card_error

    # 检查弹窗是否已关闭（提交成功）
    try:
        dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
        visible_dialogs = [d for d in dialogs if d.is_displayed()]
        if not visible_dialogs:
            print("🎉 信用卡添加成功！(弹窗已关闭)")
            return True, ""
    except Exception:
        pass

    print("  ⏳ 开始检测结果...")

    user_notified_captcha = False
    last_retry_click_time = time.time() - 5  # 首次重试等待5秒，后续重试等待10秒
    retry_click_count = 0
    max_retry_clicks = 3
    loading_stuck_since = None  # 按钮进入 loading 状态的时间
    start = time.time()

    while time.time() - start < max_wait:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        # 检查1: 弹窗是否已关闭（成功标志）
        try:
            dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
            visible_dialogs = [d for d in dialogs if d.is_displayed()]
            if not visible_dialogs:
                print("🎉 信用卡添加成功！(弹窗已关闭)")
                return True, ""
        except Exception:
            pass

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
            turnstile = driver.find_elements(
                By.CSS_SELECTOR,
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '[data-testid="challenge-widget-container"], '
                'iframe[title*="challenge"], '
                'iframe[title*="Turnstile"]'
            )
            visible_turnstile = [el for el in turnstile if el.is_displayed()]
            if visible_turnstile:
                captcha_type = 'turnstile'

            # hCaptcha (主文档 + 所有 iframe 内部)
            if not captcha_type:
                hcaptcha = driver.find_elements(
                    By.CSS_SELECTOR,
                    'iframe[src*="hcaptcha.com"], '
                    'iframe[src*="hcaptcha"], '
                    '.HCaptcha-container, '
                    '.h-captcha, '
                    '#HCaptcha-root, '
                    'iframe[title*="hCaptcha"], '
                    'iframe[title*="hcaptcha"], '
                    'iframe[data-hcaptcha-widget-id], '
                    '[data-hcaptcha-widget-id]'
                )
                visible_hcaptcha = [el for el in hcaptcha if el.is_displayed()]
                if visible_hcaptcha:
                    captcha_type = 'hcaptcha'

                # 在嵌套 iframe 中查找 hCaptcha
                if not captcha_type:
                    try:
                        all_iframes = driver.find_elements(By.TAG_NAME, 'iframe')
                        for iframe in all_iframes:
                            try:
                                if not iframe.is_displayed():
                                    continue
                                driver.switch_to.frame(iframe)
                                inner_hcaptcha = driver.find_elements(
                                    By.CSS_SELECTOR,
                                    'iframe[src*="hcaptcha.com"], '
                                    'iframe[src*="hcaptcha-inner"], '
                                    'iframe[src*="hcaptcha"], '
                                    '.h-captcha, '
                                    '[data-hcaptcha-widget-id]'
                                )
                                if any(el.is_displayed() for el in inner_hcaptcha):
                                    captcha_type = 'hcaptcha'
                                driver.switch_to.default_content()
                                if captcha_type:
                                    break
                            except Exception:
                                try:
                                    driver.switch_to.default_content()
                                except Exception:
                                    pass
                    except Exception:
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass

            # Stripe 3DS 验证弹窗
            if not captcha_type:
                threed_frames = driver.find_elements(
                    By.CSS_SELECTOR,
                    'iframe[name*="__stripeJSAuth"], '
                    'iframe[src*="3ds"], '
                    'iframe[title*="3D Secure"]'
                )
                visible_3ds = [el for el in threed_frames if el.is_displayed()]
                if visible_3ds:
                    captcha_type = 'unknown'

            # 页面文本检测
            if not captcha_type:
                body_text = driver.find_element(By.TAG_NAME, 'body').text.lower()
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
                            hc = driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="hcaptcha.com"]')
                            if any(el.is_displayed() for el in hc):
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
                            # 先检查是否意外直接通过了
                            try:
                                dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
                                visible_dialogs = [d for d in dialogs if d.is_displayed()]
                                if not visible_dialogs:
                                    print("  🎉 hCaptcha 直接通过，信用卡添加成功！")
                                    return True, ""
                            except Exception:
                                pass
                            try:
                                modals = driver.find_elements(By.CSS_SELECTOR,
                                    '.LightboxModal-open, .HCaptcha-container')
                                if not any(m.is_displayed() for m in modals):
                                    print("  ✅ hCaptcha 验证已通过！")
                                    solved = True
                                    break
                            except Exception:
                                pass
                            # 检测图片挑战 iframe 是否已加载
                            try:
                                challenge_iframes = driver.find_elements(By.CSS_SELECTOR,
                                    'iframe[src*="hcaptcha.com/challenge"], '
                                    'iframe[src*="hcaptcha.com/getcaptcha"], '
                                    'iframe[src*="newassets.hcaptcha.com"][style*="position"]')
                                # 图片挑战 iframe 通常尺寸较大（宽>300px）
                                for cf in challenge_iframes:
                                    if cf.is_displayed() and cf.size.get('width', 0) > 300:
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
                            try:
                                dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
                                if not any(d.is_displayed() for d in dialogs):
                                    print("  🎉 Turnstile 通过，信用卡添加成功！")
                                    return True, ""
                            except Exception:
                                pass
                    if captcha_solver.is_available():
                        print("  🤖 尝试使用 2Captcha 解决 Turnstile...")
                        solved = captcha_solver.solve_turnstile(driver)
                else:
                    if captcha_solver.is_available():
                        print("  🤖 尝试使用 2Captcha 自动解决...")
                        solved = captcha_solver.solve_hcaptcha(driver) or captcha_solver.solve_turnstile(driver)

                if solved:
                    time.sleep(5)
                    try:
                        dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
                        visible_dialogs = [d for d in dialogs if d.is_displayed()]
                        if not visible_dialogs:
                            print("  🎉 验证解决成功，信用卡添加成功！")
                            return True, ""
                    except Exception:
                        pass
                    # 检查 LightboxModal 是否关闭
                    try:
                        modals = driver.find_elements(By.CSS_SELECTOR,
                            '.LightboxModal-open, .HCaptcha-container')
                        if not any(m.is_displayed() for m in modals):
                            print("  ✅ 验证已通过，等待页面响应...")
                    except Exception:
                        pass
                    # 验证完成后弹窗仍在 → 重新点击提交按钮
                    try:
                        resubmit_btn = _find_payment_submit_button(driver)
                        if resubmit_btn and resubmit_btn.is_enabled():
                            print("  🔄 验证解决后重新点击提交按钮...")
                            try:
                                resubmit_btn.click()
                            except Exception:
                                driver.execute_script("arguments[0].click();", resubmit_btn)
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
        elapsed = int(time.time() - start)
        if time.time() - last_retry_click_time > 7:
            try:
                retry_btn = _find_payment_submit_button(driver)
                if retry_btn:
                    btn_enabled = retry_btn.is_enabled()
                    aria_disabled = retry_btn.get_attribute('aria-disabled') == 'true'
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
                                    driver.execute_script("arguments[0].click();", retry_btn)
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
                iframe_count = len(driver.find_elements(By.TAG_NAME, 'iframe'))
                dialog_count = len([d for d in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]') if d.is_displayed()])
                print(f"  ⏳ 等待中... ({elapsed}s) 弹窗:{dialog_count} iframe:{iframe_count}")
            except Exception:
                pass
        time.sleep(3)

    # 超时
    print(f"  ⚠️ 等待提交结果超时 ({max_wait}秒)")
    # 最后检查一次弹窗状态
    try:
        dialogs = driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]')
        visible_dialogs = [d for d in dialogs if d.is_displayed()]
        if not visible_dialogs:
            print("🎉 信用卡添加成功！(弹窗已关闭)")
            return True, ""
    except Exception:
        pass

    _close_payment_dialog(driver)
    return False, f"[超时] 等待提交结果超过{max_wait}秒"


def _fill_stripe_payment_element(driver, card_info):
    """
    在 Stripe Payment Element iframe 内填写信用卡信息

    Stripe Payment Element 是一个统一的支付表单组件，
    包含卡号、有效期、CVC 等字段，可能使用 div[contenteditable]
    或嵌套 iframe 的方式渲染

    参数:
        driver: 已切入 Stripe iframe 的驱动
        card_info: 信用卡信息
    返回:
        bool: 是否成功填写
    """
    filled_any = False

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

    def try_fill_selectors(selectors, value, label):
        nonlocal filled_any
        if not value:
            return False
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    tag = el.tag_name.lower()
                    if tag == 'select':
                        # 下拉选择框：用 JS 设置值
                        from selenium.webdriver.support.ui import Select
                        select = Select(el)
                        try:
                            select.select_by_visible_text(value)
                        except Exception:
                            try:
                                select.select_by_value(value)
                            except Exception:
                                # 尝试模糊匹配
                                for opt in select.options:
                                    if value.lower() in opt.text.lower():
                                        opt.click()
                                        break
                    else:
                        el.click()
                        time.sleep(0.3)
                        type_slowly(el, value)
                    print(f"  ✅ 填写 {label}")
                    filled_any = True
                    return True
            except Exception:
                continue
        return False

    # 先尝试在当前 frame 直接填写
    number = card_info.get('number', '')
    exp_month = card_info.get('expiry_month', '')
    exp_year = card_info.get('expiry_year', '')
    expiry = f"{exp_month}{exp_year[-2:]}" if exp_year else exp_month
    cvc = card_info.get('cvc', '')

    def try_fill_billing_fields():
        """尝试填写 Stripe iframe 内的账单地址字段"""
        for field_name, selectors, value_fn in billing_fields:
            value = value_fn(card_info)
            if value:
                try_fill_selectors(selectors, value, field_name)
                time.sleep(0.3)

    if try_fill_selectors(card_selectors, number, '卡号'):
        time.sleep(0.5)
        try_fill_selectors(expiry_selectors, expiry, '有效期')
        time.sleep(0.5)
        try_fill_selectors(cvc_selectors, cvc, 'CVC')
        time.sleep(0.5)
        # 同一 frame 内可能也有账单地址字段
        try_fill_billing_fields()
        return filled_any

    # 当前 iframe 没有直接字段，可能有嵌套 iframe
    # Stripe Payment Element 每个字段（卡号、有效期、CVC、账单地址）各自在独立的嵌套 iframe 中
    card_filled_nested = False
    expiry_filled_nested = False
    cvc_filled_nested = False
    billing_filled_in_nested = False
    try:
        inner_frames = driver.find_elements(By.TAG_NAME, 'iframe')
        for frame in inner_frames:
            try:
                if not frame.is_displayed():
                    continue
                driver.switch_to.frame(frame)

                if not card_filled_nested and try_fill_selectors(card_selectors, number, '卡号'):
                    card_filled_nested = True
                elif not expiry_filled_nested and try_fill_selectors(expiry_selectors, expiry, '有效期'):
                    expiry_filled_nested = True
                elif not cvc_filled_nested and try_fill_selectors(cvc_selectors, cvc, 'CVC'):
                    cvc_filled_nested = True
                else:
                    # 尝试填写账单地址字段（可能分布在不同嵌套 iframe 中）
                    for field_name, selectors, value_fn in billing_fields:
                        value = value_fn(card_info)
                        if value and try_fill_selectors(selectors, value, field_name):
                            billing_filled_in_nested = True
                            time.sleep(0.2)

                driver.switch_to.parent_frame()
                time.sleep(0.3)
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    pass
    except Exception:
        pass

    if card_filled_nested or expiry_filled_nested or cvc_filled_nested:
        # 卡信息填了部分，也尝试在父 frame 层填写账单地址
        if not billing_filled_in_nested:
            try_fill_billing_fields()
        return filled_any

    # 最后尝试: 用 Tab 键在表单字段间切换输入
    print("  ⚠️ 未找到独立字段，尝试 Tab 键导航输入...")
    try:
        # 点击 iframe 区域获取焦点
        # Windows 上 Stripe iframe 内 body 可能尺寸为零，无法直接 click()
        # 使用 JS focus + ActionChains 点击坐标作为回退
        try:
            body = driver.find_element(By.TAG_NAME, 'body')
            body.click()
        except Exception:
            # body 尺寸为零时，用 JS 聚焦 + ActionChains 点击 iframe 中心
            driver.execute_script("document.body.focus();")
            try:
                # 尝试找到任意可见元素并点击
                first_el = driver.execute_script(
                    "return document.querySelector('div, span, input, label, p');"
                )
                if first_el:
                    ActionChains(driver).move_to_element(first_el).click().perform()
                else:
                    ActionChains(driver).send_keys("").perform()
            except Exception:
                pass
        time.sleep(0.5)

        # 输入卡号
        actions = ActionChains(driver)
        for char in number:
            actions.send_keys(char)
            actions.pause(0.05)
        actions.perform()
        print("  ✅ 输入卡号 (Tab 方式)")
        filled_any = True
        time.sleep(0.5)

        # Tab 到有效期
        ActionChains(driver).send_keys(Keys.TAB).perform()
        time.sleep(0.3)
        actions = ActionChains(driver)
        for char in expiry:
            actions.send_keys(char)
            actions.pause(0.05)
        actions.perform()
        print("  ✅ 输入有效期 (Tab 方式)")
        time.sleep(0.5)

        # Tab 到 CVC
        ActionChains(driver).send_keys(Keys.TAB).perform()
        time.sleep(0.3)
        actions = ActionChains(driver)
        for char in cvc:
            actions.send_keys(char)
            actions.pause(0.05)
        actions.perform()
        print("  ✅ 输入 CVC (Tab 方式)")

    except Exception as e:
        print(f"  ❌ Tab 方式输入失败: {e}")

    return filled_any


def _close_payment_dialog(driver):
    """关闭 Add a payment method 弹窗"""
    try:
        driver.switch_to.default_content()
        # 点击 Cancel 按钮
        cancel_btn = driver.execute_script("""
            var dialog = document.querySelector('[role="dialog"]');
            if (!dialog) return null;
            var buttons = dialog.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                if (buttons[i].textContent.trim() === 'Cancel') {
                    return buttons[i];
                }
            }
            // 查找关闭按钮 (aria-label="Close")
            var close = dialog.querySelector('button[aria-label="Close"]');
            return close;
        """)
        if cancel_btn:
            cancel_btn.click()
            print("  🔘 已关闭弹窗")
            time.sleep(2)
    except Exception:
        pass


def _fill_stripe_field(driver, field_name, selectors_str, value):
    """
    填写 Stripe 表单字段
    先在主文档查找，找不到则递归遍历所有 iframe
    """
    selectors = [s.strip() for s in selectors_str.split(',')]

    def try_fill():
        for selector in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    except Exception:
                        pass
                    type_slowly(el, value)
                    return True
            except Exception:
                continue
        return False

    # 在主文档中查找
    if try_fill():
        print(f"  ✅ 在主文档找到 {field_name}")
        return True

    # 递归遍历 iframe（支持 2 层嵌套）
    driver.switch_to.default_content()

    def traverse_frames(depth=0, max_depth=2):
        if depth >= max_depth:
            return False

        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for i, frame in enumerate(frames):
            try:
                if not frame.is_displayed():
                    continue

                driver.switch_to.frame(frame)

                if try_fill():
                    print(f"  ✅ 在 iframe (d={depth}, i={i}) 中找到 {field_name}")
                    driver.switch_to.default_content()
                    return True

                if traverse_frames(depth + 1, max_depth):
                    return True

                driver.switch_to.parent_frame()

            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    pass
                continue

        return False

    if traverse_frames():
        return True

    driver.switch_to.default_content()
    print(f"  ❌ 未找到 {field_name} 输入框")
    return False


def _fill_visible_field(driver, field_name, selectors_str, value):
    """填写主文档或 iframe 中的可见字段"""
    selectors = [s.strip() for s in selectors_str.split(',')]

    # 在主文档中查找
    for selector in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            if el.is_displayed():
                if el.tag_name == 'select':
                    el.send_keys(value)
                    el.send_keys(Keys.ENTER)
                else:
                    el.clear()
                    type_slowly(el, value)
                print(f"  ✅ 填写 {field_name}: {value}")
                return True
        except Exception:
            continue

    # 在 iframe 中查找
    driver.switch_to.default_content()
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for frame in frames:
        try:
            if not frame.is_displayed():
                continue
            driver.switch_to.frame(frame)
            for selector in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, selector)
                    if el.is_displayed():
                        if el.tag_name == 'select':
                            el.send_keys(value)
                        else:
                            el.clear()
                            type_slowly(el, value)
                        print(f"  ✅ 填写 {field_name}: {value} (iframe)")
                        driver.switch_to.default_content()
                        return True
                except Exception:
                    continue
            driver.switch_to.default_content()
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    return False
