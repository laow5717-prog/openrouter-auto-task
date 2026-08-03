"""AdsPower 环境池与 remote 会话关闭的单测。

全部用假客户端，不打真实接口、不起浏览器——CI 与本机都能跑。
真实链路（建环境/接管/出口 IP/Stripe 可达/add_init_script 注入）由
scripts/probe_adspower.py 人工验证，那部分依赖本机 AdsPower 客户端，不适合进 CI。
"""

import tempfile
import os

import pytest

from src.browser import driver as D
from src.browser.adspower_driver import AdsPowerProfilePool
from src.models.adspower_profile import AdsPowerProfileModel
from src.models.database import Database
from src.services.adspower import AdsPowerQuotaExceeded, AdsPowerError


class FakeClient:
    """假 AdsPower 客户端。quota 为剩余可创建环境数，模拟配额上限。"""

    def __init__(self, proxies=None, quota=2):
        self.proxies = proxies if proxies is not None else [
            {"proxy_id": "1", "profile_count": "0"},
            {"proxy_id": "2", "profile_count": "0"},
        ]
        self.quota = quota
        self.created = []
        self.created_payloads = []
        self.deleted = []
        self.stopped = []
        self._seq = 0

    def list_all_proxies(self):
        return [dict(p) for p in self.proxies]

    def create_profile(self, payload):
        if self.quota <= 0:
            raise AdsPowerQuotaExceeded(
                "AdsPower 接口失败: If the number of imported accounts exceeds the limit of 12")
        self.quota -= 1
        self._seq += 1
        pid = f"pid{self._seq}"
        self.created.append((pid, payload.get("proxyid")))
        self.created_payloads.append(payload)
        # 绑定后该代理的占用数 +1，模拟服务端行为
        for p in self.proxies:
            if str(p["proxy_id"]) == str(payload.get("proxyid")):
                p["profile_count"] = str(int(p["profile_count"]) + 1)
        return pid, str(self._seq)

    def delete_profiles(self, ids):
        self.deleted.extend(ids)
        self.quota += len(ids)
        return len(ids)

    def stop_profile(self, pid):
        self.stopped.append(pid)


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


# 平台层状态：这些不写 accounts，要落到 platform_accounts 的对应平台行上
_PLATFORM_LAYER = ('archived', 'recharged', 'subscribed')


def _account(db, email, status, platform='opencode'):
    """按状态所属的层建账号。

    status 传平台层的值（recharged/archived/subscribed）时，身份层记 'registered'
    并在 platform_accounts 建一行——这正是真实流水线写出来的形状：能充值成功的账号，
    GitHub 必然已注册好。传身份层的值则只写 accounts。
    """
    if status in _PLATFORM_LAYER:
        db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, 'registered')",
                   (email,))
        db.execute("INSERT INTO platform_accounts (platform, email, status) VALUES (?, ?, ?)",
                   (platform, email, status))
    else:
        db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, ?)",
                   (email, status))


def _pool(db, client, **kw):
    return AdsPowerProfilePool(client, AdsPowerProfileModel(db),
                               log=lambda m: None, **kw)


def test_ensure_profile_reuses_mapping(db):
    """已有映射的账号不应再建环境——重建等于丢掉登录态。"""
    client = FakeClient()
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")

    pid1, proxy1, created1 = pool.ensure_profile("a@x.com")
    pid2, proxy2, created2 = pool.ensure_profile("a@x.com")

    assert created1 is True and created2 is False
    assert pid1 == pid2 and proxy1 == proxy2
    assert len(client.created) == 1


def test_fingerprint_restricts_os_to_desktop(db):
    """指纹必须显式限定 Windows/macOS。

    AdsPower 不设 ua_system_version 时会在**所有**系统里随机，包含 Android/iOS/Linux；
    移动端 UA 会让 GitHub 与 Stripe 返回移动版页面，而本项目所有选择器都按桌面版编写。
    这条断言存在的意义就是不让这个字段被顺手删掉。
    """
    client = FakeClient()
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")
    pool.ensure_profile("a@x.com")

    fp = client.created_payloads[0]["fingerprint_config"]
    systems = fp["random_ua"]["ua_system_version"]
    assert systems, "ua_system_version 不能为空——留空等于在所有系统里随机"
    for s in systems:
        assert s.startswith("Windows") or s.startswith("Mac OS X"), \
            f"{s} 不是桌面系统"


def test_ua_systems_override_is_honoured(db):
    """配置里指定的系统集合要真正传到建环境请求里（例如只留 Windows）。"""
    client = FakeClient()
    pool = _pool(db, client, ua_systems=["Windows 11"])
    _account(db, "a@x.com", "registered")
    pool.ensure_profile("a@x.com")

    fp = client.created_payloads[0]["fingerprint_config"]
    assert fp["random_ua"]["ua_system_version"] == ["Windows 11"]


