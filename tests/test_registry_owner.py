"""三个 registry 的 owner 隔离。

这三条对应并发改造里最危险的三个缺陷——共同点是**都不报错**：
registry 是跨平台共享的，不带 owner 时一个平台会看见/放掉另一个平台的占用，
表现只是「行为悄悄变错」，实跑观察发现不了。所以必须由测试守着。
"""

import pytest

from src.web.app import AppState
from src.web.worker import PaymentCardRegistry, ProxyRegistry

A, B = 'opencode', 'infron'


@pytest.fixture
def accounts():
    return AppState(db=None, models={}).account_registry


# ---------- ★1 snapshot 不区分 owner → 轮转永不收敛 ----------

def test_snapshot_only_sees_its_own_owner(accounts):
    """流水线用 snapshot() 判断「本轮还有账号在飞」。

    不过滤 owner 的话，A 平台会把 B 平台正在跑的账号看成自己这轮在飞，
    于是永远走 'wait' 分支、轮边界永不触发、失败账号永不重试、idle_rounds
    永不递增 —— 任务静默不收敛，没有任何报错。这是整个改造里最隐蔽的一个。
    """
    accounts.claim('a@x.com', owner=A)
    accounts.claim('b@x.com', owner=B)

    assert set(accounts.snapshot(owner=A)) == {'a@x.com'}
    assert set(accounts.snapshot(owner=B)) == {'b@x.com'}
    assert set(accounts.snapshot()) == {'a@x.com', 'b@x.com'}, '不传 owner 仍是全局视图'


def test_a_platform_alone_sees_an_empty_snapshot_when_only_b_is_running(accounts):
    """B 在跑、A 什么都没领时，A 的快照必须是空的——否则 A 会一直等下去。"""
    accounts.claim('b@x.com', owner=B)
    assert accounts.snapshot(owner=A) == {}, 'A 看见了 B 的在飞账号，会永远等待'


def test_account_exclusion_is_still_global(accounts):
    """排他判定**不看 owner**：同一 email 不能同时在两个平台跑。

    这是物理约束——Chrome profile 目录按 email 命名，AdsPower 环境也按 email 分配。
    两个 worker 同用一个 profile 会互删 Singleton 锁，浏览器随机崩溃。
    """
    assert accounts.claim('same@x.com', owner=A) is True
    assert accounts.claim('same@x.com', owner=B) is False, '同一账号被两个平台同时领走了'
    assert accounts.is_claimed('same@x.com') is True


# ---------- ★2 收尾无差别全清 ----------

def test_account_release_all_by_owner_keeps_the_other_platform(accounts):
    accounts.claim('a@x.com', owner=A)
    accounts.claim('b@x.com', owner=B)

    accounts.release_all(owner=A)

    assert accounts.is_claimed('a@x.com') is False
    assert accounts.is_claimed('b@x.com') is True, 'A 跑完把 B 持有的账号也放掉了'


def test_card_teardown_only_releases_its_own_in_flight():
    """一个平台收尾不能把另一个平台正在刷的卡从 in-flight 抹掉。

    抹掉的后果是同一张卡可能被两个平台同时提交给发卡行——叠加 velocity 风控，
    比单平台重复刷更容易触发拒付甚至锁卡。
    """
    reg = PaymentCardRegistry()
    reg.try_acquire(A, '4111', 'a@x.com')
    reg.try_acquire(B, '4222', 'b@x.com')

    reg.release_all(A, include_in_flight=True)

    assert reg.in_flight_numbers() == {'4222'}, 'B 正在刷的卡被 A 的收尾放掉了'


def test_round_boundary_still_keeps_in_flight():
    """轮边界只清本轮归属，in-flight 不动——那是全局的发卡行防护，不该被轮边界打断。"""
    reg = PaymentCardRegistry()
    reg.try_acquire(A, '4111', 'a@x.com')

    reg.release_all(A)                      # 轮边界（不带 include_in_flight）

    assert reg.in_flight_numbers() == {'4111'}
    assert reg.used_numbers(A) == set()


