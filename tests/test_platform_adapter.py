"""平台适配器抽象的契约测试。

核心命题（AC12）：新增一个平台只需实现 PlatformAdapter 并注册，**编排层一行都不用改**。
这里用一个完全虚构的 StubAdapter 跑通 registration.recharge_account 的整条充值编排，
如果哪天有人在编排层写死了 opencode 的东西，这个文件会立刻红。

另一半是 PaymentResult 的 outcome 语义（AC13）：needs_captcha / error / unknown 三者
**不消耗卡**是硬约束，每一条都是线上事故换来的。
"""

import pytest

import src.platforms as platforms
from src.platforms.base import (
    CAP_TOPUP,
    OUTCOMES_KEEPING_CARD,
    Credentials,
    PaymentResult,
    PlatformAdapter,
    SessionResult,
)
from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.models.valid_card import ValidCardModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.recharge_log import RechargeLogModel
from src.models.platform_account import PlatformAccountModel
from src.models.account import AccountModel
from src.services import registration

STUB = 'stubplatform'


class StubAdapter:
    """一个完全虚构的平台。除了 slug 与返回值，什么真实站点知识都没有。"""

    slug = STUB
    display_name = 'Stub 平台'
    capabilities = frozenset({CAP_TOPUP})
    max_card_attempts = 3
    recharge_skip_balance = 50.0
    default_topup_amount = 7.0

    def __init__(self, outcomes=None, balance=None):
        # 按调用序返回的 outcome 列表；用完后一律 'failed'
        self._outcomes = list(outcomes or ['success'])
        self._balance = balance
        self.calls = []

    def module_names(self):
        return []

    def extract_tenant_id(self, url):
        return 'tnt_1' if 'stub' in (url or '') else None

    def ensure_session(self, session, creds, monitor=None, timeout=240):
        self.calls.append(('ensure_session', creds.email))
        return SessionResult(ok=True, tenant_id='tnt_1', detail='stub 已登录')

    def read_balance(self, session, tenant_id, monitor=None):
        return self._balance

    def read_balance_from_current_page(self, session):
        return self._balance

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        oc = self._outcomes.pop(0) if self._outcomes else 'failed'
        self.calls.append(('top_up', card.get('number'), oc))
        return PaymentResult(ok=(oc == 'success'), outcome=oc,
                             err='' if oc == 'success' else f'stub {oc}',
                             last4=str(card.get('number', ''))[-4:],
                             balance_after=99.0 if oc == 'success' else None)

    def subscribe(self, session, tenant_id, card, monitor=None, should_stop=None, dry=False):
        raise AssertionError('StubAdapter 不支持订阅，编排层不该调它')


class _FakeSession:
    def capture_frame(self):
        pass


@pytest.fixture
def stub():
    a = StubAdapter()
    platforms.register(a)
    yield a
    platforms.unregister(STUB)


@pytest.fixture
def models(db):
    gid = CardGroupModel(db).create('g', 'payment')
    return {
        'gid': gid,
        'card_pool': CardPoolModel(db),
        'valid_card': ValidCardModel(db),
        'card_state': CardPaymentStateModel(db),
        'recharge_log': RechargeLogModel(db),
        'platform_account': PlatformAccountModel(db),
        'account': AccountModel(db),
    }


def _card(number='4111111111111111'):
    return {'number': number, 'expiry_month': '12', 'expiry_year': '2030', 'cvc': '123',
            'first_name': 'T', 'last_name': 'U'}


def _recharge(stub, models, cards, **kw):
    return registration.recharge_account(
        'a@x.com', 'pw',
        payment_cards=cards,
        recharge_log_model=models['recharge_log'],
        valid_card_model=models['valid_card'],
        card_pool_model=models['card_pool'],
        account_model=models['account'],
        card_state_model=models['card_state'],
        platform=STUB,
        platform_account_model=models['platform_account'],
        adapter=stub,
        browser_factory=lambda email: _FakeSession(),
        **kw,
    )


# ---------- 注册表 ----------

