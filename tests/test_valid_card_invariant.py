"""有效卡不变式：在某平台验证成功过的卡，在**那个平台**永不被标为 invalid。

曾支付/绑定成功过的卡是已被证明可用的好卡；它再次被拒只应进入临时冷却，
而非永久作废。此约束落在 card_pool.mark_invalid_by_number 的最底层——
无论上层调用方是否漏判，有效卡都不会被误标无效。

多平台改造把这条不变式收窄到了单平台内，那是本文件末尾对照组的主题：守卫原先查的是
整张 valid_cards 表，于是一张在 opencode 成功过的卡到了新平台被拒也永远标不成
invalid——坏卡会一轮一轮被反复选中、反复拒付，把额度和风控配额一起耗光。
"""

from src.models.card_pool import CardPoolModel
from src.models.card_group import CardGroupModel
from src.models.valid_card import ValidCardModel
from src.utils import CARD_STATUS_INVALID

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
    valid = ValidCardModel(db)
    gid = groups.create('g1', 'payment', '')
    return pool, valid, gid


def test_valid_card_not_marked_invalid(db):
    """已记入 valid_cards 的卡，mark_invalid 是 no-op（状态保持有效）。"""
    pool, valid, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    valid.record(OC, _card(num), source_type='payment', source_email='a@b.com')

    pool.mark_invalid_by_number(OC, num)

    assert pool.get_platform_status(OC, num) != CARD_STATUS_INVALID


def test_non_valid_card_still_marked_invalid(db):
    """未曾成功过的普通卡，mark_invalid 仍照常生效。"""
    pool, valid, gid = _setup(db)
    num = '4222222222222222'
    pool.add_cards(gid, [_card(num)])

    pool.mark_invalid_by_number(OC, num)

    assert pool.get_platform_status(OC, num) == CARD_STATUS_INVALID


def test_valid_card_stays_out_of_invalid_bucket_after_reject(db):
    """有效卡即便被尝试标无效，也不落入无效桶、仍留在有效(在库)桶。"""
    pool, valid, gid = _setup(db)
    good, bad = '4111111111111111', '4222222222222222'
    pool.add_cards(gid, [_card(good), _card(bad)])
    valid.record(OC, _card(good), source_type='payment', source_email='a@b.com')

    pool.mark_invalid_by_number(OC, good)
    pool.mark_invalid_by_number(OC, bad)

    invalid_rows, _ = pool.get_by_group(OC, gid, bucket='invalid')
    valid_rows, _ = pool.get_by_group(OC, gid, bucket='valid')
    assert good not in [r['card_number'] for r in invalid_rows]
    assert good in [r['card_number'] for r in valid_rows]
    assert bad in [r['card_number'] for r in invalid_rows]


# ---------- 跨平台对照组（AC5：本次改造里最高风险的一处） ----------

def test_valid_on_one_platform_can_still_be_invalidated_on_another(db):
    """AC5：卡在 A 平台是有效卡，在 B 平台被拒时**仍然能**被判废。

    这正是守卫反转前会挡掉的场景。不改的后果很具体：跨平台复用的坏卡在新平台上
    永远只进冷却、每轮都被重新选中，反复拒付直到额度耗尽。
    """
    pool, valid, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    valid.record(OC, _card(num), source_type='payment', source_email='a@b.com')

    pool.mark_invalid_by_number(OTHER, num)

    assert pool.get_platform_status(OTHER, num) == CARD_STATUS_INVALID   # B 平台判废
    assert pool.get_platform_status(OC, num) != CARD_STATUS_INVALID      # A 平台不受影响


def test_valid_bucket_is_per_platform(db):
    """「有效卡」这个分类本身也是按平台算的。

    在 opencode 验证过的卡，从 infron 的视角看还是张没验证过的新卡——它该落在
    未验证桶，而不是有效桶。
    """
    pool, valid, gid = _setup(db)
    num = '4111111111111111'
    pool.add_cards(gid, [_card(num)])
    valid.record(OC, _card(num), source_type='payment', source_email='a@b.com')

    oc_valid, _ = pool.get_by_group(OC, gid, bucket='valid')
    oc_unverified, _ = pool.get_by_group(OC, gid, bucket='unverified')
    other_valid, _ = pool.get_by_group(OTHER, gid, bucket='valid')
    other_unverified, _ = pool.get_by_group(OTHER, gid, bucket='unverified')

    assert [r['card_number'] for r in oc_valid] == [num]
    assert oc_unverified == []
    assert other_valid == []
    assert [r['card_number'] for r in other_unverified] == [num]


def test_is_valid_is_per_platform(db):
    """ValidCardModel.is_valid 同样按平台判断——它是「判废还是冷却」的直接判据。"""
    _pool, valid, _gid = _setup(db)
    num = '4111111111111111'
    valid.record(OC, _card(num), source_type='payment', source_email='a@b.com')

    assert valid.is_valid(OC, num) is True
    assert valid.is_valid(OTHER, num) is False


def test_bound_email_is_per_platform(db):
    """同一张卡在两个平台可以绑给两个不同账号，各记各的。"""
    _pool, valid, _gid = _setup(db)
    num = '4111111111111111'
    valid.record(OC, _card(num), source_type='payment', source_email='a@oc.com')
    valid.record(OTHER, _card(num), source_type='payment', source_email='b@other.com')

    assert valid.get_bound_email(OC, num) == 'a@oc.com'
    assert valid.get_bound_email(OTHER, num) == 'b@other.com'
