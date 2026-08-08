"""多 worker 并发充值时，同一张卡绝不被两个账号**同时**提交给发卡行。

为什么单独立一个文件：`test_registry.py` 盯的是 PaymentCardRegistry 这个数据结构
本身（acquire 单胜者、release 后可再领……），但它不经过 `recharge_account`。真正
决定安全与否的是编排层**怎么用**它——acquire 放在 try 外还是内、release 在不在
finally 里、`continue`/`break`/异常三条路径是否都释放。

R3（一个账号一次会话连刷多张卡）把这套用法整个改了：以前一次会话只碰一张卡、
拿了就返回；现在要在一个循环里反复「拿一张→刷→放→拿下一张」。持有窗口从
「一次」变成「N 次」，任何一条提前退出的路径漏掉 release，都会把那张卡永久锁死
在 in-flight 里——不报错，只是它再也不会被任何账号选中，卡池悄悄变小。

风险是真金白银的：同一张卡在两个会话里同时扣款会叠加发卡行的 velocity 风控，
比单纯重复刷更容易触发拒付甚至锁卡。所以这里不靠读代码判断，直接起线程压。
"""

import threading
import time

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
from src.web.worker import PaymentCardRegistry

STUB = 'concurrentstub'


class _Session:
    """假会话。带 email 是为了让 top_up 能知道是谁在刷——适配器接口只收 session，
    不收 email，而重叠检测必须能区分账号。"""

    def __init__(self, email):
        self.email = email

    def capture_frame(self):
        pass


class _ContentionStub:
    """在 top_up 里记录「此刻谁正在刷哪张卡」，任何重叠都留证。

    hold 是刻意的：付款是个耗时操作，瞬时完成的桩根本压不出竞态。

    max_per_card 同样是刻意的：编排层现在**粘卡**（一张卡过款就一直用它，直到它失败）。
    一个只会返回 success 的桩会让每个会话死守第一张卡刷满 max_card_attempts，整个文件的
    争用就压不出来了——4 个账号各占一张卡，永远不会去抢别人手里的。刷够 N 笔就回一次
    failed，会话才会往下一张卡走，卡与卡之间的争用才真的发生。
    """

    slug = STUB
    display_name = '并发压测桩'
    capabilities = frozenset({CAP_TOPUP})
    recharge_skip_balance = 1e9
    default_topup_amount = 7.0
    max_card_attempts = 50

    def __init__(self, hold=0.02, max_per_card=2):
        self.hold = hold
        self.max_per_card = max_per_card
        self._lock = threading.Lock()
        self._active = {}          # card_number -> email，当前正在刷的
        self.violations = []       # [(card, 先到的 email, 后到的 email)]
        self.usage = []            # [(card, email)]，全部调用流水
        self.peak_per_email = {}   # email -> 同时持有的卡数峰值
        self._held = {}            # email -> 当前持有卡数
        self._charges = {}         # (email, card) -> 本会话已在这张卡上刷了几笔

    def module_names(self):
        return []

    def extract_tenant_id(self, url):
        return 'tnt'

    def ensure_session(self, session, creds, monitor=None, timeout=240):
        return SessionResult(ok=True, tenant_id='tnt', detail='ok')

    def read_balance(self, session, tenant_id, monitor=None):
        return 0.0

    def read_balance_from_current_page(self, session):
        return 0.0

    def fetch_apikey(self, session, tenant_id, monitor=None):
        return None

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        num = card['number']
        email = session.email
        with self._lock:
            holder = self._active.get(num)
            if holder is not None:
                self.violations.append((num, holder, email))
            self._active[num] = email
            self.usage.append((num, email))
            n = self._held.get(email, 0) + 1
            self._held[email] = n
            self.peak_per_email[email] = max(self.peak_per_email.get(email, 0), n)
            charges = self._charges.get((email, num), 0) + 1
            self._charges[(email, num)] = charges

        time.sleep(self.hold)          # 模拟真实付款耗时，把竞态窗口撑开

        with self._lock:
            if self._active.get(num) == email:
                del self._active[num]
            self._held[email] = self._held.get(email, 1) - 1
        ok = charges <= self.max_per_card
        return PaymentResult(ok=ok, outcome='success' if ok else 'failed',
                             err='' if ok else '刷够了，换卡',
                             last4=str(num)[-4:], balance_after=None)


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


def _card(n):
    return {'number': f'411111111111{n:04d}', 'expiry_month': '12', 'expiry_year': '2030',
            'cvc': '123', 'first_name': 'T', 'last_name': 'U'}


