"""连充循环、连续失败判废、失败冷却、随机金额（编排层行为）。

这四条规则全部落在 registration.recharge_account 的主循环里，彼此耦合得很紧：

  - 成功后不返回、继续用**同一张卡**充下一笔  →  所以成功的卡**不能**进冷却
  - 失败即冷却 24h + 计数 +1，连续 3 次才判废  →  所以判废一张坏卡最快要 3 天
  - 每笔金额独立随机                          →  所以记账必须记这一笔的实际值

用一个虚构的 StubAdapter 驱动，不碰任何真实站点。测试里普遍把 fail_cooldown_hours
设成 0，好让多次失败能在同一个用例里连着发生——真实配置下它们会被冷却隔开。
"""

import pytest

import src.platforms as platforms
from src.config import RechargeConfig
from src.models.account import AccountModel
from src.models.card_group import CardGroupModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.card_pool import CardPoolModel
from src.models.platform_account import PlatformAccountModel
from src.models.recharge_log import RechargeLogModel
from src.models.valid_card import ValidCardModel
from src.platforms.base import CAP_TOPUP, PaymentResult, SessionResult
from src.services import registration

STUB = 'stubplatform'
OTHER = 'otherplatform'


class StubAdapter:
    """按调用序返回预设 outcome 的虚构平台。

    balances 与 outcomes 一一对应：某一笔要让 balance_after 是 None（模拟适配器
    读不到余额）就在对应位置放 None。缺省行为是成功时返回 balance_after=None，
    逼着编排层走「累计充值额」那条兜底判据。
    """

    display_name = 'Stub 平台'
    capabilities = frozenset({CAP_TOPUP})
    recharge_skip_balance = 1e9        # 大到永不触发归档预检，专心测充值循环
    default_topup_amount = 7.0

    def __init__(self, outcomes=None, balances=None, max_card_attempts=8, slug=STUB):
        self.slug = slug
        self.max_card_attempts = max_card_attempts
        self._outcomes = list(outcomes or ['success'])
        self._balances = list(balances or [])
        self.calls = []
        self.amounts = []

    def module_names(self):
        return []

    def extract_tenant_id(self, url):
        return 'tnt_1'

    def ensure_session(self, session, creds, monitor=None, timeout=240):
        return SessionResult(ok=True, tenant_id='tnt_1', detail='stub 已登录')

    def read_balance(self, session, tenant_id, monitor=None):
        return 0.0

    def read_balance_from_current_page(self, session):
        return 0.0

    def fetch_apikey(self, session, tenant_id, monitor=None):
        return None

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        i = len(self.calls)
        oc = self._outcomes[i] if i < len(self._outcomes) else 'failed'
        bal = self._balances[i] if i < len(self._balances) else None
        self.calls.append(('top_up', card.get('number'), oc))
        self.amounts.append(amount)
        return PaymentResult(ok=(oc == 'success'), outcome=oc,
                             err='' if oc == 'success' else f'stub {oc}',
                             last4=str(card.get('number', ''))[-4:],
                             balance_after=bal)


class _FakeSession:
    def capture_frame(self):
        pass