def test_opencode_is_registered_and_satisfies_protocol():
    a = platforms.get('opencode')
    assert isinstance(a, PlatformAdapter)
    assert a.slug == 'opencode'


def test_unknown_platform_raises_instead_of_falling_back(stub):
    """未知平台必须抛错。静默回落到默认平台会把数据写到错的地方。"""
    with pytest.raises(KeyError):
        platforms.get('no-such-platform')


def test_registering_a_new_platform_needs_no_orchestration_change(stub):
    """AC12：注册即可用，编排层无感。"""
    assert STUB in platforms.all_slugs()
    assert platforms.get(STUB) is stub
    assert {d['slug'] for d in platforms.describe_all()} >= {'opencode', STUB}


# ---------- 编排层跑通一个虚构平台（AC12 的实证） ----------

def test_orchestration_runs_end_to_end_on_a_fictional_platform(stub, models):
    """整条充值编排在一个纯虚构平台上跑通，且把结果写到该平台的行上。"""
    ok, err, _responses, last4, outcome = _recharge(stub, models, [_card()])

    assert (ok, outcome) == (True, 'topup')
    assert last4 == '1111'
    assert ('ensure_session', 'a@x.com') in stub.calls
    # 状态全部落在 stub 平台，opencode 那边毫无痕迹
    assert models['platform_account'].get_status(STUB, 'a@x.com') == 'recharged'
    assert models['platform_account'].get(STUB, 'a@x.com')['tenant_id'] == 'tnt_1'
    assert models['platform_account'].get('opencode', 'a@x.com') is None
    assert models['valid_card'].is_valid(STUB, '4111111111111111') is True
    assert models['valid_card'].is_valid('opencode', '4111111111111111') is False


def test_adapter_supplies_its_own_skip_balance(models):
    """余额阈值来自 adapter，不是写死的 $20。"""
    stub = StubAdapter(balance=60.0)      # 60 ≥ StubAdapter.recharge_skip_balance(50)
    ok, _err, _r, _l4, outcome = _recharge(stub, models, [_card()])

    assert (ok, outcome) == (False, 'archived')
    assert models['platform_account'].get_status(STUB, 'a@x.com') == 'archived'
    assert ('top_up' not in [c[0] for c in stub.calls]), '已归档不该再试卡'


def test_adapter_supplies_its_own_attempt_cap(models):
    """单次试卡上限来自 adapter（stub 是 3），不是写死的 8。"""
    stub = StubAdapter(outcomes=['failed'] * 10)
    cards = [_card(f'41111111111100{i:02d}') for i in range(8)]
    _recharge(stub, models, cards)

    tried = [c for c in stub.calls if c[0] == 'top_up']
    assert len(tried) == 3, f'应在 3 张后停手，实际试了 {len(tried)}'


# ---------- outcome 语义（AC13） ----------

@pytest.mark.parametrize('outcome', ['error', 'unknown'])
def test_non_card_failures_do_not_consume_the_card(stub_free_outcome, models, outcome):
    """error / unknown 都不是卡的问题——绝不能标废、也不能进冷却。

    一次网络抖动或页面加载失败就把好卡打成 invalid，是不可逆的损失。
    """
    stub = StubAdapter(outcomes=[outcome])
    platforms.register(stub)
    try:
        num = '4111111111111111'
        _recharge(stub, models, [_card(num)])

        assert models['card_pool'].get_platform_status(STUB, num) == '', '不该标废'
        assert models['card_state'].in_cooldown(STUB, num) is False, '不该冷却'
    finally:
        platforms.unregister(STUB)


def test_needs_captcha_stops_immediately_without_consuming_cards(models):
    """needs_captcha 是账号级风控：立即停手，不再换卡，也不消耗当前这张。"""
    stub = StubAdapter(outcomes=['needs_captcha', 'success'])
    platforms.register(stub)
    try:
        cards = [_card('4111111111111111'), _card('4222222222222222')]
        ok, err, _r, _l4, outcome = _recharge(stub, models, cards)

        assert (ok, outcome) == (False, 'failed')
        assert 'hCaptcha' in err
        assert len([c for c in stub.calls if c[0] == 'top_up']) == 1, '碰到风控不该继续换卡'
        assert models['card_pool'].get_platform_status(STUB, '4111111111111111') == ''
    finally:
        platforms.unregister(STUB)


