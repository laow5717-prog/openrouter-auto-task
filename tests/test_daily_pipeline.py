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
from src.models.platform_account import PlatformAccountModel
from src.models.card_group import CardGroupModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.card_pool import CardPoolModel
from src.models.database import Database
from src.models.proxy import ProxyModel
from src.models.recharge_log import RechargeLogModel
from src.models.valid_card import ValidCardModel
from src.web.app import AppState, build_models


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
                "UPDATE accounts SET identity_status='registered', login_password='pw' "
                "WHERE email=?", (email,))
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
            self._mark_recharged(email)
            return "success", ""
        finally:
            self._exit(email)

    def _mark_recharged(self, email):
        """充值成功是**平台层**状态，写 platform_accounts 而非 accounts。"""
        self.db.execute(
            "INSERT OR REPLACE INTO platform_accounts (platform, email, status) "
            "VALUES ('opencode', ?, 'recharged')", (email,))


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
                # 烧掉一张可用卡：可选卡集合变化 → 轮转判定视为「有进展」。
                # invalid 现在是**平台状态**，写 card_platform_state 而不是 card_pool。
                self.db.execute(
                    "INSERT OR REPLACE INTO card_platform_state (card_number, platform, status) "
                    "SELECT cp.card_number, 'opencode', 'invalid' FROM card_pool cp "
                    "LEFT JOIN card_platform_state cps "
                    "  ON cps.card_number = cp.card_number AND cps.platform = 'opencode' "
                    "WHERE COALESCE(cp.status,'') != 'expired' "
                    "  AND COALESCE(cps.status,'') NOT IN ('invalid','bound') LIMIT 1")
                return "failed", "declined"
            self._mark_recharged(email)
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
    # 用生产同一份构造，而不是在这里手抄一份：抄的那份漏掉新加的模型时，
    # 失败现象是流水线内部一句「严重错误: 'settings'」被 except 吞掉，
    # 表面上只看到「一个账号都没充成」，与真正的原因隔着十万八千里。
    models = build_models(db)
    gid = models['card_group'].create('pay-group', 'pay')
    models['card_pool'].add_cards(gid, _full_cards(n_cards))
    for i in range(n_registered):
        db.execute("INSERT INTO accounts (email, login_password, identity_status) "
                   "VALUES (?,?,?)", (f'reg{i}@example.com', 'pw', 'registered'))
    # imported 账号自带 email_verify_link（DB 收码），故 _hotmail_for_account 可注册它们
    for i in range(n_imported):
        db.execute("INSERT INTO accounts (email, email_password, email_verify_link, "
                   "identity_status) VALUES (?,?,?,?)",
                   (f'imp{i}@example.com', 'ep', 'https://ruoanzhu.example/s?e=x', 'imported'))

    state = AppState(db, models)
    tracker = tracker_cls(db)
    state._register_one_account = tracker.register
    state._recharge_one_account = tracker.recharge

    # 并发度按平台取（cfg.concurrency.workers_for），所以**两个都要覆盖**：
    # 只改 max_workers 的话，config.yaml 里 platform_workers 给 opencode 配的值
    # 会盖过它，测试想要的串行/并行就控制不住了。
    orig_max = cfg.concurrency.max_workers
    orig_per = dict(cfg.concurrency.platform_workers)
    cfg.concurrency.max_workers = workers
    cfg.concurrency.platform_workers = {}
    try:
        state.run_daily_pipeline('opencode', gid, login_password=None, captcha_api_key=None)
    finally:
        cfg.concurrency.max_workers = orig_max
        cfg.concurrency.platform_workers = orig_per

    # 账号最终状态 = 平台状态优先（recharged 等），没有平台行则看身份状态。
    # 两层拆分后「一个账号处于什么状态」不再是单列，这里合成出旧口径供断言复用。
    status_counts = {}
    for row in db.fetchall(
            "SELECT COALESCE(NULLIF(pa.status,''), a.identity_status) s, COUNT(*) c "
            "FROM accounts a "
            "LEFT JOIN platform_accounts pa ON pa.email = a.email AND pa.platform='opencode' "
            "GROUP BY s"):
        status_counts[row['s']] = row['c']

    usable_left, _ = models['card_pool'].get_usable_cards_as_list('opencode', gid)

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
    emails = [f'imp{i}@example.com' for i in range(4)]
    assert r['status_counts'].get('recharged') == 4
    assert sorted(r['registered_calls']) == emails
    # 每个账号被充两次：注册转正后作为新账号充一次，四个都跑完后作为回退池再复用一次
    # （回退池的语义见 test_recharged_accounts_are_reused_at_most_once）。
    assert sorted(r['recharged_calls']) == sorted(emails * 2)
    assert r['is_running'] is False
    assert r['peak'] == 1, "串行模式出现并发"


def test_recharged_accounts_are_reused_at_most_once(no_browser):
    """已充值账号可被复用，但每次运行至多一次。

    旧行为是 recharged 永久退出轮转（「一个账号只充一笔」）。现在它的含义变成
    「有一些余额」，没到 balance_cap 就还能加码——但**每次运行只给一次**：

      - 够用：recharge_account 那一次会话内部就会连充到上限才收手，再给第二次
        会话没有增量；
      - 必须：判据里的 DB 余额可能是 NULL（balance_after 读不到时不落库），
        不设这道闸的话该账号永远满足「未达上限」，被一轮轮反复领走、任务不收敛。
    """
    r = _run_pipeline(2, n_registered=3, n_imported=0)
    assert r['status_counts'].get('recharged') == 3

    calls = r['recharged_calls']
    per_email = {e: calls.count(e) for e in set(calls)}
    assert set(per_email) == {f'reg{i}@example.com' for i in range(3)}
    assert all(n == 2 for n in per_email.values()), \
        f"每个账号应为「新账号一次 + 复用一次」，实际 {per_email}"


def test_reuse_only_kicks_in_after_new_accounts_are_exhausted(no_browser):
    """回退池排在最后：只要还有没跑过的账号，就不回头加码老账号。

    顺序是有意的——先把钱铺开到更多账号上。余额集中在少数号里，一个被封就全赔进去。
    """
    r = _run_pipeline(1, n_registered=2, n_imported=0)
    calls = r['recharged_calls']

    assert sorted(calls[:2]) == ['reg0@example.com', 'reg1@example.com'], \
        f"前两次应把两个新账号各跑一遍，实际 {calls[:2]}"
    assert len(calls) == 4, f"随后才轮到复用，共 4 次，实际 {calls}"


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
    # 每个账号被试三次：第一轮失败 → 第二轮成功 → 转 recharged 后作为回退池复用一次。
    # 前两次是本用例的主题（跨轮重试），第三次来自「新账号领完才复用老账号」。
    calls = sorted(r['recharged_calls'])
    assert calls == ['reg0@example.com'] * 3 + ['reg1@example.com'] * 3
    # 只有每个账号的**第一次**失败烧卡（_FlakyOnceTracker 的语义），故仍是 2 张
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
