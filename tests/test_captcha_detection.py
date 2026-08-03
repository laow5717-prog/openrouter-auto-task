"""hCaptcha 挑战检测与充值登录路径的回归测试。

全部用假 frame/page 对象，不起浏览器。

背景（2026-08-03 事故）：检测器原先按「任意 hcaptcha 帧的 body 文本命中关键词」判定，
关键词里含 "i am human" —— 那正是 Stripe 结账页常驻 checkbox 帧的固定标签，于是页面上
根本没有验证码时也必然命中，每张卡白烧 3 次付费解题后返回 needs_captcha，把整个账号的
充值当场终止，真实付款结果被掩盖。
"""

import pytest

from src.browser import opencode_billing as ob
from src.browser import opencode_login as ol


class FakeFrame:
    def __init__(self, url, selectors=(), body=""):
        self.url = url
        self._selectors = set(selectors)
        self._body = body

    def query_selector(self, sel):
        return object() if sel in self._selectors else None

    def inner_text(self, _sel, timeout=None):
        return self._body


class FakePage:
    def __init__(self, frames):
        self.frames = frames


class FakeSession:
    def __init__(self, frames):
        self.page = FakePage(frames)


# Stripe 结账页永远存在的常驻 checkbox 帧：body 文本固定是 "I am human"。
CHECKBOX = FakeFrame(
    "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha-checkbox.html#frame=checkbox",
    body="I am human\nhCaptcha\nPrivacy - Terms")

# 真正弹出的图像挑战帧：题面已渲染。
CHALLENGE = FakeFrame(
    "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha-challenge.html#frame=challenge",
    selectors=[".prompt-text", ".task-grid"],
    body="Please click each image containing a bus")

# 挑战帧被提前创建但还没渲染题目（空壳）——不能算作「需要人工」。
CHALLENGE_EMPTY = FakeFrame(
    "https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha-challenge.html#frame=challenge")


def test_checkbox_frame_alone_is_not_a_challenge():
    """核心回归：只有常驻 checkbox 帧时绝不能判定为需要验证。

    这正是那次事故的形态——页面上什么都没有，检测器却一直报有挑战。
    """
    assert ob._captcha_challenge_present(FakeSession([CHECKBOX])) is None


def test_i_am_human_is_not_a_trigger_word():
    """'i am human' 必须已从关键词表移除——它是 checkbox 帧的标签，不是挑战特征。"""
    assert "i am human" not in ob._CAPTCHA_TEXT_HINTS


def test_real_challenge_is_detected():
    """真的弹出图像挑战时必须能识别到（不能矫枉过正把真挑战也漏掉）。"""
    fr = ob._captcha_challenge_present(FakeSession([CHECKBOX, CHALLENGE]))
    assert fr is CHALLENGE


def test_empty_challenge_shell_is_not_a_challenge():
    """挑战帧存在但没渲染题目时不算——只看 URL 会在题目出现前就误判。"""
    assert ob._captcha_challenge_present(FakeSession([CHECKBOX, CHALLENGE_EMPTY])) is None


def test_no_hcaptcha_frames_at_all():
    assert ob._captcha_challenge_present(FakeSession([])) is None


def test_frame_debug_lists_url_fragments():
    """诊断函数要能列出帧片段——判据收紧后，真挑战若失配是「静默不解题」，靠它留痕。"""
    marks = ob._captcha_frames_debug(FakeSession([CHECKBOX, CHALLENGE]))
    assert any("frame=checkbox" in m for m in marks)
    assert any("frame=challenge" in m for m in marks)


def test_login_helper_supports_skipping_go_page():
    """充值走 zen 的 billing 页，不需要 /go；该开关必须存在且默认保持订阅流程的行为。"""
    import inspect
    sig = inspect.signature(ol.login_and_open_own_go)
    assert "open_go" in sig.parameters
    assert sig.parameters["open_go"].default is True


def test_ensure_session_accepts_verify_link():
    """新设备邮箱验证要自动收码，收信链接必须能传进来。"""
    import inspect
    sig = inspect.signature(ob.ensure_opencode_session)
    assert "verify_link" in sig.parameters


def test_auto_verify_device_without_link_returns_false():
    """没有收信链接时不能假装成功——调用方要据此回退到等人工。"""
    assert ob._auto_verify_device(FakeSession([]), None, None) is False
    assert ob._auto_verify_device(FakeSession([]), None, "") is False
