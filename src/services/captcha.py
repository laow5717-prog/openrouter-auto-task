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

    page_url = driver.current_url
    print(f"  hCaptcha sitekey: {sitekey[:20]}...")
    print(f"  Page URL: {page_url}")

    for attempt in range(max_retries):
        try:
            print(f"  Calling 2Captcha for hCaptcha... (attempt {attempt + 1}/{max_retries})")
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
    try:
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

        sitekey = driver.execute_script("""
            var html = document.documentElement.outerHTML;
            var patterns = [
                /data-sitekey="(0x[A-Za-z0-9_-]+)"/,
                /sitekey['":\\\\s]+'?(0x[A-Za-z0-9_-]+)/,
                /turnstile[^}]*sitekey['":\\\\s]+'?(0x[A-Za-z0-9_-]+)/,
            ];
            for (var i = 0; i < patterns.length; i++) {
                var m = html.match(patterns[i]);
                if (m) return m[1];
            }
            return null;
        """)
        if sitekey:
            return sitekey

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
    attrs = node.get('attributes', [])
    if attrs:
        attr_dict = dict(zip(attrs[::2], attrs[1::2]))
        sitekey = attr_dict.get('data-sitekey', '')
        if sitekey and sitekey.startswith('0x'):
            return sitekey

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


def _extract_hcaptcha_sitekey(driver):
    try:
        sitekey = driver.execute_script("""
            var iframes = document.querySelectorAll('iframe[src*="hcaptcha.com"]');
            for (var i = 0; i < iframes.length; i++) {
                var match = iframes[i].src.match(/sitekey=([a-f0-9-]+)/);
                if (match) return match[1];
            }
            var el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            var hc = document.querySelector('.h-captcha');
            if (hc && hc.getAttribute('data-sitekey')) return hc.getAttribute('data-sitekey');
            return null;
        """)
        return sitekey
    except Exception as e:
        print(f"  Extract hCaptcha sitekey error: {e}")
        return None


def _inject_hcaptcha_token(driver, token, sitekey):
    try:
        injected = driver.execute_script("""
            var token = arguments[0];
            var success = false;
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
            try {
                if (window.hcaptcha) {
                    var containers = document.querySelectorAll('.h-captcha, [data-hcaptcha-widget-id]');
                    containers.forEach(function(c) {
                        try {
                            var widgetId = c.getAttribute('data-hcaptcha-widget-id');
                            if (widgetId) { hcaptcha.setResponse(widgetId, token); success = true; }
                        } catch(e) {}
                    });
                }
            } catch(e) {}
            try { document.dispatchEvent(new CustomEvent('hcaptcha-solved', {detail: {token: token}})); } catch(e) {}
            try {
                var event = new Event('input', { bubbles: true });
                textareas.forEach(function(ta) { ta.dispatchEvent(event); });
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
