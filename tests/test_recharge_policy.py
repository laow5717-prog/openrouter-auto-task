"""充值策略参数（RechargeConfig）。

这个对象是四条业务规则的**唯一参数来源**：金额区间、单账号余额上限、连续失败判废
阈值、失败冷却时长。它本身不知道这些数字怎么被用——判定全在编排层。

本文件盯两件事：
  1. 取值永远可用。config.yaml 是人手写的，一个越界的数字不该让整条充值流水线抛异常。
  2. with_overrides 绝不污染全局。cfg 是进程级单例，两个平台并发跑时原地改参数
     会让后启动的那个把前一个的配置覆盖掉——这类竞态不报错、不留日志。
"""

import textwrap

from src.config import ConfigLoader, RechargeConfig


def test_defaults_match_the_documented_policy():
    c = RechargeConfig()
    assert (c.amount_min, c.amount_max) == (20, 100)
    assert c.balance_cap == 200.0
    assert c.max_fail_streak == 3
    assert c.fail_cooldown_hours == 24


def test_pick_amount_stays_inside_the_range():
    c = RechargeConfig(amount_min=30, amount_max=60)
    for _ in range(200):
        assert 30 <= c.pick_amount() <= 60


def test_pick_amount_varies_across_calls():
    """按笔随机，不是一次算好用到底。"""
    c = RechargeConfig(amount_min=20, amount_max=100)
    assert len({c.pick_amount() for _ in range(50)}) > 1


def test_fixed_range_yields_a_fixed_amount():
    """min==max 时退化成固定金额——测试与「就要充某个数」的场景都要靠它。"""
    c = RechargeConfig(amount_min=37, amount_max=37)
    assert {c.pick_amount() for _ in range(20)} == {37}


def test_inverted_range_is_clamped_not_raised():
    """min > max 是明显的配置笔误，取下界而不是抛 ValueError 打断充值。"""
    c = RechargeConfig(amount_min=80, amount_max=20)
    assert c.bounds() == (80, 80)
    assert c.pick_amount() == 80


def test_out_of_bound_values_are_clamped():
    c = RechargeConfig(amount_min=0, amount_max=99999)
    lo, hi = c.bounds()
    assert lo == RechargeConfig.AMOUNT_FLOOR
    assert hi == RechargeConfig.AMOUNT_CEILING


def test_fail_threshold_is_never_below_one():
    """配成 0 会让 `streak >= threshold` 恒真——首拒即判废，比改造前还激进。

    手改 config.yaml 时把这个字段当开关写个 0 是很自然的笔误，而后果
    （整池好卡被一次性烧掉）不可逆，所以在读取处就兜住。
    """
    for bad in (0, -1, -100):
        assert RechargeConfig(max_fail_streak=bad).fail_threshold() == 1


def test_fail_threshold_survives_a_non_numeric_config():
    assert RechargeConfig(max_fail_streak='oops').fail_threshold() == 1


def test_fail_threshold_passes_sane_values_through():
    assert RechargeConfig(max_fail_streak=3).fail_threshold() == 3
    assert RechargeConfig(max_fail_streak=1).fail_threshold() == 1


def test_with_overrides_returns_a_new_instance():
    base = RechargeConfig()
    other = base.with_overrides(amount_min=50, amount_max=50)

    assert other is not base
    assert (other.amount_min, other.amount_max) == (50, 50)
    assert (base.amount_min, base.amount_max) == (20, 100), '原实例必须原样不动'


def test_with_overrides_ignores_none_and_unknown_keys():
    base = RechargeConfig(amount_min=25, balance_cap=300.0)
    got = base.with_overrides(amount_min=None, balance_cap=None, nonsense=1)

    assert got.amount_min == 25
    assert got.balance_cap == 300.0


def test_with_overrides_can_change_every_policy_field():
    got = RechargeConfig().with_overrides(
        amount_min=5, amount_max=15, balance_cap=42.0,
        max_fail_streak=1, fail_cooldown_hours=6)

    assert (got.amount_min, got.amount_max) == (5, 15)
    assert got.balance_cap == 42.0
    assert got.max_fail_streak == 1
    assert got.fail_cooldown_hours == 6


def test_yaml_section_is_parsed(tmp_path):
    p = tmp_path / 'config.yaml'
    p.write_text(textwrap.dedent("""
        recharge:
          amount_min: 15
          amount_max: 45
          balance_cap: 75
          max_fail_streak: 5
          fail_cooldown_hours: 12
    """), encoding='utf-8')

    c = ConfigLoader(str(p)).config.recharge

    assert (c.amount_min, c.amount_max) == (15, 45)
    assert c.balance_cap == 75.0
    assert c.max_fail_streak == 5
    assert c.fail_cooldown_hours == 12


def test_missing_yaml_section_falls_back_to_defaults(tmp_path):
    """没配 recharge: 段时行为要可预期，而不是报错或拿到空值。"""
    p = tmp_path / 'config.yaml'
    p.write_text("batch:\n  interval_min: 1\n", encoding='utf-8')

    c = ConfigLoader(str(p)).config.recharge

    assert (c.amount_min, c.amount_max) == (20, 100)
    assert c.max_fail_streak == 3