def test_each_account_gets_distinct_proxy(db):
    """两个账号必须绑到不同代理，否则代理隔离白做。"""
    client = FakeClient()
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")
    _account(db, "b@x.com", "registered")

    _pid_a, proxy_a, _ = pool.ensure_profile("a@x.com")
    _pid_b, proxy_b, _ = pool.ensure_profile("b@x.com")

    assert proxy_a != proxy_b


def test_pick_free_proxy_falls_back_to_least_used(db):
    """代理全被占用时取占用最少的，而不是报错或直连。"""
    client = FakeClient(proxies=[
        {"proxy_id": "1", "profile_count": "3"},
        {"proxy_id": "2", "profile_count": "1"},
    ])
    pool = _pool(db, client)
    assert pool.pick_free_proxy() == "2"


def test_empty_proxy_list_raises(db):
    """代理列表为空必须报错——静默直连会让所有账号共用本机 IP 且看不出来。"""
    pool = _pool(db, FakeClient(proxies=[]))
    _account(db, "a@x.com", "registered")
    with pytest.raises(AdsPowerError):
        pool.ensure_profile("a@x.com")


def test_quota_exceeded_triggers_reclaim_then_succeeds(db):
    """配额满 → 回收已充值账号的环境 → 重试建环境成功。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "done@x.com", "recharged")
    _account(db, "new@x.com", "registered")

    old_pid, _, _ = pool.ensure_profile("done@x.com")   # 吃掉最后一个配额
    new_pid, _, created = pool.ensure_profile("new@x.com")

    assert created is True
    assert old_pid in client.deleted            # 已充值账号的环境被回收
    assert new_pid not in client.deleted
    assert AdsPowerProfileModel(db).get_by_email("done@x.com") is None   # 本地映射同步清掉
    assert AdsPowerProfileModel(db).get_by_email("new@x.com")["profile_id"] == new_pid


def test_failed_registration_profiles_are_reclaimable(db):
    """注册没成功的账号，其环境里空无一物，必须可回收。

    2026-08-03 线上事故：failed/pending 不在回收名单里，一批注册失败的账号把环境
    永久占死，配额卡在 11/12，此后每个账号都报「配额已满且无可回收」，流水线瘫痪。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "dead@x.com", "failed")       # 注册失败，环境无登录态
    _account(db, "new@x.com", "imported")

    old_pid, _, _ = pool.ensure_profile("dead@x.com")
    _new_pid, _, created = pool.ensure_profile("new@x.com")

    assert created is True
    assert old_pid in client.deleted


