"""日志按平台分流。

改造前的机制是「把某个实例的绑定方法塞进各模块的 globals 当 print」。两个平台
同时跑时会**双重串台**：

1. _patch_prints 各装一次，后装的覆盖先装的 → 所有平台的 print 都进同一个实例
   的日志流。src.payments.stripe_checkout 尤其致命——opencode 与 infron 的
   module_names() 都含它。
2. 钩子里的 self 是绑死的，contextvar 只知道 worker、不知道平台。

现在改成「只装一次的模块级 dispatcher + contextvar 解析归属」。新的失败模式是
**漏绑**：contextvars 不跨线程继承，某个线程入口忘了绑，那条链路的日志就跑到
另一个平台的流里去——不报错、不崩溃。所以每个绑定点都要有测试。
"""

import inspect
import threading

import pytest

import src.web.app as app_mod
from src.web.app import AppState, SharedResources, dispatch_print


@pytest.fixture
def two_platforms():
    shared = SharedResources(db=None, models={})
    a = AppState(db=None, models={}, platform='opencode', shared=shared)
    b = AppState(db=None, models={}, platform='infron', shared=shared)
    return a, b


def _msgs(ctx):
    return [l.split('] ', 1)[-1] for l in ctx.logs]


# ---------- 归属解析 ----------

def test_dispatch_routes_to_the_bound_context(two_platforms):
    a, b = two_platforms

    tok = a.bind_logs()
    dispatch_print('给 A 的')
    a.unbind_logs(tok)

    tok = b.bind_logs()
    dispatch_print('给 B 的')
    b.unbind_logs(tok)

    assert _msgs(a) == ['给 A 的']
    assert _msgs(b) == ['给 B 的'], '日志串到另一个平台去了'


def test_unbound_falls_back_to_plain_print_without_guessing(two_platforms, capsys):
    """无归属时如实退化，**不猜平台**——猜错就是写进另一个平台的流，比丢掉更难查。"""
    a, b = two_platforms
    dispatch_print('没有归属的一行')

    assert a.logs == [] and b.logs == []
    assert '没有归属的一行' in capsys.readouterr().out


def test_binding_is_per_thread(two_platforms):
    """两个线程各绑各的，互不干扰——这是并发分流的基础。"""
    a, b = two_platforms
    barrier = threading.Barrier(2)

    def run(ctx, msg):
        tok = ctx.bind_logs()
        barrier.wait()               # 逼两个线程真正重叠
        dispatch_print(msg)
        ctx.unbind_logs(tok)

    ts = [threading.Thread(target=run, args=(a, 'A 的')),
          threading.Thread(target=run, args=(b, 'B 的'))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert _msgs(a) == ['A 的']
    assert _msgs(b) == ['B 的']


def test_a_new_thread_without_binding_does_not_inherit(two_platforms, capsys):
    """contextvars 不跨线程继承——这正是每个线程入口都必须显式绑的原因。

    这条不是在测「我们的代码对不对」，是把这个语言特性钉在这里：
    将来有人以为「父线程绑了子线程就有」，这条会告诉他不是。
    """
    a, _ = two_platforms
    tok = a.bind_logs()

    def child():
        dispatch_print('子线程的')

    t = threading.Thread(target=child)
    t.start()
    t.join()
    a.unbind_logs(tok)

    assert a.logs == [], '子线程竟然继承了父线程的绑定？那本文件的前提就变了'
    assert '子线程的' in capsys.readouterr().out


# ---------- 钩子只装一次 ----------

def test_patching_is_idempotent():
    """装的是模块级函数，与实例无关；重复装没有意义，反而掩盖「装的是谁」。"""
    app_mod._prints_patched = False
    try:
        app_mod.patch_prints()
        first = app_mod.registration.print
        app_mod.patch_prints()
        assert app_mod.registration.print is first
        assert first is dispatch_print, '钩子必须是模块级 dispatcher，不能是绑定方法'
    finally:
        app_mod._prints_patched = False
        app_mod.patch_prints()


def test_shared_module_is_hooked_to_the_dispatcher():
    """src.payments.stripe_checkout 被两个平台的 module_names() 同时声明。

    旧机制下它必然被后装的实例覆盖，是串台最严重的一处。
    """
    app_mod._prints_patched = False
    app_mod.patch_prints()
    import src.payments.stripe_checkout as sc
    assert sc.print is dispatch_print


def test_shared_module_print_routes_by_context(two_platforms):
    """共享模块里的 print 也要按当前 ctx 分流，而不是固定进某一个平台。"""
    app_mod._prints_patched = False
    app_mod.patch_prints()
    import src.payments.stripe_checkout as sc

    a, b = two_platforms
    tok = a.bind_logs()
    sc.print('共享模块 → A')
    a.unbind_logs(tok)

    tok = b.bind_logs()
    sc.print('共享模块 → B')
    b.unbind_logs(tok)

    assert _msgs(a) == ['共享模块 → A']
    assert _msgs(b) == ['共享模块 → B'], '共享模块的日志固定进了一个平台'


# ---------- 每个绑定点 ----------

def test_worker_pool_binds_the_context_in_its_thread():
    """worker 线程体必须绑 ctx。

    两个平台各有一套同名的 W1..W4，只绑 worker 的话 dispatcher 解析不出
    这条日志属于哪个平台。
    """
    from src.web.worker import WorkerPool
    src = inspect.getsource(WorkerPool._run_in_worker)
    assert 'bind_logs()' in src, 'worker 线程没绑 ctx，日志无法按平台分流'
    assert 'unbind_logs' in src, '没有复位，绑定会泄漏到协调线程后续阶段'


@pytest.mark.parametrize('fn_name', [
    'run_batch_task', 'run_daily_pipeline', 'run_daily_subscribe_pipeline',
])
def test_each_pipeline_binds_at_its_thread_entry(fn_name):
    src = inspect.getsource(getattr(AppState, fn_name))
    assert '_patch_prints()' in src, f'{fn_name} 没在线程入口绑定日志归属'


def test_single_account_recharge_binds_inside_the_worker_thread():
    """充值端点的绑定必须在 _do_recharge 线程**内部**。

    实际踩过：_patch_prints() 写在请求线程里，而工作跑在随后新起的线程 ——
    contextvars 不继承，绑定完全无效，那条链路的日志会退化成裸 print。
    """
    import src.api.routes as routes
    src = inspect.getsource(routes.recharge_account)

    i_def = src.index('def _do_recharge')
    tail = src[i_def:]
    assert '_patch_prints()' in tail, '绑定不在 _do_recharge 线程内，对该线程无效'
    assert '_patch_prints()' not in src[:i_def], \
        '请求线程里还留着绑定——那是无效的，删掉免得误导'
