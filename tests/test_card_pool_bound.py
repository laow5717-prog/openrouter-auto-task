"""卡池「已绑定」状态测试：落实一卡一账号，防止卡被复用。

此前该约束只在建任务时由 card_bindings 实时派生，卡池自身不留痕：
界面看不出卡已被用掉，且不走那条过滤的入口（如启动校验的可用卡计数）
会把已绑卡算进可用数。

多平台改造后，「已绑定」是**平台状态**：一张卡在 opencode 绑给了某账号，不妨碍它
在另一个平台被选中——那边是另一套账号、另一个商户。本文件末尾的对照组钉住这条。
"""

from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.utils import (
    CARD_STATUS_BOUND, CARD_STATUS_INVALID, CARD_STATUS_EXPIRED, CARD_STATUS_PAID,
)

OC = 'opencode'
OTHER = 'infron'


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

    usable, _ = pool.get_usable_cards_as_list(OC, gid)
    assert len(usable) == 2

    pool.mark_bound_by_number(OC, '4111111111111111')
    usable, excluded = pool.get_usable_cards_as_list(OC, gid)

    assert len(usable) == 1
    assert usable[0]['number'] == '4222222222222222'
    assert len(excluded) == 1
    assert excluded[0]['status'] == CARD_STATUS_BOUND


def test_bound_does_not_overwrite_invalid(db):
    """invalid 的信息量比 bound 更大（记录着卡不可用及其归因），不该被覆盖。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    pool.mark_invalid_by_number(OC, num)

    pool.mark_bound_by_number(OC, num)

    assert pool.get_platform_status(OC, num) == CARD_STATUS_INVALID


def test_bound_does_not_overwrite_paid(db):
    """paid 是卡可用性的最强证据，保留它；重复绑定另有 card_bindings 那层派生过滤兜底。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    pool.mark_status_by_number(OC, num, CARD_STATUS_PAID)

    pool.mark_bound_by_number(OC, num)

    assert pool.get_platform_status(OC, num) == CARD_STATUS_PAID


def test_bound_does_not_apply_to_expired_card(db):
    """已过期的卡不该再被标 bound——它本就不会被选中，标了只会掩盖过期这个事实。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num, month='01', year='2020')])   # 入库即 expired

    pool.mark_bound_by_number(OC, num)

    assert pool.get_platform_status(OC, num) == ''
    usable, excluded = pool.get_usable_cards_as_list(OC, gid)
    assert usable == []
    assert excluded[0]['status'] == CARD_STATUS_EXPIRED


def test_refresh_expired_and_bound_coexist(db):
    """卡先被绑走、之后到期：两个事实并存，不再互相顶掉。

    拆表前 status 只有一列，refresh_expired_status 必须小心不覆盖 bound；现在 expired
    在 card_pool、bound 在 card_platform_state，各记各的。有效状态取 expired（它更能
    说明这张卡为什么不能用）。
    """
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    pool.mark_bound_by_number(OC, num)

    # 手工把有效期改成过去，模拟"绑定在先、到期在后"
    db.execute("UPDATE card_pool SET expiry_month='01', expiry_year='2020' WHERE card_number=?",
               (num,))
    pool.refresh_expired_status(gid)

    assert pool.get_platform_status(OC, num) == CARD_STATUS_BOUND   # 绑定事实还在
    usable, excluded = pool.get_usable_cards_as_list(OC, gid)
    assert usable == []
    assert excluded[0]['status'] == CARD_STATUS_EXPIRED             # 有效状态是过期


def test_bound_card_not_in_invalid_bucket(db):
    """已绑定的卡是正常消耗掉的，不该被展示成无效卡。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    pool.mark_bound_by_number(OC, num)

    invalid_rows, total = pool.get_by_group(OC, gid, bucket='invalid')

    assert total == 0
    assert invalid_rows == []


def test_unbound_expired_card_still_marked_expired(db):
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    db.execute("UPDATE card_pool SET expiry_month='01', expiry_year='2020' WHERE card_number=?",
               (num,))

    pool.refresh_expired_status(gid)

    rows = pool.get_all_by_group(gid)
    assert rows[0]['status'] == CARD_STATUS_EXPIRED


# ---------- 跨平台对照组（AC2） ----------

def test_bound_on_one_platform_still_selectable_on_another(db):
    """AC2：卡在 opencode 被绑给某账号后，在另一个平台仍出现在可选集里。

    这是多平台改造的核心诉求——「一卡一账号」是平台内的约束，不是全局的。
    """
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])

    pool.mark_bound_by_number(OC, num)

    assert pool.get_usable_cards_as_list(OC, gid)[0] == []          # opencode 不可选
    other_usable, _ = pool.get_usable_cards_as_list(OTHER, gid)
    assert [c['number'] for c in other_usable] == [num]             # 别的平台照常可选


def test_invalid_on_one_platform_still_selectable_on_another(db):
    """AC3：卡在 opencode 被判废，在另一个平台仍可选。

    拒付是发卡行对**这个商户**的判断，换个平台的商户号可能就过了。
    """
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])

    pool.mark_invalid_by_number(OC, num)

    assert pool.get_usable_cards_as_list(OC, gid)[0] == []
    other_usable, _ = pool.get_usable_cards_as_list(OTHER, gid)
    assert [c['number'] for c in other_usable] == [num]


def test_expired_is_global_across_platforms(db):
    """AC7：过期是卡自己的属性，所有平台一致——这条不隔离。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num, month='01', year='2020')])

    for platform in (OC, OTHER):
        usable, excluded = pool.get_usable_cards_as_list(platform, gid)
        assert usable == []
        assert excluded[0]['status'] == CARD_STATUS_EXPIRED


def test_platform_statuses_do_not_leak_into_each_other(db):
    """同一张卡可以在两个平台各持一个互不相干的状态。"""
    pool, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])

    pool.mark_status_by_number(OC, num, CARD_STATUS_PAID)
    pool.mark_invalid_by_number(OTHER, num)

    assert pool.get_platform_status(OC, num) == CARD_STATUS_PAID
    assert pool.get_platform_status(OTHER, num) == CARD_STATUS_INVALID
