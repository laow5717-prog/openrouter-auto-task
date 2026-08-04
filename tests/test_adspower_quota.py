"""AdsPower 环境配额的按平台仲裁。

在此之前代码里**根本没有配额上限的概念**——12 只存在于注释，真正的配额是撞了才
发现的。单平台时撞墙就整批收敛也能用；多平台并发后不行了：配额是两平台共用的
物理资源，而运行状态已按平台拆开，「撞墙就停自己」会退化成一个平台饿死等待、
另一个反复抛错自杀。
"""

import threading
import time

import pytest

from src.browser.adspower_quota import AdsPowerQuota

A, B = 'opencode', 'infron'


@pytest.fixture
def q():
    return AdsPowerQuota(total=11, reserved={A: 7, B: 4})


# ---------- 上限（AC5 / AC6） ----------

def test_hard_total_is_eleven_not_twelve():
    """AdsPower 配额是 12，但它自带的 Default Profile 也占一个名额。

    实测会卡在 11/12 —— 第 12 个建不出来。这个 1 不能省。
    """
    assert AdsPowerQuota.TOTAL == 11


def test_reserved_adds_up_to_total():
    assert sum(AdsPowerQuota.DEFAULT_RESERVED.values()) == AdsPowerQuota.TOTAL


def test_total_never_exceeds_the_cap(q):
    got = sum(1 for _ in range(20) if q.acquire(A, timeout=0) or q.acquire(B, timeout=0))
    assert q.total_held() <= 11
    assert got == 11, f'总共只该发出 11 个，实际 {got}'


def test_each_platform_gets_its_reserved_share(q):
    for _ in range(7):
        assert q.acquire(A, timeout=0) is True
    for _ in range(4):
        assert q.acquire(B, timeout=0) is True
    assert q.held(A) == 7 and q.held(B) == 4


def test_reserved_share_is_guaranteed_even_when_the_other_is_greedy(q):
    """B 先抢，也不能吃掉 A 的自有额度。"""
    for _ in range(4):
        q.acquire(B, timeout=0)
    for _ in range(7):
        assert q.acquire(A, timeout=0) is True, 'A 的自有额度被 B 吃掉了'


# ---------- 借用（AC7） ----------

def test_can_borrow_when_the_other_is_idle(q):
    """对方空闲时可以超出自有额度借用，但总数仍受上限约束。"""
    for _ in range(7):
        q.acquire(A, timeout=0)
    assert q.acquire(A, timeout=0) is True, 'B 空闲时 A 应能借'
    assert q.held(A) == 8
    assert q.total_held() <= 11


def test_borrowing_still_respects_the_total(q):
    for _ in range(11):
        q.acquire(A, timeout=0)
    assert q.acquire(A, timeout=0) is False, '借用不能突破总上限'
    assert q.total_held() == 11


def test_recall_asks_only_from_platforms_that_actually_borrowed(q):
    """归还请求只发给**确实超出自己额度**的平台。"""
    for _ in range(9):          # A 借了 2 个（自有 7）
        q.acquire(A, timeout=0)

    asked = q.request_recall(B)
    assert asked > 0
    assert q.recall_pending(A) > 0, '没向借用方要回额度'
    assert q.recall_pending(B) == 0, '不该向请求方自己发归还请求'


def test_no_recall_when_nobody_borrowed(q):
    for _ in range(7):
        q.acquire(A, timeout=0)
    assert q.request_recall(B) == 0, '没人借用时不该发归还请求'


def test_borrower_stops_borrowing_once_recalled(q):
    """被要求归还后不能再借——否则原主永远等不到。"""
    for _ in range(9):
        q.acquire(A, timeout=0)
    q.request_recall(B)

    assert q.acquire(A, timeout=0) is False, '被要求归还了还在继续借'


def test_recall_does_not_block_using_your_own_reserved_share(q):
    """归还请求只拦「借」，不拦「用自己的」——自有额度本来就是它的。"""
    for _ in range(9):
        q.acquire(A, timeout=0)
    q.request_recall(B)
    for _ in range(3):          # A 还到 6 个（低于自有 7）
        q.release(A)

    assert q.acquire(A, timeout=0) is True, '自有额度内被误拦了'


