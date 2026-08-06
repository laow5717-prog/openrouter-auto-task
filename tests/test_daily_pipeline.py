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

    # 每次成功充值记账的金额。**必须落进 recharge_logs**：_reusable_recharged 用
    # 「DB 余额 + 本次运行已充金额」判断账号有没有到 balance_cap，而这个桩不写
    # credits_balance（模拟 balance_after 读不到），于是累计金额是唯一推进项。
    # 桩若不记账，账号的有效余额恒为 0、永远满足「未达上限」，被无限重领——
    # 表现是整个测试文件挂死，不是断言失败。踩过一次，别改回去。
    AMOUNT = 20

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
        """充值成功是**平台层**状态，写 platform_accounts 而非 accounts。

        刻意**不写 credits_balance**：真实环境里 balance_after 常读不到
        （infron 常态、opencode 偶发），update_balance 遇 None 直接 return。
        余额留空正是复用闸最难的那个输入，测试要盯的就是它。
        """
        self.db.execute(
            "INSERT OR REPLACE INTO platform_accounts (platform, email, status) "
            "VALUES ('opencode', ?, 'recharged')", (email,))
        self._log_success(email)

    def _log_success(self, email):
        """记一笔成功充值。生产里由 registration.recharge_account 写，桩必须同样写——
        它是 _reusable_recharged 判断「本次运行已充多少」的唯一账本。"""
        self.db.execute(
            "INSERT INTO recharge_logs (platform, email, card_display, amount, status) "
            "VALUES ('opencode', ?, '4111000000000000', ?, 'success')",
            (email, self.AMOUNT))


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
    """充值永远失败且不动卡池（模拟登录类故障）——验证连续两轮完全空转后收敛，不死循环。"""

    def recharge(self, email, login_password, **kw):
        self._enter(email)
        try:
            with self.lock:
                self.recharged_calls.append(email)
            threading.Event().wait(self.delay)
            return "failed", "login error"
        finally:
            self._exit(email)


class _AlwaysFailBurningCardsTracker(_Tracker):
    """每次充值都失败，且每次都烧掉一张卡——「刷完卡池才停」的最小复现。

    这个 tracker 的历史值得留着：2026-08-05 它是「不收敛 bug」的复现（2 个账号反复
    失败、跑到第 113 轮仍在打转），当天的修法是让烧卡不算进展、两轮零进展即收敛。
    那个修法在 08-06 翻到另一头——成功付款 0 次时两轮就停，卡池里还剩 2596 张没试。
    用户要的是刷完卡池，判据遂改回「卡集合有增减都算进展」，本 tracker 的期望行为
    随之从「有限轮内收敛」变成「一直跑到卡耗尽」。同一段代码两次相反的期望，
    都由现场证据定，不是反复无常。
    """

    def recharge(self, email, login_password, **kw):
        self._enter(email)
        try:
            with self.lock:
                self.recharged_calls.append(email)
            threading.Event().wait(self.delay)
            self.db.execute(
                "INSERT OR REPLACE INTO card_platform_state (card_number, platform, status) "
                "SELECT cp.card_number, 'opencode', 'invalid' FROM card_pool cp "
                "LEFT JOIN card_platform_state cps "
                "  ON cps.card_number = cp.card_number AND cps.platform = 'opencode' "
                "WHERE COALESCE(cp.status,'') != 'expired' "
                "  AND COALESCE(cps.status,'') NOT IN ('invalid','bound') LIMIT 1")
            return "failed", "declined"
        finally:
            self._exit(email)


@pytest.fixture
def no_browser(monkeypatch):
    def _explode(*a, **kw):
        raise AssertionError("测试中不应创建真实浏览器——有函数漏打桩了")
    monkeypatch.setattr(driver_module, 'create_driver', _explode)
    monkeypatch.setattr(driver_module, 'create_driver_vanilla', _explode)