def test_registered_accounts_are_never_reclaimed(db):
    """registered 的环境装着刚拿到的 GitHub 登录态，正是下一步充值要用的，绝不能删。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "ready@x.com", "registered")
    _account(db, "new@x.com", "imported")

    pool.ensure_profile("ready@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == []


def test_reclaim_prefers_useless_profiles_first(db):
    """回收顺序：注册失败的（无登录态）优先于已充值的（登录态已无用但曾有效）。"""
    client = FakeClient(quota=2)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "paid@x.com", "recharged")
    _account(db, "dead@x.com", "failed")
    _account(db, "new@x.com", "imported")

    pool.ensure_profile("paid@x.com")
    dead_pid, _, _ = pool.ensure_profile("dead@x.com")
    pool.ensure_profile("new@x.com")

    assert client.deleted == [dead_pid]


def test_reclaim_skips_busy_accounts(db):
    """正在被 worker 使用的账号，其环境绝不能被回收——那会让浏览器凭空消失。"""
    client = FakeClient(quota=1)
    busy = {"done@x.com"}
    pool = _pool(db, client, reclaim_batch=1, is_busy=lambda e: e in busy)
    _account(db, "done@x.com", "recharged")
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("done@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == []


def test_reclaim_without_candidates_raises(db):
    """无可回收环境时原样抛配额异常，由上层把该账号记 failed 并继续下一个。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")      # 未完成，不可回收
    _account(db, "b@x.com", "registered")

    pool.ensure_profile("a@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("b@x.com")


def test_reclaim_blocked_while_any_platform_unfinished(db):
    """多平台：只要还有**任何一个**平台没跑完，环境就不能回收。

    环境是按 email 分配的（GitHub 授权态跨平台共享），所以判据必须是「所有开通过的
    平台都到终态」。若误判成「有一个平台到终态就能删」，会把另一个平台正在用的
    浏览器环境删掉，那个账号的 GitHub 登录态随之全丢。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES ('multi@x.com', 'registered')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('opencode', 'multi@x.com', 'recharged')")      # 已跑完
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('other', 'multi@x.com', '')")                  # 还没跑完
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("multi@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == [], "还有平台没跑完，环境不该被回收"


def test_reclaim_allowed_once_all_platforms_finished(db):
    """上一条的对照组：所有开通过的平台都到终态后，环境即可回收。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES ('multi@x.com', 'registered')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('opencode', 'multi@x.com', 'recharged')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('other', 'multi@x.com', 'subscribed')")
    _account(db, "new@x.com", "registered")

    old_pid, _, _ = pool.ensure_profile("multi@x.com")
    pool.ensure_profile("new@x.com")
    assert old_pid in client.deleted


def test_registered_account_without_platform_row_is_never_reclaimed(db):
    """已注册但还没在任何平台开通的账号，环境绝不可回收。

    它的 GitHub 授权态正是下一步登录要用的东西。判据里「至少开通过一个平台」的
    EXISTS 就是为这条存在——少了它，NOT EXISTS 对这类账号恒为真，会把刚注册好的
    账号环境全删光。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "ready@x.com", "registered")   # 无 platform_accounts 行
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("ready@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == []


def test_orphan_mapping_is_reclaimed_first(db):
    """账号已删、映射还在的孤儿环境必须能被回收，且优先级最高。

    此前 reclaim_candidates 用 INNER JOIN accounts，孤儿直接被 JOIN 掉、永远进不了
    候选集，对应的远端环境无人回收，在只有 12 格的配额里白占一格（生产库实测有一个）。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "ghost@x.com", "registered")
    _account(db, "new@x.com", "registered")

    old_pid, _, _ = pool.ensure_profile("ghost@x.com")
    db.execute("DELETE FROM accounts WHERE email='ghost@x.com'")   # 映射成孤儿

    pool.ensure_profile("new@x.com")

    assert old_pid in client.deleted
    assert db.fetchone("SELECT 1 FROM adspower_profiles WHERE email='ghost@x.com'") is None


def test_reclaim_stops_profiles_before_delete(db):
    """运行中的环境删不掉，回收必须先 stop。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "done@x.com", "recharged")
    _account(db, "new@x.com", "registered")

    old_pid, _, _ = pool.ensure_profile("done@x.com")
    pool.ensure_profile("new@x.com")
    assert old_pid in client.stopped
    assert client.stopped.index(old_pid) >= 0 and old_pid in client.deleted


def test_reclaim_keeps_mapping_when_delete_fails(db):
    """远端删除失败时不能清本地映射，否则留下没人再管的孤儿环境永久占配额。"""
    class FailingDelete(FakeClient):
        def delete_profiles(self, ids):
            raise AdsPowerError("删除失败")

    client = FailingDelete(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "done@x.com", "recharged")
    pool.ensure_profile("done@x.com")

    assert pool.reclaim() == []
    assert AdsPowerProfileModel(db).get_by_email("done@x.com") is not None


# ---------- BrowserSession remote 分支 ----------

def _remote_session(remote_stop, browser, playwright):
    s = D.BrowserSession.__new__(D.BrowserSession)
    s.context = None
    s.playwright = playwright
    s._closed = False
    s._temp_profile = None
    s._user_data_dir = None
    s._remote_browser = browser
    s._remote_stop = remote_stop
    return s


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_remote_quit_disconnects_and_stops(monkeypatch):
    """remote 模式：断开 CDP + 停 playwright + 请求关闭环境，且绝不按 profile 目录杀进程。"""
    killed = []
    monkeypatch.setattr(D, '_kill_chrome_for_profile',
                        lambda *a, **k: killed.append(a))

    stops = []
    browser, pw = _FakeBrowser(), _FakePlaywright()
    session = _remote_session(lambda: stops.append(1), browser, pw)
    session.quit()

    assert browser.closed is True
    assert pw.stopped is True
    assert stops == [1]
    assert killed == []        # AdsPower 的 Chrome 不归我们管


def test_remote_quit_is_idempotent():
    """重复 quit 只应关一次环境。"""
    stops = []
    session = _remote_session(lambda: stops.append(1), _FakeBrowser(), _FakePlaywright())
    session.quit()
    session.quit()
    assert stops == [1]


def test_remote_quit_stops_even_if_disconnect_fails():
    """断开连接失败也必须把环境关掉，否则浏览器永远留在后台吃内存。"""
    class BadBrowser:
        def close(self):
            raise RuntimeError("connection lost")

    stops = []
    session = _remote_session(lambda: stops.append(1), BadBrowser(), _FakePlaywright())
    session.quit()
    assert stops == [1]
