"""
浏览器自动化模块
使用 Selenium + selenium-stealth 实现 Cloudflare 注册、
账单页面导航及信用卡添加流程
"""

import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium_stealth import stealth

from config import (
    MAX_WAIT_TIME,
    SHORT_WAIT_TIME,
    ERROR_PAGE_MAX_RETRIES,
    BUTTON_CLICK_MAX_RETRIES,
)


def _get_matching_chromedriver():
    """
    通过 webdriver-manager 获取与当前 Chrome 匹配的 chromedriver 路径
    自动下载正确版本，避免系统中旧版 chromedriver 导致的兼容性问题
    """
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        path = ChromeDriverManager().install()
        print(f"  📦 chromedriver: {path}")
        return path
    except Exception as e:
        print(f"  ⚠️ 自动获取 chromedriver 失败: {e}")
    return None


def create_driver(headless=False):
    """
    创建带有反检测的 Chrome 浏览器驱动

    参数:
        headless: 是否使用无头模式
    返回:
        浏览器驱动实例
    """
    print(f"🌐 正在初始化浏览器 (Headless: {headless})...")
    options = Options()

    if headless:
        print("  👻 使用伪无头模式 (Off-screen)...")
        options.add_argument("--window-position=-10000,-10000")

    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # 使用 Selenium Manager 下载与当前 Chrome 匹配的 chromedriver
    chromedriver_path = _get_matching_chromedriver()
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_window_size(1280, 900)

    # 应用 selenium-stealth 反检测
    _apply_stealth(driver)

    print("✅ 浏览器初始化成功")
    return driver


def _apply_stealth(driver):
    """应用 selenium-stealth 反检测"""
    print("🎭 应用反检测伪装...")
    try:
        stealth(
            driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
    except Exception as e:
        print(f"  ⚠️ stealth 应用失败: {e}")


def type_slowly(element, text, delay=0.05):
    """模拟人工缓慢输入"""
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


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

        # 阶段2: 尝试自动点击 Turnstile checkbox（只尝试几次）
        if not auto_click_attempted:
            auto_click_attempted = True
            print("  🤖 尝试自动点击验证框...")
            if _try_click_turnstile(driver):
                # 点击后等待一段时间看是否通过
                time.sleep(8)
                if not _is_challenge_page(driver):
                    print("  ✅ 自动点击成功，验证已通过！")
                    return True
                print("  ⚠️ 自动点击未能通过验证")

        # 阶段3: 提示用户手动操作
        if not user_notified:
            user_notified = True
            remaining = int(max_wait - (time.time() - start))
            print("")
            print("  " + "=" * 50)
            print("  ⚠️  需要手动完成人机验证！")
            print("  👉 请在浏览器窗口中勾选验证框")
            print("  👉 (中文: '确认您是真人' / 英文: 'Verify you are human')")
            print(f"  ⏰ 剩余等待时间: {remaining} 秒")
            print("  " + "=" * 50)
            print("")

            # 尝试将浏览器窗口移到可见位置并置前
            try:
                driver.set_window_position(100, 100)
                driver.execute_script("window.focus();")
            except Exception:
                pass

        # 持续等待用户操作
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

    # 获取 iframe 元素在页面中的位置
    try:
        box_model = driver.execute_cdp_cmd('DOM.getBoxModel', {'backendNodeId': backend_node_id})
        content = box_model['model']['content']
        # content 是 [x1,y1, x2,y2, x3,y3, x4,y4] 四个角的坐标
        x = (content[0] + content[2]) / 2  # 中心 x — 但 checkbox 在左侧
        y = (content[1] + content[5]) / 2  # 中心 y
        # checkbox 在 iframe 左侧约 30px 处
        click_x = content[0] + 30
        click_y = (content[1] + content[5]) / 2

        print(f"    → CDP: 找到 Turnstile iframe, 点击坐标 ({click_x:.0f}, {click_y:.0f})")

        # 使用 CDP Input 事件模拟点击
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': click_x,
            'y': click_y,
            'button': 'left',
            'clickCount': 1,
        })
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': click_x,
            'y': click_y,
            'button': 'left',
            'clickCount': 1,
        })
        print("    ✅ CDP: 已模拟点击 Turnstile checkbox")
        return True
    except Exception as e:
        print(f"    ⚠️ CDP 点击失败: {e}")
        return False


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
    time.sleep(5)

    # 检查是否已通过
    if _is_turnstile_solved(driver):
        print("  ✅ 人机验证已自动通过！")
        return True

    # 自动点击失败，提示用户手动操作
    print("")
    print("  " + "=" * 50)
    print("  ⚠️  需要手动完成人机验证！")
    print("  👉 请在浏览器窗口中勾选验证框")
    print("  👉 (中文: '确认您是真人' / 英文: 'Verify you are human')")
    print(f"  ⏰ 等待时间: 最长 {max_wait} 秒")
    print("  " + "=" * 50)
    print("")

    # 将浏览器窗口移到可见位置
    try:
        driver.set_window_position(100, 100)
        driver.execute_script("window.focus();")
    except Exception:
        pass

    # 等待用户手动完成验证
    start = time.time()
    while time.time() - start < max_wait:
        if _is_turnstile_solved(driver):
            elapsed = int(time.time() - start)
            print(f"  ✅ 人机验证已通过！(耗时 {elapsed} 秒)")
            return True
        time.sleep(2)

    print("  ❌ 人机验证超时")
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

        # 方法3: 检查人机验证提示文字是否消失（中英文）
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if ('let us know you are human' not in body_text and
                'verify you are human' not in body_text and
                '确认您是真人' not in body_text and
                '证明你是人类' not in body_text):
                signup_btn = driver.find_elements(By.CSS_SELECTOR, 'button[type="submit"]')
                if signup_btn:
                    return True
        except Exception:
            pass

    except Exception:
        pass

    return False


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


