"""opencode OAuth「unknown state」错误页的识别与恢复回归测试。

全部用假 session/page 对象 + 假时钟，不起浏览器。

背景（2026-08-08 事故）：`auth.opencode.ai` 是 SST OpenAuth，它的 state 存在 cookie 里且
是一次性的；回调时对不上就甩出

    The browser was in an unknown state. This could be because certain cookies expired
    or the browser was switched in the middle of an authentication flow.

原先代码识别不了这一页：`_wait_until` 只读 URL 不读正文，浏览器停在错误页时会一路空转到
「未能取到 workspace id」，随后 15 轮 provision 重试全在同一个坏状态里打转。现场表现是
充值流水线里的浏览器长时间挂在这一页，日志里只有一行「未登录，点 Continue with GitHub」
之后再无输出。

两个成因都在本文件设防：
  C1 `_click_continue_github` 的裸 goto 兜底直接打 auth.opencode.ai/github/authorize，
     绕过 opencode 侧的 /authorize，OpenAuth 没机会种 state → 回调必炸（确定性错误）。
  C2 GitHub 登录 + 新设备邮箱验证要跑好几分钟，期间最初那张 authorize 页的 state 放凉了。
"""

import inspect

import pytest

from src.platforms.opencode import login as ol


# 错误页原文（大小写照抄现场，检测必须大小写不敏感）
ERROR_BODY = (
    "The browser was in an unknown state. This could be because certain cookies "
    "expired or the browser was switched in the middle of an authentication flow."
)


class FakeClock:
    """虚拟时钟：sleep 只推进时间不真的等。

    没有它，一条主流程用例要真实耗掉 20 秒以上——`_wait_until` 的超时判断读 time.time()，
    而流程里散布着 sleep(2)/sleep(3)。
    """

    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeContext:
    def __init__(self):
        self.cleared = []          # 每次 clear_cookies 的 kwargs

    def clear_cookies(self, **kwargs):
        self.cleared.append(kwargs)


class FakeLocator:
    def __init__(self, count, on_click=None):
        self._count = count
        self._on_click = on_click

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self._on_click:
            self._on_click()

    def is_enabled(self):
        return True

    def is_visible(self):
        return True


class FakePage:
    def __init__(self, session):
        self._s = session
        self.context = FakeContext()

    def inner_text(self, _sel, timeout=None):
        if self._s.body_raises:
            raise RuntimeError("Execution context was destroyed (navigating)")
        return self._s.body

    def get_by_role(self, role, name=None):
        if role == "link" and name == "Continue with GitHub":
            return FakeLocator(1 if self._s.has_continue_link else 0,
                               on_click=self._s.click_continue)
        return FakeLocator(0)

    def locator(self, _sel):
        return FakeLocator(0)

    def evaluate(self, _script):
        return False


class FakeSession:
    """按脚本改变 url/body 的假会话。

    on_get(session, url) 在每次导航后调用，用来模拟「第 N 次访问 /auth 落到哪」；
    on_continue(session) 在点「Continue with GitHub」后调用。
    """

    def __init__(self, url="", body="", on_get=None, on_continue=None,
                 has_continue_link=True):
        self.url = url
        self.body = body
        self.body_raises = False
        self.has_continue_link = has_continue_link
        self._on_get = on_get
        self._on_continue = on_continue
        self.navigations = []
        self.page = FakePage(self)

    @property
    def current_url(self):
        return self.url

    def get(self, url):
        self.navigations.append(url)
        if self._on_get:
            self._on_get(self, url)

    def capture_frame(self):
        pass

    def click_continue(self):
        if self._on_continue:
            self._on_continue(self)


# --- 错误页识别 -------------------------------------------------------------

def test_auth_broken_detects_unknown_state():
    """现场原文必须命中——这是整个修复的入口条件。"""
    assert ol._auth_broken(FakeSession(body=ERROR_BODY)) is True


def test_auth_broken_detects_alternate_wording():
    """文案微调（去掉 "unknown state" 措辞）时，冗余判据仍要兜住。"""
    body = "Your cookies expired during the authentication flow. Please try again."
    assert ol._auth_broken(FakeSession(body=body)) is True


@pytest.mark.parametrize("body", [
    "Dashboard\nYour workspace\nBilling",
    "Authorize opencode\nThis application will be able to read your profile",
    "This account is flagged and cannot authorize a third party application",
    "",
])
def test_auth_broken_false_on_normal_pages(body):
    """正常页面绝不能命中——误报会把好流程推进恢复重试，白烧时间。

    flagged 页也在列：它是另一条已有的终态分支，不能被这里抢走。
    """
    assert ol._auth_broken(FakeSession(body=body)) is False


def test_auth_broken_false_when_read_raises():
    """导航中读正文会抛异常，此时一律判 False，不能把正常跳转误判成坏页。"""
    s = FakeSession(body=ERROR_BODY)
    s.body_raises = True
    assert ol._auth_broken(s) is False


# --- 清 cookie 的作用域 -----------------------------------------------------

