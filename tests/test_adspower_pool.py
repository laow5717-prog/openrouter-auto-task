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


def _account(db, email, status, platform='opencode', balance=None):
    """按状态所属的层建账号。

    status 传平台层的值（recharged/archived/subscribed）时，身份层记 'registered'
    并在 platform_accounts 建一行——这正是真实流水线写出来的形状：能充值成功的账号，
    GitHub 必然已注册好。传身份层的值则只写 accounts。

    balance 写进 credits_balance，供「牺牲余额最高的」那组测试用。不传即 NULL，
    也就是真实流水线里「余额读不到」的形状。
    """
    if status in _PLATFORM_LAYER:
        db.execute("INSERT INTO accounts (email, identity_status) VALUES (?, 'registered')",
                   (email,))
        db.execute("INSERT INTO platform_accounts "
                   "(platform, email, status, credits_balance) VALUES (?, ?, ?, ?)",
                   (platform, email, status, balance))
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
    """上一条的对照组：所有开通过的平台都**真跑完**后，环境即可回收。

    2026-08-08 改语义前这里用的是 recharged + subscribed。那个组合现在落第 3 档
    （recharged 意味着余额未满、下一轮还要跑），不再是「真跑完」的例子——它挪去了
    test_recharged_sacrificed_only_as_last_resort。真终态是 archived / subscribed。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES ('multi@x.com', 'registered')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('opencode', 'multi@x.com', 'archived')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('other', 'multi@x.com', 'subscribed')")
    _account(db, "new@x.com", "registered")

    old_pid, _, _ = pool.ensure_profile("multi@x.com")
    pool.ensure_profile("new@x.com")
    assert old_pid in client.deleted


# --- recharged 是最后手段（2026-08-08） -------------------------------------
#
# 背景：08-05 的 reuse-by-balance-cap 让「余额未满的 recharged 账号」重新进轮转，
# 但环境回收判据没跟着改，继续把 recharged 当终态删。现场代价是 14 个 recharged
# 账号里 5 个的环境映射被删光，每个下次跑都要 GitHub 完整重登 + 一次新设备邮箱验证。

def test_recharged_ranks_after_truly_done(db):
    """最直接的档位断言：recharged 的 rank 必须**严格大于**真终态的 rank。

    这条不依赖余额、不依赖 last_used_at，是「两档没合并」的唯一无歧义证据。
    行为层的测试（谁先被删）都会被 ORDER BY 的后续层次干扰——实测把 archived 余额
    设成 200、recharged 设成 20 时，即使两档合并，按余额降序也照样先删 archived，
    测试恒过。
    """
    _account(db, "done@x.com", "archived", balance=200.0)
    _account(db, "reuse@x.com", "recharged", balance=20.0)
    model = AdsPowerProfileModel(db)
    model.upsert("done@x.com", "pid_done")
    model.upsert("reuse@x.com", "pid_reuse")

    ranks = {r["email"]: r["rank"] for r in model.reclaim_candidates()}
    assert ranks["done@x.com"] < ranks["reuse@x.com"], (
        f"recharged 必须排在真终态之后，实际 {ranks}")


def test_archived_reclaimed_before_recharged(db):
    """核心回归：有真终态环境可回收时，recharged 的环境一个都不许动。

    ⚠️ recharged **必须先创建**，让它的 last_used_at 早于 archived。两档合并时
    tie-break 会退化成 last_used_at ASC（LRU），于是先删 recharged —— 正是要抓的 bug。
    反过来构造（archived 先建）的话，同档时 LRU 也会先删 archived，测试恒过、
    抓不住任何东西。实测：这个顺序写反时，把 RECHARGED_RANK 改回与 PLATFORM_DONE_RANK
    相等，34 项测试仍然全绿。
    """
    client = FakeClient(quota=2)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "reuse@x.com", "recharged", balance=20.0)
    _account(db, "done@x.com", "archived", balance=200.0)

    reuse_pid, _, _ = pool.ensure_profile("reuse@x.com")     # 更早被用
    done_pid, _, _ = pool.ensure_profile("done@x.com")
    _account(db, "new@x.com", "registered")
    pool.ensure_profile("new@x.com")            # 配额满 → 触发回收

    assert done_pid in client.deleted, "真终态环境才该被回收"
    assert reuse_pid not in client.deleted, "还要再跑的账号，环境不该被牺牲"


def test_recharged_sacrificed_only_as_last_resort(db):
    """没有真终态可回收时，才牺牲 recharged——否则配额满了整条流水线会瘫痪。

    2026-08-03 踩过：一批环境永久占死配额，每个账号都报「配额已满且无可回收」。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "reuse@x.com", "recharged", balance=20.0)
    _account(db, "new@x.com", "registered")

    reuse_pid, _, _ = pool.ensure_profile("reuse@x.com")
    pool.ensure_profile("new@x.com")

    assert reuse_pid in client.deleted


