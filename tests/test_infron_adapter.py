"""infron 适配器的单元测试。

重点在两个纯函数：magic link 的时间闸门、余额解析。它们不需要浏览器，却正是最容易
出错的地方——闸门写错会静默用上旧链接，余额解析把 0 当成读不到会让归档判断走错。
"""

import inspect
import re
from datetime import datetime, timedelta

import pytest

import src.platforms as platforms
from src.platforms.base import CAP_SUBSCRIBE, CAP_TOPUP, Credentials, PlatformAdapter
from src.platforms.infron import credits as ic
from src.platforms.infron import login as il


# ---------- 适配器契约 ----------

def test_infron_satisfies_protocol_and_is_topup_only():
    a = platforms.get('infron')
    assert isinstance(a, PlatformAdapter)
    assert CAP_TOPUP in a.capabilities
    assert CAP_SUBSCRIBE not in a.capabilities, 'infron 是纯充值制，没有订阅'


def test_infron_has_no_tenant_id():
    """infron 控制台就是 /dashboard，URL 里没有租户段。"""
    a = platforms.get('infron')
    for url in ('https://infron.ai/dashboard',
                'https://infron.ai/dashboard/credits',
                'https://infron.ai/login'):
        assert a.extract_tenant_id(url) is None


def test_infron_is_more_conservative_than_opencode():
    """新平台风控未知，单次试卡上限应比 opencode 保守。"""
    assert platforms.get('infron').max_card_attempts < platforms.get('opencode').max_card_attempts


class _DeadSession:
    """所有页面操作都失败的会话——模拟页面/基础设施故障。"""

    class _Page:
        url = 'about:blank'
        frames = []

        def evaluate(self, _js):
            raise RuntimeError('页面挂了')

        def __getattr__(self, _name):
            raise RuntimeError('页面挂了')

    page = _Page()

    def get(self, _url):
        raise RuntimeError('导航失败')

    def capture_frame(self):
        pass


def test_page_failure_yields_error_not_failed():
    """走不到付款的故障必须归 error —— 那是「不消耗卡」的 outcome。

    若归成 failed，一次页面抽风就会把好卡判废，而判废不可逆。这是整条充值链路上
    最容易写错、后果又最严重的一处。
    """
    a = platforms.get('infron')
    r = a.top_up(_DeadSession(), None, {'number': '4111111111111111'}, amount=50)
    assert r.outcome == 'error', f'页面故障应归 error，实际 {r.outcome}'
    assert r.keeps_card is True
    assert r.ok is False
    assert r.last4 == '1111'


def test_topup_uses_adapter_default_amount():
    """不传金额时用适配器自己的默认档位，不是写死的值。"""
    a = platforms.get('infron')
    assert a.default_topup_amount == 50, 'infron 最低档位是 $50'


# ---------- magic link 的时间闸门 ----------

LINK_A = 'https://infron.ai/api/user/magic-link/verify?token=1d20cf07-3267-4ce7-8979-4d54de972b73'
LINK_B = 'https://infron.ai/api/user/magic-link/verify?token=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def _mail(when, link, subject='Infron - Sign In Link-Infron'):
    return {'subject': subject,
            'time': when.strftime('%Y-%m-%d %H:%M:%S'),
            'body': f'Hey there! Click to sign in. {link} This link will expire in 30 minutes.'}


def test_picks_link_that_arrived_after_request():
    now = datetime.now()
    mails = [_mail(now, LINK_B), _mail(now - timedelta(hours=2), LINK_A)]
    link, _ = il._find_magic_link(mails, since=now - timedelta(seconds=5))
    assert link == LINK_B


def test_ignores_stale_link_from_a_previous_round():
    """收件箱里的旧链接必须被跳过——它一次性且 30 分钟过期，用了必然失败。

    这个失败看起来像「站点抽风」，极难往「用错了链接」上想，所以闸门不能省。
    """
    now = datetime.now()
    mails = [_mail(now - timedelta(hours=2), LINK_A)]
    link, _ = il._find_magic_link(mails, since=now)
    assert link is None


def test_tolerates_clock_skew():
    """收信服务与本机时钟未必严丝合缝，卡太死会把刚到的新邮件判成旧邮件。"""
    now = datetime.now()
    mails = [_mail(now - timedelta(seconds=30), LINK_A)]     # 比 since 早半分钟
    link, _ = il._find_magic_link(mails, since=now)
    assert link == LINK_A, '90 秒容差内的邮件应被接受'


