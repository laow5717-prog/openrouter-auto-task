"""worker 相关 API 的契约测试。

重点是**向后兼容**：老前端不改也要能用，所以 /api/status 的顶层字段一个不能少，
/video_feed 不带参数也要能出图。
"""

import tempfile

import pytest

from src.web.app import create_app, gen_frames


@pytest.fixture
def client():
    app = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield app.test_client(), app.config['APP_STATE']
    app.config['REAPER'].stop()
    app.config['DB'].close()


def test_status_keeps_legacy_fields(client):
    c, _ = client
    data = c.get('/api/status').get_json()
    for field in ('is_running', 'current_action', 'success', 'fail',
                  'total_inventory', 'logs'):
        assert field in data, f"顶层字段 {field} 丢失，老前端会坏"


def test_status_exposes_single_worker_by_default(client):
    c, _ = client
    data = c.get('/api/status').get_json()
    assert [w['id'] for w in data['workers']] == ['W1']
    assert data['parallel_mode'] is False


def test_status_lists_all_workers_in_parallel(client):
    c, state = client
    state.ensure_workers(3)
    state.parallel_mode = True
    data = c.get('/api/status').get_json()

    assert [w['id'] for w in data['workers']] == ['W1', 'W2', 'W3']
    assert data['parallel_mode'] is True
    for w in data['workers']:
        assert set(w) == {'id', 'current_action', 'busy', 'log_seq'}


def test_worker_logs_incremental_fetch(client):
    c, state = client
    state.ensure_workers(2)
    w2 = state.get_worker('W2')
    w2.add_log('第一条')
    w2.add_log('第二条')

    first = c.get('/api/workers/W2/logs?index=0').get_json()
    assert len(first['logs']) == 2
    assert first['worker_id'] == 'W2'

    empty = c.get(f"/api/workers/W2/logs?index={first['next_index']}").get_json()
    assert empty['logs'] == []

    w2.add_log('第三条')
    more = c.get(f"/api/workers/W2/logs?index={first['next_index']}").get_json()
    assert len(more['logs']) == 1 and '第三条' in more['logs'][0]


def test_worker_logs_are_isolated(client):
    c, state = client
    state.ensure_workers(2)
    state.get_worker('W1').add_log('仅 W1')
    state.get_worker('W2').add_log('仅 W2')

    logs1 = c.get('/api/workers/W1/logs').get_json()['logs']
    logs2 = c.get('/api/workers/W2/logs').get_json()['logs']

    assert any('仅 W1' in x for x in logs1) and not any('仅 W2' in x for x in logs1)
    assert any('仅 W2' in x for x in logs2) and not any('仅 W1' in x for x in logs2)


def test_unknown_worker_falls_back_to_primary(client):
    """前端在 worker 数变化时可能请求已不存在的 id，不能 404。"""
    c, _ = client
    data = c.get('/api/workers/W99/logs').get_json()
    assert data['worker_id'] == 'W1'


def test_video_feed_without_param_uses_primary(client):
    """老 URL 必须继续可用（不消费无限流，只验证帧来源）。"""
    _, state = client
    state.ensure_workers(2)
    state.primary_worker.update_frame(b'PRIMARY')
    state.get_worker('W2').update_frame(b'SECOND')

    assert b'PRIMARY' in next(gen_frames(state.get_worker(None)))
    assert b'SECOND' in next(gen_frames(state.get_worker('W2')))
