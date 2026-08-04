"""「开始充值」端到端：API 启动 → 注册 → 登录充值，一条链路跑通。

补的是既有两层测试之间的那道缝——它真的裂开过：

  - tests/test_daily_start_gate.py 打桩 run_daily_pipeline，只验证**门**；
  - tests/test_daily_pipeline.py 直接调 state.run_daily_pipeline，绕过**门**，只验证流水线。

两层各自全绿，合起来却跑不通：启动门要求账号有 login_password，而 imported 账号的
密码正是注册流程写回去的，于是「账号列表里只有刚导入的邮箱」这个场景在门口就 400 了
（2026-08-05 修复）。流水线的三段能力一直是好的，只是从没被 API 放进去过。

所以本文件**不打桩 run_daily_pipeline**，只打桩最外层的浏览器动作，从 POST 进、
断言三段都发生且顺序正确。任何一段被谁不小心切断，这里会红。

「登录」不是独立的一段：它在 _recharge_one_account 内部（登录成功才谈得上充值），
所以链路在本文件的可观测粒度上是 register → recharge 两个调用。
"""

import tempfile
import time

import pytest

from src.browser import driver as driver_module
from src.config import cfg
from src.web.app import create_app

PLATFORM = 'opencode'


@pytest.fixture
def no_browser(monkeypatch):
    """安全网：漏打桩会立刻炸出来，而不是静默起一个真实 Chrome。"""
    def _explode(*a, **kw):
        raise AssertionError("测试中不应创建真实浏览器——有函数漏打桩了")
    monkeypatch.setattr(driver_module, 'create_driver', _explode)
    monkeypatch.setattr(driver_module, 'create_driver_vanilla', _explode)


@pytest.fixture
def serial():
    """强制串行，让调用顺序可断言。

    两个都要覆盖：只改 max_workers 的话，config.yaml 里 platform_workers 给
    opencode 配的值会盖过它。
    """
    orig_max = cfg.concurrency.max_workers
    orig_per = dict(cfg.concurrency.platform_workers)
    cfg.concurrency.max_workers = 1
    cfg.concurrency.platform_workers = {}
    yield
    cfg.concurrency.max_workers = orig_max
    cfg.concurrency.platform_workers = orig_per


@pytest.fixture
def app():
    a = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield a
    a.config['REAPER'].stop()
    a.config['DB'].close()


class _Calls:
    """按调用顺序记录三段动作，并模拟它们对 DB 的状态推进。"""

    def __init__(self, db):
        self.db = db
        self.seq = []              # [('register', email), ('recharge', email), ...]

    def register(self, acct, worker=None, proxy=None):
        email = acct['email']
        self.seq.append(('register', email))
        # 注册成功写回 GitHub 密码——正是这个字段让账号在下一轮成为「可充值账号」，
        # 闭环全靠它。
        self.db.execute(
            "UPDATE accounts SET identity_status='registered', login_password='pw' "
            "WHERE email=?", (email,))
        return "registered", "ok"

    def recharge(self, email, login_password, **kw):
        self.seq.append(('recharge', email))
        assert login_password == 'pw', "充值拿到的应当是注册写回的那个密码"
        self.db.execute(
            "INSERT OR REPLACE INTO platform_accounts (platform, email, status) "
            "VALUES (?, ?, 'recharged')", (PLATFORM, email))
        return "success", ""


def _wait_idle(state, timeout=20):
    """等后台流水线线程收敛。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not state.is_running:
            return True
        time.sleep(0.05)
    return False


def test_imported_only_runs_register_then_recharge(app, no_browser, serial):
    """账号列表只有待导入邮箱时，「开始充值」要自己把注册那一段补上。"""
    db = app.config['DB']
    m = app.config['MODELS']
    gid = m['card_group'].create('g', group_type='payment')
    m['card_pool'].add_cards(gid, [{
        'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
        'cvc': '123', 'first_name': 'T', 'last_name': 'U',
        'country': 'United States', 'address': 'a', 'city': 'c',
        'state': 's', 'zip': '12345'}])
    # 唯一的账号：刚导入、没有 GitHub、没有登录密码，但带收码链接。
    db.execute("INSERT INTO accounts (email, email_password, email_verify_link, "
               "identity_status) VALUES (?,?,?,?)",
               ('imp0@example.com', 'ep', 'https://ruoanzhu.example/s?e=x', 'imported'))

    state = app.config['RUN_CONTEXTS'][PLATFORM]
    calls = _Calls(db)
    state._register_one_account = calls.register
    state._recharge_one_account = calls.recharge
    state._hotmail_map = {}        # 让 xlsx 存在与否不影响结论

    r = app.test_client().post('/api/daily/start',
                               json={'group_id': gid, 'platform': PLATFORM})

    assert r.status_code == 200, r.get_json()
    assert r.get_json()['registerable_accounts'] == 1
    assert _wait_idle(state), "流水线未在超时内收敛"

    kinds = [k for k, _ in calls.seq]
    assert kinds[0] == 'register', f"第一段必须是注册，实际调用序列: {calls.seq}"
    assert 'recharge' in kinds, f"注册后没有接上充值，实际调用序列: {calls.seq}"
    assert all(e == 'imp0@example.com' for _, e in calls.seq)

    row = db.fetchone("SELECT status FROM platform_accounts WHERE email=? AND platform=?",
                      ('imp0@example.com', PLATFORM))
    assert row and row['status'] == 'recharged'


def test_imported_without_verify_link_never_starts(app, no_browser, serial):
    """无收码数据的 imported 领不走，门就不该放行——放行只会空跑一轮。"""
    db = app.config['DB']
    m = app.config['MODELS']
    gid = m['card_group'].create('g', group_type='payment')
    m['card_pool'].add_cards(gid, [{
        'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
        'cvc': '123', 'first_name': 'T', 'last_name': 'U'}])
    db.execute("INSERT INTO accounts (email, identity_status) VALUES (?,?)",
               ('nolink@test.invalid', 'imported'))

    state = app.config['RUN_CONTEXTS'][PLATFORM]
    calls = _Calls(db)
    state._register_one_account = calls.register
    state._recharge_one_account = calls.recharge
    state._hotmail_map = {}

    r = app.test_client().post('/api/daily/start',
                               json={'group_id': gid, 'platform': PLATFORM})

    assert r.status_code == 400
    assert calls.seq == [], "被拒的请求不该跑出任何动作"