def test_clear_cookies_scoped_to_opencode_domains():
    """只按域清 opencode，绝不无参全清。

    全清会连 github.com 的登录 cookie 一起抹掉，逼出一次完整重登 + 一次新设备邮箱验证
    （实测数分钟 + 一封验证码）。同见 spec/backend/browser-profile-guidelines.md
    「Cookies 不可删」。
    """
    s = FakeSession()
    assert ol._clear_opencode_cookies(s) is True
    calls = s.page.context.cleared
    assert calls, "应当调用过 clear_cookies"
    assert all(call.get("domain") for call in calls), "每次调用都必须带 domain"
    domains = {call["domain"] for call in calls}
    assert domains == set(ol._OPENCODE_COOKIE_DOMAINS)
    assert "github.com" not in domains


def test_clear_cookies_survives_missing_context():
    """拿不到 context 时返回 False 而不是炸——恢复流程照走，只是少一层加强。"""
    s = FakeSession()
    del s.page.context
    assert ol._clear_opencode_cookies(s) is False


# --- C1：裸 goto 兜底必须消失 -----------------------------------------------

def test_no_bare_goto_in_click_continue_github():
    """`_click_continue_github` 不许再直接 goto——那是 C1，回调必然撞 unknown state。

    判据看函数体里有没有 goto 调用，而不是全模块搜 URL 字面量：docstring 里要留着这个
    URL 解释「为什么不能这么做」。
    """
    src = inspect.getsource(ol._click_continue_github)
    body = src.split('"""')[-1]          # 去掉 docstring，只看代码
    assert ".goto(" not in body
    assert "github/authorize" not in body


def test_click_continue_reloads_auth_entry_when_link_missing():
    """找不到链接时改为重载 /auth 再找一次（顺带刷新 state），而不是硬打 authorize。"""
    s = FakeSession(has_continue_link=False)
    assert ol._click_continue_github(s) is False
    assert s.navigations == [ol._AUTH_ENTRY]


# --- 主流程：恢复 -----------------------------------------------------------

def test_recovers_from_error_page_then_gets_wid(monkeypatch):
    """核心回归：OAuth 回调落在错误页时，重开 authorize 并最终取到 wid。

    原先这里会空转到超时后报「未能取到 workspace id」。
    """
    monkeypatch.setattr(ol, "time", FakeClock())
    state = {"gets": 0}

    def on_get(s, _url):
        state["gets"] += 1
        if state["gets"] == 1:
            s.url = "https://auth.opencode.ai/authorize?client_id=opencode"
            s.body = "Continue with GitHub"
        else:
            # 恢复：新 authorize 拿到新鲜 state，会话建立，直接回落自己的 workspace
            s.url = "https://opencode.ai/workspace/wrk_ABC123"
            s.body = "Dashboard"

    def on_continue(s):
        # 回调落在 OpenAuth 错误页（state 对不上）
        s.url = "https://auth.opencode.ai/callback?code=xyz"
        s.body = ERROR_BODY

    s = FakeSession(on_get=on_get, on_continue=on_continue)
    res = ol.login_and_open_own_go(s, open_go=False)

    assert res["wid"] == "wrk_ABC123"
    assert res["ok"] is True
    assert res["flagged"] is False
    assert s.navigations.count(ol._AUTH_ENTRY) >= 2, "恢复必须重新走一趟 /auth"


def test_detail_mentions_state_when_recovery_exhausted(monkeypatch):
    """恢复配额用尽仍停在错误页时，detail 要如实说是认证 state 问题。

    不能混报成「未能取到 workspace id」——那是 provision 慢的说法，两种故障的排查路径
    完全不同，混报会让下一次从头查起。
    """
    monkeypatch.setattr(ol, "time", FakeClock())

    def stuck(s, _url=None):
        s.url = "https://auth.opencode.ai/error"
        s.body = ERROR_BODY

    s = FakeSession(on_get=stuck, on_continue=stuck)
    res = ol.login_and_open_own_go(s, open_go=False)

    assert res["ok"] is False
    assert res["wid"] is None
    assert res["flagged"] is False
    assert "unknown state" in res["detail"]
    assert f"已恢复 {ol._MAX_AUTH_RECOVER} 次" in res["detail"]
    assert "workspace id" not in res["detail"]


def test_recovery_is_capped(monkeypatch):
    """恢复次数有上限——坏状态里无限重来只会把单账号耗时线性放大。"""
    monkeypatch.setattr(ol, "time", FakeClock())

    def stuck(s, _url=None):
        s.url = "https://auth.opencode.ai/error"
        s.body = ERROR_BODY

    s = FakeSession(on_get=stuck, on_continue=stuck)
    ol.login_and_open_own_go(s, open_go=False)

    # 清 cookie 只发生在第 2 次恢复，且总共只有那一次（每次清两个域）
    assert len(s.page.context.cleared) == len(ol._OPENCODE_COOKIE_DOMAINS)


def test_flagged_still_wins_over_recovery(monkeypatch):
    """被 flag 的账号要立刻返回 flagged，不能被恢复重试拖住。

    flagged 是确定性终态（新号常见），空转恢复对它毫无意义。
    """
    monkeypatch.setattr(ol, "time", FakeClock())

    def on_get(s, _url):
        s.url = "https://auth.opencode.ai/authorize?client_id=opencode"
        s.body = "Continue with GitHub"

    def on_continue(s):
        s.url = "https://github.com/login/oauth/authorize?client_id=x"
        s.body = "This account is flagged and cannot authorize a third party application"

    s = FakeSession(on_get=on_get, on_continue=on_continue)
    res = ol.login_and_open_own_go(s, open_go=False)

    assert res["flagged"] is True
    assert res["ok"] is False
    assert "flagged" in res["detail"]
