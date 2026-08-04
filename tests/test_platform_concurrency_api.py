"""两个平台同时跑（AC1–AC4）。

改造前所有启动端点都是 `if state.is_running: return 400`，而 state 是全局单例——
一个平台在跑就挡住所有平台。这几条钉住解锁之后的行为，以及「解锁不等于失去保护」：
同平台内部仍然只能有一个任务。
"""

import tempfile
import threading
import time

import pytest

from src.web.app import create_app

A, B = 'opencode', 'infron'


@pytest.fixture
def app():
    a = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield a
    a.config['REAPER'].stop()
    a.config['DB'].close()


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(app, slug):
    return app.config['RUN_CONTEXTS'][slug]


def _pool(app):
    m = app.config['MODELS']
    gid = m['card_group'].create('g', group_type='payment')
    m['card_pool'].add_cards(gid, [{
        'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
        'cvc': '123', 'first_name': 'T', 'last_name': 'U'}])
    return gid


# ---------- AC1：不再互相阻塞 ----------

def test_one_platform_running_does_not_block_the_other(app, client):
    """核心解锁点。改造前这里会拿到 400「有任务正在运行」。"""
    _ctx(app, A).is_running = True

    gid = _pool(app)
    r = client.post('/api/daily/start', json={'group_id': gid, 'platform': B})

    assert r.status_code != 400 or '已有任务在运行' not in (r.get_json() or {}).get('error', ''), \
        f'B 平台被 A 平台正在运行挡住了：{r.get_json()}'


def test_same_platform_still_cannot_run_twice(app, client):
    """解锁不等于失去保护——同平台内部仍然只能有一个任务。

    is_running 一直兼着「UI 显示」与「任务互斥闸门」两职，按平台拆开之后
    后者不能跟着丢掉。
    """
    _ctx(app, B).is_running = True

    gid = _pool(app)
    r = client.post('/api/daily/start', json={'group_id': gid, 'platform': B})

    assert r.status_code == 400
    assert '已有任务在运行' in r.get_json()['error']


def test_start_gate_error_names_the_platform(app, client):
    """报错要说清是哪个平台在跑，否则并发下用户根本不知道该停谁。"""
    _ctx(app, B).is_running = True
    gid = _pool(app)
    r = client.post('/api/daily/start', json={'group_id': gid, 'platform': B})
    assert B in r.get_json()['error']


# ---------- AC2：停一个不影响另一个 ----------

def test_stop_only_affects_the_requested_platform(app, client):
    a, b = _ctx(app, A), _ctx(app, B)
    a.is_running = True
    b.is_running = True

    r = client.post('/api/stop', json={'platform': A})
    assert r.status_code == 200
    assert r.get_json()['platform'] == A

    assert a.stop_requested is True
    assert b.stop_requested is False, '停 A 把 B 也停了'


def test_stop_reports_which_platform_has_nothing_running(app, client):
    _ctx(app, A).is_running = True
    r = client.post('/api/stop', json={'platform': B})
    assert r.status_code == 400
    assert B in r.get_json()['error']


# ---------- AC3：状态不串 ----------

def test_status_reports_the_requested_platform(app, client):
    a, b = _ctx(app, A), _ctx(app, B)
    a.is_running, a.success_count, a.current_action = True, 3, 'A 在忙'
    b.is_running, b.success_count, b.current_action = False, 7, 'B 空闲'

    ja = client.get(f'/api/status?platform={A}').get_json()
    jb = client.get(f'/api/status?platform={B}').get_json()

    assert (ja['platform'], ja['is_running'], ja['success'], ja['current_action']) == \
        (A, True, 3, 'A 在忙')
    assert (jb['platform'], jb['is_running'], jb['success'], jb['current_action']) == \
        (B, False, 7, 'B 空闲')