@pytest.fixture
def models(db):
    CardGroupModel(db).create('g', 'payment')
    return {
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


def _cards(n):
    return [_card(f'41111111111100{i:02d}') for i in range(n)]


def _run(adapter, models, cards, recharge_cfg=None, email='a@x.com', platform=None):
    platforms.register(adapter)
    try:
        return registration.recharge_account(
            email, 'pw',
            payment_cards=cards,
            recharge_log_model=models['recharge_log'],
            valid_card_model=models['valid_card'],
            card_pool_model=models['card_pool'],
            account_model=models['account'],
            card_state_model=models['card_state'],
            platform=platform or adapter.slug,
            platform_account_model=models['platform_account'],
            adapter=adapter,
            browser_factory=lambda e: _FakeSession(),
            recharge_cfg=recharge_cfg or RechargeConfig(fail_cooldown_hours=0),
        )
    finally:
        platforms.unregister(adapter.slug)


def _topups(adapter):
    return [c for c in adapter.calls if c[0] == 'top_up']


# 只关心「循环走了哪些卡」的用例用它：把 balance_cap 抬到不可能触及，免得随机金额
# 恰好累计到默认的 $200 时循环提前收手，断言变成偶发红。
_SHAPE_ONLY = RechargeConfig(fail_cooldown_hours=0, balance_cap=1e9)


# ---------- R3：成功后继续用同一账号充值 ----------


def test_success_does_not_stop_the_loop(models):
    """AC7：第一笔成功后继续充，而不是立刻换账号。"""
    stub = StubAdapter(outcomes=['success', 'success', 'failed'])
    ok, _err, responses, _l4, outcome = _run(stub, models, _cards(3),
                                             recharge_cfg=_SHAPE_ONLY)

    assert (ok, outcome) == (True, 'topup')
    assert sum(1 for r in responses if r['ok']) == 2


# ---------- 粘卡：能过款的卡就一直用，不换 ----------


def test_a_successful_card_keeps_being_used(models):
    """成功过的卡继续用来充下一笔，不换下一张。

    换卡的代价是拿一张没验证过的卡去赌：卡池里能过款的卡本就稀有，一笔一换等于
    把好卡晾在一边、笔笔都给账号叠一次拒付风险。
    """
    stub = StubAdapter(outcomes=['success'] * 3, max_card_attempts=3)
    _ok, _err, responses, _l4, _outcome = _run(stub, models, _cards(5),
                                               recharge_cfg=_SHAPE_ONLY)

    charged = [num for _c, num, _oc in _topups(stub)]
    assert len(charged) == 3
    assert len(set(charged)) == 1, f'成功的卡该被粘住，实际换了卡: {charged}'
    assert {r['card_last4'] for r in responses} == {charged[0][-4:]}


def test_the_next_card_is_only_reached_after_a_failure(models):
    """粘卡到这张卡自己失败为止，之后才轮到下一张。"""
    stub = StubAdapter(outcomes=['success', 'failed', 'success', 'success'],
                       max_card_attempts=4)
    cards = _cards(2)
    _run(stub, models, cards, recharge_cfg=_SHAPE_ONLY)

    charged = [num for _c, num, _oc in _topups(stub)]
    assert charged == [cards[0]['number'], cards[0]['number'],
                       cards[1]['number'], cards[1]['number']], \
        f'应是「第一张成功→失败→换第二张」，实际 {charged}'


def test_sticking_to_a_card_does_not_bypass_the_attempt_cap(models):
    """粘卡的每一笔照样计入 max_card_attempts——上限不能被粘卡架空。"""
    stub = StubAdapter(outcomes=['success'] * 20, max_card_attempts=4)
    _ok, err, _r, _l4, _outcome = _run(stub, models, _cards(6),
                                       recharge_cfg=_SHAPE_ONLY)

    assert len(_topups(stub)) == 4
    assert '张卡上限' in err, f'停手原因应是试卡上限，实际: {err}'


def test_a_stuck_card_is_released_exactly_once(models):
    """粘卡期间一直持有这张卡的 in-flight 占用，离开时释放，且不重复释放。"""
    from src.web.worker import PaymentCardRegistry

    registry = PaymentCardRegistry()
    stub = StubAdapter(outcomes=['success', 'success', 'failed'], max_card_attempts=3)
    platforms.register(stub)
    try:
        registration.recharge_account(
            'a@x.com', 'pw',
            payment_cards=_cards(2),
            recharge_log_model=models['recharge_log'],
            valid_card_model=models['valid_card'],
            card_pool_model=models['card_pool'],
            account_model=models['account'],
            card_state_model=models['card_state'],
            payment_registry=registry,
            platform=STUB,
            platform_account_model=models['platform_account'],
            adapter=stub,
            browser_factory=lambda e: _FakeSession(),
            recharge_cfg=_SHAPE_ONLY,
        )
    finally:
        platforms.unregister(STUB)

    assert registry.in_flight_numbers() == set(), '粘卡结束后仍有卡被占用'


def test_attempt_cap_stops_the_loop(models):
    """AC8：达到 max_card_attempts 就停，哪怕还有可选卡、哪怕笔笔都成功。"""
    stub = StubAdapter(outcomes=['success'] * 10, max_card_attempts=3)
    ok, err, _r, _l4, outcome = _run(stub, models, _cards(8))

    assert (ok, outcome) == (True, 'topup')
    assert len(_topups(stub)) == 3
    assert '上限' in err


def test_zero_attempt_cap_means_unlimited(models):
    """max_card_attempts=0 是「不限制」的哨兵值：卡池有多少张就试多少张。

    2026-08-12 按要求把 opencode 从 8 改为不限制。这条守住 0 不会被当成
    「一张都不试」——`attempts >= 0` 恒真，少一个 `> 0` 判断就是整条充值线停摆。
    """
    stub = StubAdapter(outcomes=['failed'] * 20, max_card_attempts=0)
    ok, _err, _r, _l4, outcome = _run(stub, models, _cards(12))

    assert (ok, outcome) == (False, 'failed')
    assert len(_topups(stub)) == 12, '不限制时应把 12 张卡全试完'


def test_zero_attempt_cap_still_honours_balance_cap(models):
    """不限制试卡数**不等于**不限制充值额：balance_cap 是剩下的唯一一道上限。"""
    stub = StubAdapter(outcomes=['success'] * 10, balances=[150.0] * 10,
                       max_card_attempts=0)
    cfg = RechargeConfig(balance_cap=100.0, fail_cooldown_hours=0)
    ok, _err, _r, _l4, outcome = _run(stub, models, _cards(10), recharge_cfg=cfg)

    assert (ok, outcome) == (True, 'topup')
    assert len(_topups(stub)) == 1, '余额已超上限却还在充'


def test_balance_cap_stops_the_loop(models):
    """AC9：适配器报得出余额时，余额达上限即停。"""
    stub = StubAdapter(outcomes=['success'] * 5, balances=[150.0] * 5)
    cfg = RechargeConfig(balance_cap=100.0, fail_cooldown_hours=0)
    ok, err, _r, _l4, outcome = _run(stub, models, _cards(5), recharge_cfg=cfg)

    assert (ok, outcome) == (True, 'topup')
    assert len(_topups(stub)) == 1, '第一笔就超上限，不该再充'
    assert '上限' in err


def test_balance_cap_falls_back_to_session_total(models):
    """AC9 兜底：balance_after 读不到（None）时用本次累计充值额判上限。

    没有这条兜底的话，infron 这类读不到余额的平台会一路充到 max_card_attempts。
    """
    stub = StubAdapter(outcomes=['success'] * 5)          # balances 全 None
    cfg = RechargeConfig(amount_min=50, amount_max=50, balance_cap=120.0,
                         fail_cooldown_hours=0)
    _ok, err, _r, _l4, _outcome = _run(stub, models, _cards(5), recharge_cfg=cfg)

    assert len(_topups(stub)) == 3, '50+50+50=150 ≥ 120，应在第三笔后停'
    assert '累计' in err


def test_session_total_caps_even_when_balance_is_reported(models):
    """balance_cap 必须是**硬**上限，不能只在 balance_after 为 None 时才兜底。

    这一条防的是「适配器报得出余额、但报得不对」：只要有个平台把 success 判成功却
    回了个陈旧或偏低的余额，只看余额的话循环会一路刷到 max_card_attempts，
    单个账号能吃掉 8 × $100 = $800。这里模拟余额恒为 $1（远低于上限）。
    """
    stub = StubAdapter(outcomes=['success'] * 8, balances=[1.0] * 8)
    cfg = RechargeConfig(amount_min=50, amount_max=50, balance_cap=120.0,
                         fail_cooldown_hours=0)
    _ok, err, _r, _l4, _outcome = _run(stub, models, _cards(8), recharge_cfg=cfg)

    assert len(_topups(stub)) == 3, '50×3=150 ≥ 120，第三笔后就该停，而不是刷满 8 张'
    assert '累计' in err


def test_captcha_after_a_success_still_reports_topup(models):
    """AC10：hCaptcha 打断循环，但已成功的笔数不该被一次风控抹掉。"""
    stub = StubAdapter(outcomes=['success', 'needs_captcha', 'success'])
    ok, _err, responses, _l4, outcome = _run(stub, models, _cards(3))

    assert (ok, outcome) == (True, 'topup'), '成功过一笔就是 topup'
    assert len(_topups(stub)) == 2, '碰到风控不该继续换卡'
    assert sum(1 for r in responses if r['ok']) == 1


def test_captcha_before_any_success_reports_failed(models):
    """一笔都没成时仍是 failed，且 err 要说清是 hCaptcha——运维据此判断要不要人工介入。"""
    stub = StubAdapter(outcomes=['needs_captcha', 'success'])
    ok, err, _r, _l4, outcome = _run(stub, models, _cards(2))

    assert (ok, outcome) == (False, 'failed')
    assert 'hCaptcha' in err
    assert len(_topups(stub)) == 1


def test_running_out_of_cards_ends_the_loop(models):
    """卡试完了自然收手，不报错。

    粘卡下「卡用完」意味着每张卡都被刷到失败为止：两张卡各一次成功、一次失败。
    """
    stub = StubAdapter(outcomes=['success', 'failed', 'success', 'failed'])
    ok, _err, _r, _l4, outcome = _run(stub, models, _cards(2),
                                      recharge_cfg=_SHAPE_ONLY)

    assert (ok, outcome) == (True, 'topup')
    assert len(_topups(stub)) == 4


# ---------- R1：连续失败 3 次才判废 ----------


def test_three_consecutive_failures_invalidate_the_card(models):
    """AC1：第 1、2 次拒付只冷却，第 3 次才判废。"""
    num = '4111111111111111'
    cfg = RechargeConfig(fail_cooldown_hours=0, max_fail_streak=3)

    for i in (1, 2):
        _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)
        assert models['card_pool'].get_platform_status(STUB, num) == '', f'第 {i} 次不该判废'
        assert models['card_state'].get_fail_streak(STUB, num) == i

    _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)
    assert models['card_pool'].get_platform_status(STUB, num) == 'invalid'


