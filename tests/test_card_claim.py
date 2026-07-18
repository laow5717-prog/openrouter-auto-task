"""card_bindings 并发领取的正确性测试。

这是并行执行的地基：如果 claim_batch 不是原子的，两个 worker 会拿到同一张卡，
导致一卡多绑（违反"一卡绑一账号"）。此处不涉及浏览器，可无副作用地反复运行。
"""

import threading

from conftest import make_cards


def test_claim_batch_is_exclusive(card_model, task_id):
    """同一批卡不会被两个 worker 同时领走 —— 并发正确性的核心断言。"""
    card_model.create_batch(task_id, make_cards(20))

    results = {}
    barrier = threading.Barrier(2)      # 让两个线程尽量同时进入 claim

    def worker(wid):
        barrier.wait()
        claimed = card_model.claim_batch(task_id, wid, limit=10)
        results[wid] = {r['id'] for r in claimed}

    threads = [threading.Thread(target=worker, args=(f'W{i}',)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results['W1'] & results['W2'] == set(), "两个 worker 领到了同一张卡"
    assert len(results['W1']) == 10
    assert len(results['W2']) == 10
    assert card_model.get_pending(task_id) == []


def test_claim_batch_respects_limit_and_availability(card_model, task_id):
    card_model.create_batch(task_id, make_cards(3))

    assert len(card_model.claim_batch(task_id, 'W1', limit=2)) == 2
    # W2 只剩 1 张可领，即使 limit 更大
    assert len(card_model.claim_batch(task_id, 'W2', limit=5)) == 1
    # 卡池已空
    assert card_model.claim_batch(task_id, 'W3', limit=5) == []
    # limit<=0 不应产生任何副作用
    assert card_model.claim_batch(task_id, 'W4', limit=0) == []


def test_claim_batch_returns_only_own_cards(card_model, task_id):
    """claim_batch 回读时必须按 worker_id 过滤，不能带出别人的 processing 记录。"""
    card_model.create_batch(task_id, make_cards(4))
    w1 = card_model.claim_batch(task_id, 'W1', limit=2)
    w2 = card_model.claim_batch(task_id, 'W2', limit=2)

    assert {r['worker_id'] for r in w1} == {'W1'}
    assert {r['worker_id'] for r in w2} == {'W2'}


def test_claimed_cards_carry_parsed_card_data(card_model, task_id):
    """领取结果必须带解析好的 card 字典，与 get_pending 的契约一致。"""
    card_model.create_batch(task_id, make_cards(1))
    claimed = card_model.claim_batch(task_id, 'W1', limit=1)

    assert claimed[0]['card']['number'] == '4111000000000000'
    assert claimed[0]['card']['cvc'] == '123'


def test_release_unused_returns_cards_to_pool(card_model, task_id):
    card_model.create_batch(task_id, make_cards(5))
    card_model.claim_batch(task_id, 'W1', limit=5)

    assert card_model.get_pending(task_id) == []
    assert card_model.release_unused(task_id, 'W1') == 5

    back = card_model.get_pending(task_id)
    assert len(back) == 5
    assert all(r['worker_id'] == '' and r['claimed_at'] is None for r in back)
    # 释放后可被另一 worker 正常领取
    assert len(card_model.claim_batch(task_id, 'W2', limit=5)) == 5


def test_release_unused_leaves_finished_cards_alone(card_model, task_id):
    """已 success/failed 的卡不该被释放回 pending。"""
    card_model.create_batch(task_id, make_cards(3))
    claimed = card_model.claim_batch(task_id, 'W1', limit=3)
    card_model.mark_success(claimed[0]['id'], 'a@example.com')
    card_model.mark_failed(claimed[1]['id'], 'boom')

    assert card_model.release_unused(task_id, 'W1') == 1        # 只剩第 3 张
    summary = card_model.get_summary(task_id)
    assert summary['success'] == 1
    assert summary['failed'] == 1
    assert summary['pending'] == 1


def test_reap_stale_only_reclaims_timed_out_rows(card_model, task_id, db):
    card_model.create_batch(task_id, make_cards(2))
    claimed = card_model.claim_batch(task_id, 'W1', limit=2)

    # 把第一张的领取时间人为回拨 30 分钟，模拟失联 worker
    db.execute(
        "UPDATE card_bindings SET claimed_at=datetime('now','localtime','-30 minutes') WHERE id=?",
        (claimed[0]['id'],),
    )

    assert card_model.reap_stale(timeout_minutes=20) == 1
    pending = card_model.get_pending(task_id)
    assert [r['id'] for r in pending] == [claimed[0]['id']]
    # 未超时的那张仍归 W1 持有
    assert card_model.release_unused(task_id, 'W1') == 1


def test_reap_stale_noop_when_nothing_expired(card_model, task_id):
    card_model.create_batch(task_id, make_cards(2))
    card_model.claim_batch(task_id, 'W1', limit=2)
    assert card_model.reap_stale(timeout_minutes=20) == 0


def test_reset_all_processing_clears_every_worker(card_model, task_id):
    """进程重启语义：所有 worker 都已消失，无条件释放。"""
    card_model.create_batch(task_id, make_cards(6))
    card_model.claim_batch(task_id, 'W1', limit=3)
    card_model.claim_batch(task_id, 'W2', limit=3)

    assert card_model.reset_all_processing() == 6
    assert len(card_model.get_pending(task_id)) == 6


def test_summary_counts_processing_as_pending(card_model, task_id):
    """并发运行时前端进度不能因为卡被领取而掉数。"""
    card_model.create_batch(task_id, make_cards(10))
    before = card_model.get_summary(task_id)['pending']

    card_model.claim_batch(task_id, 'W1', limit=4)
    after = card_model.get_summary(task_id)

    assert before == 10
    assert after['pending'] == 10          # 口径 = 未完成
    assert after['processing'] == 4
    assert card_model.get_global_summary()['processing'] == 4


def test_delete_pending_by_task_also_removes_processing(card_model, task_id):
    """收尾清理时 worker 已退出，残留 processing 等同 pending。"""
    card_model.create_batch(task_id, make_cards(5))
    claimed = card_model.claim_batch(task_id, 'W1', limit=2)
    card_model.mark_success(claimed[0]['id'], 'a@example.com')

    assert card_model.delete_pending_by_task(task_id) == 4      # 1 processing + 3 pending
    assert card_model.get_summary(task_id)['total'] == 1


def test_concurrent_claim_never_double_allocates(card_model, task_id):
    """压力版：4 个 worker 反复小批量领取，总量守恒且无重复。"""
    card_model.create_batch(task_id, make_cards(60))

    seen = []
    seen_lock = threading.Lock()

    def worker(wid):
        while True:
            claimed = card_model.claim_batch(task_id, wid, limit=3)
            if not claimed:
                break
            with seen_lock:
                seen.extend(r['id'] for r in claimed)
            # 立刻结算，腾出 worker_id 以便下一轮领取
            for r in claimed:
                card_model.mark_success(r['id'], f'{wid}@example.com')

    threads = [threading.Thread(target=worker, args=(f'W{i}',)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == 60, "领取总数与卡池不符"
    assert len(set(seen)) == 60, "同一张卡被领取了多次"
    assert card_model.get_summary(task_id)['success'] == 60
