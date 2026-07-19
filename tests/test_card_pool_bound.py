"""卡池「已绑定」状态测试：落实一卡一账号，防止卡被复用。

此前该约束只在建任务时由 card_bindings 实时派生，卡池自身不留痕：
界面看不出卡已被用掉，且不走那条过滤的入口（如启动校验的可用卡计数）
会把已绑卡算进可用数。
"""

from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.utils import CARD_STATUS_BOUND, CARD_STATUS_INVALID, CARD_STATUS_EXPIRED


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
    gid = groups.create('g1', 'bind', '')
    return pool, gid


def test_bound_card_is_not_selectable(db):
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111'), _card('4222222222222222')])

    usable, _ = pool.get_usable_cards_as_list(gid)
    assert len(usable) == 2

    pool.mark_bound_by_number('4111111111111111')
    usable, excluded = pool.get_usable_cards_as_list(gid)

    assert [c['number'] for c in usable] == ['4222222222222222']
    assert len(excluded) == 1


def test_bound_does_not_overwrite_invalid(db):
    """invalid 记录着「卡有问题」这一更重要的事实，不能被 bound 覆盖。"""
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111')])
    pool.mark_invalid_by_number('4111111111111111')

    pool.mark_bound_by_number('4111111111111111')

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_INVALID


def test_bound_does_not_overwrite_expired(db):
    """过期卡不该被改写成已绑定——它压根不会被选去绑。"""
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111', month='01', year='2020')])

    pool.mark_bound_by_number('4111111111111111')

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_EXPIRED


def test_refresh_expired_does_not_overwrite_bound(db):
    """已绑定是历史事实：卡绑定后才到期，刷新不得把 bound 改写成 expired，
    否则界面上会显示成「已过期」，看不出这张卡其实已被某账号占用。"""
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111', month='01', year='2020')])
    # 直接置位，模拟「绑定在先、到期在后」的历史记录
    pool.mark_status_by_number('4111111111111111', CARD_STATUS_BOUND)

    pool.refresh_expired_status(gid)

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_BOUND


def test_bound_card_not_shown_as_invalid_bucket(db):
    """已绑定的卡不该被归入「无效」桶——它是正常消耗，不是坏卡。"""
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111'), _card('4222222222222222')])
    pool.mark_bound_by_number('4111111111111111')
    pool.mark_invalid_by_number('4222222222222222')

    invalid_rows, _total = pool.get_by_group(gid, bucket='invalid')
    numbers = [r['card_number'] for r in invalid_rows]

    assert '4222222222222222' in numbers
    assert '4111111111111111' not in numbers


def test_expired_card_still_marked_expired(db):
    """未绑定的过期卡照常被标为 expired，不受本次改动影响。"""
    pool, gid = _setup(db)
    pool.add_cards(gid, [_card('4111111111111111', month='01', year='2020')])

    pool.refresh_expired_status(gid)

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_EXPIRED
