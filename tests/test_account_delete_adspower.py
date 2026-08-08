"""删账号必须同步释放 AdsPower 环境。

环境配额只有 12 格，删账号是终局操作——不在删除时释放，那一格会一直被占着，
直到下一次撞配额触发 reclaim 才被当孤儿收掉。

另一半同样重要：释放是 best-effort。AdsPower 没开、连不上、删除报错，都**不许**
让账号删不掉——否则一个外部依赖故障就能把「清理账号列表」这件事整个卡死。
"""

import tempfile

import pytest

from src.web.app import create_app


@pytest.fixture
def client():
    app = create_app(db_path=tempfile.mktemp(suffix='.db'))
    yield app.test_client(), app
    app.config['REAPER'].stop()
    app.config['DB'].close()


class FakePool:
    def __init__(self, result=None, boom=None):
        self.result = result or {"released": [], "skipped_busy": [],
                                 "failed": [], "no_profile": []}
        self.boom = boom
        self.calls = []

    def release_many(self, emails):
        self.calls.append(list(emails))
        if self.boom:
            raise self.boom
        return self.result


def _seed(app, *emails):
    db = app.config['DB']
    for e in emails:
        db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, 'registered')",
                   (e,))


def _exists(app, email):
    return app.config['DB'].fetchone(
        "SELECT 1 FROM accounts WHERE email=?", (email,)) is not None


def _wire(monkeypatch, app, enabled=True, pool=None):
    state = app.config['APP_STATE']
    monkeypatch.setattr(type(state), 'adspower_enabled',
                        property(lambda self: enabled), raising=False)
    monkeypatch.setattr(type(state), '_ensure_adspower',
                        lambda self: (None, pool), raising=False)
    return state


def test_delete_releases_profiles(client, monkeypatch):
    c, app = client
    _seed(app, 'a@x.com', 'b@x.com')
    pool = FakePool({"released": ['a@x.com', 'b@x.com'], "skipped_busy": [],
                     "failed": [], "no_profile": []})
    _wire(monkeypatch, app, pool=pool)

    body = c.post('/api/accounts/delete',
                  json={'emails': ['a@x.com', 'b@x.com']}).get_json()

    assert pool.calls == [['a@x.com', 'b@x.com']]
    assert body['deleted'] == 2
    assert body['adspower']['released'] == ['a@x.com', 'b@x.com']
    assert not _exists(app, 'a@x.com')


def test_busy_accounts_are_reported_but_still_deleted(client, monkeypatch):
    """正在跑的账号：环境保留，但账号照删——用户的删除意图不因此被拒。"""
    c, app = client
    _seed(app, 'busy@x.com')
    pool = FakePool({"released": [], "skipped_busy": ['busy@x.com'],
                     "failed": [], "no_profile": []})
    _wire(monkeypatch, app, pool=pool)

    body = c.post('/api/accounts/delete', json={'emails': ['busy@x.com']}).get_json()

    assert body['deleted'] == 1
    assert body['adspower']['skipped_busy'] == ['busy@x.com']
    assert body['adspower']['reason'], '被跳过必须给出可展示的原因，否则前端只能静默'
    assert not _exists(app, 'busy@x.com')


def test_delete_works_when_adspower_disabled(client, monkeypatch):
    c, app = client
    _seed(app, 'a@x.com')
    pool = FakePool()
    _wire(monkeypatch, app, enabled=False, pool=pool)

    body = c.post('/api/accounts/delete', json={'emails': ['a@x.com']}).get_json()

    assert pool.calls == [], '未启用时不该去碰环境池'
    assert body['deleted'] == 1 and body['adspower']['reason']


def test_adspower_failure_never_blocks_deletion(client, monkeypatch):
    """环境池抛任何异常，账号都必须删掉——外部依赖故障不能卡住账号清理。"""
    c, app = client
    _seed(app, 'a@x.com')
    pool = FakePool(boom=RuntimeError('AdsPower 客户端未启动'))
    _wire(monkeypatch, app, pool=pool)

    res = c.post('/api/accounts/delete', json={'emails': ['a@x.com']})
    body = res.get_json()

    assert res.status_code == 200
    assert body['deleted'] == 1
    assert body['adspower']['failed'] == ['a@x.com']
    assert not _exists(app, 'a@x.com')


def test_delete_without_pool_still_removes_account(client, monkeypatch):
    """_ensure_adspower 返回 None（配置不全）时按未启用处理。"""
    c, app = client
    _seed(app, 'a@x.com')
    _wire(monkeypatch, app, pool=None)

    body = c.post('/api/accounts/delete', json={'emails': ['a@x.com']}).get_json()

    assert body['deleted'] == 1 and body['adspower']['reason']


# --- 归档同样要释放环境（2026-08-08） ---------------------------------------
#
# 归档 = 「以后不再参与任何任务」，那一格配额留着就是白占，与删账号是同一件事。
# 区别只在账号行还在（可以取消归档），所以这里额外验「状态改了、行还在」。

def _status(app, email):
    row = app.config['DB'].fetchone(
        "SELECT identity_status FROM accounts WHERE email=?", (email,))
    return row['identity_status'] if row else None


def test_archive_releases_profiles(client, monkeypatch):
    c, app = client
    _seed(app, 'a@x.com', 'b@x.com')
    pool = FakePool({"released": ['a@x.com', 'b@x.com'], "skipped_busy": [],
                     "failed": [], "no_profile": []})
    _wire(monkeypatch, app, pool=pool)

    body = c.post('/api/accounts/archive',
                  json={'emails': ['a@x.com', 'b@x.com']}).get_json()

    assert pool.calls == [['a@x.com', 'b@x.com']]
    assert body['retired'] == 2
    assert body['adspower']['released'] == ['a@x.com', 'b@x.com']
    # 与删除的区别：账号行还在，只是状态变了（可以取消归档）
    assert _exists(app, 'a@x.com')
    assert _status(app, 'a@x.com') == 'retired'


def test_busy_account_keeps_profile_but_is_still_archived(client, monkeypatch):
    """正在跑的账号：环境保留（删了那个 worker 的浏览器会凭空消失），状态照改。"""
    c, app = client
    _seed(app, 'busy@x.com')
    pool = FakePool({"released": [], "skipped_busy": ['busy@x.com'],
                     "failed": [], "no_profile": []})
    _wire(monkeypatch, app, pool=pool)

    body = c.post('/api/accounts/archive', json={'emails': ['busy@x.com']}).get_json()

    assert body['retired'] == 1
    assert body['adspower']['skipped_busy'] == ['busy@x.com']
    assert _status(app, 'busy@x.com') == 'retired'


def test_adspower_failure_never_blocks_archive(client, monkeypatch):
    """环境释放抛异常也不许让归档失败——与删账号同一条红线。"""
    c, app = client
    _seed(app, 'a@x.com')
    _wire(monkeypatch, app, pool=FakePool(boom=RuntimeError("adspower down")))

    body = c.post('/api/accounts/archive', json={'emails': ['a@x.com']}).get_json()

    assert body['retired'] == 1
    assert _status(app, 'a@x.com') == 'retired'
    assert body['adspower']['reason']


def test_archive_works_when_adspower_disabled(client, monkeypatch):
    c, app = client
    _seed(app, 'a@x.com')
    pool = FakePool()
    _wire(monkeypatch, app, enabled=False, pool=pool)

    body = c.post('/api/accounts/archive', json={'emails': ['a@x.com']}).get_json()

    assert pool.calls == [], 'AdsPower 关着时不该去碰它'
    assert body['retired'] == 1
    assert _status(app, 'a@x.com') == 'retired'
