"""卡的临时冷却按平台隔离（AC4）。

3DS 是「商户 + 发卡行」共同决定的：换平台就是换一个 Stripe 商户号，同一张卡不一定
再触发。速率冷却同理，各平台风控阈值互不相干。若不隔离，opencode 上一次 3DS 会让
这张卡在所有平台一起停摆 24 小时。
"""

from src.models.card_payment_state import CardPaymentStateModel

OC = 'opencode'
OTHER = 'infron'
NUM = '4111111111111111'


def test_cooldown_on_one_platform_does_not_affect_another(db):
    """AC4：A 平台进入冷却，B 平台立即可用。"""
    st = CardPaymentStateModel(db)

    st.set_cooldown(OC, NUM, hours=24, reason='3DS')

    assert st.in_cooldown(OC, NUM) is True
    assert st.in_cooldown(OTHER, NUM) is False


def test_each_platform_keeps_its_own_expiry(db):
    """两个平台各自记一份到期时间，互不覆盖。"""
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=24, reason='opencode 侧 3DS')
    st.set_cooldown(OTHER, NUM, hours=1, reason='infron 侧速率')

    assert st.get_tds_until(OC, NUM) != st.get_tds_until(OTHER, NUM)
    assert st.get_state_map(OC)[NUM]['tds_reason'] == 'opencode 侧 3DS'
    assert st.get_state_map(OTHER)[NUM]['tds_reason'] == 'infron 侧速率'


def test_state_map_only_returns_requested_platform(db):
    """批量取状态时不能把别的平台的卡混进来——那会让选卡误排除。"""
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=24)
    st.set_cooldown(OTHER, '4222222222222222', hours=24)

    assert set(st.get_state_map(OC)) == {NUM}
    assert set(st.get_state_map(OTHER)) == {'4222222222222222'}


def test_reset_cooldown_is_idempotent_per_platform(db):
    """同平台重复标记覆盖到期时间，不影响另一平台。"""
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=1, reason='first')
    st.set_cooldown(OC, NUM, hours=24, reason='second')
    st.set_cooldown(OTHER, NUM, hours=1, reason='other')

    assert st.get_state_map(OC)[NUM]['tds_reason'] == 'second'
    assert st.get_state_map(OTHER)[NUM]['tds_reason'] == 'other'


def test_expired_cooldown_counts_as_usable(db):
    """到期即视为可用（传负数小时模拟已过期）。"""
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=-1, reason='已过期')

    assert st.in_cooldown(OC, NUM) is False


def test_alias_names_share_the_platform_signature(db):
    """3DS 专名别名与主方法同签名，别落下 platform。"""
    st = CardPaymentStateModel(db)
    st.set_tds(OC, NUM, hours=24)
    assert st.in_tds_cooldown(OC, NUM) is True
    assert st.in_tds_cooldown(OTHER, NUM) is False
