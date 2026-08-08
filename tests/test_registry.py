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


OC = 'opencode'
OTHER = 'infron'


# ==================== PaymentCardRegistry ====================


def test_card_cannot_be_used_by_two_accounts():
    reg = _state().payment_registry
    assert reg.try_acquire(OC, '4111111111111111', 'a@example.com') is True
    assert reg.try_acquire(OC, '4111111111111111', 'b@example.com') is False


def test_card_reacquire_by_same_account_is_idempotent():
    """同一账号内一张卡可支付多笔，重复占用必须成功。"""
    reg = _state().payment_registry
    assert reg.try_acquire(OC, '4111', 'a@example.com') is True
    assert reg.try_acquire(OC, '4111', 'a@example.com') is True


def test_card_release_frees_it_for_others():
    reg = _state().payment_registry
    reg.try_acquire(OC, '4111', 'a@example.com')
    reg.release('4111')
    assert reg.try_acquire(OC, '4111', 'b@example.com') is True


def test_concurrent_card_acquire_has_single_winner():
    reg = _state().payment_registry
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def attempt(i):
        barrier.wait()
        ok = reg.try_acquire(OC, '4111', f'acct{i}@example.com')
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
    assert reg.try_acquire(OC, '', 'a@example.com') is False
    assert reg.try_acquire(OC, None, 'a@example.com') is False


# ==================== 回归：争用不等于耗尽 ====================


def test_release_lets_a_waiting_worker_proceed():
    """按笔释放：一笔支付有结果后，别的 worker 应立刻能用上这张卡。

    实跑中踩过的坑：早先实现把卡占到整个账号处理结束，卡池偏紧时其它 worker
    一张都拿不到，被 registration 误判成「卡池已耗尽」，编排层据此把该账号
    永久放弃（done[email]='no_cards'）。临时争用被当成了永久耗尽。"""
    reg = _state().payment_registry

    # W1 取卡付款
    assert reg.try_acquire(OC, '4111', 'a@example.com') is True
    # W2 此刻拿不到
    assert reg.try_acquire(OC, '4111', 'b@example.com') is False
    # W1 这笔付完就放
    reg.release('4111')
    # W2 立刻可用——不必等 W1 整个账号跑完
    assert reg.try_acquire(OC, '4111', 'b@example.com') is True


def test_in_flight_set_empties_after_each_attempt():
    """按笔释放后，登记表不应残留——残留会让卡永远拿不到。"""
    reg = _state().payment_registry
    for i in range(5):
        num = f'411{i}'
        reg.try_acquire(OC, num, 'a@example.com')
        reg.release(num)
    assert reg.in_flight_numbers() == set()


def test_used_numbers_survives_release():
    """本轮归属在 release 后仍保留——它是选卡层去重的依据，不能随 in-flight 一起消失。"""
    reg = _state().payment_registry
    reg.try_acquire(OC, '4444', 'a@example.com')
    reg.release('4444')
    assert '4444' in reg.used_numbers(OC)
    assert '4444' not in reg.in_flight_numbers()


def test_release_all_clears_round_ownership():
    """整轮结束后归属清空，下一轮这张卡可以给别的账号用。"""
    reg = _state().payment_registry
    reg.try_acquire(OC, '4333', 'a@example.com')
    reg.release('4333')
    reg.release_all()
    assert reg.used_numbers(OC) == set()


def test_used_records_first_account_only():
    """归属记首个使用者；同一账号重复取不改归属。"""
    reg = _state().payment_registry
    reg.try_acquire(OC, '4555', 'a@example.com')
    reg.release('4555')
    reg.try_acquire(OC, '4555', 'a@example.com')
    assert reg._used[(OC, '4555')] == 'a@example.com'


# ==================== 选卡层：同一轮不重复用卡 ====================


def _cards(*numbers):
    return [{'number': n} for n in numbers]


def test_eligible_cards_excludes_cards_used_by_other_accounts():
    """核心回归：本轮已被别的账号试过的卡，不再发给下一个账号。

    2026-08-03 实测：_eligible_cards 是每账号进入时的快照，两个 worker 从同一有序列表
    头部出发只差几十秒，一轮里 5 张卡被两个账号各刷一次；第二次注定失败（第一次已被拒
    并标 invalid，只是后者的快照更早），白烧一次拒付还叠加风控 velocity。
    """
    state = _state()
    state.payment_registry.try_acquire(OC, '4111', 'a@example.com')
    state.payment_registry.release('4111')
    left = state._exclude_used_this_run(OC, _cards('4111', '4222', '4333'))
    assert [c['number'] for c in left] == ['4222', '4333']


def test_eligible_cards_falls_back_when_all_used():
    """全被试过时必须放行，否则「暂时争用」会被 registration 误判成「卡池耗尽」，
    编排层据此永久放弃该账号——这个坑早先踩过。"""
    state = _state()
    for n in ('4111', '4222'):
        state.payment_registry.try_acquire(OC, n, 'a@example.com')
        state.payment_registry.release(n)
    left = state._exclude_used_this_run(OC, _cards('4111', '4222'))
    assert [c['number'] for c in left] == ['4111', '4222']


