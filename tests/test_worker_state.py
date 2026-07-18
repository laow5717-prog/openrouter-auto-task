"""WorkerState 隔离与日志路由测试。

重点验证 R4.2：并发下两个 worker 的日志不得串台。这个性质靠 contextvars
"新线程不继承绑定"来保证，一旦有人误把绑定改成全局变量，这里会红。
"""

import threading

from src.web.app import AppState
from src.web.worker import WorkerState, bind_current_worker, get_current_worker


class _FakeModels(dict):
    pass


def _state():
    return AppState(db=None, models=_FakeModels())


def test_logs_are_isolated_between_workers():
    st = _state()
    w1, w2 = st.ensure_workers(2)
    done = threading.Barrier(3)

    def emit(worker, messages):
        bind_current_worker(worker)
        for m in messages:
            st.dispatch_print(m)
        done.wait()

    threading.Thread(target=emit, args=(w1, ['a1', 'a2'])).start()
    threading.Thread(target=emit, args=(w2, ['b1', 'b2'])).start()
    done.wait()

    logs1, _ = w1.get_logs()
    logs2, _ = w2.get_logs()

    assert all('a' in line for line in logs1), f"W1 日志被污染: {logs1}"
    assert all('b' in line for line in logs2), f"W2 日志被污染: {logs2}"
    assert len(logs1) == 2 and len(logs2) == 2


def test_aggregate_stream_tags_worker_id():
    st = _state()
    st.parallel_mode = True          # 前缀仅在并行时添加
    w1, w2 = st.ensure_workers(2)
    barrier = threading.Barrier(3)

    def emit(worker, msg):
        bind_current_worker(worker)
        st.dispatch_print(msg)
        barrier.wait()

    threading.Thread(target=emit, args=(w1, 'hello')).start()
    threading.Thread(target=emit, args=(w2, 'world')).start()
    barrier.wait()

    agg = st.get_logs()
    assert any('[W1] hello' in line for line in agg)
    assert any('[W2] world' in line for line in agg)


def test_unbound_thread_logs_only_to_aggregate():
    """串行路径/请求线程未绑定 worker 时，行为与改造前一致。"""
    st = _state()
    st.dispatch_print('serial message')

    assert get_current_worker() is None
    agg = st.get_logs()
    assert any('serial message' in line for line in agg)
    assert not any('[W' in line for line in agg)
    assert w_logs_empty(st)


def w_logs_empty(st):
    logs, _ = st.primary_worker.get_logs()
    return logs == []


def test_new_thread_does_not_inherit_binding():
    """契约测试：contextvars 不跨线程继承 —— worker 隔离正是建立在这上面。"""
    st = _state()
    w1 = st.primary_worker
    bind_current_worker(w1)
    seen = {}

    def child():
        seen['worker'] = get_current_worker()

    t = threading.Thread(target=child)
    t.start()
    t.join()

    assert get_current_worker() is w1
    assert seen['worker'] is None, "子线程继承了 worker 绑定，隔离前提被破坏"


def test_log_seq_advances_and_supports_incremental_fetch():
    w = WorkerState('W9')
    for i in range(5):
        w.add_log(f'msg{i}')

    logs, nxt = w.get_logs(0)
    assert len(logs) == 5 and nxt == 5

    w.add_log('msg5')
    logs, nxt = w.get_logs(nxt)
    assert len(logs) == 1 and 'msg5' in logs[0] and nxt == 6


def test_log_buffer_evicts_but_seq_keeps_growing():
    w = WorkerState('W9')
    total = WorkerState.MAX_LOGS + 10
    for i in range(total):
        w.add_log(f'msg{i}')

    assert w.log_seq == total
    logs, nxt = w.get_logs(0)
    assert len(logs) == WorkerState.MAX_LOGS      # 旧的被淘汰
    assert nxt == total
    assert 'msg{}'.format(total - 1) in logs[-1]


def test_force_stop_touches_every_worker():
    st = _state()
    workers = st.ensure_workers(3)
    for w in workers:
        w.set_active_driver(object())
        w._screenshot_stop.clear()

    st.force_stop()

    assert st.stop_requested
    for w in workers:
        assert w._active_driver is None
        assert w._screenshot_stop.is_set()


def test_frames_are_per_worker():
    st = _state()
    w1, w2 = st.ensure_workers(2)
    w1.update_frame(b'frame-1')
    w2.update_frame(b'frame-2')

    assert w1.get_frame() == b'frame-1'
    assert w2.get_frame() == b'frame-2'
    assert st.get_frame() == b'frame-1'       # 旧接口委托给主 worker


def test_monitor_raises_on_stop_request():
    st = _state()
    w = st.primary_worker
    monitor = w.make_monitor(st)

    monitor(_FakeDriver(), 'step')            # 未请求停止时正常返回
    st.stop_requested = True
    try:
        monitor(_FakeDriver(), 'step')
        assert False, "停止请求下 monitor 未抛出 InterruptedError"
    except InterruptedError:
        pass


class _FakeDriver:
    def get_screenshot_as_png(self):
        return b''
