"""流水线并发语义的集成测试（打桩掉浏览器）。

把 registration.* 换成假实现，就能在无浏览器的前提下验证调度层的正确性：
卡不会被重复消费、未用掉的卡会退回、串行与并行结果一致。
真正需要浏览器的部分（登录/绑卡/支付）由手动实跑验证。
"""

import threading

import pytest

from conftest import make_cards
from src.web import app as app_module
from src.web.app import AppState
from src.web.worker import WorkerPool


class _Recorder:
    """记录每次"注册并绑卡"调用，模拟绑掉前 n 张卡。"""

    def __init__(self, bind_per_account=2, fail_emails=()):
        self.calls = []
        self.bind_per_account = bind_per_account
        self.fail_emails = set(fail_emails)
        self.lock = threading.Lock()
        self.counter = 0
        self.concurrent_peak = 0
        self._active = 0

    def register_and_bind_cards(self, db, account_model, card_binding_model, task_id,
                                batch_records, cf_password=None, max_bindable_cards=2,
                                captcha_api_key=None, monitor_callback=None,
                                **kwargs):
        # **kwargs 吸收 claim_more 等新增可选参数：本替身只验证卡的领取/归还编排，
        # 不该因被测函数新增可选形参而失败
        with self.lock:
            self._active += 1
            self.concurrent_peak = max(self.concurrent_peak, self._active)
            self.counter += 1
            email = f'acct{self.counter}@example.com'
            self.calls.append({'email': email, 'ids': [r['id'] for r in batch_records]})
        try:
            bound = 0
            for rec in batch_records[:self.bind_per_account]:
                card_binding_model.mark_success(rec['id'], email)
                bound += 1
            return email, 'pw', bound
        finally:
            with self.lock:
                self._active -= 1


@pytest.fixture
def state(db):
    from src.models.card_binding import CardBindingModel
    from src.models.account import AccountModel
    from src.models.task import TaskModel
    st = AppState(db, {
        'card_binding': CardBindingModel(db),
        'account': AccountModel(db),
        'task': TaskModel(db),
    })
    return st


@pytest.fixture
def no_wait(monkeypatch):
    """去掉注册间隔的等待，否则测试要跑好几秒。"""
    monkeypatch.setattr(app_module.random, 'randint', lambda a, b: 0)


def _run_loop(state, task_id, recorder, monkeypatch, workers):
    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards',
                        recorder.register_and_bind_cards)
    pool = WorkerPool(state, workers)
    state._register_bind_loop(task_id, 'pw', max_bindable_cards=2,
                              captcha_api_key=None, pool=pool)


def test_every_card_consumed_exactly_once_in_parallel(state, task_id, monkeypatch, no_wait):
    """并发注册时，同一张卡不能被两个账号绑走。"""
    cards = make_cards(12)
    state.models['card_binding'].create_batch(task_id, cards)

    rec = _Recorder(bind_per_account=2)
    _run_loop(state, task_id, rec, monkeypatch, workers=3)

    all_ids = [i for call in rec.calls for i in call['ids']]
    assert len(all_ids) == len(set(all_ids)), "同一张卡被分配给了多个账号"

    summary = state.models['card_binding'].get_summary(task_id)
    assert summary['success'] == 12
    assert summary['pending'] == 0
    assert summary['processing'] == 0


def test_parallel_actually_overlaps(state, task_id, monkeypatch, no_wait):
    """确认多个 worker 真的同时在跑，而不是退化成串行。"""
    state.models['card_binding'].create_batch(task_id, make_cards(20))

    rec = _Recorder(bind_per_account=2)
    original = rec.register_and_bind_cards

    def slow(*a, **kw):
        result = original(*a, **kw)
        threading.Event().wait(0.02)      # 拉长重叠窗口
        return result

    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards', slow)
    pool = WorkerPool(state, 3)
    state._register_bind_loop(task_id, 'pw', 2, None, pool=pool)

    assert rec.concurrent_peak > 1, "worker 没有真正并发"