def test_declined_card_never_succeeded_here_is_invalidated(models):
    """明确拒付 + 本平台从未成功过 → 判废。"""
    stub = StubAdapter(outcomes=['failed'])
    platforms.register(stub)
    try:
        num = '4111111111111111'
        _recharge(stub, models, [_card(num)])
        assert models['card_pool'].get_platform_status(STUB, num) == 'invalid'
    finally:
        platforms.unregister(STUB)


def test_declined_card_that_succeeded_here_only_cools_down(models):
    """明确拒付 + 本平台成功过 → 只进 24h 冷却，不判废。"""
    stub = StubAdapter(outcomes=['success', 'failed'])
    platforms.register(stub)
    try:
        num = '4111111111111111'
        _recharge(stub, models, [_card(num)])          # 先成功一次，留下 recharge_logs
        _recharge(stub, models, [_card(num)])          # 再被拒

        assert models['card_pool'].get_platform_status(STUB, num) != 'invalid'
        assert models['card_state'].in_cooldown(STUB, num) is True
    finally:
        platforms.unregister(STUB)


def test_keeps_card_property_matches_the_documented_set():
    """PaymentResult.keeps_card 与文档里那三个 outcome 严格对应。"""
    for oc in OUTCOMES_KEEPING_CARD:
        assert PaymentResult(ok=False, outcome=oc).keeps_card is True
    for oc in ('success', 'failed', 'dry_ready'):
        assert PaymentResult(ok=False, outcome=oc).keeps_card is False


@pytest.fixture
def stub_free_outcome():
    """参数化用例自行注册 stub，这里只保证收尾干净。"""
    yield
    platforms.unregister(STUB)


# ---------- 能力声明与实现必须自洽 ----------

def test_every_registered_adapter_satisfies_the_required_protocol():
    """所有注册的适配器都必须满足 PlatformAdapter。

    接 infron 时这条真的挂过：协议把 subscribe 与 fetch_apikey 声明成必需方法，
    而 infron 没有订阅、也拿不到 key 明文，于是 isinstance 返回 False。修法不是在
    infron 里造假实现，而是把 subscribe 拆成可选的 SubscribingAdapter，
    fetch_apikey 如实返回 None（契约里 None 就是「抓不到」）。
    """
    for slug in platforms.all_slugs():
        assert isinstance(platforms.get(slug), PlatformAdapter), f"{slug} 不满足 PlatformAdapter"


def test_subscribe_capability_matches_implementation():
    """声明了 CAP_SUBSCRIBE 就必须真的能订阅；没声明就不该被当成能订阅。

    两者脱节的后果是编排层按 capabilities 放行后调到不存在的方法，
    或者反过来白白跳过一个其实支持订阅的平台。
    """
    from src.platforms.base import CAP_SUBSCRIBE, SubscribingAdapter
    for slug in platforms.all_slugs():
        a = platforms.get(slug)
        declared = CAP_SUBSCRIBE in a.capabilities
        implemented = isinstance(a, SubscribingAdapter)
        assert declared == implemented, (
            f"{slug} 声明订阅={declared} 但实现={implemented}")


def test_topup_capability_matches_implementation():
    from src.platforms.base import CAP_TOPUP
    for slug in platforms.all_slugs():
        a = platforms.get(slug)
        if CAP_TOPUP in a.capabilities:
            assert callable(getattr(a, 'top_up', None)), f"{slug} 声明充值但没有 top_up"


def test_infron_is_registered_and_is_topup_only():
    a = platforms.get('infron')
    assert a.slug == 'infron'
    assert sorted(a.capabilities) == ['topup'], 'infron 是纯充值制，不该声明订阅'
    assert a.extract_tenant_id('https://infron.ai/dashboard') is None, 'infron 无租户 id'
    assert a.fetch_apikey(None, None) is None, 'key 页脱敏，如实返回 None'
