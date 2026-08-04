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


# ---------- 连续失败计数 ----------


def test_fail_streak_starts_at_zero_without_a_row(db):
    """没有记录 = 从没失败过 = 计数 0，不该报错也不该建行。"""
    st = CardPaymentStateModel(db)
    assert st.get_fail_streak(OC, NUM) == 0
    assert st.get_state_map(OC) == {}


def test_bump_fail_streak_increments_and_returns_new_value(db):
    st = CardPaymentStateModel(db)
    assert st.bump_fail_streak(OC, NUM) == 1
    assert st.bump_fail_streak(OC, NUM) == 2
    assert st.bump_fail_streak(OC, NUM) == 3
    assert st.get_fail_streak(OC, NUM) == 3


def test_reset_fail_streak_clears_the_count(db):
    st = CardPaymentStateModel(db)
    st.bump_fail_streak(OC, NUM)
    st.bump_fail_streak(OC, NUM)

    st.reset_fail_streak(OC, NUM)

    assert st.get_fail_streak(OC, NUM) == 0
    assert st.bump_fail_streak(OC, NUM) == 1, '清零后应从 1 重新数起'


def test_fail_streak_is_isolated_per_platform(db):
    """AC3：在一个平台失败 3 次，另一个平台的计数仍是 0。"""
    st = CardPaymentStateModel(db)
    for _ in range(3):
        st.bump_fail_streak(OC, NUM)

    assert st.get_fail_streak(OC, NUM) == 3
    assert st.get_fail_streak(OTHER, NUM) == 0


def test_cooldown_and_fail_streak_do_not_clobber_each_other(db):
    """两者共用一行但语义独立：标冷却不清计数，清计数不解冷却。"""
    st = CardPaymentStateModel(db)
    st.bump_fail_streak(OC, NUM)
    st.bump_fail_streak(OC, NUM)

    st.set_cooldown(OC, NUM, hours=24, reason='充值失败')
    assert st.get_fail_streak(OC, NUM) == 2, 'set_cooldown 不该清掉计数'

    st.reset_fail_streak(OC, NUM)
    assert st.in_cooldown(OC, NUM) is True, 'reset_fail_streak 不该解掉冷却'


def test_state_map_carries_the_fail_streak(db):
    st = CardPaymentStateModel(db)
    st.bump_fail_streak(OC, NUM)
    st.set_cooldown(OC, NUM, hours=24, reason='充值失败')

    entry = st.get_state_map(OC)[NUM]
    assert entry['fail_streak'] == 1
    assert entry['in_cooldown'] is True


def test_reset_does_not_create_a_row_for_an_untouched_card(db):
    """从没失败过的卡不该因为一次成功就凭空多出一行全零噪声。"""
    st = CardPaymentStateModel(db)
    st.reset_fail_streak(OC, NUM)
    assert st.get_state_map(OC) == {}
