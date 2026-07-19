"""补绑失败后「换卡继续」的行为测试。

对应的真实故障：一张卡因 Stripe Link 缺手机号而绑定失败后，日志打印「尝试下一张」
却直接关掉了浏览器——因为该批次只领了这一张卡，而代码没有再领的能力。

此处把浏览器相关调用全部打桩，只验证取卡与重试的编排逻辑。
"""

import src.services.registration as reg


class FakeAccountModel:
    def __init__(self):
        self.bound = {}

    def get_email_password(self, email):
        return None

    def update_bound_cards(self, email, count, sync_status=True):
        self.bound[email] = count

    def update_status(self, email, status):
        pass


class FakeCardBindingModel:
    def __init__(self):
        self.success, self.failed = [], []

    def mark_success(self, binding_id, email):
        self.success.append(binding_id)

    def mark_failed(self, binding_id, err):
        self.failed.append((binding_id, err))


def _record(rid):
    return {"id": rid, "card_display": f"{rid:04d}", "card": {"number": str(rid)}}


def _patch(monkeypatch, results):
    """把浏览器动作打桩；results 为按调用序返回的 (成功?, 错误原因) 列表。"""
    calls = {"n": 0}

    def fake_add_card(driver, card_info):
        i = calls["n"]
        calls["n"] += 1
        return results[i] if i < len(results) else (False, "no more stub")

    monkeypatch.setattr(reg, "create_driver", lambda **kw: object())
    monkeypatch.setattr(reg, "close_driver", lambda d: None)
    monkeypatch.setattr(reg, "login_cloudflare", lambda *a, **kw: "acct123")
    monkeypatch.setattr(reg, "navigate_to_billing", lambda d: True)
    monkeypatch.setattr(reg, "get_bound_card_count", lambda d: 0)
    monkeypatch.setattr(reg, "add_credit_card", fake_add_card)
    monkeypatch.setattr(reg.time, "sleep", lambda s: None)
    return calls


def test_failed_card_falls_through_to_next_in_batch(monkeypatch):
    """批次内还有卡时，失败后必须继续试下一张，而不是收工。"""
    _patch(monkeypatch, [(False, "[Stripe字段错误] phone"), (True, None)])
    acct, cards = FakeAccountModel(), FakeCardBindingModel()

    bound, login_ok = reg.bind_cards_to_existing_account(
        acct, cards, task_id=1, email="a@x.com", cf_password="pw",
        batch_records=[_record(1), _record(2)], max_bindable_cards=1)

    assert login_ok is True
    assert bound == 1
    assert cards.failed and cards.failed[0][0] == 1
    assert cards.success == [2]


def test_claims_more_cards_when_batch_exhausted(monkeypatch):
    """批次里的卡都失败且仍未补够时，应再领一批继续，而不是直接关浏览器。"""
    _patch(monkeypatch, [(False, "err1"), (True, None)])
    acct, cards = FakeAccountModel(), FakeCardBindingModel()
    handed = []

    def claim_more(n):
        if handed:
            return []
        handed.append(n)
        return [_record(9)]

    bound, _ = reg.bind_cards_to_existing_account(
        acct, cards, task_id=1, email="a@x.com", cf_password="pw",
        batch_records=[_record(1)], max_bindable_cards=1, claim_more=claim_more)

    assert handed == [1], "应按仍缺的张数再领卡"
    assert bound == 1
    assert cards.success == [9]


def test_no_claim_more_when_target_already_met(monkeypatch):
    """已补够就不该再领卡——否则会把卡池里的卡白白占住。"""
    _patch(monkeypatch, [(True, None)])
    acct, cards = FakeAccountModel(), FakeCardBindingModel()
    handed = []

    reg.bind_cards_to_existing_account(
        acct, cards, task_id=1, email="a@x.com", cf_password="pw",
        batch_records=[_record(1)], max_bindable_cards=1,
        claim_more=lambda n: handed.append(n) or [_record(9)])

    assert handed == []


def test_extra_claim_rounds_are_bounded(monkeypatch):
    """再领卡的轮数必须有上限，否则一个账号会把整个卡池吃光。"""
    _patch(monkeypatch, [(False, f"err{i}") for i in range(20)])
    acct, cards = FakeAccountModel(), FakeCardBindingModel()
    rounds = []
    counter = {"id": 100}

    def claim_more(n):
        rounds.append(n)
        counter["id"] += 1
        return [_record(counter["id"])]

    bound, _ = reg.bind_cards_to_existing_account(
        acct, cards, task_id=1, email="a@x.com", cf_password="pw",
        batch_records=[_record(1)], max_bindable_cards=1, claim_more=claim_more)

    assert bound == 0
    assert len(rounds) <= 3, f"再领轮数应有上限，实际 {len(rounds)}"


def test_duplicate_records_are_not_bound_twice(monkeypatch):
    """claim_more 回流已在队列中的记录时，不能重复绑定同一张卡。"""
    _patch(monkeypatch, [(False, "err1"), (False, "err2"), (False, "err3")])
    acct, cards = FakeAccountModel(), FakeCardBindingModel()

    # 每次都把已在队列里的 1 号连同新卡一起返回
    def claim_more(n):
        return [_record(1), _record(7)] if 7 not in [c for c in cards.success] else []

    reg.bind_cards_to_existing_account(
        acct, cards, task_id=1, email="a@x.com", cf_password="pw",
        batch_records=[_record(1)], max_bindable_cards=2, claim_more=claim_more)

    attempted = [bid for bid, _ in cards.failed]
    assert attempted.count(1) == 1, f"1 号卡被重复尝试: {attempted}"