def test_skew_tolerance_has_a_limit():
    now = datetime.now()
    mails = [_mail(now - timedelta(seconds=600), LINK_A)]
    link, _ = il._find_magic_link(mails, since=now)
    assert link is None, '远超容差的旧邮件不该被接受'


def test_ignores_unrelated_mail():
    now = datetime.now()
    mails = [{'subject': 'Your GitHub launch code',
              'time': now.strftime('%Y-%m-%d %H:%M:%S'),
              'body': 'code 12345678'}]
    link, _ = il._find_magic_link(mails, since=now - timedelta(seconds=5))
    assert link is None


def test_no_since_accepts_any_matching_mail():
    """不传 since 时不做时间过滤（供调试用）。"""
    mails = [_mail(datetime.now() - timedelta(days=3), LINK_A)]
    link, _ = il._find_magic_link(mails, since=None)
    assert link == LINK_A


# ---------- 余额解析 ----------

class _FakeSession:
    def __init__(self, text):
        self._text = text

    class _Page:
        def __init__(self, text):
            self._text = text

        def evaluate(self, _js):
            return self._text

    @property
    def page(self):
        return self._Page(self._text)


def _bal(text):
    return ic.read_balance_from_current_page(_FakeSession(text))


def test_zero_balance_is_zero_not_none():
    """余额为 0 必须返回 0.0。

    0.0 是「读到了，账上没钱」，None 是「没读到」。编排层的归档预检拿 None 会跳过
    判断继续充值，拿 0.0 才会正确判定「未达阈值，该充」。混为一谈会让余额恰好为零
    时逻辑微妙地走错。
    """
    got = _bal('Available Balance\n$ 0.00000000\nTop Up')
    assert got == 0.0
    assert got is not None


def test_reads_nonzero_balance():
    assert _bal('Available Balance\n$ 20.50000000\nTop Up') == 20.5


def test_reads_balance_with_thousands_separator():
    assert _bal('Available Balance\n$ 1,234.56\nTop Up') == 1234.56


def test_returns_none_when_balance_block_absent():
    assert _bal('Dashboard\nWelcome back\nNo billing info here') is None


@pytest.mark.parametrize('text', ['', 'Available Balance', 'Balance $ 5.00'])
def test_returns_none_on_unparsable_text(text):
    assert _bal(text) is None


# ---------- Top Up 弹窗的步骤判定 ----------

_STEP1 = ('Top Up Credits\nAdd credits to your account. '
          'Confirm details on the next step.\n$50 $100 $300\nPay $52.85\nClose')
_STEP2 = ('Top Up Credits\nEnter your card or another Stripe payment method '
          'to complete the top-up.\nBack Pay $52.85 Close')


class _ModalSession:
    """只实现 current_step 需要的那点接口。"""

    def __init__(self, modal_text):
        self._t = modal_text

    class _Page:
        def __init__(self, t):
            self._t = t

        def evaluate(self, _js):
            return {'text': self._t} if self._t else None

    @property
    def page(self):
        return self._Page(self._t)


def test_step_detection_distinguishes_the_two_pay_buttons():
    """两步弹窗的 Pay 按钮同名，只能靠副标题区分——认错步骤会在第一步就去找卡号框。"""
    assert ic.current_step(_ModalSession(_STEP1)) == 1
    assert ic.current_step(_ModalSession(_STEP2)) == 2


def test_step_is_none_when_modal_absent():
    assert ic.current_step(_ModalSession(None)) is None


def test_unknown_modal_text_falls_back_to_step_one():
    """站点改文案时保守按第一步处理，而不是崩掉。"""
    assert ic.current_step(_ModalSession('Top Up Credits\n某种新文案')) == 1


def test_preset_amounts_match_the_site():
    assert ic._PRESET_AMOUNTS == (50, 100, 300)


def test_user_stop_propagates_through_the_catch_all():
    """用户主动停止必须能穿透 top_up 的兜底 except，不能被收敛成 error。

    吞掉它的后果是「点了停止但任务继续跑下一张卡」——用户以为停了，钱还在扣。
    """
    from src.platforms.infron import credits as c

    class _Sess:
        def get(self, _url):
            pass

        def capture_frame(self):
            pass

        class _Page:
            def evaluate(self, _js):
                return 'Available Balance $ 0.00000000'

        page = _Page()

    with pytest.raises(InterruptedError):
        c.top_up(_Sess(), {'number': '4111111111111111'}, 50,
                 should_stop=lambda: True)