def test_exclude_used_normalises_spaces_in_card_numbers():
    """卡池里的卡号可能带空格，登记表存的是原样串，比对必须去空格后再比。"""
    state = _state()
    state.payment_registry.try_acquire(OC, '4111222233334444', 'a@example.com')
    left = state._exclude_used_this_run(OC, _cards('4111 2222 3333 4444', '4222'))
    assert [c['number'] for c in left] == ['4222']


# ==================== 选卡层：好卡排在新卡前面 ====================


def test_eligible_cards_puts_proven_cards_first(db):
    """已成功付款过的卡排队首，没验证过的新卡垫后。

    次序是反过来改的：早先新卡优先，想的是先把卡池消化掉。实际跑下来那等于笔笔都拿
    一张没验证过的卡赌运气——拒付率高、还给账号叠 velocity 风控，而少数真能过款的卡
    被晾在队尾。现在能过款的卡先用，新卡只在好卡全进冷却或判废后才被动用。
    """
    from src.models.card_group import CardGroupModel
    from src.models.card_pool import CardPoolModel
    from src.web.app import build_models

    gid = CardGroupModel(db).create('g', 'payment')
    raw = [{'number': f'411100000000000{i}', 'expiry_month': '12', 'expiry_year': '2030',
            'cvc': '123', 'first_name': 'T', 'last_name': 'U', 'country': 'US',
            'address': 'a', 'city': 'c', 'state': 's', 'zip': '1'} for i in range(4)]
    CardPoolModel(db).add_cards(gid, raw)

    models = build_models(db)
    proven = raw[2]['number']                      # 队列中段的一张，排序真的动过才会跑到队首
    log_id = models['recharge_log'].create(OC, 'a@example.com', proven, amount=20)
    models['recharge_log'].mark_success(log_id)

    state = AppState(db, models, platform=OC)
    got = [c['number'] for c in state._eligible_cards(gid, exclude_used=False)]

    assert got[0] == proven, f'成功过的卡该排队首，实际次序 {got}'
    assert sorted(got) == sorted(c['number'] for c in raw), '不该漏卡或多卡'


# ==================== 跨平台：两级排他的语义刻意相反（AC6） ====================


def test_used_is_isolated_per_platform():
    """本轮归属按平台隔离：在 opencode 试过的卡，不影响另一个平台的选卡。

    _used 纯粹是选卡策略——避免同一轮里两个账号做重复功。「在 opencode 试过」
    对另一个平台没有参考意义，那边它还是张没人碰过的新卡。
    """
    state = _state()
    state.payment_registry.try_acquire(OC, '4111', 'a@example.com')
    state.payment_registry.release('4111')

    assert state.payment_registry.used_numbers(OC) == {'4111'}
    assert state.payment_registry.used_numbers(OTHER) == set()

    left = state._exclude_used_this_run(OTHER, _cards('4111', '4222'))
    assert [c['number'] for c in left] == ['4111', '4222'], "别的平台不该被 opencode 的归属挡住"


def test_in_flight_stays_global_across_platforms():
    """in-flight **不**按平台隔离：同一张卡不能同时在两个平台提交支付。

    这不是并发正确性问题，是业务风险——发卡行看的是卡，不是我们在跑哪个平台。
    同一张卡在两处同时扣款会叠加 velocity 风控，比单平台重复刷更容易被拒甚至锁卡。
    """
    reg = _state().payment_registry
    assert reg.try_acquire(OC, '4111', 'a@example.com') is True
    assert reg.try_acquire(OTHER, '4111', 'b@example.com') is False, \
        "同一张卡在另一个平台也必须被 in-flight 拦住"

    reg.release('4111')
    assert reg.try_acquire(OTHER, '4111', 'b@example.com') is True


def test_release_all_by_platform_keeps_other_platform_and_in_flight():
    """轮边界只清本平台的归属：另一个平台的归属与全局 in-flight 都不受影响。"""
    reg = _state().payment_registry
    reg.try_acquire(OC, '4111', 'a@example.com')
    reg.try_acquire(OTHER, '4222', 'b@example.com')

    reg.release_all(OC)

    assert reg.used_numbers(OC) == set()
    assert reg.used_numbers(OTHER) == {'4222'}
    assert reg.in_flight_numbers() == {'4111', '4222'}, "轮边界不该打断全局 in-flight"


def test_release_all_without_platform_clears_everything():
    """任务收尾（不带平台）则全清，含 in-flight。"""
    reg = _state().payment_registry
    reg.try_acquire(OC, '4111', 'a@example.com')
    reg.try_acquire(OTHER, '4222', 'b@example.com')

    reg.release_all()

    assert reg.used_numbers(OC) == set()
    assert reg.used_numbers(OTHER) == set()
    assert reg.in_flight_numbers() == set()
