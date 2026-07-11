"""
验证码自动解决模块
使用 2Captcha 服务解决 Cloudflare Turnstile 人机验证
"""

import time

try:
    from twocaptcha import TwoCaptcha
except ImportError:
    TwoCaptcha = None

_solver = None
_api_key = None


def init_solver(api_key):
    global _solver, _api_key
    if not api_key:
        _solver = None
        _api_key = None
        return
    if TwoCaptcha is None:
        print("  2captcha-python not installed, captcha service unavailable")
        return
    _api_key = api_key
    _solver = TwoCaptcha(api_key)
    print(f"  2Captcha solver initialized (Key: {api_key[:8]}...)")


def is_available():
    return _solver is not None


def solve_turnstile(driver, max_retries=2):
    if not is_available():
        print("  2Captcha solver not initialized")
        return False

    sitekey = _extract_turnstile_sitekey(driver)
    if not sitekey:
        print("  Cannot extract Turnstile sitekey")
        return False

    page_url = driver.current_url
    print(f"  Turnstile sitekey: {sitekey[:20]}...")
    print(f"  Page URL: {page_url}")

    for attempt in range(max_retries):
        try:
            print(f"  Calling 2Captcha for Turnstile... (attempt {attempt + 1}/{max_retries})")
            result = _solver.turnstile(sitekey=sitekey, url=page_url)
            token = result.get('code', '')
            if not token:
                print(f"  2Captcha returned empty token")
                continue

            print(f"  2Captcha returned token (length: {len(token)})")

            if _inject_turnstile_token(driver, token):
                print("  Turnstile token injected")
                time.sleep(3)
                return True
            else:
                print("  Token injection failed, retrying...")

        except Exception as e:
            error_msg = str(e)
            print(f"  2Captcha call failed: {error_msg}")
            if _is_fatal_error(error_msg):
                return False

    return False


def solve_hcaptcha(driver, max_retries=2):
    if not is_available():
        print("  2Captcha solver not initialized")
        return False

    sitekey = _extract_hcaptcha_sitekey(driver)
    if not sitekey:
        print("  Cannot extract hCaptcha sitekey")
        return False

    rqdata = _extract_hcaptcha_rqdata(driver)

    page_url = driver.current_url
    print(f"  hCaptcha sitekey: {sitekey[:20]}...")
    if rqdata:
        print(f"  hCaptcha rqdata found (length: {len(rqdata)})")
    print(f"  Page URL: {page_url}")

    for attempt in range(max_retries):
        try:
            print(f"  Calling 2Captcha for hCaptcha... (attempt {attempt + 1}/{max_retries})")
            if rqdata:
                result = _solver.hcaptcha(sitekey=sitekey, url=page_url, data=rqdata)
            else:
                result = _solver.hcaptcha(sitekey=sitekey, url=page_url)
            token = result.get('code', '')
            if not token:
                print(f"  2Captcha returned empty token")
                continue

            print(f"  2Captcha returned token (length: {len(token)})")

            if _inject_hcaptcha_token(driver, token, sitekey):
                print("  hCaptcha token injected")
                time.sleep(3)
                return True
            else:
                print("  Token injection failed, retrying...")

        except Exception as e:
            error_msg = str(e)
            print(f"  2Captcha call failed: {error_msg}")
            if _is_fatal_error(error_msg):
                return False

    return False


def _is_fatal_error(error_msg):
    if 'ERROR_WRONG_USER_KEY' in error_msg or 'ERROR_KEY_DOES_NOT_EXIST' in error_msg:
        print("  Invalid API Key, stopping retries")
        return True
    if 'ERROR_ZERO_BALANCE' in error_msg:
        print("  2Captcha balance is zero, stopping retries")
        return True
    return False


