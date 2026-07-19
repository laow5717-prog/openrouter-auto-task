"""绑卡失败归因测试：决定要不要把底料卡永久标为 invalid。

误标不可逆——好卡被打成 invalid 后再也不会被选中，因此宁可漏标。
最典型的踩坑：Stripe Link 勾选框要求填手机号，报错被打上 [Stripe字段错误] 前缀，
若只看前缀归因，会把一整批完好的卡全部废掉。
"""

import pytest

from src.utils import is_card_fault


@pytest.mark.parametrize("err", [
    "[外部原因] card was declined",
    "[外部原因] insufficient funds",
    "[外部原因] 银行卡被拒",
    "[表单字段错误] card number is invalid",
    "[表单字段错误] security code is incorrect",
    "[表单字段错误] 卡号错误",
    "[表单字段错误] card has expired",
    "[Stripe字段错误] Your card was declined.",
    "[控制台表单错误] Setup intent error: Your card's security code is incorrect",
])
def test_card_faults_are_attributed(err):
    assert is_card_fault(err) is True, f"应归因于卡片: {err}"


@pytest.mark.parametrize("err", [
    # 真实踩过的坑：Link 要手机号，与卡无关，却带 [Stripe字段错误] 前缀
    "[Stripe字段错误] Please provide a mobile phone number.",
    "[Stripe字段错误] Please provide a phone number",
    # 自动化流程/环境问题
    "[操作失败] 未找到添加付款方式按钮",
    "[操作失败] 支付弹窗未出现",
    "[操作失败] Stripe表单未加载",
    "[操作失败] 填写信用卡信息失败",
    "[验证超时] Turnstile人机验证超时",
    "[浏览器中断] 点击提交按钮失败",
    "[超时] 等待提交结果超过60秒",
    "[控制台表单错误] Captcha is required.",
    # 空/未知
    "",
    None,
    "bind failed",
    "unknown error",
])
def test_non_card_faults_are_not_attributed(err):
    assert is_card_fault(err) is False, f"不应归因于卡片: {err}"


def test_not_card_phrases_win_over_fault_prefix():
    """否定词优先于前缀——前缀看着像卡的问题也不能标。"""
    assert is_card_fault("[表单字段错误] Please provide a mobile phone number.") is False


def test_unknown_prefix_falls_back_to_phrase_match():
    """前缀不认识时按文案判定，两个方向都要正确。"""
    assert is_card_fault("[新前缀] your card was declined by the issuer") is True
    assert is_card_fault("[新前缀] something went wrong, try again") is False
