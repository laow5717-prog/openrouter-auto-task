"""Turnstile token 交付。

守的是一类静默失败：2Captcha 解出 token 后只设 input.value，React 的受控组件
不会感知（值追踪器认为「没变过」），提交时 payload 带空 token，CF 后端静默拒绝——
不报错、不发验证邮件，上层只看到「等待验证邮件超时」。

这些用例在真实 Chromium 里跑，因为要验的恰恰是 React 值追踪器的行为，
mock 掉 DOM 就什么也没验到。
"""

import pytest

from src.services.captcha import _inject_turnstile_token

TOKEN = 'x' * 816          # 2Captcha 实际返回的 token 长度量级


@pytest.fixture(scope='module')
def page():
    """真实浏览器页面。用 bundled chromium + headless——这里只验 DOM 行为，
    不涉及 Cloudflare 检测，不需要 create_driver 那套反检测配置。"""
    pw = pytest.importorskip('patchright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()


class _Driver:
    """_inject_turnstile_token 只用到 .page。"""
    def __init__(self, page):
        self.page = page


def _load(page, html, setup=None):
    """装载 DOM，可选地再跑一段 setup JS。

    setup 必须走 evaluate，不能写成 HTML 里的 <script>——set_content 不会执行
    脚本标签，监听器和回调根本不会被装上，测试会假绿。
    """
    page.set_content(html)
    if setup:
        page.evaluate(setup)
    return _Driver(page)


def test_fills_named_response_field(page):
    driver = _load(page, '<input type="hidden" name="cf_challenge_response" value="">')

    assert _inject_turnstile_token(driver, TOKEN) is True
    assert page.eval_on_selector('input', 'el => el.value') == TOKEN


def test_dispatches_input_and_change_events(page):
    """React 靠这两个事件感知变更。只赋值不派发 = token 永远进不了组件状态。"""
    driver = _load(
        page,
        '<input type="hidden" name="cf_challenge_response" value="">',
        """() => {
            window.seen = [];
            const el = document.querySelector('input');
            el.addEventListener('input', () => window.seen.push('input'));
            el.addEventListener('change', () => window.seen.push('change'));
        }""",
    )

    _inject_turnstile_token(driver, TOKEN)

    assert page.evaluate('() => window.seen') == ['input', 'change']


def test_uses_native_setter_so_react_tracker_sees_change(page):
    """回归核心：React 在 input 上挂 _valueTracker，直接 el.value = x 之后
    tracker 记录的旧值也被同步更新，onChange 会被判定为「值没变」而跳过。
    必须用 prototype 上的 native setter 绕开它。

    这里复刻 React 的追踪机制来断言——比断言「调了某个 API」更接近真实后果。
    """
    driver = _load(
        page,
        '<input type="hidden" name="cf_challenge_response" value="">',
        """() => {
            // 复刻 React 的 _valueTracker：在实例上拦截 value
            const el = document.querySelector('input');
            let tracked = el.value;
            window.instanceSetterUsed = false;
            Object.defineProperty(el, 'value', {
                get() { return tracked; },
                set(v) { tracked = v; window.instanceSetterUsed = true; },
                configurable: true,
            });
        }""",
    )

    _inject_turnstile_token(driver, TOKEN)

    # 走 prototype setter 才能绕开实例上的拦截；若走了实例 setter，React 收不到变更
    assert page.evaluate('() => window.instanceSetterUsed') is False, \
        '用了实例 setter，会被 React 值追踪器吞掉'


def test_invokes_data_callback(page):
    """Turnstile 没有 hCaptcha 那样的 setResponse，data-callback 是唯一公开的交付路径。"""
    driver = _load(
        page,
        '<div class="cf-turnstile" data-callback="onTurnstileDone"></div>'
        '<input type="hidden" name="cf_challenge_response" value="">',
        """() => {
            window.callbackToken = null;
            window.onTurnstileDone = (t) => { window.callbackToken = t; };
        }""",
    )

    _inject_turnstile_token(driver, TOKEN)

    assert page.evaluate('() => window.callbackToken') == TOKEN


def test_fills_hidden_response_field_by_id(page):
    """真实页面上字段形如 id="cf-chl-widget-yw345_response"，name 未必匹配。"""
    driver = _load(page, '<input type="hidden" id="cf-chl-widget-yw345_response" value="">')

    assert _inject_turnstile_token(driver, TOKEN) is True
    assert page.eval_on_selector('input', 'el => el.value') == TOKEN


def test_does_not_overwrite_already_filled_field(page):
    """已经有真 token 的字段不能被覆盖——那可能是浏览器自己通过验证拿到的。"""
    existing = 'y' * 500
    driver = _load(page, f'<input type="hidden" id="foo_response" value="{existing}">')

    _inject_turnstile_token(driver, TOKEN)

    assert page.eval_on_selector('input', 'el => el.value') == existing


def test_reports_failure_when_no_field_exists(page):
    """页面上没有任何 response 字段时必须返回 False，不能谎报成功——
    上游据此决定要不要提交表单。"""
    driver = _load(page, '<div>no turnstile here</div>')

    assert _inject_turnstile_token(driver, TOKEN) is False