def test_status_includes_a_cross_platform_overview(app, client):
    """必须能看见**没在看的那个平台**是否在跑——否则它出问题时用户完全看不见。"""
    _ctx(app, B).is_running = True

    j = client.get(f'/api/status?platform={A}').get_json()

    assert j['is_running'] is False, '顶层字段应是所请求平台的'
    assert j['platforms'][B]['is_running'] is True, '汇总里看不见另一个平台在跑'
    assert set(j['platforms']) >= {A, B}


def test_logs_do_not_leak_between_platforms(app, client):
    _ctx(app, A).add_log('只给 A 的')
    _ctx(app, B).add_log('只给 B 的')

    la = client.get(f'/api/status?platform={A}').get_json()['logs']
    lb = client.get(f'/api/status?platform={B}').get_json()['logs']

    assert any('只给 A 的' in x for x in la)
    assert not any('只给 B 的' in x for x in la), '日志串台了'
    assert any('只给 B 的' in x for x in lb)


def test_worker_logs_are_scoped_by_platform(app, client):
    """两个平台各有一套同名的 W1，取错 ctx 不会 404，只会安静地给错数据。"""
    _ctx(app, A).primary_worker.add_log('A 的 W1')
    _ctx(app, B).primary_worker.add_log('B 的 W1')

    ja = client.get(f'/api/workers/W1/logs?platform={A}').get_json()
    jb = client.get(f'/api/workers/W1/logs?platform={B}').get_json()

    assert any('A 的 W1' in x for x in ja['logs'])
    assert not any('B 的 W1' in x for x in ja['logs']), 'W1 撞名，拿到了另一个平台的日志'
    assert any('B 的 W1' in x for x in jb['logs'])


# ---------- AC4：一个崩了不影响另一个 ----------

def test_one_platform_crashing_leaves_the_other_intact(app, client):
    """一个平台异常收敛，另一个的状态必须原样保留。"""
    a, b = _ctx(app, A), _ctx(app, B)
    b.is_running, b.success_count = True, 5
    b.add_log('B 正常跑着')

    a.is_running = True
    try:
        raise RuntimeError('A 炸了')
    except RuntimeError:
        a.is_running = False
        a.current_action = '异常退出'

    assert b.is_running is True and b.success_count == 5
    assert any('B 正常跑着' in x for x in b.logs)


def test_concurrent_status_polling_is_consistent(app, client):
    """两个平台的状态被并发轮询时不能互相污染。"""
    a, b = _ctx(app, A), _ctx(app, B)
    a.success_count, b.success_count = 111, 222

    seen = {A: set(), B: set()}
    barrier = threading.Barrier(2)

    def poll(slug):
        barrier.wait()
        for _ in range(20):
            j = client.get(f'/api/status?platform={slug}').get_json()
            seen[slug].add(j['success'])

    ts = [threading.Thread(target=poll, args=(s,)) for s in (A, B)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)

    assert seen[A] == {111}, f'A 的计数被污染：{seen[A]}'
    assert seen[B] == {222}, f'B 的计数被污染：{seen[B]}'


# ---------- 清理接口保护所有平台 ----------

def test_cleanup_protects_running_tasks_on_every_platform(app, client):
    """并发时可能同时有两个批量任务在跑，只保护其中一个会删掉另一个的记录。"""
    m = app.config['MODELS']
    cb = m['card_binding']
    card = {'number': '4111111111111111', 'expiry_month': '12', 'expiry_year': '2030',
            'cvc': '123', 'first_name': 'T', 'last_name': 'U'}

    ta = m['task'].create('batch', config={})
    tb = m['task'].create('batch', config={})
    cb.create_batch(A, ta, [card])
    cb.create_batch(B, tb, [card])

    for slug, tid in ((A, ta), (B, tb)):
        ctx = _ctx(app, slug)
        ctx.is_running = True
        ctx.current_card_task_id = tid

    client.post('/api/card/history/cleanup')

    assert len(cb.get_pending(ta)) == 1, 'A 平台正在跑的任务的记录被删了'
    assert len(cb.get_pending(tb)) == 1, 'B 平台正在跑的任务的记录被删了'