def _run_pipeline(workers, n_registered=0, n_imported=4, n_cards=16, tracker_cls=_Tracker,
                  balance_cap=60):
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
    orig_cap = cfg.recharge.balance_cap
    cfg.concurrency.max_workers = workers
    cfg.concurrency.platform_workers = {}
    # 余额上限决定一个账号能被复用几次（cap / _Tracker.AMOUNT）。默认 200 意味着
    # 每账号 10 笔，测试要跑很久；这里压到 60 = 3 笔，行为一致但快得多。
    cfg.recharge.balance_cap = balance_cap
    try:
        state.run_daily_pipeline('opencode', gid, login_password=None, captcha_api_key=None)
    finally:
        cfg.concurrency.max_workers = orig_max
        cfg.concurrency.platform_workers = orig_per
        cfg.recharge.balance_cap = orig_cap

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
    # 每个账号被充到 balance_cap 为止：cap 60 / 每笔 20 = 3 笔
    # （复用语义见 test_recharged_accounts_reused_until_balance_cap）。
    assert sorted(r['recharged_calls']) == sorted(emails * 3)
    assert r['is_running'] is False
    assert r['peak'] == 1, "串行模式出现并发"


def test_recharged_accounts_reused_until_balance_cap(no_browser):
    """已充值账号持续参与轮转，直到累计充值达到 balance_cap 才出局。

    旧行为是「每次运行至多复用一次」——余额 $20、上限 $200 的账号一轮只能充一笔，
    钱铺不开。那道次数闸存在的真正理由是**防死循环**：判据里的 DB 余额可能是 NULL
    （balance_after 读不到时 update_balance 直接 return），只看它的话账号永远满足
    「未达上限」，被一轮轮反复领走、任务不收敛。

    现在改由金额自己挡：有效余额 = DB 余额 + **本次运行已充金额**（recharge_logs 聚合）。
    每成功一笔就推进 AMOUNT，最多 ceil(cap / AMOUNT) 次必然越过 cap。次数闸遂可撤掉。

    本用例的桩刻意不写 credits_balance（模拟余额读不到），所以推进全靠累计金额——
    这正是最难的那条路径。它同时覆盖「DB 余额停在旧值不更新」，两者是同一条路径。
    """
    r = _run_pipeline(2, n_registered=3, n_imported=0, balance_cap=60)
    assert r['status_counts'].get('recharged') == 3

    calls = r['recharged_calls']
    per_email = {e: calls.count(e) for e in set(calls)}
    assert set(per_email) == {f'reg{i}@example.com' for i in range(3)}
    expected = 60 // _Tracker.AMOUNT
    assert all(n == expected for n in per_email.values()), \
        f"每个账号应充到 cap（{expected} 笔）才出局，实际 {per_email}"


def test_reuse_scales_with_balance_cap(no_browser):
    """复用次数由 balance_cap 决定，而不是某个写死的次数。

    与上一个用例配对：cap 翻倍，笔数就翻倍。若哪天有人偷偷加回次数闸，
    这条会立刻红——上一条不会（它可能恰好等于那个次数）。
    """
    r = _run_pipeline(1, n_registered=1, n_imported=0, balance_cap=100)
    assert r['recharged_calls'].count('reg0@example.com') == 100 // _Tracker.AMOUNT


def test_reusable_and_payable_are_claimed_in_one_tier(no_browser):
    """余额未满的老账号与可充值新账号**同档**领取，待注册 imported 排其后。

    曾经老账号排在 imported 之后，于是库里几十个待注册账号会把它饿死——worker
    一直在注册，余额 $20 的号几乎永远轮不到。而注册耗时、还容易被 GitHub flag，
    现成账号优先反而更稳。

    断言方式：给 1 个 registered + 2 个 imported。若老账号仍排在 imported 之后，
    reg0 的第二笔必然要等两个 imported 都注册完；同档则不必。
    """
    r = _run_pipeline(1, n_registered=1, n_imported=2, balance_cap=60)
    calls = r['recharged_calls']
    # reg0 充满 cap 需要 3 笔，且这 3 笔应在 imp1 被注册之前就跑完
    # （现成账号优先，注册排最后）。
    first_reg0_run = [c for c in calls[:3]]
    assert first_reg0_run == ['reg0@example.com'] * 3, \
        f"现成账号应连续充到 cap 再去注册，实际前三次 {first_reg0_run}"
    assert len(r['registered_calls']) == 2, "两个 imported 最终都要被注册"


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
    # 每个账号：第一轮失败 → 第二轮成功（+20）→ 作为余额未满的账号继续充到 cap 60，
    # 即再来 2 笔。共 1 次失败 + 3 次成功 = 4 次。
    # 前两次是本用例的主题（跨轮重试），后两次来自余额未达上限的持续复用。
    calls = sorted(r['recharged_calls'])
    assert calls == ['reg0@example.com'] * 4 + ['reg1@example.com'] * 4
    # 只有每个账号的**第一次**失败烧卡（_FlakyOnceTracker 的语义），故仍是 2 张
    assert r['usable_cards_left'] == 8 - 2
    assert r['is_running'] is False


