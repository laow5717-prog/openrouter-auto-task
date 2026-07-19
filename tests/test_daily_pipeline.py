"""run_daily_pipeline 的端到端测试（浏览器全打桩）。

这是 AC2/AC3 的自动化部分：跑完整三阶段，断言串行与并行的账面一致、
没有一张卡被两个账号绑走、收尾后无残留占用。

安全网：create_driver 被替换成会抛异常的桩。任何漏打桩的调用都会立刻炸出来，
而不是静默启动一个真实 Chrome 窗口（曾经踩过）。
"""

import tempfile
import threading

import pytest

from src.browser import driver as driver_module
from src.config import cfg
from src.models.account import AccountModel
from src.models.card_binding import CardBindingModel
from src.models.card_group import CardGroupModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.card_pool import CardPoolModel
from src.models.database import Database
from src.models.invoice_payment_state import InvoicePaymentStateModel
from src.models.recharge_log import RechargeLogModel
from src.models.task import TaskModel
from src.models.valid_card import ValidCardModel
from src.web import app as app_module
from src.web.app import AppState


def _full_cards(n):
    return [{'number': f'4111{i:012d}', 'expiry_month': '12', 'expiry_year': '2030',
             'cvc': '123', 'first_name': 'T', 'last_name': f'U{i}',
             'country': 'United States', 'address': 'a', 'city': 'c',
             'state': 's', 'zip': '12345'} for i in range(n)]


class _Stub:
    """假的 registration.*，模拟每个账号绑掉 bind_per 张卡。"""

    def __init__(self, bind_per=2, delay=0.02):
        self.bind_per = bind_per
        self.delay = delay
        self.lock = threading.Lock()
        self.n = 0
        self.peak = 0
        self._active = 0
        self._in_flight_emails = set()
        self.overlapping_emails = []      # 同一 email 被并发处理的证据

    def _enter(self, email=None):
        with self.lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            if email:
                if email in self._in_flight_emails:
                    self.overlapping_emails.append(email)
                self._in_flight_emails.add(email)
            self._current_email = email

    def _exit(self, email=None):
        with self.lock:
            self._active -= 1
            if email:
                self._in_flight_emails.discard(email)

    # **kwargs 吸收 claim_more 等新增可选参数：这些替身验证的是流水线编排，
    # 不该因被测函数新增可选形参而失败
    def bind_existing(self, account_model, card_binding_model, task_id, email,
                      cf_password, batch_records, max_bindable_cards=2,
                      captcha_api_key=None, monitor_callback=None, **kwargs):
        self._enter(email)
        try:
            threading.Event().wait(self.delay)
            bound = 0
            for r in batch_records[:self.bind_per]:
                card_binding_model.mark_success(r['id'], email)
                bound += 1
            return bound, True
        finally:
            self._exit(email)

    def register(self, db, account_model, card_binding_model, task_id, batch_records,
                 cf_password=None, max_bindable_cards=2, captcha_api_key=None,
                 monitor_callback=None, **kwargs):
        with self.lock:
            self.n += 1
            email = f'new{self.n}@example.com'
        self._enter(email)
        try:
            account_model.upsert(email, 'pw')
            threading.Event().wait(self.delay)
            bound = 0
            for r in batch_records[:self.bind_per]:
                card_binding_model.mark_success(r['id'], email)
                bound += 1
            return email, 'pw', bound
        finally:
            self._exit(email)

    def recharge(self, email, cf_password, **kw):
        self._enter(email)
        try:
            threading.Event().wait(self.delay)
            if kw.get('invoice_daily_cap') is not None:
                return True, "", [], '1234', "topup", {'today_count': 99, 'paid': 0}
            return True, "", [], '1234', "topup"
        finally:
            self._exit(email)


@pytest.fixture
def no_browser(monkeypatch):
    """任何真实浏览器创建都视为测试缺陷。"""
    def _explode(*a, **kw):
        raise AssertionError("测试中不应创建真实浏览器——有函数漏打桩了")
    monkeypatch.setattr(driver_module, 'create_driver', _explode)


@pytest.fixture
def stub(monkeypatch, no_browser):
    s = _Stub()
    monkeypatch.setattr(app_module.registration, 'bind_cards_to_existing_account', s.bind_existing)
    monkeypatch.setattr(app_module.registration, 'register_and_bind_cards', s.register)
    monkeypatch.setattr(app_module.registration, 'recharge_account', s.recharge)
    monkeypatch.setattr(app_module.random, 'randint', lambda a, b: 0)
    return s