def _extract_turnstile_sitekey(driver):
    import re as _re
    try:
        # 方法1: 主文档直接查找 data-sitekey 属性
        sitekey = driver.execute_script("""
            var el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            var cf = document.querySelector('.cf-turnstile');
            if (cf && cf.getAttribute('data-sitekey')) return cf.getAttribute('data-sitekey');
            var widgets = document.querySelectorAll('[id*="cf-chl-widget"]');
            for (var i = 0; i < widgets.length; i++) {
                var sk = widgets[i].getAttribute('data-sitekey');
                if (sk) return sk;
            }
            return null;
        """)
        if sitekey:
            return sitekey

        # 方法2: 从 Turnstile iframe src URL 中提取 sitekey
        # iframe src 格式: https://challenges.cloudflare.com/.../0xABCDEF.../...
        sitekey = driver.execute_script("""
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                var src = iframes[i].src || '';
                if (src.indexOf('challenges.cloudflare.com') >= 0 || src.indexOf('turnstile') >= 0) {
                    // sitekey 在 URL 路径中，格式为 0x 开头的十六进制字符串
                    var match = src.match(/\\/(?:sitekey\\/|)(0x[A-Fa-f0-9A-Za-z_-]{5,})/);
                    if (match) return match[1];
                    // 也可能在查询参数中
                    var urlMatch = src.match(/[?&](?:sitekey|k)=(0x[A-Fa-f0-9A-Za-z_-]{5,})/);
                    if (urlMatch) return urlMatch[1];
                }
            }
            return null;
        """)
        if sitekey:
            return sitekey

        # 方法3: 从页面 HTML/JS 中正则匹配 sitekey
        sitekey = driver.execute_script("""
            var html = document.documentElement.outerHTML;
            var patterns = [
                /data-sitekey="(0x[A-Za-z0-9_-]+)"/,
                /sitekey['":\\s]+'?(0x[A-Za-z0-9_-]+)/,
                /turnstile[^}]*sitekey['":\\s]+'?(0x[A-Za-z0-9_-]+)/,
                /turnstile\\.render\\([^)]*sitekey['":\\s]+'?(0x[A-Za-z0-9_-]+)/,
            ];
            for (var i = 0; i < patterns.length; i++) {
                var m = html.match(patterns[i]);
                if (m) return m[1];
            }
            return null;
        """)
        if sitekey:
            return sitekey

        # 方法4: CDP 穿透 shadow DOM，查找 data-sitekey 属性和 iframe src
        try:
            doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1, 'pierce': True})
            sitekey = _find_sitekey_in_dom_tree(doc['root'])
            if sitekey:
                return sitekey
        except Exception:
            pass

    except Exception as e:
        print(f"  Extract sitekey error: {e}")

    return None


def _find_sitekey_in_dom_tree(node):
    import re as _re
    attrs = node.get('attributes', [])
    if attrs:
        attr_dict = dict(zip(attrs[::2], attrs[1::2]))
        # 直接查找 data-sitekey 属性
        sitekey = attr_dict.get('data-sitekey', '')
        if sitekey and sitekey.startswith('0x'):
            return sitekey

        # 从 Turnstile iframe 的 src URL 中提取 sitekey
        if node.get('nodeName', '').lower() == 'iframe':
            src = attr_dict.get('src', '')
            if 'challenges.cloudflare.com' in src or 'turnstile' in src.lower():
                # URL 路径中的 sitekey: /0xABCDEF.../
                m = _re.search(r'/(?:sitekey/)?(0x[A-Fa-f0-9A-Za-z_-]{5,})', src)
                if m:
                    return m.group(1)
                # 查询参数中的 sitekey
                m = _re.search(r'[?&](?:sitekey|k)=(0x[A-Fa-f0-9A-Za-z_-]{5,})', src)
                if m:
                    return m.group(1)

    for child in node.get('children', []):
        result = _find_sitekey_in_dom_tree(child)
        if result:
            return result

    for shadow in node.get('shadowRoots', []):
        for child in shadow.get('children', []):
            result = _find_sitekey_in_dom_tree(child)
            if result:
                return result

    return None


def _extract_hcaptcha_rqdata(driver):
    """从 Stripe 嵌套的 hCaptcha iframe URL 中提取 enterprise rqdata 参数"""
    import re as _re
    import urllib.parse
    try:
        all_iframes = driver.find_elements(
            __import__('selenium').webdriver.common.by.By.TAG_NAME, 'iframe')
        for iframe in all_iframes:
            try:
                src = iframe.get_attribute('src') or ''
                # Stripe hCaptcha iframe: js.stripe.com/v3/hcaptcha-inner-*.html#...&rqdata=...
                if 'hcaptcha' not in src.lower():
                    continue
                # rqdata 可能在 hash fragment 或 query 参数中
                m = _re.search(r'[#&?]rqdata=([^&]+)', src)
                if m:
                    return urllib.parse.unquote(m.group(1))
            except Exception:
                pass
    except Exception as e:
        print(f"  Extract hCaptcha rqdata error: {e}")
    return None