def _run_concurrently(stub, models, registry, emails, cards, cfg):
    """每个 email 一个线程，同时跑 recharge_account，共用一个 registry。"""
    errors = []

    def one(email):
        try:
            registration.recharge_account(
                email, 'pw',
                payment_cards=list(cards),          # 各自一份快照，与生产一致
                recharge_log_model=models['recharge_log'],
                valid_card_model=models['valid_card'],
                card_pool_model=models['card_pool'],
                account_model=models['account'],
                card_state_model=models['card_state'],
                payment_registry=registry,
                platform=STUB,
                platform_account_model=models['platform_account'],
                adapter=stub,
                browser_factory=lambda e: _Session(e),
                recharge_cfg=cfg,
            )
        except Exception as e:                      # noqa: BLE001 - 线程里要留证
            errors.append((email, repr(e)))

    threads = [threading.Thread(target=one, args=(e,), name=f't-{e}') for e in emails]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not [t for t in threads if t.is_alive()], '有线程卡住未退出'
    assert errors == [], f'线程内抛异常: {errors}'


@pytest.fixture
def stub():
    s = _ContentionStub()
    platforms.register(s)
    yield s
    platforms.unregister(STUB)


# 连充要能一路刷到卡用完，才能把争用压满：上限设到远大于卡数。
_LOOSE = RechargeConfig(amount_min=20, amount_max=20, balance_cap=10 ** 9,
                        fail_cooldown_hours=0)


def test_no_two_accounts_pay_with_one_card_at_the_same_time(stub, models):
    """4 个账号同抢 8 张卡，任何时刻同一张卡只能有一个账号在刷。"""
    registry = PaymentCardRegistry()
    cards = [_card(i) for i in range(8)]
    emails = [f'w{i}@x.com' for i in range(4)]

    _run_concurrently(stub, models, registry, emails, cards, _LOOSE)

    assert stub.violations == [], (
        f'同一张卡被两个账号同时提交支付: {stub.violations}')


def test_a_session_holds_only_one_card_at_a_time(stub, models):
    """R3 连充下，单个会话任何时刻也只持有一张卡。

    循环里必须「拿一张→刷→放→再拿下一张」。若 release 被挪出 finally 或漏在某条
    分支上，一个会话就会攥着好几张卡不放——对外表现是卡池莫名其妙变小。
    """
    registry = PaymentCardRegistry()
    cards = [_card(i) for i in range(6)]

    _run_concurrently(stub, models, registry, ['solo@x.com'], cards, _LOOSE)

    assert stub.peak_per_email['solo@x.com'] == 1, \
        f"单会话同时持有多张卡: {stub.peak_per_email}"


def test_in_flight_is_empty_after_all_sessions_finish(stub, models):
    """跑完之后 in-flight 必须清空——漏一个就等于永久锁死一张卡。"""
    registry = PaymentCardRegistry()
    cards = [_card(i) for i in range(8)]
    emails = [f'w{i}@x.com' for i in range(4)]

    _run_concurrently(stub, models, registry, emails, cards, _LOOSE)

    assert registry.in_flight_numbers() == set(), \
        f'仍被占用的卡: {registry.in_flight_numbers()}'


def test_a_session_never_goes_back_to_an_earlier_card(stub, models):
    """连充循环不回头：离开一张卡之后不会再回来刷它。

    这条用例原先断言的是「同一会话不会把同一张卡刷两遍」。粘卡改造推翻了那个不变量
    ——一张能过款的卡本来就该被连着刷。真正还成立、也真正要守的是**不回头**：卡与卡
    之间只前进，否则一张刚被判失败并冷却的卡会在同一会话里被重新捡起来刷。
    """
    registry = PaymentCardRegistry()
    cards = [_card(i) for i in range(6)]

    _run_concurrently(stub, models, registry, ['solo@x.com'], cards, _LOOSE)

    used = [num for num, email in stub.usage if email == 'solo@x.com']
    # 连着刷同一张卡折叠成一段，段与段之间不得出现重复卡号
    segments = [n for i, n in enumerate(used) if i == 0 or n != used[i - 1]]
    assert len(segments) == len(set(segments)), f'同一会话回头刷了旧卡: {used}'