def _run_pipeline(workers, stub_ref, n_cards=16, n_accounts=3):
    path = tempfile.mktemp(suffix='.db')
    db = Database(path)
    models = {
        'account': AccountModel(db), 'task': TaskModel(db),
        'card_binding': CardBindingModel(db), 'recharge_log': RechargeLogModel(db),
        'card_group': CardGroupModel(db), 'card_pool': CardPoolModel(db),
        'valid_card': ValidCardModel(db), 'card_state': CardPaymentStateModel(db),
        'invoice_state': InvoicePaymentStateModel(db),
    }
    gid = models['card_group'].create('bind-group', 'bind')
    models['card_pool'].add_cards(gid, _full_cards(n_cards))
    for i in range(n_accounts):
        db.execute("INSERT INTO accounts (email, cf_password, status) VALUES (?,?,?)",
                   (f'existing{i}@example.com', 'pw', 'registered'))

    state = AppState(db, models)
    original = cfg.concurrency.max_workers
    cfg.concurrency.max_workers = workers
    try:
        state.run_daily_pipeline(bind_group_id=gid, payment_group_id=None,
                                 cf_password='pw', max_bindable_cards=2,
                                 captcha_api_key=None)
    finally:
        cfg.concurrency.max_workers = original

    dupes = db.fetchall("""SELECT card_data_json, COUNT(DISTINCT bound_to_email) c
                           FROM card_bindings WHERE status='success'
                           GROUP BY card_data_json HAVING c > 1""")
    result = {
        'summary': models['card_binding'].get_global_summary(),
        'accounts': models['account'].count(),
        'dupes': len(dupes),
        'claims_left': len(state.account_registry.snapshot()),
        'in_flight_cards': len(state.payment_registry.in_flight_numbers()),
        'is_running': state.is_running,
        'parallel_mode': state.parallel_mode,
        'overlapping_emails': list(stub_ref.overlapping_emails),
    }
    db.close()
    return result


def test_pipeline_completes_serially(stub):
    r = _run_pipeline(1, stub)
    assert r['summary']['success'] == 16
    assert r['summary']['processing'] == 0
    assert r['dupes'] == 0
    assert r['is_running'] is False
    assert stub.peak == 1, "串行模式出现了并发"


def test_pipeline_completes_in_parallel(stub):
    r = _run_pipeline(3, stub)
    assert r['summary']['success'] == 16
    assert r['summary']['processing'] == 0
    assert r['dupes'] == 0, "有卡被两个账号绑走"
    assert stub.peak > 1, "并行模式没有真正并发"


def test_serial_and_parallel_produce_same_ledger(stub):
    """AC3 的自动化部分：并发不改变最终账面。"""
    serial = _run_pipeline(1, stub)
    stub.n = 0                                   # 重置账号编号计数
    parallel = _run_pipeline(3, stub)

    assert serial['summary']['success'] == parallel['summary']['success']
    assert serial['summary']['failed'] == parallel['summary']['failed']
    assert serial['accounts'] == parallel['accounts']


def test_pipeline_releases_all_runtime_claims(stub):
    """收尾必须干净：账号占用、支付卡占用、processing 卡全部释放。"""
    r = _run_pipeline(3, stub)
    assert r['claims_left'] == 0, "账号占用泄漏"
    assert r['in_flight_cards'] == 0, "支付卡占用泄漏"
    assert r['summary']['processing'] == 0, "卡滞留在 processing"
    assert r['parallel_mode'] is False, "parallel_mode 未复位"


def test_no_account_processed_twice_concurrently(stub):
    """回归守卫：同一账号不被并发处理（Chrome profile 单实例约束，C2）。

    诚实说明其强度：把 AccountRegistry.claim 的排他逻辑删掉，本测试**仍会通过**
    （已做变异验证）。因为当前每个阶段传给 pool.map 的账号列表本身就无重复，
    map 天然不会把同一账号发给两个 worker——排他在这条路径上是冗余的第二道闸。

    所以这里守的是**未来的结构变更**：一旦有人让同一账号在一个阶段内出现多次
    （比如把阶段2的轮次循环拍平成一个大列表），这个断言会立刻变红。
    AccountRegistry 本身的排他正确性由 test_registry.py 覆盖，那里的断言会因
    同样的变异而失败。"""
    r = _run_pipeline(3, stub)
    assert r['overlapping_emails'] == [], \
        f"同一账号被并发处理: {r['overlapping_emails']}"
    assert stub.peak > 1, "并发度不足，这个断言没有说服力"