def test_serial_and_parallel_consume_the_same_cards(db, task_id, monkeypatch, no_wait):
    """max_workers=1 与 =3 的最终账面必须一致（R1.2/AC3 的自动化部分）。"""
    from src.models.card_binding import CardBindingModel
    from src.models.account import AccountModel
    from src.models.task import TaskModel

    def run(workers):
        st = AppState(db, {
            'card_binding': CardBindingModel(db),
            'account': AccountModel(db),
            'task': TaskModel(db),
        })
        tid = TaskModel(db).create('test', config={})
        st.models['card_binding'].create_batch(tid, make_cards(10, prefix='42'))
        rec = _Recorder(bind_per_account=2)
        monkeypatch.setattr(app_module.registration, 'register_and_bind_cards',
                            rec.register_and_bind_cards)
        st._register_bind_loop(tid, 'pw', 2, None, pool=WorkerPool(st, workers))
        return st.models['card_binding'].get_summary(tid), len(rec.calls)

    serial_summary, serial_accounts = run(1)
    parallel_summary, parallel_accounts = run(3)

    assert serial_summary['success'] == parallel_summary['success'] == 10
    assert serial_summary['pending'] == parallel_summary['pending'] == 0
    assert serial_accounts == parallel_accounts == 5      # 10 张卡 / 每号 2 张


def test_unbound_cards_return_to_pool(state, task_id, monkeypatch, no_wait):
    """绑不掉的卡必须退回 pending，不能滞留在 processing。"""
    state.models['card_binding'].create_batch(task_id, make_cards(6))

    rec = _Recorder(bind_per_account=1)     # 每号只绑 1 张，另一张应退回
    _run_loop(state, task_id, rec, monkeypatch, workers=2)

    summary = state.models['card_binding'].get_summary(task_id)
    assert summary['processing'] == 0, "有卡滞留在 processing"
    assert summary['success'] == 6


def test_exception_releases_claimed_cards(state, task_id, monkeypatch, no_wait):
    """注册抛异常时，已领取的卡必须退回卡池而不是丢失。"""
    state.models['card_binding'].create_batch(task_id, make_cards(4))
    calls = {'n': 0}

    def boom(*a, **kw):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise RuntimeError('注册炸了')
        # 第三次开始正常绑掉
        for rec in kw['batch_records']:
            kw['card_binding_model'].mark_success(rec['id'], 'ok@example.com')
        return 'ok@example.com', 'pw', len(kw['batch_records'])

    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards', boom)
    state._register_bind_loop(task_id, 'pw', 2, None, pool=WorkerPool(state, 1))

    summary = state.models['card_binding'].get_summary(task_id)
    assert summary['processing'] == 0, "异常路径漏了 release_unused"


def test_stop_request_halts_the_loop(state, task_id, monkeypatch, no_wait):
    state.models['card_binding'].create_batch(task_id, make_cards(20))

    def stop_after_first(*a, **kw):
        state.stop_requested = True
        return 'x@example.com', 'pw', 0

    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards', stop_after_first)
    state._register_bind_loop(task_id, 'pw', 2, None, pool=WorkerPool(state, 2))

    summary = state.models['card_binding'].get_summary(task_id)
    assert summary['processing'] == 0, "停止后仍有卡滞留在 processing"
    assert summary['pending'] > 0, "停止后卡池应该还有剩余"


def test_fail_streak_stops_the_loop(state, task_id, monkeypatch, no_wait):
    """连续失败达阈值后必须收敛，且卡全部退回。"""
    state.models['card_binding'].create_batch(task_id, make_cards(40))

    def always_fail(*a, **kw):
        return None, None, 0                 # email=None → 注册失败

    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards', always_fail)
    state._register_bind_loop(task_id, 'pw', 2, None, pool=WorkerPool(state, 2))

    summary = state.models['card_binding'].get_summary(task_id)
    assert summary['success'] == 0
    assert summary['processing'] == 0
    assert summary['pending'] == 40, "失败的卡应全部退回卡池"