# ---------- 输入截断必须被发现 ----------

class _FlakyInput:
    """模拟 Stripe 的受控输入：第一次只吃进前几个字符（DOM 重排丢字符），第二次正常。

    这是实测撞到的真实故障——卡号没填完就去填下一个字段，没有异常、没有报错，
    只是卡少了几位，表现成「付款莫名失败」。
    """

    def __init__(self, drop_first=True, formats=False):
        self.value = ''
        self._drop_first = drop_first
        self._round = 0
        self._formats = formats

    def click(self, timeout=None):
        pass

    def fill(self, v, timeout=None):
        self.value = v

    def press_sequentially(self, text, delay=None):
        self._round += 1
        if self._drop_first and self._round == 1:
            self.value = text[:6]            # 只吃进一部分
        else:
            self.value = (' '.join(text[i:i + 4] for i in range(0, len(text), 4))
                          if self._formats else text)

    def input_value(self, timeout=None):
        return self.value


def test_truncated_input_is_retried_until_complete():
    """字符被吞掉时必须回读发现并重来，不能当成功。"""
    from src.payments.stripe_checkout import _type_and_verify
    box = _FlakyInput(drop_first=True)
    assert _type_and_verify(box, '4111111111111111', delay=0) is True
    assert box.value == '4111111111111111'


def test_gives_up_after_repeated_truncation():
    """一直填不完整就如实返回 False，让上层归到 error（不消耗卡）。"""
    from src.payments.stripe_checkout import _type_and_verify

    class _AlwaysShort(_FlakyInput):
        def press_sequentially(self, text, delay=None):
            self.value = text[:4]

    assert _type_and_verify(_AlwaysShort(), '4111111111111111', delay=0, attempts=2) is False


def test_accepts_stripe_formatted_card_number():
    """Stripe 会把卡号格式化成 '4111 1111 1111 1111'，校验只比数字。"""
    from src.payments.stripe_checkout import _type_and_verify
    box = _FlakyInput(drop_first=False, formats=True)
    assert _type_and_verify(box, '4111111111111111', delay=0) is True
    assert ' ' in box.value, '这个替身确实做了格式化'


def test_address_candidates_include_bare_names():
    """地址字段也要有裸 name 候选——只给 Checkout 的 ID 会让邮编填不进去。"""
    from src.payments.stripe_checkout import _ADDR_FIELD_CANDIDATES
    assert _ADDR_FIELD_CANDIDATES['zip'][0] == "input[name='postalCode']"
    assert _ADDR_FIELD_CANDIDATES['city'][0] == "input[name='locality']"


# ---------- 3DS 挑战：先等宽限，别一见就关 ----------

def test_threeds_challenge_gets_a_grace_period_before_cancel():
    """3DS 挑战不能一出现就 Cancel —— 很多挑战几十秒内会自动放行。

    实跑事故：立刻 Cancel 等于亲手作废一笔本可成功的付款，而且关掉后还不返回，
    白等满整个 timeout 才得出 unknown。5 张卡有 3 张栽在这里。
    语义与 opencode 对齐：宽限期内等它自己过，仍在才关并判 failed。
    """
    src = inspect.getsource(ic.detect_payment_result)
    assert '_THREEDS_CHALLENGE_GRACE_SEC' in src, '缺少宽限期，挑战会被立刻掐掉'
    # 关闭动作必须在宽限判断之后，且关闭后要有明确结论（return failed）
    i_grace = src.index('_THREEDS_CHALLENGE_GRACE_SEC')
    i_close = src.index('_close_challenge_lightbox')
    assert i_grace < i_close, '必须先判宽限期再关挑战'
    tail = src[i_close:i_close + 400]
    assert 'return' in tail and 'failed' in tail, '关掉挑战后必须立即判 failed，不能继续空等'


def test_page_is_not_reloaded_while_a_challenge_is_open():
    """挑战开着时绝不能重载页面 —— 重载会把挑战连同待授权的付款一起冲掉。

    轮询里每 5 轮刷一次余额页是必要的（弹窗背后的余额不会自更新），但漏掉
    「挑战期间不刷」这个条件，等于每 30 秒亲手掐死一次正在进行的 3DS。
    """
    src = inspect.getsource(ic.detect_payment_result)
    m = re.search(r'if rounds % 5 == 0([^:]*):', src)
    assert m, '找不到周期性重载的条件'
    assert 'challenge_since is None' in m.group(1), \
        '周期性重载没有排除「3DS 挑战正开着」的情况'