def test_release_decrements_the_recall(q):
    for _ in range(9):
        q.acquire(A, timeout=0)
    q.request_recall(B)
    pending = q.recall_pending(A)

    q.release(A)
    assert q.recall_pending(A) == pending - 1


# ---------- 等待而非报错（AC8） ----------

def test_acquire_waits_instead_of_failing(q):
    """配额是共用资源，对方跑完就会释放。直接判失败会让账号白白进失败集合。"""
    for _ in range(11):
        q.acquire(A, timeout=0)

    def free_it():
        time.sleep(0.3)
        q.release(A)

    threading.Thread(target=free_it, daemon=True).start()
    t0 = time.time()
    assert q.acquire(B, timeout=5) is True, '没等到释放就放弃了'
    assert time.time() - t0 >= 0.25, '看起来根本没等'


def test_acquire_times_out_rather_than_hanging_forever(q):
    for _ in range(11):
        q.acquire(A, timeout=0)
    t0 = time.time()
    assert q.acquire(B, timeout=0.5) is False
    assert time.time() - t0 < 3, '超时没生效，会一直挂着'


def test_should_stop_interrupts_the_wait_promptly(q):
    """用户点停止时要立刻退出，不必等满 timeout。"""
    for _ in range(11):
        q.acquire(A, timeout=0)

    stop = threading.Event()
    threading.Thread(target=lambda: (time.sleep(0.2), stop.set()), daemon=True).start()

    t0 = time.time()
    assert q.acquire(B, timeout=30, should_stop=stop.is_set) is False
    assert time.time() - t0 < 5, '停止信号没能及时打断等待'


# ---------- 释放 ----------

def test_release_below_zero_is_a_noop(q):
    q.release(A)
    assert q.held(A) == 0, '多还了一次就变成负数，会凭空多出额度'


def test_release_frees_capacity_for_the_other_platform(q):
    for _ in range(11):
        q.acquire(A, timeout=0)
    assert q.acquire(B, timeout=0) is False
    q.release(A)
    assert q.acquire(B, timeout=0) is True


# ---------- 并发（这才是真正要防的） ----------

def test_concurrent_acquire_never_exceeds_the_cap():
    """多线程同时抢，任何时刻都不能超限，也不能死锁。

    配额仲裁器的全部意义就在并发场景，单线程测试证明不了什么。
    """
    q = AdsPowerQuota(total=11, reserved={A: 7, B: 4})
    peak = [0]
    peak_lock = threading.Lock()
    barrier = threading.Barrier(22)

    def worker(plat):
        barrier.wait()                       # 逼所有线程同时冲
        if q.acquire(plat, timeout=2):
            with peak_lock:
                peak[0] = max(peak[0], q.total_held())
            time.sleep(0.01)
            q.release(plat)

    ts = [threading.Thread(target=worker, args=(A if i % 2 else B,)) for i in range(22)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in ts), '有线程卡住了——死锁'
    assert peak[0] <= 11, f'并发下超限了，峰值 {peak[0]}'
    assert q.total_held() == 0, '全部释放后仍有残留'


def test_unknown_platform_still_gets_some_share():
    """没在 reserved 里声明的平台不能拿不到任何额度——新接平台时忘了配也得能跑。"""
    q = AdsPowerQuota(total=11, reserved={A: 7})
    assert q.reserved_for('brandnew') > 0
    assert q.acquire('brandnew', timeout=0) is True


# ---------- 接入点：仲裁器写对 ≠ 接对了 ----------

def test_factory_acquires_before_entering_the_pool():
    """配额必须在**进池之前**取。

    池的 _lock 串行化「挑代理→建环境→撞配额→回收→重试」整条链。持着池锁再去等
    配额的话，释放方永远拿不到池锁来删环境 —— 直接死锁。顺序只能是「先配额，后池」。
    """
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.browser_factory)
    i_acq = src.index('quota.acquire')
    i_create = src.index('create_driver_adspower(email')
    assert i_acq < i_create, '在建环境之后才取配额，会死锁'


def test_factory_gives_the_quota_back_when_the_session_fails_to_start():
    """没起来就得立刻还，否则额度只出不进，几个账号之后再也起不来浏览器。"""
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.browser_factory)
    assert 'except BaseException' in src and '_give_back()' in src, \
        '建会话失败的路径没有归还额度'
    assert '_on_closed' in src, '没挂关闭回调，会话关掉后额度不会归还'


