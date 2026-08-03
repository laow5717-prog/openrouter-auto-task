"""run_daily_pipeline 的并行编排测试（浏览器全打桩）。

覆盖重构后的「worker 自治 run_until_empty」充值管线：
  - 账号耗尽自动补号（注册 imported）→ 注册成功者随后被充值（闭环，可跨 worker）
  - 充值成功账号（recharged）不被重复充值（去重）
  - 2 worker 真并发、同一账号不被并发处理、收尾释放干净、串行=并行账面一致

安全网：create_driver / create_driver_vanilla 被替换成抛异常的桩——任何漏打桩
调用会立刻炸出来，而不是静默起一个真实 Chrome（曾踩过）。
"""

import tempfile
import threading

import pytest

from src.browser import driver as driver_module
from src.config import cfg
from src.models.account import AccountModel
from src.models.card_group import CardGroupModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.card_pool import CardPoolModel
from src.models.database import Database
from src.models.proxy import ProxyModel
from src.models.recharge_log import RechargeLogModel
from src.models.valid_card import ValidCardModel
from src.web.app import AppState


def _full_cards(n):
    return [{'number': f'4111{i:012d}', 'expiry_month': '12', 'expiry_year': '2030',
             'cvc': '123', 'first_name': 'T', 'last_name': f'U{i}',
             'country': 'United States', 'address': 'a', 'city': 'c',
             'state': 's', 'zip': '12345'} for i in range(n)]


class _Tracker:
    """记录并发峰值与「同一 email 是否被并发处理」，并模拟注册/充值对 DB 的状态推进。"""

    def __init__(self, db, delay=0.02):
        self.db = db
        self.delay = delay
        self.lock = threading.Lock()
        self.peak = 0
        self._active = 0
        self._in_flight = set()
        self.overlapping = []          # 同一 email 被并发处理的证据
        self.recharged_calls = []      # 每次充值处理的 email（查重复充值）
        self.registered_calls = []

    def _enter(self, email):
        with self.lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            if email in self._in_flight:
                self.overlapping.append(email)
            self._in_flight.add(email)

    def _exit(self, email):
        with self.lock:
            self._active -= 1
            self._in_flight.discard(email)

    def register(self, acct, worker=None, proxy=None):
        """假 _register_one_account：imported → registered（存 login_password）。"""
        email = acct['email']
        self._enter(email)
        try:
            with self.lock:
                self.registered_calls.append(email)
            threading.Event().wait(self.delay)
            self.db.execute(
                "UPDATE accounts SET status='registered', login_password='pw' WHERE email=?",
                (email,))
            return "registered", "ok"
        finally:
            self._exit(email)

    def recharge(self, email, login_password, **kw):
        """假 _recharge_one_account：registered → recharged，成功。"""
        self._enter(email)
        try:
            with self.lock:
                self.recharged_calls.append(email)
            threading.Event().wait(self.delay)
            self.db.execute("UPDATE accounts SET status='recharged' WHERE email=?", (email,))
            return "success", ""
        finally:
            self._exit(email)


class _FlakyOnceTracker(_Tracker):
    """每个账号第一次充值失败并烧掉一张卡（模拟被拒后标 invalid），第二次成功。

    验证轮转重试：失败账号不再永久入 done，一轮轮完后开新一轮重来（A 失败→
    换 B→…→下一轮再试 A），直到充成。"""

    def recharge(self, email, login_password, **kw):
        self._enter(email)
        try:
            with self.lock:
                self.recharged_calls.append(email)
                first = self.recharged_calls.count(email) == 1
            threading.Event().wait(self.delay)
            if first:
                # 烧掉一张可用卡：可选卡集合变化 → 轮转判定视为「有进展」
                self.db.execute(
                    "UPDATE card_pool SET status='invalid' WHERE id IN ("
                    "SELECT id FROM card_pool "
                    "WHERE COALESCE(status,'') NOT IN ('invalid','expired') LIMIT 1)")
                return "failed", "declined"
            self.db.execute("UPDATE accounts SET status='recharged' WHERE email=?", (email,))
            return "success", ""
        finally:
            self._exit(email)


class _AlwaysFailTracker(_Tracker):
    """充值永远失败且不动卡池（模拟登录类故障）——验证连续两轮零进展后收敛，不死循环。"""

    def recharge(self, email, login_password, **kw):
        self._enter(email)
        try:
            with self.lock:
                self.recharged_calls.append(email)
            threading.Event().wait(self.delay)
            return "failed", "login error"
        finally:
            self._exit(email)


@pytest.fixture
def no_browser(monkeypatch):
    def _explode(*a, **kw):
        raise AssertionError("测试中不应创建真实浏览器——有函数漏打桩了")
    monkeypatch.setattr(driver_module, 'create_driver', _explode)
    monkeypatch.setattr(driver_module, 'create_driver_vanilla', _explode)