def test_exception_inside_the_loop_still_releases_the_card(models):
    """付款抛异常时也要放开卡——finally 的存在意义。

    不放的话那张卡在本进程内永久 in-flight，谁也再选不中它，且没有任何报错。
    """
    class _Exploding(_ContentionStub):
        def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
            raise RuntimeError('付款页崩了')

    stub = _Exploding()
    platforms.register(stub)
    try:
        registry = PaymentCardRegistry()
        # recharge_account 把循环内的异常兜住并返回 failed，不外抛
        registration.recharge_account(
            'boom@x.com', 'pw',
            payment_cards=[_card(0)],
            recharge_log_model=models['recharge_log'],
            card_pool_model=models['card_pool'],
            card_state_model=models['card_state'],
            payment_registry=registry,
            platform=STUB,
            platform_account_model=models['platform_account'],
            adapter=stub,
            browser_factory=lambda e: _Session(e),
            recharge_cfg=_LOOSE,
        )
        assert registry.in_flight_numbers() == set(), '异常路径漏放了卡'
    finally:
        platforms.unregister(STUB)


def test_subscribe_pipeline_also_takes_card_exclusion(db, monkeypatch):
    """订阅侧同样要走 acquire/release，且与充值侧**共用**同一个 in-flight 集合。

    这条曾经是真漏的：只有 registration.recharge_account 在排他，
    `_subscribe_one_account` 的试卡循环直接就 `adapter.subscribe(...)` 了。后果有两层：

      - 订阅任务自己的多个 worker 会从各自的 _eligible_cards 快照里挑中同一张卡同时提交；
      - 订阅任务与充值任务本就设计成可以并发跑，于是同一张卡在几秒内被两个**商户号**
        分别请求授权——典型盗刷特征，直接触发发卡行风控。

    这里从最外层验证：一张卡被「充值侧」占住时，订阅侧必须跳过它、不得提交。
    """
    from src.config import cfg as _cfg
    from src.platforms.base import CAP_SUBSCRIBE
    import src.browser.driver as driver_module
    from src.web.app import AppState, build_models

    # 本用例盯的是选卡排他，不是浏览器供给：关掉 AdsPower 走 create_driver_vanilla，
    # 再把它换成假会话，全程不起真浏览器。
    monkeypatch.setattr(_cfg.adspower, 'enabled', False)
    monkeypatch.setattr(driver_module, 'create_driver_vanilla',
                        lambda profile_id=None, proxy=None: _Session(profile_id))
    monkeypatch.setattr(driver_module, 'close_driver', lambda s: None)

    class _SubStub(_ContentionStub):
        capabilities = frozenset({CAP_TOPUP, CAP_SUBSCRIBE})
        max_subscribe_attempts = 10

        def subscribe(self, session, tenant_id, card, monitor=None,
                      should_stop=None, dry=False):
            return self.top_up(session, tenant_id, card)

    stub = _SubStub()
    platforms.register(stub)
    try:
        gid = CardGroupModel(db).create('g', 'payment')
        pool = CardPoolModel(db)
        raw = [_card(i) for i in range(4)]
        pool.add_cards(gid, [{**c, 'country': 'US', 'address': 'a', 'city': 'c',
                              'state': 's', 'zip': '1'} for c in raw])
        db.execute("INSERT INTO accounts (email, login_password, identity_status) "
                   "VALUES (?,?,?)", ('sub@x.com', 'pw', 'registered'))

        # 用生产同一份构造：手抄的 dict 漏掉新加的模型时，报错会是流水线里
        # 一句被吞掉的 KeyError，与被测行为毫无关系。
        state = AppState(db, build_models(db), platform=STUB)

        # 充值侧先占住第 0 张卡（模拟另一条流水线正在刷它）
        held = raw[0]['number']
        assert state.payment_registry.try_acquire('opencode', held, 'other@x.com') is True

        state._subscribe_one_account({'email': 'sub@x.com', 'login_password': 'pw',
                                      'identity_status': 'registered'},
                                     gid, None)

        submitted = {num for num, _e in stub.usage}
        assert held not in submitted, \
            '订阅侧提交了一张正被充值侧占用的卡——跨流水线排他失效'
        assert state.payment_registry.in_flight_owner(held) == 'opencode', \
            '订阅侧把别人持有的卡释放掉了'
    finally:
        platforms.unregister(STUB)


def test_failed_acquire_does_not_release_someone_elses_card(stub, models):
    """抢不到卡时必须**直接跳过**，绝不能走到 finally 去 release。

    那会把持有者的卡凭空放掉，于是两个账号同时刷同一张——这正是本文件要防的事故。
    构造：先让 A 占住卡，再让 B 跑一遍，确认 B 结束后 A 的占用仍在。
    """
    registry = PaymentCardRegistry()
    num = _card(0)['number']
    assert registry.try_acquire(STUB, num, 'holder@x.com') is True

    _run_concurrently(stub, models, registry, ['intruder@x.com'], [_card(0)], _LOOSE)

    assert num in registry.in_flight_numbers(), 'B 把 A 持有的卡释放掉了'
    assert registry.in_flight_owner(num) == STUB
    assert [e for _n, e in stub.usage if e == 'intruder@x.com'] == [], \
        '抢不到卡却仍然提交了支付'