# ---------- 不支持的卡种直接短路 ----------

def test_unsupported_brand_short_circuits_without_touching_the_browser():
    """14 位 Diners 在 infron 必然走不通，直接短路，不必花 40 秒走完流程。

    关键是归 error（不消耗卡）—— 卡本身没问题，只是这个平台没启用该卡种。
    用 _DeadSession 保证真的没碰浏览器：碰了就会抛异常。
    """
    a = platforms.get('infron')
    r = a.top_up(_DeadSession(), None, {'number': '30569309025904'}, amount=50)
    assert r.outcome == 'error'
    assert r.keeps_card is True
    assert 'skipped_unsupported_brand' in (r.steps or []), '应在碰浏览器之前就短路'


@pytest.mark.parametrize('number,digits', [
    ('4111111111111111', 16),        # Visa —— 实测能拿到真实拒付
    ('341154807281004', 15),         # Amex —— 实测能拿到真实拒付
])
def test_supported_brands_are_not_short_circuited(number, digits):
    """15/16 位实测都能走到银行，绝不能被短路挡掉——那会让整个卡池作废。"""
    assert len(number) == digits
    a = platforms.get('infron')
    r = a.top_up(_DeadSession(), None, {'number': number}, amount=50)
    # 走进了真实流程（因而被 _DeadSession 打挂），而不是被短路
    assert 'skipped_unsupported_brand' not in (r.steps or [])


# ---------- 表单校验 ≠ 拒付 ----------

def test_input_validation_is_checked_before_decline():
    """"card number is incomplete" 必须归 error（不消耗卡），不能归拒付。

    实跑事故：Payment Element 报 "Your card number is incomplete."（客户端校验，
    卡根本没提交给银行），却被 _DECLINE_HINTS 里的裸词 "card number is" 吞掉判成
    拒付 —— 两张好 Diners 被判废，而判废不可逆。

    这条同时钉住**判定顺序**：input-invalid 必须先于 decline，否则裸词又会抢先命中。
    """
    from src.payments.stripe_checkout import _DECLINE_HINTS, _INPUT_INVALID_HINTS

    text = 'your card number is incomplete.'
    assert any(h in text for h in _INPUT_INVALID_HINTS), '表单校验表应能命中'
    assert any(h in text for h in _DECLINE_HINTS), \
        '拒付表也会命中同一句 —— 正因如此，顺序不能颠倒'

    # 只比对真正的判定语句，不要撞上注释里提到的同名标识符
    src = inspect.getsource(ic.detect_payment_result)
    order = re.findall(r'next\(\(h for h in (_\w+_HINTS)', src)
    assert order[:2] == ['_INPUT_INVALID_HINTS', '_DECLINE_HINTS'], \
        f'判定顺序错了：{order}。表单校验必须先判，否则拒付表的裸词会抢先命中'


def test_real_decline_is_still_failed():
    """真实银行拒付仍归 failed —— 上面的修复不能把拒付也放过。"""
    from src.payments.stripe_checkout import _DECLINE_HINTS, _INPUT_INVALID_HINTS

    text = 'your card was declined.'
    assert not any(h in text for h in _INPUT_INVALID_HINTS)
    assert any(h in text for h in _DECLINE_HINTS)


def test_expired_card_is_a_decline_not_an_input_error():
    """「卡已过期」是卡的属性，仍归拒付；只有 "expiration date is incomplete" 才是填写问题。"""
    from src.payments.stripe_checkout import _DECLINE_HINTS, _INPUT_INVALID_HINTS

    assert not any(h in 'your card has expired.' for h in _INPUT_INVALID_HINTS)
    assert any(h in 'your card has expired.' for h in _DECLINE_HINTS)
    assert any(h in "your card's expiration date is incomplete."
               for h in _INPUT_INVALID_HINTS)


