"""HTTP 层的平台参数贯穿测试。

模型层的隔离已由 test_card_pool_bound / test_valid_card_invariant 等钉住，这里验的是
另一件事：**参数真的从请求一路传到了 SQL**。漏传一处的表现不是报错，而是安静地返回
另一个平台的数据——那种 bug 只有在两个平台都跑起来之后才会被发现，代价很高。
"""

import tempfile

import pytest

from src.web.app import create_app

OC = 'opencode'
OTHER = 'stubplatform'


@pytest.fixture
def client():
    app = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield app.test_client(), app.config['MODELS']
    app.config['REAPER'].stop()
    app.config['DB'].close()


def _card(number):
    return {'number': number, 'expiry_month': '12', 'expiry_year': '2030', 'cvc': '123',
            'first_name': 'T', 'last_name': 'U'}


@pytest.fixture
def pool(client):
    _, models = client
    gid = models['card_group'].create('g', group_type='payment')
    models['card_pool'].add_cards(gid, [_card('4111111111111111'),
                                        _card('4222222222222222')])
    return gid


def test_platforms_endpoint_lists_registry(client):
    c, _ = client
    body = c.get('/api/platforms').get_json()
    assert OC in [p['slug'] for p in body['data']]
    assert body['current'] == OC


def test_card_pool_list_is_scoped_by_platform(client, pool):
    """一张卡在 opencode 判废后，带另一个平台参数取列表时它仍是可用的。"""
    c, models = client
    models['card_pool'].mark_invalid_by_number(OC, '4111111111111111')

    oc = c.get(f'/api/card-pool/{pool}?platform={OC}&bucket=invalid').get_json()
    other = c.get(f'/api/card-pool/{pool}?platform={OTHER}&bucket=invalid').get_json()

    assert [r['card_number'] for r in oc['data']] == ['4111111111111111']
    assert other['data'] == [], 'invalid 不该跨平台泄漏到别的平台的无效桶'
    assert oc['platform'] == OC and other['platform'] == OTHER


def test_card_pool_bucket_counts_are_scoped(client, pool):
    c, models = client
    models['card_pool'].mark_invalid_by_number(OC, '4111111111111111')

    oc = c.get(f'/api/card-pool/{pool}?platform={OC}').get_json()['buckets']
    other = c.get(f'/api/card-pool/{pool}?platform={OTHER}').get_json()['buckets']

    assert (oc['invalid'], oc['unverified']) == (1, 1)
    assert (other['invalid'], other['unverified']) == (0, 2)


def test_valid_cards_list_is_scoped(client):
    c, models = client
    models['valid_card'].record(OC, _card('4111111111111111'),
                                source_type='payment', source_email='a@x.com')

    oc = c.get(f'/api/valid-cards?platform={OC}').get_json()
    other = c.get(f'/api/valid-cards?platform={OTHER}').get_json()

    assert oc['total'] == 1 and oc['summary']['payment_count'] == 1
    assert other['total'] == 0 and other['summary']['payment_count'] == 0


def test_accounts_list_shows_current_platform_state(client):
    """账号列表：身份字段共用，平台字段随 platform 参数变。"""
    c, models = client
    models['account'].upsert('a@x.com', login_password='gh-pw', identity_status='registered')
    models['platform_account'].update_status(OC, 'a@x.com', 'recharged')
    models['platform_account'].update_balance(OC, 'a@x.com', 20.0)

    oc = c.get(f'/api/accounts?platform={OC}').get_json()['data'][0]
    other = c.get(f'/api/accounts?platform={OTHER}').get_json()['data'][0]

    assert oc['identity_status'] == other['identity_status'] == 'registered'
    assert oc['password'] == other['password'] == 'gh-pw'      # GitHub 密码共用
    assert oc['platform_status'] == 'recharged'
    assert other['platform_status'] == '', '未在该平台开通时应为空'
    assert oc['credits_balance'] == 20.0
    assert other['credits_balance'] is None


def test_pipeline_start_requires_platform(client, pool):
    """流水线启动缺平台参数直接 400——猜错平台会把数据写到别处。"""
    c, _ = client
    for url in ('/api/daily/start', '/api/daily/subscribe/start'):
        r = c.post(url, json={'group_id': pool, 'platform': ''})
        assert r.status_code == 400, url
        assert '平台' in r.get_json()['error']


def test_deleting_account_clears_all_platform_rows(client):
    """删身份时连带清掉它在所有平台的账号行，不留孤儿。"""
    c, models = client
    models['account'].upsert('a@x.com', identity_status='registered')
    models['platform_account'].update_status(OC, 'a@x.com', 'recharged')
    models['platform_account'].update_status(OTHER, 'a@x.com', 'pending')

    c.post('/api/accounts/delete', json={'emails': ['a@x.com']})

    assert models['platform_account'].get(OC, 'a@x.com') is None
    assert models['platform_account'].get(OTHER, 'a@x.com') is None


def test_single_account_recharge_applies_the_platform(client):
    """单账号充值端点必须把 platform 落到 AppState。

    接第二个平台时才发现的缺陷：端点收了 platform 参数却从没应用，
    `_recharge_one_account` 读的是 `AppState.platform`，于是不管传什么都跑 opencode。
    现象很隐蔽——日志里是另一个平台的登录流程，而请求明明指定了 infron。

    把 _recharge_one_account 打桩，只验参数落地，不真的起浏览器。
    """
    import time

    c, models = client
    models['account'].upsert('a@x.com', login_password='pw', identity_status='registered')
    state = c.application.config['APP_STATE']

    seen = {}

    def _stub(email, login_password, payment_group_id=None, **kw):
        seen['platform'] = state.platform
        return 'failed', 'stubbed'

    state._recharge_one_account = _stub

    r = c.post('/api/accounts/recharge',
               json={'email': 'a@x.com', 'platform': 'infron', 'payment_group_id': 1})
    assert r.status_code == 200, r.get_json()

    for _ in range(50):
        if 'platform' in seen:
            break
        time.sleep(0.1)

    assert seen.get('platform') == 'infron', \
        f"端点未把 platform 落到 AppState，充值时看到的是 {seen.get('platform')!r}"


def test_recharge_clears_stale_stop_flag(client):
    """新充值必须清掉上一轮残留的 stop_requested，否则一启动就自杀。

    实跑撞到的：上一次任务被停止后 stop_requested 一直是 True（worker 抛
    InterruptedError 时也会置它），下一次充值在第一个检查点就中断，日志只留
    「收到停止请求，正在中断」——看起来像用户又点了停止，而不是上一轮的残留。
    三条流水线入口都成对复位了，只有这个端点漏了。
    """
    import time

    c, models = client
    models['account'].upsert('a@x.com', login_password='pw', identity_status='registered')
    state = c.application.config['APP_STATE']
    state.stop_requested = True          # 模拟上一轮停止后的残留

    seen = {}

    def _stub(email, login_password, payment_group_id=None, **kw):
        seen['stop'] = state.stop_requested
        return 'failed', 'stubbed'

    state._recharge_one_account = _stub

    r = c.post('/api/accounts/recharge',
               json={'email': 'a@x.com', 'platform': 'infron', 'payment_group_id': 1})
    assert r.status_code == 200, r.get_json()

    for _ in range(50):
        if 'stop' in seen:
            break
        time.sleep(0.1)

    assert seen.get('stop') is False, '残留的停止标志没被清掉，本次充值会立刻中断'