def test_close_driver_triggers_the_release_hook():
    """close_driver 是所有关闭路径的唯一收口，回收挂在这里才不会漏。"""
    from src.browser.driver import close_driver

    called = []

    class _S:
        def quit(self):
            pass
    s = _S()
    s._on_closed = lambda: called.append(1)

    close_driver(s)
    assert called == [1]


def test_release_hook_runs_even_if_quit_raises():
    from src.browser.driver import close_driver

    called = []

    class _S:
        def quit(self):
            raise RuntimeError('浏览器已经死了')
    s = _S()
    s._on_closed = lambda: called.append(1)

    close_driver(s)
    assert called == [1], 'quit 抛异常就不还额度的话，浏览器崩一次就漏一个额度'


def test_quota_exhaustion_no_longer_stops_the_whole_run():
    """配额拿不到只该跳过本账号，不能置全局 stop。

    曾经的理由是「配额是全局的，下一个账号必然撞同一堵墙」。多平台并发后不成立：
    配额由仲裁器管、拿不到会先等；而按平台拆分后置 stop 只停自己、配额却是共用的
    —— 结果是一个平台饿死在等待、另一个反复抛错自杀。
    """
    import inspect
    from src.web.app import AppState

    for fn in (AppState.run_daily_pipeline, AppState.run_daily_subscribe_pipeline):
        src = inspect.getsource(fn)
        i = src.index('except AdsPowerError')
        block = src[i:i + 1200]
        assert 'self.stop_requested = True' not in block, \
            f'{fn.__name__}: 配额异常仍在置全局 stop'
        assert 'request_recall' in block, \
            f'{fn.__name__}: 没有向借用方请求归还额度'


def test_teardown_reconciles_leaked_quota():
    """异常路径可能漏还。收尾时本平台不该再持有任何额度。"""
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState._stop_started_adspower)
    assert 'quota.held(self.platform)' in src and 'quota.release' in src, \
        '收尾没有对账，泄漏的额度会一直占着'


# ---------- 手动打开的会话（账号列表的「查看」） ----------


def test_manual_open_uses_the_accounts_adspower_environment():
    """「查看」必须开该账号的 AdsPower 环境，不能退回本地 Chrome profile。

    登录态（GitHub cookie / 平台 session）全在那个环境里；本地
    data/profiles/<email> 是另一个几乎空的目录。开错的后果不只是「看不到东西」：
    ensure_session 会在这个错误的环境里重新走一遍 OAuth，白白给账号多一次
    新设备登录记录。
    """
    import inspect
    from src.api import routes

    src = inspect.getsource(routes.open_account_browser)
    i_factory = src.index('state.browser_factory(')
    i_local = src.index('create_driver(headless=False')
    assert i_factory < i_local, 'AdsPower 分支必须在本地 profile 分支之前'
    assert 'track_for_teardown=False' in src, \
        '手动会话必须声明不纳入任务收尾，否则跑完任务会被顺手关掉'


def test_manual_session_is_not_tracked_for_pipeline_teardown():
    """手动会话不进 _adspower_started。

    进去的话，任何一次流水线跑完调 _stop_started_adspower() 都会关掉用户正在看的
    浏览器；那里还有一段「把本平台仍持有的配额全部当泄漏还掉」的对账，会把手动
    会话那一份也还掉，等用户真关浏览器时 _on_closed 再还一次——配额凭空多一个。
    """
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.browser_factory)
    assert 'if pid and track_for_teardown:' in src, \
        '手动会话仍会被登记进 _adspower_started'


def test_manual_session_ignores_a_leftover_stop_flag():
    """手动开浏览器不看 stop_requested——它是跨任务残留的。

    只有三条流水线入口会复位它，「打开浏览器」不会。挂着它的话，只要之前停过一次
    任务，此后每次点「查看」都会在第一次检查点立刻放弃、报「等待配额超时」，
    而配额其实是空的。
    """
    import inspect
    from src.web.app import AppState

    src = inspect.getsource(AppState.browser_factory)
    assert '_should_stop = (lambda: self.stop_requested) if track_for_teardown else None' in src, \
        '手动会话仍被任务的停止标志掐着'
