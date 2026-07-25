"""有效卡不变式：valid_cards 成员（曾成功过的卡）永不被标为 invalid。

曾支付/绑定成功过的卡是已被证明可用的好卡；它再次被拒只应进入临时冷却，
而非永久作废。此约束落在 card_pool.mark_invalid_by_number 的最底层——
无论上层调用方是否漏判，有效卡都不会被误标无效。
"""

from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.models.valid_card import ValidCardModel
from src.utils import CARD_STATUS_INVALID


def _card(number, month='12', year='2030'):
    return {
        'number': number, 'expiry_month': month, 'expiry_year': year,
        'cvc': '123', 'first_name': 'T', 'last_name': 'U', 'country': 'US',
        'address': 'a', 'address2': '', 'city': 'c', 'state': 's',
        'zip': '10001', 'company': '',
    }


def _setup(db):
    groups = CardGroupModel(db)
    pool = CardPoolModel(db)
    valid = ValidCardModel(db)
    gid = groups.create('g1', 'payment', '')
    return pool, valid, gid


def test_valid_card_not_marked_invalid(db):
    """已记入 valid_cards 的卡，mark_invalid 是 no-op（状态保持有效）。"""
    pool, valid, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    valid.record(_card(num), source_type='payment', source_email='a@b.com')

    pool.mark_invalid_by_number(num)

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] != CARD_STATUS_INVALID


def test_non_valid_card_still_marked_invalid(db):
    """未曾成功过的普通卡，mark_invalid 仍照常生效。"""
    pool, valid, gid = _setup(db)
    num = '4222222222222222'
    pool.add_cards(gid, [_card(num)])

    pool.mark_invalid_by_number(num)

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_INVALID


def test_valid_card_stays_out_of_invalid_bucket_after_reject(db):
    """有效卡即便被尝试标无效，也不落入无效桶、仍留在有效(在库)桶。"""
    pool, valid, gid = _setup(db)
    good, bad = '4111111111111111', '4222222222222222'
    pool.add_cards(gid, [_card(good), _card(bad)])
    valid.record(_card(good), source_type='payment', source_email='a@b.com')

    pool.mark_invalid_by_number(good)
    pool.mark_invalid_by_number(bad)

    invalid_rows, _ = pool.get_by_group(gid, bucket='invalid')
    valid_rows, _ = pool.get_by_group(gid, bucket='valid')
    assert good not in [r['card_number'] for r in invalid_rows]
    assert good in [r['card_number'] for r in valid_rows]
    assert bad in [r['card_number'] for r in invalid_rows]