def test_a_success_resets_the_failure_streak(models):
    """AC2：中间成功一次，计数清零，之后要再连着失败 3 次才判废。"""
    num = '4111111111111111'
    cfg = RechargeConfig(fail_cooldown_hours=0, max_fail_streak=3)

    for _ in range(2):
        _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)
    assert models['card_state'].get_fail_streak(STUB, num) == 2

    # max_card_attempts=1：只要一笔成功。不设的话粘卡会接着刷第二笔，
    # 而桩在 outcomes 用完后一律回 'failed'，刚清零的计数又被顶回 1。
    _run(StubAdapter(outcomes=['success'], max_card_attempts=1), models, [_card(num)],
         recharge_cfg=cfg)
    assert models['card_state'].get_fail_streak(STUB, num) == 0

    for i in (1, 2):
        _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)
        assert models['card_pool'].get_platform_status(STUB, num) != 'invalid', \
            f'清零后第 {i} 次失败不该判废'


def test_failure_streak_is_isolated_per_platform(models):
    """AC3：在一个平台失败到判废，另一个平台的计数仍是 0。"""
    num = '4111111111111111'
    cfg = RechargeConfig(fail_cooldown_hours=0, max_fail_streak=3)

    for _ in range(3):
        _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)

    assert models['card_pool'].get_platform_status(STUB, num) == 'invalid'
    assert models['card_state'].get_fail_streak(OTHER, num) == 0
    assert models['card_pool'].get_platform_status(OTHER, num) == ''


