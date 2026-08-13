"""拒付原因文案的提取。

回归 2026-08-12：此前各调用点按字符偏移截 `body[idx-30:idx+60]`，而 body 是整帧的
inner_text（多行），截出来的片段两头各挂半行无关内容，真原因被夹在中间。这不影响
充值成功率，但让人看不清问题——当天统计拒付原因时 26.9% 归不了类，正是被两头的
半行拖的，排查时还因此把方向带偏过一次。
"""

from src.payments.stripe_checkout import decline_line, _DECLINE_LINE_MAX


# 生产库里真实的帧文本形状：支付方式列表在上、表单标签在下，原因夹在中间
REAL_BODY = """pay opencode
card information
visa, mastercard, amex, discover, diners club, unionpay
your card was declined. please contact your card issuer.
cardholder name
b
billing address
pay $20.00"""

CAPTCHA_WORDS = ("hcaptcha", "sitekey", "site key", "challenge", "captcha")


def test_returns_only_the_reason_line():
    """只要那一行，不要上下相邻行的碎片。"""
    assert decline_line(REAL_BODY) == \
        "your card was declined. please contact your card issuer."


def test_does_not_leak_neighbouring_lines():
    """具体守住那两个噪音源——它们正是当初误导排查的东西。"""
    got = decline_line(REAL_BODY)
    assert "unionpay" not in got, "上一行的支付方式列表又漏进来了"
    assert "cardholder name" not in got, "下一行的表单标签又漏进来了"
    assert "\n" not in got, "结果必须是单行"


def test_credit_card_specific_decline():
    body = ("visa, mastercard, amex, discover, diners club, unionpay\n"
            "your credit card was declined. try paying with a debit card instead.\n"
            "cardholder name")
    assert decline_line(body) == \
        "your credit card was declined. try paying with a debit card instead."


def test_captcha_state_is_not_mistaken_for_a_decline():
    """hCaptcha 的内部状态含 expired/incorrect，会撞卡拒付关键词，必须能排除掉。

    没有这层防护，人机验证会被误判成卡拒付——卡被冤枉地冷却甚至判废。
    """
    body = "hcaptcha challenge\nsitekey incorrect\nchallenge-expired"
    assert decline_line(body, exclude=CAPTCHA_WORDS) == ""
    # 不传 exclude 时确实会误命中，这正是调用方必须传的原因
    assert decline_line(body) == "sitekey incorrect"


def test_exclude_skips_the_line_and_keeps_looking():
    """排除词只跳过那一行，不能让整帧的真拒付行一起丢掉。"""
    body = ("sitekey incorrect\n"
            "your card was declined. please contact your card issuer.")
    assert decline_line(body, exclude=CAPTCHA_WORDS) == \
        "your card was declined. please contact your card issuer."


def test_single_line_body_still_works():
    """整帧渲染成一行时（无换行）也要拿得到，真原因在句首。"""
    got = decline_line("foo bar your card was declined. please contact issuer. baz")
    assert "declined" in got


def test_long_line_is_truncated():
    body = "x" * 500 + " declined"
    assert len(decline_line(body)) <= _DECLINE_LINE_MAX


def test_no_match_and_empty_input():
    assert decline_line("everything is fine") == ""
    assert decline_line("") == ""
    assert decline_line(None) == ""


def test_call_sites_use_it():
    """守接线：两条支付管线都要用它，别有一条又退回字符切片。"""
    import inspect
    from src.platforms.opencode import billing, subscribe

    for mod in (billing, subscribe):
        src = inspect.getsource(mod)
        assert "decline_line(" in src, f"{mod.__name__} 没有使用 decline_line"
        assert "idx-30" not in src and "idx - 30" not in src, \
            f"{mod.__name__} 还留着按字符偏移截取的旧写法"