def _run_pipeline(workers, n_registered=0, n_imported=4, n_cards=16, tracker_cls=_Tracker):
    path = tempfile.mktemp(suffix='.db')
    db = Database(path)
    models = {
        'account': AccountModel(db), 'recharge_log': RechargeLogModel(db),
        'card_group': CardGroupModel(db), 'card_pool': CardPoolModel(db),
        'valid_card': ValidCardModel(db), 'card_state': CardPaymentStateModel(db),
        'proxy': ProxyModel(db),
    }
    gid = models['card_group'].create('pay-group', 'pay')
    models['card_pool'].add_cards(gid, _full_cards(n_cards))
    for i in range(n_registered):
        db.execute("INSERT INTO accounts (email, login_password, status) VALUES (?,?,?)",
                   (f'reg{i}@example.com', 'pw', 'registered'))
    # imported 账号自带 email_verify_link（DB 收码），故 _hotmail_for_account 可注册它们
    for i in range(n_imported):
        db.execute("INSERT INTO accounts (email, email_password, email_verify_link, status) "
                   "VALUES (?,?,?,?)",
                   (f'imp{i}@example.com', 'ep', 'https://ruoanzhu.example/s?e=x', 'imported'))

    state = AppState(db, models)
    tracker = tracker_cls(db)
    state._register_one_account = tracker.register
    state._recharge_one_account = tracker.recharge

    original = cfg.concurrency.max_workers
    cfg.concurrency.max_workers = workers
    try:
        state.run_daily_pipeline(gid, login_password=None, captcha_api_key=None)
    finally:
        cfg.concurrency.max_workers = original

    status_counts = {}
    for row in db.fetchall("SELECT status, COUNT(*) c FROM accounts GROUP BY status"):
        status_counts[row['status']] = row['c']

    usable_left, _ = models['card_pool'].get_usable_cards_as_list(gid)

    result = {
        'status_counts': status_counts,
        'usable_cards_left': len(usable_left),
        'recharged_calls': list(tracker.recharged_calls),
        'registered_calls': list(tracker.registered_calls),
        'overlapping': list(tracker.overlapping),
        'peak': tracker.peak,
        'claims_left': len(state.account_registry.snapshot()),
        'in_flight_cards': len(state.payment_registry.in_flight_numbers()),
        'is_running': state.is_running,
        'parallel_mode': state.parallel_mode,
    }
    db.close()
    return result


def test_refill_registers_then_recharges_serially(no_browser):
    """0 可充 + 4 imported：全部注册→充值，最终都 recharged。"""
    r = _run_pipeline(1, n_registered=0, n_imported=4)
    assert r['status_counts'].get('recharged') == 4
    assert sorted(r['registered_calls']) == [f'imp{i}@example.com' for i in range(4)]
    # 每个账号恰好充值一次（注册转正后被领来充值）
    assert sorted(r['recharged_calls']) == [f'imp{i}@example.com' for i in range(4)]
    assert r['is_running'] is False
    assert r['peak'] == 1, "串行模式出现并发"


def test_recharged_not_charged_twice(no_browser):
    """去重：已 recharged 账号不再被充值。"""
    r = _run_pipeline(2, n_registered=3, n_imported=0)
    assert r['status_counts'].get('recharged') == 3
    # 每个 email 只出现一次
    assert len(r['recharged_calls']) == len(set(r['recharged_calls'])) == 3


def test_parallel_true_concurrency_no_overlap(no_browser):
    """2 worker 真并发，且同一账号不被并发处理。"""
    r = _run_pipeline(2, n_registered=6, n_imported=0)
    assert r['peak'] > 1, "并行模式没有真正并发"
    assert r['overlapping'] == [], f"同一账号被并发处理: {r['overlapping']}"
    assert r['status_counts'].get('recharged') == 6


def test_releases_all_runtime_claims(no_browser):
    """收尾干净：账号占用、支付卡占用全部释放，parallel_mode 复位。"""
    r = _run_pipeline(2, n_registered=2, n_imported=3)
    assert r['claims_left'] == 0, "账号占用泄漏"
    assert r['in_flight_cards'] == 0, "支付卡占用泄漏"
    assert r['parallel_mode'] is False, "parallel_mode 未复位"


def test_serial_and_parallel_same_ledger(no_browser):
    """并发不改变最终账面：注册数、充值数、终态一致。"""
    serial = _run_pipeline(1, n_registered=2, n_imported=3)
    parallel = _run_pipeline(3, n_registered=2, n_imported=3)
    assert serial['status_counts'].get('recharged') == parallel['status_counts'].get('recharged') == 5
    assert sorted(serial['recharged_calls']) == sorted(parallel['recharged_calls'])
    assert len(serial['registered_calls']) == len(parallel['registered_calls']) == 3


def test_failed_accounts_retried_next_round(no_browser):
    """失败账号跨轮循环使用：每个账号第一次失败（烧一张卡），下一轮重试后充成。

    旧行为下失败即永久入 done，一轮全败后卡池还有卡任务就提前结束——本测试
    锁死修复：以刷完卡为第一标准，只要有进展就开新一轮重试失败账号。"""
    r = _run_pipeline(1, n_registered=2, n_imported=0, n_cards=8,
                      tracker_cls=_FlakyOnceTracker)
    assert r['status_counts'].get('recharged') == 2, "失败账号未被下一轮重试充成"
    # 每个账号恰好被试了两次：第一轮失败 + 第二轮成功
    calls = sorted(r['recharged_calls'])
    assert calls == ['reg0@example.com'] * 2 + ['reg1@example.com'] * 2
    # 每次失败烧掉一张卡，其余卡保持可用
    assert r['usable_cards_left'] == 8 - 2
    assert r['is_running'] is False


def test_zero_progress_two_rounds_then_stop(no_browser):
    """连续两轮零进展（不付成、不动卡）才收敛——既不死循环，也不因一轮抖动早退。"""
    r = _run_pipeline(1, n_registered=2, n_imported=0, n_cards=8,
                      tracker_cls=_AlwaysFailTracker)
    # 每个账号恰好被试了两轮，之后判「账号已全部试尽」收敛
    calls = sorted(r['recharged_calls'])
    assert calls == ['reg0@example.com'] * 2 + ['reg1@example.com'] * 2
    assert r['status_counts'].get('recharged') is None
    # 卡池原样未动——证明收敛原因是账号试尽，而非卡耗尽
    assert r['usable_cards_left'] == 8
    assert r['is_running'] is False
    assert r['claims_left'] == 0 and r['in_flight_cards'] == 0