# ---------- 冷却的实时复查：快照之后被别人设上冷却的卡不能再刷 ----------


class _CooldownRaceStub(_ContentionStub):
    """刷完一张卡后，模拟「另一个 worker 把 cooled_number 刷失败并设了冷却」。

    真实场景：recharge_account 在**会话开始时**过滤一次冷却卡，之后整个会话都用那份
    快照。一次会话要连充多笔、可能跑很久，期间别的 worker 完全可能把某张卡刷废并冷却
    ——快照里它还在。in_flight 挡不住（对方早已释放），_used 只覆盖同一轮。

    继承压测桩以复用整套适配器接口，只换 top_up 的行为。
    """

    def __init__(self, card_state, cooled_number, hours=12):
        super().__init__(hold=0)
        self.card_state = card_state
        self.cooled_number = cooled_number
        self.hours = hours
        self.charged = []          # 实际被提交给「发卡行」的卡号

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        num = card['number']
        self.charged.append(num)
        self.card_state.set_cooldown(STUB, self.cooled_number, hours=self.hours,
                                     reason='别的 worker 刷失败了')
        # 一张卡只成一笔就回 failed：编排层粘卡，一直成功的话会话会死守第一张卡刷满
        # max_card_attempts，压根走不到第二张，「快照后被冷却的卡不会被刷」就无从验证。
        ok = self.charged.count(num) == 1
        return PaymentResult(ok=ok, outcome='success' if ok else 'failed',
                             err='' if ok else '刷够了，换卡',
                             last4=str(num)[-4:], balance_after=None)


def _run_one(stub, models, cards, cfg, email='a@x.com', registry=None):
    platforms.register(stub)
    try:
        return registration.recharge_account(
            email, 'pw',
            payment_cards=list(cards),
            recharge_log_model=models['recharge_log'],
            valid_card_model=models['valid_card'],
            card_pool_model=models['card_pool'],
            account_model=models['account'],
            card_state_model=models['card_state'],
            payment_registry=registry,
            platform=STUB,
            platform_account_model=models['platform_account'],
            adapter=stub,
            browser_factory=lambda e: _Session(e),
            recharge_cfg=cfg,
        )
    finally:
        platforms.unregister(STUB)


def test_card_cooled_after_the_snapshot_is_not_charged(models):
    """会话快照之后被设上冷却的卡，本会话必须跳过它。

    没有这道实时复查，同一张卡当日会被刷第二次——「当日失败不再使用」当场破功。
    """
    cards = [_card(1), _card(2)]
    stub = _CooldownRaceStub(models['card_state'], cooled_number=cards[1]['number'])

    _run_one(stub, models, cards, _LOOSE)

    assert cards[0]['number'] in stub.charged
    assert cards[1]['number'] not in stub.charged, \
        f'快照后被冷却的卡仍被提交给发卡行: {stub.charged}'


def test_cooldown_recheck_failure_does_not_break_the_session(models, monkeypatch):
    """复查本身出错时按「不在冷却」放行——安全网不该比它保护的流程更脆弱。"""
    cards = [_card(1), _card(2)]
    stub = _CooldownRaceStub(models['card_state'], cooled_number=cards[1]['number'])

    calls = {'n': 0}
    real = models['card_state'].in_cooldown

    def boom(platform, num):
        calls['n'] += 1
        if calls['n'] > 1:            # 入口的预过滤放过，循环内的复查炸
            raise RuntimeError('DB 抖了一下')
        return real(platform, num)

    monkeypatch.setattr(models['card_state'], 'in_cooldown', boom)

    _run_one(stub, models, cards, _LOOSE)

    assert cards[0]['number'] in stub.charged, '复查抛异常把整个会话带崩了'


def test_skipped_card_does_not_leak_its_in_flight_slot(models):
    """跳过冷却卡时 finally 仍要 release，否则那张卡永久锁死在 in-flight 里。

    这是 continue 写错位置（放到 try 之外）时的典型后果：不报错，卡池悄悄变小。
    """
    cards = [_card(1), _card(2)]
    stub = _CooldownRaceStub(models['card_state'], cooled_number=cards[1]['number'])
    registry = PaymentCardRegistry()

    _run_one(stub, models, cards, _LOOSE, registry=registry)

    assert registry.in_flight_numbers() == set(), \
        f'跳过的卡没有释放 in-flight 占用: {registry.in_flight_numbers()}'