def test_streak_threshold_of_one_restores_the_old_behaviour(models):
    """max_fail_streak=1 是回退开关：退回改造前的「首拒即判废」。"""
    num = '4111111111111111'
    cfg = RechargeConfig(fail_cooldown_hours=0, max_fail_streak=1)

    _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)

    assert models['card_pool'].get_platform_status(STUB, num) == 'invalid'


def test_zero_threshold_does_not_invalidate_on_the_first_decline(models):
    """max_fail_streak=0 是配置笔误，不能被当成「一次都不容忍」。

    编排层判的是 `streak >= threshold`，streak 从 1 起数，所以 0 会让条件恒真——
    连计数都不用读，首拒即判废。fail_threshold() 把它兜到 1，行为等同回退开关。
    """
    num = '4111111111111111'
    cfg = RechargeConfig(fail_cooldown_hours=0, max_fail_streak=0)

    _run(StubAdapter(outcomes=['failed']), models, [_card(num)], recharge_cfg=cfg)

    assert models['card_state'].get_fail_streak(STUB, num) == 1, '计数仍要如实累加'
    assert models['card_pool'].get_platform_status(STUB, num) == 'invalid', \
        '兜到 1 之后行为等同 max_fail_streak=1'


# ---------- R2：失败进冷却，成功不进 ----------


