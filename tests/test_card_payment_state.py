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


# ---------- 到期时刻 = max(次日 00:00, now + hours) ----------
#
# 这两条断言刻意写成**与跑测试的真实时刻无关**的不变式。set_cooldown 用的是 SQLite
# 的 datetime('now','localtime')，没法 monkeypatch Python 的 clock；照抄一遍 max 表达式
# 来比对又只是把实现重写一次，测不出任何东西。改为分别验证两个下界各自成立——
# 两者合起来就精确刻画了 max 的语义。


def _sql(db, expr, *params):
    return db.fetchone(f"SELECT {expr} AS v", params)['v']


def test_cooldown_never_expires_before_tomorrow_midnight(db):
    """当日付款失败的卡，当日不会回到可选集——「当日不再使用」的核心不变式。

    hours 特意给 1：纯滑动窗口下这张卡一小时后就回来了，自然日分支不生效的话本条必红。
    """
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=1, reason='充值失败')

    tomorrow = _sql(db, "datetime('now','localtime','start of day','+1 day')")
    assert st.get_tds_until(OC, NUM) >= tomorrow, \
        '当日失败的卡在当日就恢复了，自然日边界没生效'


def test_cooldown_respects_the_hours_floor(db):
    """小时下限同样生效，否则 23:59 失败的卡一分钟后就能再刷，当日限制形同虚设。

    先取 before 再 set：set_cooldown 内部的 now ≥ before，故 tds_until ≥ before+hours
    恒成立，不受两次取时间之间那几毫秒影响。
    """
    st = CardPaymentStateModel(db)
    before = _sql(db, "datetime('now','localtime')")
    st.set_cooldown(OC, NUM, hours=12, reason='充值失败')

    floor_ = _sql(db, "datetime(?, ?)", before, '+12 hours')
    assert st.get_tds_until(OC, NUM) >= floor_, '小时下限被自然日边界吃掉了'


def test_a_bigger_floor_pushes_the_expiry_later(db):
    """下限确实参与计算：同一时刻下，hours 越大到期越晚（或相等，被次日零点托住）。"""
    st = CardPaymentStateModel(db)
    st.set_cooldown(OC, NUM, hours=1)
    small = st.get_tds_until(OC, NUM)
    st.set_cooldown(OC, NUM, hours=48)
    big = st.get_tds_until(OC, NUM)

    assert big > small, 'hours 从 1 调到 48 到期时刻没变，下限根本没参与计算'