def test_threeds_failure_modal_returns_a_tuple_not_a_frame():
    """钉住 _threeds_failure_modal 的返回形状。

    它返回 (frame, message)，与同模块其余判定函数（返回 frame 或 None）不一样。
    曾经把它当单值用：元组 (None, '') 永远不是 None，于是**每一笔付款都被误判成
    3DS 失败并判废**——好卡被白白废掉，而日志看起来完全正常。
    这条测试是为了让下次改动时这个差异是显式的。
    """
    from src.payments.stripe_checkout import _threeds_failure_modal

    class _NoFrames:
        class _Page:
            frames = []
        page = _Page()

    got = _threeds_failure_modal(_NoFrames())
    assert isinstance(got, tuple) and len(got) == 2, '返回形状变了，调用方要跟着改'
    assert got[0] is None
    # 这才是正确的判空方式
    fr, _msg = got
    assert fr is None


# ---------- Turnstile 交互式挑战 ----------

class _Frame:
    def __init__(self, url, text='', boxes=None):
        self.url = url
        self._text = text
        self._boxes = boxes or {}
        self.clicked = []

    def inner_text(self, _sel, timeout=None):
        return self._text

    def locator(self, sel):
        frame = self

        class _Loc:
            @property
            def first(self):
                return self

            def count(self):
                return frame._boxes.get(sel, 0)

            def click(self, timeout=None):
                if not frame._boxes.get(sel):
                    raise RuntimeError('元素不存在')
                frame.clicked.append(sel)

        return _Loc()


class _TSSession:
    """带 Turnstile frame 的假会话。"""

    def __init__(self, frames, box=None):
        self._frames = frames
        self._box = box
        self.mouse_clicks = []
        outer = self

        class _Mouse:
            def click(self, x, y):
                outer.mouse_clicks.append((x, y))

        class _Page:
            frames = self._frames
            mouse = _Mouse()

            def locator(self, _sel):
                class _L:
                    @property
                    def first(self_inner):
                        return self_inner

                    def bounding_box(self_inner, timeout=None):
                        return outer._box
                return _L()

        self.page = _Page()

    def capture_frame(self):
        pass


TS_URL = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2'


def test_interactive_turnstile_gets_clicked():
    """「Verify you are human」是交互式挑战，不点就永远不会过。"""
    fr = _Frame(TS_URL, 'Verify you are human',
                boxes={"input[type='checkbox']": 1})
    assert il.click_turnstile_checkbox(_TSSession([fr])) is True
    assert fr.clicked == ["input[type='checkbox']"]


def test_passive_turnstile_is_left_alone():
    """被动形态自己转几秒就过，**不该去点**——多点一下可能反而重置挑战。

    这是本组测试里最重要的一条：加了自动点击之后，最容易犯的错就是
    见到 Turnstile frame 就点。
    """
    fr = _Frame(TS_URL, 'Verifying...', boxes={"input[type='checkbox']": 1})
    assert il.click_turnstile_checkbox(_TSSession([fr])) is False
    assert fr.clicked == [], '被动挑战被误点了'


def test_no_turnstile_frame_is_a_noop():
    fr = _Frame('https://infron.ai/login', 'Verify you are human')
    assert il.click_turnstile_checkbox(_TSSession([fr])) is False


def test_falls_back_to_clicking_by_coordinates():
    """Turnstile 有时把复选框放进 **closed** shadow DOM，选择器穿不透
    （Playwright 只能穿 open shadow DOM）。这时只能按 iframe 包围盒的坐标点。

    坐标兜底看着糙，但没有它交互式挑战就完全无解。
    """
    fr = _Frame(TS_URL, 'Verify you are human', boxes={})   # 所有选择器都找不到
    sess = _TSSession([fr], box={'x': 100, 'y': 200, 'width': 300, 'height': 65})

    assert il.click_turnstile_checkbox(sess) is True
    assert len(sess.mouse_clicks) == 1
    x, y = sess.mouse_clicks[0]
    assert x == 130, '复选框固定在挂件左端约 30px 处'
    assert y == 232.5, '应点在垂直中线上'


def test_wait_loop_throttles_clicking():
    """点击必须限流：Turnstile 点完要几秒才出结果，连点既没用又像机器人。"""
    import inspect
    src = inspect.getsource(il.wait_past_turnstile)
    assert '_TURNSTILE_CLICK_GAP_SEC' in src, '等待循环里没有点击间隔限制'
    assert 'click_turnstile_checkbox' in src, '等待循环没有接入自动勾选'


def test_click_failure_does_not_break_the_wait():
    """点不到就继续等被动放行，不能让它把整个登录搞挂。"""
    import inspect
    src = inspect.getsource(il.wait_past_turnstile)
    i = src.index('click_turnstile_checkbox')
    assert 'except Exception' in src[i:i + 300], '点击异常没有被兜住'