def _extract_hcaptcha_sitekey(driver):
    import re as _re
    try:
        # 方法1: 主文档中查找 hCaptcha 容器和 iframe
        sitekey = driver.execute_script("""
            // 从 iframe src 中提取 sitekey 参数
            var iframes = document.querySelectorAll('iframe[src*="hcaptcha.com"], iframe[src*="hcaptcha"]');
            for (var i = 0; i < iframes.length; i++) {
                var match = iframes[i].src.match(/sitekey=([a-f0-9-]+)/i);
                if (match) return match[1];
            }
            // 从 data-sitekey 属性获取
            var el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            var hc = document.querySelector('.h-captcha');
            if (hc && hc.getAttribute('data-sitekey')) return hc.getAttribute('data-sitekey');
            // 从 #HCaptcha-root 容器中查找
            var root = document.querySelector('#HCaptcha-root');
            if (root) {
                var inner = root.querySelector('[data-sitekey]');
                if (inner) return inner.getAttribute('data-sitekey');
            }
            // 从 data-hcaptcha-widget-id 元素附近查找
            var widgets = document.querySelectorAll('[data-hcaptcha-widget-id]');
            for (var i = 0; i < widgets.length; i++) {
                var sk = widgets[i].getAttribute('data-sitekey');
                if (sk) return sk;
            }
            return null;
        """)
        if sitekey:
            return sitekey

        # 方法2: 切入 iframe 内部查找 hCaptcha sitekey
        try:
            all_iframes = driver.find_elements(
                __import__('selenium').webdriver.common.by.By.TAG_NAME, 'iframe')
            for iframe in all_iframes:
                try:
                    if not iframe.is_displayed():
                        continue
                    src = iframe.get_attribute('src') or ''
                    # 从 iframe src URL 提取 sitekey
                    if 'hcaptcha' in src.lower():
                        m = _re.search(r'sitekey=([a-f0-9-]+)', src, _re.IGNORECASE)
                        if m:
                            return m.group(1)
                    # 切入 iframe 查找内部的 hCaptcha 元素
                    driver.switch_to.frame(iframe)
                    inner_sitekey = driver.execute_script("""
                        var iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
                        for (var i = 0; i < iframes.length; i++) {
                            var match = iframes[i].src.match(/sitekey=([a-f0-9-]+)/i);
                            if (match) return match[1];
                        }
                        var el = document.querySelector('[data-sitekey]');
                        if (el) return el.getAttribute('data-sitekey');
                        var hc = document.querySelector('.h-captcha');
                        if (hc && hc.getAttribute('data-sitekey')) return hc.getAttribute('data-sitekey');
                        return null;
                    """)
                    driver.switch_to.default_content()
                    if inner_sitekey:
                        return inner_sitekey
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

        # 方法3: 从页面 HTML/JS 中正则匹配
        sitekey = driver.execute_script("""
            var html = document.documentElement.outerHTML;
            var patterns = [
                /data-sitekey="([a-f0-9-]{36,})"/i,
                /hcaptcha[^}]*sitekey['":\\s]+'?([a-f0-9-]{36,})/i,
                /sitekey['":\\s]+'?([a-f0-9-]{36,})/i,
            ];
            for (var i = 0; i < patterns.length; i++) {
                var m = html.match(patterns[i]);
                if (m) return m[1];
            }
            return null;
        """)
        if sitekey:
            return sitekey

        # 方法4: CDP 穿透 shadow DOM 查找
        try:
            doc = driver.execute_cdp_cmd('DOM.getDocument', {'depth': -1, 'pierce': True})
            sitekey = _find_hcaptcha_sitekey_in_dom_tree(doc['root'])
            if sitekey:
                return sitekey
        except Exception:
            pass

    except Exception as e:
        print(f"  Extract hCaptcha sitekey error: {e}")

    return None


def _find_hcaptcha_sitekey_in_dom_tree(node):
    """CDP DOM 树中递归查找 hCaptcha sitekey"""
    import re as _re
    attrs = node.get('attributes', [])
    if attrs:
        attr_dict = dict(zip(attrs[::2], attrs[1::2]))
        # data-sitekey 属性（hCaptcha sitekey 是 UUID 格式）
        sitekey = attr_dict.get('data-sitekey', '')
        if sitekey and _re.match(r'^[a-f0-9-]{36,}$', sitekey, _re.IGNORECASE):
            return sitekey

        # 从 hCaptcha iframe src URL 中提取
        if node.get('nodeName', '').lower() == 'iframe':
            src = attr_dict.get('src', '')
            if 'hcaptcha' in src.lower():
                m = _re.search(r'sitekey=([a-f0-9-]+)', src, _re.IGNORECASE)
                if m:
                    return m.group(1)

    for child in node.get('children', []):
        result = _find_hcaptcha_sitekey_in_dom_tree(child)
        if result:
            return result

    for shadow in node.get('shadowRoots', []):
        for child in shadow.get('children', []):
            result = _find_hcaptcha_sitekey_in_dom_tree(child)
            if result:
                return result

    return None


