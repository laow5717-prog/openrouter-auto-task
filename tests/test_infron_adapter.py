"""infron 适配器的单元测试。

重点在两个纯函数：magic link 的时间闸门、余额解析。它们不需要浏览器，却正是最容易
出错的地方——闸门写错会静默用上旧链接，余额解析把 0 当成读不到会让归档判断走错。
"""

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
