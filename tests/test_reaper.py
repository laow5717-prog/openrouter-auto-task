"""失联 worker 的卡回收测试（AC4/AC5）。"""

import tempfile

from src.models.card_binding import CardBindingModel
from src.models.database import Database
from src.web.app import AppState, create_app
from src.web.worker import ClaimReaper

from conftest import make_cards


def _reaper(db, timeout=20):
    st = AppState(db, {'card_binding': CardBindingModel(db)})
    return st, ClaimReaper(st.models['card_binding'], st, timeout)


def test_reap_once_reclaims_timed_out_cards(db, card_model, task_id):
    card_model.create_batch('opencode', task_id, make_cards(3))
    claimed = card_model.claim_batch(task_id, 'W1', limit=3)
    db.execute(
        "UPDATE card_bindings SET claimed_at=datetime('now','localtime','-45 minutes') WHERE id=?",
        (claimed[0]['id'],),
    )

    st, reaper = _reaper(db, timeout=20)
    assert reaper.reap_once() == 1
    assert any('worker 疑似失联' in line for line in st.get_logs())
    assert len(card_model.get_pending(task_id)) == 1


def test_reap_once_is_quiet_when_nothing_expired(db, card_model, task_id):
    card_model.create_batch('opencode', task_id, make_cards(2))
    card_model.claim_batch(task_id, 'W1', limit=2)

    st, reaper = _reaper(db)
    assert reaper.reap_once() == 0
    assert not any('[回收]' in line for line in st.get_logs())


def test_reap_once_swallows_errors(db):
    """回收失败不该拖垮服务。"""
    class Broken:
        def reap_stale(self, minutes):
            raise RuntimeError('库炸了')

    st = AppState(db, {})
    reaper = ClaimReaper(Broken(), st, 20)
    assert reaper.reap_once() == 0
    assert any('执行失败' in line for line in st.get_logs())


def test_reclaimed_card_can_be_consumed_by_another_worker(db, card_model, task_id):
    """AC4 的核心：回收后的卡必须能被后续 worker 正常领取。"""
    card_model.create_batch('opencode', task_id, make_cards(2))
    card_model.claim_batch(task_id, 'W1', limit=2)
    db.execute("UPDATE card_bindings SET claimed_at=datetime('now','localtime','-60 minutes')")

    st, reaper = _reaper(db, timeout=20)
    reaper.reap_once()

    regained = card_model.claim_batch(task_id, 'W2', limit=2)
    assert len(regained) == 2
    assert {r['worker_id'] for r in regained} == {'W2'}


def test_startup_resets_leftover_processing():
    """AC5：服务启动时残留的 processing 全部回到 pending 并记日志。"""
    path = tempfile.mktemp(suffix='.db')

    # 先造出"上次运行崩溃后"的残局
    db = Database(path)
    from src.models.task import TaskModel
    cm = CardBindingModel(db)
    tid = TaskModel(db).create('test', config={})
    cm.create_batch('opencode', tid, make_cards(5))
    cm.claim_batch(tid, 'W1', limit=5)
    assert cm.get_summary(tid)['processing'] == 5
    db.close()

    # 重新启动应用
    app = create_app(db_path=path)
    state = app.config['APP_STATE']
    model = app.config['MODELS']['card_binding']

    assert model.get_summary(tid)['processing'] == 0
    assert len(model.get_pending(tid)) == 5
    assert any('重置了 5 张' in line for line in state.get_logs())

    app.config['REAPER'].stop()
    app.config['DB'].close()


def test_reaper_thread_starts_and_stops():
    path = tempfile.mktemp(suffix='.db')
    app = create_app(db_path=path)
    reaper = app.config['REAPER']

    assert reaper._thread is not None and reaper._thread.is_alive()
    reaper.stop()
    reaper._thread.join(timeout=5)
    assert not reaper._thread.is_alive(), "回收线程未能停止"
    app.config['DB'].close()