def test_recharged_sacrifice_is_capped_at_one(db):
    """哪怕 reclaim_batch=3，第 3 档单次也只牺牲一个。

    每牺牲一个的代价是一次 GitHub 完整重登 + 一次新设备邮箱验证（数分钟 + 一封
    验证码）。批量删三个等于一次赔三份，而配额只要腾出一格就能继续跑。
    """
    client = FakeClient(quota=3)
    pool = _pool(db, client, reclaim_batch=3)
    for i in range(3):
        _account(db, f"reuse{i}@x.com", "recharged", balance=20.0 + i)
        pool.ensure_profile(f"reuse{i}@x.com")
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("new@x.com")

    assert len(client.deleted) == 1, f"只该牺牲 1 个，实际删了 {client.deleted}"


def test_recharged_sacrifice_picks_highest_balance(db):
    """牺牲余额最高的：离 balance_cap 最近，剩下要充的笔数最少，损失最小。"""
    client = FakeClient(quota=3)
    pool = _pool(db, client, reclaim_batch=1)
    pids = {}
    for email, bal in (("low@x.com", 20.0), ("high@x.com", 110.0), ("mid@x.com", 49.0)):
        _account(db, email, "recharged", balance=bal)
        pids[email], _, _ = pool.ensure_profile(email)
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("new@x.com")

    assert client.deleted == [pids["high@x.com"]]


def test_null_balance_sacrificed_last(db):
    """余额为 NULL 的排在所有有余额的之后——不知道就保守保留。

    update_balance 在 balance_after 读不到时直接 return（infron 常态、opencode 偶发），
    所以 NULL 是「读不到」而不是「余额为 0」。
    """
    client = FakeClient(quota=2)
    pool = _pool(db, client, reclaim_batch=1)
    _account(db, "unknown@x.com", "recharged", balance=None)
    _account(db, "known@x.com", "recharged", balance=20.0)
    unknown_pid, _, _ = pool.ensure_profile("unknown@x.com")
    known_pid, _, _ = pool.ensure_profile("known@x.com")
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("new@x.com")

    assert client.deleted == [known_pid]
    assert unknown_pid not in client.deleted