def test_failure_puts_the_card_into_cooldown(models):
    """AC4：任何卡失败后立即进冷却。"""
    num = '4111111111111111'
    _run(StubAdapter(outcomes=['failed']), models, [_card(num)],
         recharge_cfg=RechargeConfig(fail_cooldown_hours=24))

    assert models['card_state'].in_cooldown(STUB, num) is True


def test_success_does_not_put_the_card_into_cooldown(models):
    """AC5：成功的卡不进冷却——否则同一账号连充第二笔就无卡可用了。"""
    num = '4111111111111111'
    _run(StubAdapter(outcomes=['success'], max_card_attempts=1), models, [_card(num)],
         recharge_cfg=RechargeConfig(fail_cooldown_hours=24))

    assert models['card_state'].in_cooldown(STUB, num) is False


@pytest.mark.parametrize('outcome', ['error', 'unknown'])
def test_non_card_failures_touch_neither_streak_nor_cooldown(models, outcome):
    """AC6：error / unknown 不是卡的问题，既不计入失败次数也不冷却。

    这是硬约束：一次网络抖动或页面加载失败把好卡推向判废，是不可逆的损失。
    """
    num = '4111111111111111'
    _run(StubAdapter(outcomes=[outcome]), models, [_card(num)])

    assert models['card_state'].get_fail_streak(STUB, num) == 0
    assert models['card_state'].in_cooldown(STUB, num) is False
    assert models['card_pool'].get_platform_status(STUB, num) == ''


def test_repeated_non_card_failures_never_invalidate(models):
    """连着十次 error 也不该判废——它压根不是卡的问题。"""
    num = '4111111111111111'
    _run(StubAdapter(outcomes=['error'] * 10), models, _cards(10))

    assert models['card_pool'].get_platform_status(STUB, num) == ''


# ---------- R4：随机金额 ----------


def test_amount_is_drawn_from_the_configured_range(models):
    """AC11：每笔金额独立取自区间，笔与笔之间可以不同。"""
    stub = StubAdapter(outcomes=['success'] * 40, max_card_attempts=40)
    cfg = RechargeConfig(amount_min=20, amount_max=100, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, _cards(40), recharge_cfg=cfg)

    assert len(stub.amounts) == 40
    assert all(20 <= a <= 100 for a in stub.amounts)
    assert len(set(stub.amounts)) > 1, '每笔应独立随机，不是算一次用到底'


