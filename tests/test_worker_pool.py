"""WorkerPool 调度语义测试。

重点：
- max_workers=1 必须走同线程分支（串行等价性的结构性保证，R1.2）
- map 的 barrier 语义与结果顺序
- 单项异常不拖垮整批
- 每项工作都跑在已绑定 worker 的线程里（日志才能进对分栏）
"""

import threading

from src.web.app import AppState
from src.web.worker import WorkerPool, clamp_workers, get_current_worker


def _pool(max_workers):
    st = AppState(db=None, models={})
    return st, WorkerPool(st, max_workers)


# ==================== 并发度夹紧 ====================


def test_clamp_workers_bounds():
    assert clamp_workers(0) == 1
    assert clamp_workers(1) == 1
    assert clamp_workers(4) == 4
    assert clamp_workers(99) == 4
    assert clamp_workers(-5) == 1


def test_clamp_workers_handles_garbage():
    assert clamp_workers(None) == 1
    assert clamp_workers('abc') == 1
    assert clamp_workers('3') == 3


def test_clamp_logs_when_adjusting():
    st, pool = _pool(99)
    assert pool.max_workers == 4
    assert any('已调整为 4' in line for line in st.get_logs())


# ==================== 串行等价性 ====================


def test_serial_pool_uses_no_threads():
    """max_workers=1 必须同线程执行——这是 R1.2 的结构性保证。"""
    st, pool = _pool(1)
    assert pool.is_serial
    main_thread = threading.current_thread()
    seen = []

    pool.map([1, 2, 3], lambda w, item: seen.append(threading.current_thread()))
    assert all(t is main_thread for t in seen), "串行模式创建了线程"


def test_serial_pool_does_not_set_parallel_mode():
    st, pool = _pool(1)
    assert st.parallel_mode is False


def test_parallel_pool_sets_parallel_mode():
    st, pool = _pool(2)
    assert st.parallel_mode is True


def test_serial_binding_does_not_leak_to_coordinator():
    """串行分支跑在协调线程上，执行完必须复位绑定，否则后续阶段日志会被错记。"""
    st, pool = _pool(1)
    assert get_current_worker() is None
    pool.map([1], lambda w, item: None)
    assert get_current_worker() is None, "worker 绑定泄漏到了协调线程"


# ==================== map ====================


def test_map_preserves_order():
    st, pool = _pool(3)
    result = pool.map(range(20), lambda w, item: item * 2)
    assert result == [i * 2 for i in range(20)]


def test_map_is_a_barrier():
    """map 返回时所有工作都已结束（阶段2 每轮一次的语义依赖这点）。"""
    st, pool = _pool(3)
    done = []
    lock = threading.Lock()

    def work(w, item):
        with lock:
            done.append(item)

    pool.map(range(30), work)
    assert len(done) == 30


def test_map_distributes_across_workers():
    st, pool = _pool(3)
    seen_workers = set()
    lock = threading.Lock()

    def work(w, item):
        with lock:
            seen_workers.add(w.worker_id)
        # 让所有 worker 都有机会取到活
        threading.Event().wait(0.01)

    pool.map(range(30), work)
    assert len(seen_workers) > 1, "工作没有分发到多个 worker"


def test_map_empty_items_is_noop():
    st, pool = _pool(2)
    assert pool.map([], lambda w, item: 1 / 0) == []


def test_every_item_runs_bound_to_its_worker():
    st, pool = _pool(2)
    bindings = pool.map(range(10), lambda w, item: get_current_worker() is w)
    assert all(bindings), "有工作跑在未绑定 worker 的线程里"


# ==================== 异常隔离 ====================


def test_single_failure_does_not_abort_batch():
    st, pool = _pool(2)

    def work(w, item):
        if item == 3:
            raise ValueError('boom')
        return item

    result = pool.map(range(6), work)
    assert result == [0, 1, 2, None, 4, 5]
    assert any('boom' in line for line in st.get_logs())


def test_interrupted_error_sets_global_stop():
    """用户停止：一个 worker 感知到就让全局收敛。"""
    st, pool = _pool(2)

    def work(w, item):
        if item == 0:
            raise InterruptedError('user stop')
        return item

    pool.map(range(4), work)
    assert st.stop_requested is True


# ==================== run_until_empty ====================


def test_run_until_empty_consumes_until_none():
    st, pool = _pool(3)
    items = list(range(25))
    lock = threading.Lock()

    def produce():
        with lock:
            return items.pop(0) if items else None

    results = pool.run_until_empty(produce, lambda w, item: item)
    assert sorted(r for r in results if r is not None) == list(range(25))
    assert items == []


def test_run_until_empty_serial_mode():
    st, pool = _pool(1)
    items = [1, 2, 3]

    def produce():
        return items.pop(0) if items else None

    assert pool.run_until_empty(produce, lambda w, item: item * 10) == [10, 20, 30]


def test_run_until_empty_stops_immediately_when_nothing_to_do():
    st, pool = _pool(2)
    assert pool.run_until_empty(lambda: None, lambda w, item: 1 / 0) == []


# ==================== 收尾 ====================


def test_worker_released_after_run():
    st, pool = _pool(2)
    pool.map(range(4), lambda w, item: w.set_active_driver(object()))

    for w in pool.workers:
        assert w.busy is False
        assert w._active_driver is None
        assert w.current_action == "空闲"