def test_unfinished_platform_still_never_reclaimed(db):
    """既有保护不能被新档破坏：任一平台还没跑完时，环境永不回收。

    第 3 档放宽的只是 recharged，不是「所有还没跑完的」。
    """
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1)
    db.execute("INSERT INTO accounts (email, identity_status) VALUES ('busy@x.com', 'registered')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('opencode', 'busy@x.com', 'recharged')")
    db.execute("INSERT INTO platform_accounts (platform, email, status) "
               "VALUES ('other', 'busy@x.com', 'registered')")   # 还没跑完
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("busy@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == []


def test_busy_recharged_is_not_sacrificed(db):
    """正被 worker 使用的 recharged 环境不能牺牲——删了那个 worker 的浏览器会凭空消失。"""
    client = FakeClient(quota=1)
    pool = _pool(db, client, reclaim_batch=1,
                 is_busy=lambda e: e == "reuse@x.com")
    _account(db, "reuse@x.com", "recharged", balance=20.0)
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("reuse@x.com")
    with pytest.raises(AdsPowerQuotaExceeded):
        pool.ensure_profile("new@x.com")
    assert client.deleted == []


def test_sacrifice_log_names_the_cost(db):
    """牺牲活账号要单独记一条并带余额——这条日志的频率就是配额压力的直接指标。

    原先只有一句「已回收 N 个环境释放配额」，看不出删的是垃圾还是活账号。
    """
    logs = []
    client = FakeClient(quota=1)
    pool = AdsPowerProfilePool(client, AdsPowerProfileModel(db),
                              log=logs.append, reclaim_batch=1)
    _account(db, "reuse@x.com", "recharged", balance=110.0)
    _account(db, "new@x.com", "registered")

    pool.ensure_profile("reuse@x.com")
    pool.ensure_profile("new@x.com")

    line = next((m for m in logs if "牺牲" in m), None)
    assert line is not None, f"没有牺牲日志: {logs}"
    assert "reuse@x.com" in line
    assert "110" in line


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


# ---------- release_many：删账号时同步释放环境 ----------

def test_release_many_deletes_remote_and_mapping(db):
    """删账号必须把远端环境和本地映射一起清掉，否则那格配额白占着。"""
    client = FakeClient(quota=5)
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")
    _account(db, "b@x.com", "registered")
    pid_a, _, _ = pool.ensure_profile("a@x.com")
    pid_b, _, _ = pool.ensure_profile("b@x.com")

    out = pool.release_many(["a@x.com", "b@x.com"])

    assert set(out["released"]) == {"a@x.com", "b@x.com"}
    assert set(client.deleted) == {pid_a, pid_b}
    m = AdsPowerProfileModel(db)
    assert m.get_by_email("a@x.com") is None
    assert m.get_by_email("b@x.com") is None


def test_release_many_batches_stop_and_delete(db):
    """整批只发一次 delete。逐个调 release 会为每个账号各等 1.5 秒，删多了直接卡死接口。"""
    calls = []

    class CountingDelete(FakeClient):
        def delete_profiles(self, ids):
            calls.append(list(ids))
            return super().delete_profiles(ids)

    client = CountingDelete(quota=5)
    pool = _pool(db, client)
    for e in ("a@x.com", "b@x.com", "c@x.com"):
        _account(db, e, "registered")
        pool.ensure_profile(e)

    pool.release_many(["a@x.com", "b@x.com", "c@x.com"])

    assert len(calls) == 1 and len(calls[0]) == 3
    # 删除前必须先停：AdsPower 拒绝删除运行中的环境
    assert len(client.stopped) == 3


def test_release_many_skips_busy(db):
    """正在跑的账号：环境保留（删了 worker 的浏览器会凭空消失），但要回报出来。"""
    client = FakeClient(quota=5)
    busy = {"busy@x.com"}
    pool = _pool(db, client, is_busy=lambda e: e in busy)
    _account(db, "busy@x.com", "registered")
    _account(db, "idle@x.com", "registered")
    pool.ensure_profile("busy@x.com")
    idle_pid, _, _ = pool.ensure_profile("idle@x.com")

    out = pool.release_many(["busy@x.com", "idle@x.com"])

    assert out["skipped_busy"] == ["busy@x.com"]
    assert out["released"] == ["idle@x.com"]
    assert client.deleted == [idle_pid]
    # 映射保留 —— 账号行随后被删掉，它变成孤儿，由 reclaim 第 0 档在跑完后回收
    assert AdsPowerProfileModel(db).get_by_email("busy@x.com") is not None


def test_release_many_keeps_mapping_when_delete_fails(db):
    """删除失败不清映射：先清本地再删远端，一旦失败就留下没人再管的孤儿环境。"""
    class FailingDelete(FakeClient):
        def delete_profiles(self, ids):
            raise AdsPowerError("删除失败")

    client = FailingDelete(quota=5)
    pool = _pool(db, client)
    _account(db, "a@x.com", "registered")
    pool.ensure_profile("a@x.com")

    out = pool.release_many(["a@x.com"])

    assert out["failed"] == ["a@x.com"] and out["released"] == []
    assert AdsPowerProfileModel(db).get_by_email("a@x.com") is not None


def test_release_many_ignores_accounts_without_profile(db):
    """从未跑过的账号没有映射，不该因此报错或误删别人的环境。"""
    client = FakeClient(quota=5)
    pool = _pool(db, client)

    out = pool.release_many(["never@x.com"])

    assert out["no_profile"] == ["never@x.com"]
    assert client.deleted == [] and client.stopped == []


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
