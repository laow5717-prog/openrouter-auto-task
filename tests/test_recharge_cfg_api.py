"""充值策略从 UI 透传到流水线（AC13 / AC14）。

金额区间与余额上限由前端按次传入，API 层负责校验并构造一个**新的** RechargeConfig。
两件事必须成立，否则错误极难查：

  1. 非法区间要 400 报错，不能静默夹紧——用户配的是 20-100、实际跑的是别的，
     比直接报错难查得多。
  2. 覆盖不能污染全局 cfg.recharge。它是进程级单例，两个平台并发跑时原地改
     会让后启动的那个把前一个的参数覆盖掉，且不报错、不留日志。
"""

import tempfile

import pytest

from src.config import cfg
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
def started(app):
    """一个能通过启动门的账号 + 卡池分组，返回 group_id。

    run_daily_pipeline 会被替换成一个只记参数的桩——本文件盯的是参数怎么传进去，
    不是流水线跑得对不对（那是 test_daily_pipeline.py 的事）。
    """
    m = app.config['MODELS']
    gid = m['card_group'].create('g', group_type='payment')
    m['card_pool'].add_cards(gid, [{
        'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
        'cvc': '123', 'first_name': 'T', 'last_name': 'U'}])
    m['account'].upsert('a@x.com', login_password='pw', identity_status='registered')

    captured = {}
    state = app.config['RUN_CONTEXTS'][PLATFORM]

    def _fake_pipeline(*args, **kw):
        captured['args'] = args
        captured['kw'] = kw

    state.run_daily_pipeline = _fake_pipeline
    return gid, captured


def _start(client, gid, **body):
    return client.post('/api/daily/start',
                       json={'group_id': gid, 'platform': PLATFORM, **body})


def _cfg_of(captured):
    """run_daily_pipeline 用位置参数起线程，recharge_cfg 是第 6 个。"""
    return captured['args'][5]


def test_range_reaches_the_pipeline(client, started):
    gid, captured = started
    r = _start(client, gid, amount_min=30, amount_max=60, balance_cap=150)

    assert r.status_code == 200
    got = _cfg_of(captured)
    assert (got.amount_min, got.amount_max) == (30, 60)
    assert got.balance_cap == 150.0


def test_response_echoes_the_effective_policy(client, started):
    """回显实际生效的值——用户配的和跑的是不是同一套，应当一眼可见。"""
    gid, _captured = started
    body = _start(client, gid, amount_min=25, amount_max=75, balance_cap=99).get_json()

    assert body['amount_min'] == 25
    assert body['amount_max'] == 75
    assert body['balance_cap'] == 99.0


def test_missing_fields_fall_back_to_config_defaults(client, started):
    """AC14：不传任何策略字段时用 cfg.recharge，流程不报错。"""
    gid, captured = started
    assert _start(client, gid).status_code == 200

    got = _cfg_of(captured)
    assert got.amount_min == cfg.recharge.amount_min
    assert got.amount_max == cfg.recharge.amount_max


def test_only_one_bound_given_keeps_the_other_from_config(client, started):
    """只改下界不该造出一个倒挂区间。"""
    gid, captured = started
    r = _start(client, gid, amount_min=10)

    assert r.status_code == 200
    got = _cfg_of(captured)
    assert got.amount_min == 10
    assert got.amount_max == cfg.recharge.amount_max


def test_inverted_range_is_rejected(client, started):
    gid, captured = started
    r = _start(client, gid, amount_min=90, amount_max=30)

    assert r.status_code == 400
    assert '区间' in r.get_json()['error']
    assert 'args' not in captured, '校验没过就不该起流水线'


def test_out_of_bound_range_is_rejected(client, started):
    gid, _captured = started
    assert _start(client, gid, amount_min=0, amount_max=50).status_code == 400
    assert _start(client, gid, amount_min=20, amount_max=99999).status_code == 400


def test_non_numeric_values_are_rejected(client, started):
    gid, _captured = started
    r = _start(client, gid, amount_min='abc', amount_max=50)

    assert r.status_code == 400
    assert '数字' in r.get_json()['error']


def test_non_positive_balance_cap_is_rejected(client, started):
    gid, _captured = started
    r = _start(client, gid, balance_cap=0)

    assert r.status_code == 400
    assert '余额上限' in r.get_json()['error']


def test_overrides_never_mutate_the_global_config(client, started):
    """并发下最危险的一条：原地改全局会让两个平台互相覆盖参数。"""
    gid, _captured = started
    before = (cfg.recharge.amount_min, cfg.recharge.amount_max, cfg.recharge.balance_cap)

    _start(client, gid, amount_min=41, amount_max=42, balance_cap=43)

    assert (cfg.recharge.amount_min, cfg.recharge.amount_max,
            cfg.recharge.balance_cap) == before


def test_single_account_recharge_accepts_the_same_fields(app, client):
    """单账号手动充值走同一套解析，两处不能漂移。"""
    m = app.config['MODELS']
    m['account'].upsert('b@x.com', login_password='pw', identity_status='registered')

    r = client.post('/api/accounts/recharge',
                    json={'email': 'b@x.com', 'platform': PLATFORM,
                          'amount_min': 90, 'amount_max': 30})

    assert r.status_code == 400
    assert '区间' in r.get_json()['error']
