"""AdsPower「占用锁」的分类与两条自愈路径。

回归 2026-08-16 的现场：进程被 kill 掉之后，AdsPower **云端**留着环境「打开中」的
记录，本机 local API 却认为它没开（`active` 报非 Active、`stop` 报 `Profile is not
open`）。于是：

  - start 被拒：`[k1fkeo7r] is being used by [x@gmail.com] and is not allowed to open`
    这条错当时降级成普通 AdsPowerError，走「本轮跳过、下轮重试」——而本地映射一直
    指向同一个 profile，每一轮都撞同一堵墙。8 个 worker、2 个可充账号，其中一个就此
    永久出局，实际只有 1 个浏览器在跑。
  - delete 被拒：`[] is being used by other users and cannot be deleted`
    「注册失败当场释放环境」紧接在浏览器关闭之后，云端状态还没翻转，那一格配额
    要等下一次 reclaim 才回得来。
"""

import tempfile
import os

import pytest

from src.browser import adspower_driver as AD
from src.browser.adspower_driver import AdsPowerProfilePool
from src.models.adspower_profile import AdsPowerProfileModel
from src.models.database import Database
from src.services.adspower import (
    AdsPowerError, AdsPowerProfileLocked, AdsPowerProfileMissing, _classify,
)

_START_LOCK_MSG = ("AdsPower 接口失败（/api/v2/browser-profile/start）: "
                   "[k1fkeo7r] is being used by [laow5717@gmail.com] "
                   "and is not allowed to open")
_DELETE_LOCK_MSG = ("AdsPower 接口失败（/api/v2/browser-profile/delete）: "
                    "[] is being used by other users and cannot be deleted")


@pytest.fixture
def db():
    path = tempfile.mktemp(suffix=".db")
    d = Database(path)
    yield d
    d.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """锁重试要等 _LOCK_RETRY_SEC，测试里没必要真等。"""
    monkeypatch.setattr(AD.time, 'sleep', lambda s: None)


def test_lock_is_its_own_exception():
    """两条文案都要归到 AdsPowerProfileLocked，且不能被别的分支吃掉。

    降级成普通 AdsPowerError 就是那个「每轮都撞同一堵墙」的 bug：调用方把它当瞬时
    故障重试，而锁不会自己好。
    """
    assert _classify(_START_LOCK_MSG) is AdsPowerProfileLocked
    assert _classify(_DELETE_LOCK_MSG) is AdsPowerProfileLocked
    # 仍是 AdsPowerError 的子类：既有的 except AdsPowerError 兜底路径不能因此漏接
    assert issubclass(AdsPowerProfileLocked, AdsPowerError)
    # 别的分类不受影响
    assert _classify("profile does not exist") is AdsPowerProfileMissing


class _LockClient:
    """start 前 n 次报占用锁，之后正常。stop 一律报「没开着」（云端残留锁的表现）。"""

    def __init__(self, lock_times=1):
        self.lock_times = lock_times
        self.start_calls = 0
        self.stopped = []

    def start_profile(self, profile_id, launch_args=None, headless=False):
        self.start_calls += 1
        if self.start_calls <= self.lock_times:
            raise AdsPowerProfileLocked(_START_LOCK_MSG)
        return f"ws://{profile_id}", 1234

    def stop_profile(self, profile_id):
        self.stopped.append(profile_id)
        raise AdsPowerError("AdsPower 接口失败（/api/v2/browser-profile/stop）: Profile is not open")


class _Pool:
    """只提供 _start_unlocking 用到的那一点接口。"""

    def __init__(self):
        self.logs = []

    def _log(self, m):
        self.logs.append(m)


def test_transient_lock_is_resolved_by_stop_and_retry():
    """状态没翻转那种锁：stop + 等一会儿 + 重试，登录态保住。"""
    client = _LockClient(lock_times=1)
    pool = _Pool()
    ws, port = AD._start_unlocking(client, pool, 'a@x.com', 'pid1', [], False)
    assert ws == 'ws://pid1'
    assert client.start_calls == 2, '没有重试'
    assert client.stopped == ['pid1'], 'stop 解锁那一步没走（它对状态未翻转的锁有效）'


def test_persistent_lock_is_raised_for_the_caller_to_heal():
    """云端残留那种锁：重试一次仍锁，就把异常抛出去。

    抛出去才有人做「弃用环境重建」的自愈；就地吞掉会变回死循环。
    """
    client = _LockClient(lock_times=99)
    with pytest.raises(AdsPowerProfileLocked):
        AD._start_unlocking(client, _Pool(), 'a@x.com', 'pid1', [], False)
    assert client.start_calls == 2, '应当只重试一次，不该无限重试'


class _DeleteLockClient:
    """delete 前 n 次报占用锁，之后成功。"""

    def __init__(self, lock_times=1):
        self.lock_times = lock_times
        self.delete_calls = 0
        self.deleted = []
        self.stopped = []

    def delete_profiles(self, ids):
        self.delete_calls += 1
        if self.delete_calls <= self.lock_times:
            raise AdsPowerProfileLocked(_DELETE_LOCK_MSG)
        self.deleted.extend(ids)
        return len(ids)

    def stop_profile(self, pid):
        self.stopped.append(pid)


def _pool_with_mapping(db, client, email='a@x.com', pid='pid1'):
    profiles = AdsPowerProfileModel(db)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, 'failed')", (email,))
    profiles.upsert(email, pid)
    return AdsPowerProfilePool(client, profiles, log=lambda m: None)


def test_release_retries_a_locked_delete(db):
    """删除撞锁 → 等一会儿再 stop + delete，成功后才清本地映射。"""
    client = _DeleteLockClient(lock_times=1)
    pool = _pool_with_mapping(db, client)
    assert pool.release('a@x.com') is True
    assert client.delete_calls == 2, '撞锁后没有重试删除'
    assert client.deleted == ['pid1']
    assert pool.profiles.get_by_email('a@x.com') is None, '删成功了却没清映射'


def test_release_keeps_mapping_when_lock_never_clears(db):
    """重试仍删不掉：返回 False 且**保留**本地映射。

    先清本地再删远端会留下查不到映射、也没人再删的孤儿环境，那一格配额永久没了。
    """
    client = _DeleteLockClient(lock_times=99)
    pool = _pool_with_mapping(db, client)
    assert pool.release('a@x.com') is False
    assert client.delete_calls == 2
    assert pool.profiles.get_by_email('a@x.com') is not None, \
        '删除失败却清了映射——那个环境会变成没人管的孤儿'
