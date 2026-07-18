"""账号与支付卡的并发排他测试。

这两个注册表守的是并行执行最容易出错的两处：
- AccountRegistry: 同一 Chrome profile 并发 → 互删 Singleton 锁 → 浏览器崩溃
- PaymentCardRegistry: 选卡资格闸门是快照式的 → 同一张卡被两个账号同时使用
"""

import threading

from src.web.app import AppState
from src.web.worker import bind_current_worker


def _state():
    return AppState(db=None, models={})


# ==================== AccountRegistry ====================


def test_only_one_worker_can_claim_an_email():
    st = _state()
    reg = st.account_registry
    results = []
    barrier = threading.Barrier(4)

    def attempt(worker):
        bind_current_worker(worker)
        barrier.wait()
        results.append(reg.claim('shared@example.com'))

    workers = st.ensure_workers(3)
    threads = [threading.Thread(target=attempt, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"同一 email 被多次领取: {results}"
    assert results.count(False) == 2


def test_release_allows_reclaim():
    reg = _state().account_registry
    assert reg.claim('a@example.com') is True
    assert reg.claim('a@example.com') is False
    reg.release('a@example.com')
    assert reg.claim('a@example.com') is True


def test_claim_rejects_email_with_manual_browser_open():
    """用户手动打开的浏览器占着同一个 profile 目录，worker 必须避让。"""
    st = _state()
    st.open_browsers.add('manual@example.com')
    assert st.account_registry.claim('manual@example.com') is False


def test_manual_open_rejected_when_worker_holds_email():
    """反向：worker 正在用的账号，用户不能手动打开。"""
    st = _state()
    w = st.primary_worker
    bind_current_worker(w)
    st.account_registry.claim('busy@example.com')

    ok, reason = st.account_registry.try_open_manual('busy@example.com')
    assert ok is False
    assert 'W1' in reason


def test_manual_open_rejected_when_already_open():
    st = _state()
    ok, _ = st.account_registry.try_open_manual('x@example.com')
    assert ok is True
    ok, reason = st.account_registry.try_open_manual('x@example.com')
    assert ok is False
    assert '已有浏览器打开' in reason


def test_manual_open_registers_in_open_browsers():
    st = _state()
    st.account_registry.try_open_manual('x@example.com')
    assert 'x@example.com' in st.open_browsers
    # 登记后 worker 就抢不到了
    assert st.account_registry.claim('x@example.com') is False


def test_manual_open_and_worker_claim_are_mutually_exclusive_under_race():
    """并发下 try_open_manual 与 claim 必须共用一把锁，只能有一方成功。"""
    st = _state()
    reg = st.account_registry
    email = 'race@example.com'
    outcome = {}
    barrier = threading.Barrier(3)

    def as_worker():
        bind_current_worker(st.primary_worker)
        barrier.wait()
        outcome['worker'] = reg.claim(email)

    def as_manual():
        barrier.wait()
        outcome['manual'] = reg.try_open_manual(email)[0]

    t1, t2 = threading.Thread(target=as_worker), threading.Thread(target=as_manual)
    t1.start(), t2.start()
    barrier.wait()
    t1.join(), t2.join()

    assert [outcome['worker'], outcome['manual']].count(True) == 1, \
        f"worker 与手动会话同时占用了同一 profile: {outcome}"


def test_empty_email_is_never_claimable():
    reg = _state().account_registry
    assert reg.claim('') is False
    assert reg.claim(None) is False


# ==================== PaymentCardRegistry ====================


def test_card_cannot_be_used_by_two_accounts():
    reg = _state().payment_registry
    assert reg.try_acquire('4111111111111111', 'a@example.com') is True
    assert reg.try_acquire('4111111111111111', 'b@example.com') is False


def test_card_reacquire_by_same_account_is_idempotent():
    """同一账号内一张卡可支付多笔，重复占用必须成功。"""
    reg = _state().payment_registry
    assert reg.try_acquire('4111', 'a@example.com') is True
    assert reg.try_acquire('4111', 'a@example.com') is True


def test_card_release_frees_it_for_others():
    reg = _state().payment_registry
    reg.try_acquire('4111', 'a@example.com')
    reg.release('4111')
    assert reg.try_acquire('4111', 'b@example.com') is True


def test_concurrent_card_acquire_has_single_winner():
    reg = _state().payment_registry
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def attempt(i):
        barrier.wait()
        ok = reg.try_acquire('4111', f'acct{i}@example.com')
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"一张卡被多个账号同时占用: {results}"


def test_empty_card_number_is_never_acquirable():
    reg = _state().payment_registry
    assert reg.try_acquire('', 'a@example.com') is False
    assert reg.try_acquire(None, 'a@example.com') is False


# ==================== 回归：争用不等于耗尽 ====================


def test_release_lets_a_waiting_worker_proceed():
    """按笔释放：一笔支付有结果后，别的 worker 应立刻能用上这张卡。

    实跑中踩过的坑：早先实现把卡占到整个账号处理结束，卡池偏紧时其它 worker
    一张都拿不到，被 registration 误判成「卡池已耗尽」，编排层据此把该账号
    永久放弃（done[email]='no_cards'）。临时争用被当成了永久耗尽。"""
    reg = _state().payment_registry

    # W1 取卡付款
    assert reg.try_acquire('4111', 'a@example.com') is True
    # W2 此刻拿不到
    assert reg.try_acquire('4111', 'b@example.com') is False
    # W1 这笔付完就放
    reg.release('4111')
    # W2 立刻可用——不必等 W1 整个账号跑完
    assert reg.try_acquire('4111', 'b@example.com') is True


def test_in_flight_set_empties_after_each_attempt():
    """按笔释放后，登记表不应残留——残留会让卡永远拿不到。"""
    reg = _state().payment_registry
    for i in range(5):
        num = f'411{i}'
        reg.try_acquire(num, 'a@example.com')
        reg.release(num)
    assert reg.in_flight_numbers() == set()