def add_credit_card(driver, card_info):
    """
    在 Cloudflare 账单页面添加信用卡

    参数:
        driver: 浏览器驱动
        card_info: 信用卡信息字典，包含:
            - number: 卡号
            - expiry_month: 有效期月份 (MM)
            - expiry_year: 有效期年份 (YYYY)
            - cvc: 安全码
            - name: 持卡人姓名（可选）
            - address, city, state, zip, country: 账单地址（可选）
    返回:
        bool: 是否成功添加
    """
    wait = WebDriverWait(driver, 30)

    try:
        print("\n" + "=" * 50)
        print("💳 开始添加信用卡")
        print("=" * 50)

        # 查找 "添加付款方式" 按钮
        print("🔍 查找添加付款方式按钮...")
        time.sleep(3)

        add_btn_xpaths = [
            '//button[contains(., "Add") and contains(., "payment")]',
            '//button[contains(., "Add") and contains(., "Payment")]',
            '//a[contains(., "Add") and contains(., "payment")]',
            '//button[contains(., "Payment Method")]',
            '//button[contains(., "Edit payment")]',
            '//button[contains(., "Add a payment method")]',
            '//a[contains(., "Add a payment method")]',
            '//button[contains(., "Update")]',
            '//a[contains(@href, "payment")]',
        ]

        clicked_add = False
        for xpath in add_btn_xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xpath)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.5)
                        driver.execute_script("arguments[0].click();", btn)
                        print(f"  🔘 点击了: {btn.text}")
                        clicked_add = True
                        break
            except Exception:
                continue
            if clicked_add:
                break

        if not clicked_add:
            print("  ⚠️ 未找到添加付款方式按钮，尝试直接填写表单...")

        time.sleep(5)

        # 等待支付表单加载（基于 Stripe）
        print("⏳ 等待支付表单加载...")
        page_loaded = False
        start_wait = time.time()
        while time.time() - start_wait < 30:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input, iframe")
            if len(inputs) > 2:
                page_source = driver.page_source.lower()
                if 'stripe' in page_source or 'card' in page_source or 'payment' in page_source:
                    print("  ✅ 检测到支付表单")
                    page_loaded = True
                    break
            time.sleep(1)

        if not page_loaded:
            print("  ⚠️ 支付表单可能未加载完成，尝试继续填写...")

        time.sleep(2)

        # 填写卡号（通常在 Stripe iframe 中）
        print("💳 正在填写卡号...")
        _fill_stripe_field(driver, '卡号',
            'input[name="cardnumber"], input[placeholder*="Card number"], '
            'input[placeholder*="card number"], input[placeholder*="0000"], '
            'input[autocomplete="cc-number"], input[name="number"]',
            card_info.get('number', ''))
        time.sleep(1)

        # 填写有效期
        print("📅 正在填写有效期...")
        expiry = f"{card_info.get('expiry_month', '')}{card_info.get('expiry_year', '')[-2:]}"
        _fill_stripe_field(driver, '有效期',
            'input[name="exp-date"], input[name="expirationDate"], '
            'input[id="cardExpiry"], input[placeholder="MM / YY"], '
            'input[autocomplete="cc-exp"]',
            expiry)
        time.sleep(1)

        # 填写 CVC
        print("🔒 正在填写 CVC...")
        _fill_stripe_field(driver, 'CVC',
            'input[name="cvc"], input[name="securityCode"], '
            'input[id="cardCvc"], input[placeholder="CVC"], '
            'input[autocomplete="cc-csc"]',
            card_info.get('cvc', ''))
        time.sleep(1)

        # 填写持卡人姓名（如果提供且字段存在）
        if card_info.get('name'):
            print("👤 正在填写持卡人姓名...")
            _fill_stripe_field(driver, '姓名',
                'input[name="name"], input[name="billingName"], '
                'input[id="billingName"], input[placeholder*="name"], '
                'input[autocomplete="cc-name"], input[autocomplete="name"]',
                card_info['name'])
            time.sleep(1)

        # 填写账单地址（如果提供）
        if card_info.get('address'):
            print("🏠 正在填写账单地址...")
            _fill_visible_field(driver, '地址',
                'input[name="addressLine1"], input[placeholder*="Address"], '
                'input[placeholder*="address"], input[id*="address"]',
                card_info['address'])

        if card_info.get('city'):
            _fill_visible_field(driver, '城市',
                'input[name="city"], input[placeholder*="City"], '
                'input[placeholder*="city"], input[id*="city"]',
                card_info['city'])

        if card_info.get('state'):
            _fill_visible_field(driver, '州/省',
                'input[name="state"], select[name="state"], '
                'input[id*="state"], select[id*="state"]',
                card_info['state'])

        if card_info.get('zip'):
            _fill_visible_field(driver, '邮编',
                'input[name="postalCode"], input[placeholder*="ZIP"], '
                'input[placeholder*="Zip"], input[placeholder*="zip"], '
                'input[id*="postal"], input[name="zip"]',
                card_info['zip'])

        time.sleep(2)

        # 点击提交/保存按钮
        print("🔘 查找提交按钮...")
        submit_xpaths = [
            '//button[contains(., "Save")]',
            '//button[contains(., "Add")]',
            '//button[contains(., "Submit")]',
            '//button[contains(., "Confirm")]',
            '//button[@type="submit"]',
            '//button[contains(., "Pay")]',
        ]

        for xpath in submit_xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xpath)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.5)
                        driver.execute_script("arguments[0].click();", btn)
                        print(f"  🔘 已点击提交按钮: {btn.text}")
                        time.sleep(5)

                        # 检查是否成功
                        page_text = driver.page_source.lower()
                        if 'success' in page_text or 'added' in page_text or 'saved' in page_text:
                            print("🎉 信用卡添加成功！")
                            return True

                        # 如果没有明确的错误提示，也认为可能成功
                        error_indicators = driver.find_elements(By.CSS_SELECTOR,
                            '.error, [role="alert"], .StripeElement--invalid')
                        visible_errors = [e for e in error_indicators if e.is_displayed()]
                        if not visible_errors:
                            print("✅ 表单已提交（未检测到错误）")
                            return True
                        else:
                            for err in visible_errors:
                                print(f"  ❌ 错误: {err.text}")

                        break
            except Exception:
                continue

        print("⚠️ 无法确认信用卡是否添加成功")
        return False

    except Exception as e:
        print(f"❌ 添加信用卡失败: {e}")
        return False


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