def _inject_hcaptcha_token(driver, token, sitekey):
    try:
        injected = driver.execute_script("""
            var token = arguments[0];
            var success = false;

            // 1. 填充所有 hCaptcha response textarea
            var textareas = document.querySelectorAll(
                'textarea[name="h-captcha-response"], ' +
                'textarea[name="g-recaptcha-response"], ' +
                'textarea[id*="h-captcha-response"], ' +
                'textarea[id*="g-recaptcha-response"]'
            );
            for (var i = 0; i < textareas.length; i++) {
                textareas[i].innerHTML = token;
                textareas[i].value = token;
                success = true;
            }

            // 2. 通过 hcaptcha API 设置 response
            try {
                if (window.hcaptcha) {
                    // 尝试 setResponse 方法
                    var containers = document.querySelectorAll('.h-captcha, [data-hcaptcha-widget-id]');
                    containers.forEach(function(c) {
                        try {
                            var widgetId = c.getAttribute('data-hcaptcha-widget-id');
                            if (widgetId) { hcaptcha.setResponse(widgetId, token); success = true; }
                        } catch(e) {}
                    });
                    // 如果没找到 widgetId，尝试用 getWidgetID 获取
                    if (!success) {
                        try {
                            var ids = hcaptcha.getAllIds ? hcaptcha.getAllIds() : [];
                            for (var i = 0; i < ids.length; i++) {
                                hcaptcha.setResponse(ids[i], token);
                                success = true;
                            }
                        } catch(e) {}
                    }
                }
            } catch(e) {}

            // 3. 触发回调事件
            try { document.dispatchEvent(new CustomEvent('hcaptcha-solved', {detail: {token: token}})); } catch(e) {}
            try {
                // 触发 input/change 事件
                var event = new Event('input', { bubbles: true });
                var changeEvent = new Event('change', { bubbles: true });
                textareas.forEach(function(ta) {
                    ta.dispatchEvent(event);
                    ta.dispatchEvent(changeEvent);
                });
            } catch(e) {}

            // 4. 尝试触发 hCaptcha 的原生回调 (onVerify/data-callback)
            try {
                var cbContainers = document.querySelectorAll('.h-captcha[data-callback], [data-hcaptcha-widget-id][data-callback]');
                cbContainers.forEach(function(c) {
                    var cbName = c.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {
                        window[cbName](token);
                        success = true;
                    }
                });
            } catch(e) {}

            return success;
        """, token)
        return injected
    except Exception as e:
        print(f"  Inject hCaptcha token error: {e}")
        return False


def _inject_turnstile_token(driver, token):
    try:
        injected = driver.execute_script("""
            var token = arguments[0];
            var success = false;
            var inputs = document.querySelectorAll(
                'input[name="cf-turnstile-response"], ' +
                'input[name="cf_challenge_response"], ' +
                'input[name*="turnstile"], ' +
                'input[name*="challenge_response"]'
            );
            for (var i = 0; i < inputs.length; i++) { inputs[i].value = token; success = true; }
            var hiddenInputs = document.querySelectorAll('input[type="hidden"]');
            for (var i = 0; i < hiddenInputs.length; i++) {
                var name = hiddenInputs[i].name || '';
                var id = hiddenInputs[i].id || '';
                if (name.indexOf('response') >= 0 || id.indexOf('response') >= 0) {
                    if (!hiddenInputs[i].value || hiddenInputs[i].value.length < 10) {
                        hiddenInputs[i].value = token; success = true;
                    }
                }
            }
            try {
                if (window.turnstile) {
                    var containers = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
                    containers.forEach(function(c) {
                        var widgetId = c.getAttribute('data-turnstile-id');
                    });
                }
            } catch(e) {}
            try { document.dispatchEvent(new CustomEvent('turnstile-solved', {detail: {token: token}})); } catch(e) {}
            return success;
        """, token)
        return injected
    except Exception as e:
        print(f"  Inject turnstile token error: {e}")
        return False
