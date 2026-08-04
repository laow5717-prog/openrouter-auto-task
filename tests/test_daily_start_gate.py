"""每日充值任务启动门的账号判定。

盯的是一件事：**门的判据必须与流水线领账号的判据一致**。

不一致有两种坏法，都很难查：
  - 门比流水线严 —— 「账号列表里只有刚导入的邮箱」这个最常见的开局场景直接 400，
    而流水线的补号环节（注册→登录→充值）完全跑得动，能力等于没接（本文件的起因）；
  - 门比流水线松 —— 任务起得来，但流水线一个账号都领不走，一轮空跑就收敛，
    用户看到「启动成功但什么都没干」。

imported 账号**没有 login_password**（那是注册流程写回去的），所以它不能走
`_usable` 那条判据；能不能领走取决于有没有收码数据（DB 的 email_verify_link
或 hotmail.xlsx 命中）。这两点是下面用例的全部内容。
"""

import tempfile

import pytest

from src.web.app import create_app

PLATFORM = 'opencode'


@pytest.fixture
def app():
    a = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield a
    a.config['REAPER'].stop()
    a.config['DB'].close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def group(app):
    """一个有可选卡的分组 + 一个只记参数的流水线桩，返回 (group_id, captured)。

    账号刻意不建——每个用例自己决定账号列表长什么样，那正是被测的东西。
    """
    m = app.config['MODELS']
    gid = m['card_group'].create('g', group_type='payment')
    m['card_pool'].add_cards(gid, [{
        'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
        'cvc': '123', 'first_name': 'T', 'last_name': 'U'}])

    captured = {}
    state = app.config['RUN_CONTEXTS'][PLATFORM]
    state.run_daily_pipeline = lambda *a, **kw: captured.setdefault('args', a)
    # hotmail.xlsx 是否存在因机器而异，让它对测试不可见：本文件所有用例都只靠
    # email_verify_link 这条路径成立，xlsx 命中与否不该影响结论。
    state._hotmail_map = {}
    return gid, captured


def _start(client, gid):
    return client.post('/api/daily/start',
                       json={'group_id': gid, 'platform': PLATFORM})


def test_imported_with_verify_link_can_start(client, app, group):
    """只有待注册账号也能起任务——流水线会先给它注册 GitHub 再充值。"""
    gid, captured = group
    app.config['MODELS']['account'].upsert(
        'newbie@x.com', identity_status='imported',
        email_verify_link='https://ruoanzhu.example/mail/abc')

    r = _start(client, gid)

    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['registerable_accounts'] == 1
    assert body['accounts'] == 0          # 没有登录密码，不算可充值账号
    assert 'args' in captured             # 流水线确实被起起来了


def test_imported_without_verify_link_is_not_counted(client, app, group):
    """无收码数据的 imported 不算数——流水线领不走它，放行只会空跑一轮。"""
    gid, _ = group
    app.config['MODELS']['account'].upsert(
        'nolink@test.invalid', identity_status='imported')

    r = _start(client, gid)

    assert r.status_code == 400
    assert '待注册' in r.get_json()['error']


def test_no_usable_account_at_all_is_rejected(client, group):
    """三类账号全空仍然拒绝，且文案要把待注册这一类也说清楚。"""
    gid, captured = group

    r = _start(client, gid)

    assert r.status_code == 400
    err = r.get_json()['error']
    assert '待注册' in err and '可充值' in err
    assert 'args' not in captured


def test_registered_account_path_unchanged(client, app, group):
    """放宽只加一类，原先能启动的组合行为不变。"""
    gid, captured = group
    app.config['MODELS']['account'].upsert(
        'old@x.com', login_password='pw', identity_status='registered')

    r = _start(client, gid)

    assert r.status_code == 200
    body = r.get_json()
    assert body['accounts'] == 1
    assert body['registerable_accounts'] == 0
    assert 'args' in captured