def test_proxy_release_all_by_owner_keeps_the_other_platform():
    reg = ProxyRegistry()
    reg.try_acquire('p1', 'W1', owner=A)
    reg.try_acquire('p2', 'W1', owner=B)

    reg.release_all(owner=A)

    assert reg.in_flight_keys() == {'p2'}, 'A 跑完把 B 正在用的出口 IP 放回池子了'


# ---------- ★3 worker_id 撞名 → 同一代理发给两个平台 ----------

def test_same_worker_id_on_different_platforms_is_not_the_same_holder():
    """两个平台的 W1 是不同的执行体，尽管同名。

    只比裸 worker_id 的话，A 平台的 W1 会把 B 平台的 W1 认成自己，于是
    **同一个出口 IP 被同时发给两个平台** —— 两个账号同 IP 又被关联，
    代理就白用了。而且不报任何异常、不留任何日志。
    """
    reg = ProxyRegistry()
    assert reg.try_acquire('p1', 'W1', owner=A) is True
    assert reg.try_acquire('p1', 'W1', owner=B) is False, \
        '两个平台的 W1 被认成同一个持有者，同一代理发给了两边'
    assert reg.holder_of('p1') == ('W1', A)


def test_same_holder_reacquire_is_idempotent():
    reg = ProxyRegistry()
    assert reg.try_acquire('p1', 'W1', owner=A) is True
    assert reg.try_acquire('p1', 'W1', owner=A) is True, '同一执行体重复占用应幂等'


def test_acquire_free_skips_proxies_held_by_the_other_platform():
    reg = ProxyRegistry()
    cands = [{'host': 'h1', 'port': 1, 'username': 'u'},
             {'host': 'h2', 'port': 2, 'username': 'u'}]

    got_a = reg.acquire_free(cands, 'W1', owner=A)
    got_b = reg.acquire_free(cands, 'W1', owner=B)

    assert got_a is not None and got_b is not None
    assert got_a['host'] != got_b['host'], '两个平台领到了同一个出口 IP'


# ---------- 调用点：支持 owner ≠ 传了 owner ----------

def test_pipeline_passes_owner_at_every_registry_call_site():
    """registry 支持 owner 不等于流水线传了 —— 缺陷正是出在调用点。

    这几处漏传的后果各不相同，但共同点是都不报错：
      snapshot 漏传   → 轮转永不收敛
      release_all 漏传 → 另一平台的排他保护蒸发
      acquire_free 漏传 → 同一出口 IP 发给两个平台

    用源码检查钉住。crude，但这些调用点埋在几百行的闭包里，
    构造真实并发场景来覆盖它们的代价远大于收益。
    """
    import inspect
    from src.web.app import AppState

    for fn in (AppState.run_daily_pipeline, AppState.run_daily_subscribe_pipeline):
        src = inspect.getsource(fn)
        name = fn.__name__

        assert 'account_registry.release_all(owner=' in src, \
            f'{name}: 收尾无差别全清，会放掉另一平台持有的账号'
        assert 'proxy_registry.release_all(owner=' in src, \
            f'{name}: 收尾会把另一平台正在用的出口 IP 放回池子'
        assert 'payment_registry.release_all(self.platform, include_in_flight=True)' in src, \
            f'{name}: 收尾会抹掉另一平台正在刷的卡'
        assert 'account_registry.release_all()' not in src, \
            f'{name}: 仍有无参全清残留'

    src = inspect.getsource(AppState.run_daily_pipeline)
    assert 'account_registry.snapshot(owner=' in src, \
        'run_daily_pipeline: 「本轮还有账号在飞」没按平台过滤，轮转会永不收敛'
    assert 'proxy_registry.acquire_free(usable, worker.worker_id, owner=' in src, \
        'run_daily_pipeline: 领代理没带 owner，两个平台的 W1 会认成同一持有者'