def test_burning_cards_keeps_running_until_pool_is_exhausted(no_browser):
    """充值全失败但每轮都在烧卡时，任务必须**一直跑到卡耗尽**才停。

    任务的第一标准是刷完卡池，剩多少张不该由轮数来裁决。08-06 的现场正是反例：
    成功付款 0 次，两轮零进展即收敛，卡池里还剩 2596 张从没被试过。

    终止性不靠轮数上界，靠卡集合有限且单调消耗——每次失败烧掉一张，16 张必然见底，
    届时由「分组可选卡已耗尽」收敛。所以这里断言的是「卡被烧光」而非「轮数很小」。
    """
    r = _run_pipeline(1, n_registered=2, n_imported=0, n_cards=16,
                      tracker_cls=_AlwaysFailBurningCardsTracker)
    calls = r['recharged_calls']
    assert r['usable_cards_left'] == 0, \
        f"应一直刷到卡耗尽才收敛，实际还剩 {r['usable_cards_left']} 张"
    # 每次充值恰好烧一张，故尝试次数不少于卡数。旧逻辑（烧卡不算进展）下这里会在
    # 4~6 次后就停，留下十来张没试的卡。
    assert len(calls) >= 16, f"卡没被刷完，实际充值尝试 {len(calls)} 次: {calls}"
    assert r['status_counts'].get('recharged') is None, "全失败不该有账号变 recharged"
    assert r['is_running'] is False
    assert r['claims_left'] == 0 and r['in_flight_cards'] == 0


def test_idle_two_rounds_then_stop(no_browser):
    """连续两轮**完全空转**（不付成、一张卡也没动）才收敛。

    这是唯一还会提前停的情况，也是必须保留的：一张卡都没消耗意味着流程根本没走到
    试卡环节（登录挂了/环境起不来），继续轮转既不会有不同结果，也永远到不了
    「卡耗尽」那个收敛点。容忍一轮是为了不被瞬时抖动误停。
    """
    r = _run_pipeline(1, n_registered=2, n_imported=0, n_cards=8,
                      tracker_cls=_AlwaysFailTracker)
    # 每个账号恰好被试了两轮，之后判「完全空转」收敛
    calls = sorted(r['recharged_calls'])
    assert calls == ['reg0@example.com'] * 2 + ['reg1@example.com'] * 2
    assert r['status_counts'].get('recharged') is None
    # 卡池原样未动——证明收敛原因是空转，而非卡耗尽
    assert r['usable_cards_left'] == 8
    assert r['is_running'] is False
    assert r['claims_left'] == 0 and r['in_flight_cards'] == 0


def test_idle_counter_resets_after_a_round_that_burned_cards(no_browser):
    """空转计数只看「最近连续」，烧过卡的那一轮必须把它清零。

    没有这条，一次早期抖动加上后来的一轮空转就会凑够 2 轮把任务停掉，而中间那些
    正在烧卡的轮次全被无视——恰恰是本次改动要根除的「卡还剩一堆就收工」。
    """
    burned = 4

    class _BurnThenIdle(_AlwaysFailBurningCardsTracker):
        """前 burned 次失败各烧一张卡，之后转为纯失败不动卡池。"""

        def recharge(self, email, login_password, **kw):
            with self.lock:
                n = len(self.recharged_calls)
            if n < burned:
                return super().recharge(email, login_password, **kw)
            return _AlwaysFailTracker.recharge(self, email, login_password, **kw)

    r = _run_pipeline(1, n_registered=2, n_imported=0, n_cards=16,
                      tracker_cls=_BurnThenIdle)
    calls = r['recharged_calls']
    # 烧卡阶段每轮都算进展，计数持续清零；停下来只能是后面那两轮空转所致。
    assert len(calls) >= burned + 4, \
        f"烧卡阶段被提前打断，实际充值尝试 {len(calls)} 次: {calls}"
    assert r['usable_cards_left'] == 16 - burned, "只该少掉烧掉的那几张"
    assert r['is_running'] is False
    assert r['claims_left'] == 0 and r['in_flight_cards'] == 0