def test_amount_is_recorded_in_the_log(models, db):
    """AC12：记账写这一笔的实际金额，不再恒为 20。"""
    # max_card_attempts=1 把用例框在「恰好一笔」上：粘卡下同一张卡会一直刷下去，
    # 而这里要断言的是单笔记账，不是循环行为。下同。
    stub = StubAdapter(outcomes=['success'], max_card_attempts=1)
    cfg = RechargeConfig(amount_min=37, amount_max=37, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    rows = db.fetchall("SELECT amount FROM recharge_logs WHERE platform=?", (STUB,))
    assert [r['amount'] for r in rows] == [37]


def test_failed_attempts_also_record_their_amount(models, db):
    """失败也要记实际金额——排障时要能看出金额有没有被正确传下去。"""
    stub = StubAdapter(outcomes=['failed'])
    cfg = RechargeConfig(amount_min=88, amount_max=88, fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    rows = db.fetchall("SELECT amount, status FROM recharge_logs WHERE platform=?", (STUB,))
    assert [(r['amount'], r['status']) for r in rows] == [(88, 'failed')]


def test_amount_reaches_the_adapter(models):
    """金额必须真的传到适配器——此前编排层从不传 amount，适配器一直用自己的默认值。"""
    stub = StubAdapter(outcomes=['success'], max_card_attempts=1)
    cfg = RechargeConfig(amount_min=64, amount_max=64, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    assert stub.amounts == [64]
    assert stub.amounts[0] != stub.default_topup_amount


# ---------- 记账用实扣金额，不是请求金额 ----------


class _FixedFirstChargeStub(StubAdapter):
    """模拟 opencode：首充金额由站点定死，复充才认我们传的数。

    这不是假想。opencode 的 billing 页首充入口是 "Enable Billing"，点了直接跳后端
    预先建好的 Stripe Checkout，金额写死在那个 session 里，页面上没有任何可填金额的
    地方；只有复充（"Add Balance" → 金额输入框）才认。2026-08-04 线上因此出现过
    账面 $79、实扣 $20 的记录。
    """

    FIRST = 20.0

    def __init__(self, *a, modes=None, **kw):
        super().__init__(*a, **kw)
        self._modes = list(modes or ['first'])
        self.requested = []

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        i = len(self.calls)
        mode = self._modes[i] if i < len(self._modes) else 'reload'
        self.requested.append(amount)
        res = super().top_up(session, tenant_id, card, amount=amount,
                             monitor=monitor, should_stop=should_stop)
        res.mode = mode
        res.amount = self.FIRST if mode == 'first' else amount
        return res


def test_first_charge_is_logged_at_the_amount_actually_taken(models, db):
    """首充记 $20（站点实收），而不是我们请求的随机数。"""
    stub = _FixedFirstChargeStub(outcomes=['success'], modes=['first'],
                                 max_card_attempts=1)
    cfg = RechargeConfig(amount_min=79, amount_max=79, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    assert stub.requested == [79], '请求金额照常按配置随机产生'
    rows = db.fetchall("SELECT amount FROM recharge_logs WHERE platform=?", (STUB,))
    assert [r['amount'] for r in rows] == [20.0], '账面必须是实扣的 20，不是请求的 79'


def test_reload_charge_is_logged_at_the_requested_amount(models, db):
    """复充认我们传的金额，照常记随机值。"""
    stub = _FixedFirstChargeStub(outcomes=['success'], modes=['reload'],
                                 max_card_attempts=1)
    cfg = RechargeConfig(amount_min=57, amount_max=57, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    rows = db.fetchall("SELECT amount FROM recharge_logs WHERE platform=?", (STUB,))
    assert [r['amount'] for r in rows] == [57.0]


def test_balance_cap_counts_what_was_actually_charged(models):
    """累计额上限也要按实扣算——按请求额算的话，首充只扣了 20 却记成 79，
    连充循环会提前以为到顶而收手。"""
    stub = _FixedFirstChargeStub(outcomes=['success'] * 5,
                                 modes=['first'] + ['reload'] * 4,
                                 max_card_attempts=10)
    # 请求额 50：首充实扣 20，之后每笔 50 → 20+50+50 = 120 ≥ 100 在第 3 笔后停。
    # 若按请求额算（50×2=100）会在第 2 笔就停。
    cfg = RechargeConfig(amount_min=50, amount_max=50, balance_cap=100.0,
                         fail_cooldown_hours=0)
    _run(stub, models, _cards(5), recharge_cfg=cfg)

    assert len(_topups(stub)) == 3, \
        f'应按实扣额累计到第 3 笔才到顶，实际试了 {len(_topups(stub))} 笔'


def test_adapter_that_reports_no_amount_falls_back_to_the_request(models, db):
    """适配器没回报 amount（None）时沿用请求额——老适配器不改也能跑。"""
    stub = StubAdapter(outcomes=['success'], max_card_attempts=1)   # 不设 amount 字段
    cfg = RechargeConfig(amount_min=33, amount_max=33, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    rows = db.fetchall("SELECT amount FROM recharge_logs WHERE platform=?", (STUB,))
    assert [r['amount'] for r in rows] == [33.0]


def test_opencode_reports_its_fixed_first_charge_amount():
    """真适配器的常量与站点实际一致——改动这个值等于改动记账口径。"""
    from src.platforms.opencode import billing as _b
    assert _b.FIRST_TOPUP_AMOUNT == 20.0


# ---------- 收尾余额以「刷新页面看到的数字」为准 ----------


class _StaleBalanceStub(StubAdapter):
    """付款时报一个**过时**的余额，收尾重读时才给出真实值。

    这不是臆造的场景：detect_payment_result 的 _balance_grew() 在余额「第一次比原来
    大」的瞬间就定案返回，那一刻页面未必结算完。2026-08-04 实测首充扣款后它报 20.0，
    于是账号列表一直显示 $20。
    """

    def __init__(self, *a, settled=None, **kw):
        super().__init__(*a, **kw)
        self.settled = settled
        self.read_balance_calls = 0

    def read_balance(self, session, tenant_id, monitor=None):
        self.read_balance_calls += 1
        # 第一次调用是充值前的归档预检，之后才是收尾重读
        return 0.0 if self.read_balance_calls == 1 else self.settled


def test_final_balance_comes_from_a_fresh_page_read(models):
    """收尾重读页面拿到的余额，必须覆盖付款瞬间报回来的那个过时值。"""
    stub = _StaleBalanceStub(outcomes=['success'], balances=[20.0], settled=99.0)
    cfg = RechargeConfig(amount_min=79, amount_max=79, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    got = models['platform_account'].get(STUB, 'a@x.com')['credits_balance']
    assert got == 99.0, f'落库的应是刷新页面读到的 99，而不是付款瞬间的 20，实际 {got}'


def test_final_balance_read_is_skipped_when_nothing_was_charged(models):
    """一笔都没成时不重读——没扣款就没有新余额，白导航一次页面。"""
    stub = _StaleBalanceStub(outcomes=['failed'], settled=99.0)
    _run(stub, models, [_card()])

    assert stub.read_balance_calls == 1, '只应有充值前那一次预检读取'


def test_unreadable_final_balance_keeps_the_earlier_value(models):
    """收尾读不到余额时保留循环里写的值，不要把它清空成 None。"""
    stub = _StaleBalanceStub(outcomes=['success'], balances=[42.0], settled=None)
    cfg = RechargeConfig(amount_min=20, amount_max=20, balance_cap=1e9,
                         fail_cooldown_hours=0)
    _run(stub, models, [_card()], recharge_cfg=cfg)

    got = models['platform_account'].get(STUB, 'a@x.com')['credits_balance']
    assert got == 42.0, f'读不到就该保留 42，实际 {got}'


def test_default_config_is_used_when_none_is_passed(models):
    """AC14：不传 recharge_cfg 时回落 cfg.recharge，流程不报错。"""
    stub = StubAdapter(outcomes=['success'])
    platforms.register(stub)
    try:
        ok, _err, _r, _l4, outcome = registration.recharge_account(
            'a@x.com', 'pw',
            payment_cards=[_card()],
            recharge_log_model=models['recharge_log'],
            card_pool_model=models['card_pool'],
            card_state_model=models['card_state'],
            platform=STUB,
            platform_account_model=models['platform_account'],
            adapter=stub,
            browser_factory=lambda e: _FakeSession(),
        )
    finally:
        platforms.unregister(STUB)

    assert (ok, outcome) == (True, 'topup')
    assert 20 <= stub.amounts[0] <= 100
