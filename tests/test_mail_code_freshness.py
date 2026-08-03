"""GitHub 验证码取信的时间过滤。

背景：hotmail 是**长期真实邮箱**，不是一次性临时箱。一个账号注册时收过一封 GitHub 码，
之后再做新设备验证时，那封旧邮件仍躺在收件箱里。不按时间过滤就会拿旧码去填新表单，
现象是「明明收到了验证码却验证不通过」，且每轮稳定复现。

实测现场（2026-08-03，cunninghamh22@hotmail.com 的收件箱）：
    2026-08-03 05:34:23  [GitHub] Please verify your device-GitHub   ← 上一轮的旧码
    2026-08-02 18:56:34  这是一封测试账号是否正常的邮件
"""

from datetime import datetime, timedelta

from src.services.hotmail_inbox import (
    extract_github_code_from_emails, parse_mail_time, _MAIL_TIME_TOLERANCE_SEC,
)


def _mail(time_str, subject, body=""):
    return {"time": time_str, "subject": subject, "body": body}


OLD = _mail("2026-08-03 05:34:23", "[GitHub] Please verify your device-GitHub 985485")
NEW = _mail("2026-08-03 06:40:10", "[GitHub] Please verify your device-GitHub 112233")
NOISE = _mail("2026-08-02 18:56:34", "这是一封测试账号是否正常的邮件-Semaj Cunningham")


def test_parse_mail_time_matches_ruoanzhu_format():
    assert parse_mail_time("2026-08-03 05:34:23") == datetime(2026, 8, 3, 5, 34, 23)
    assert parse_mail_time("") is None
    assert parse_mail_time("昨天 14:03") is None


def test_without_since_old_code_is_returned():
    """记录旧行为：不传 since 就会取到历史邮件——这正是要避免的。"""
    code, _ = extract_github_code_from_emails([OLD, NOISE])
    assert code == "985485"


def test_stale_code_is_rejected():
    """核心回归：只有旧邮件时必须返回 None，让轮询继续等真正的新邮件。"""
    since = datetime(2026, 8, 3, 6, 30, 0)
    code, _ = extract_github_code_from_emails([OLD, NOISE], since=since)
    assert code is None


def test_fresh_code_is_accepted():
    since = datetime(2026, 8, 3, 6, 30, 0)
    code, matched = extract_github_code_from_emails([NEW, OLD, NOISE], since=since)
    assert code == "112233"
    assert matched["time"] == NEW["time"]


def test_newest_wins_regardless_of_list_order():
    """页面通常按时间倒序，但不该依赖它——把新邮件放在列表末尾也要取到新的。"""
    since = datetime(2026, 8, 3, 5, 0, 0)
    code, _ = extract_github_code_from_emails([OLD, NOISE, NEW], since=since)
    assert code == "112233"


def test_clock_skew_tolerance_keeps_a_just_arrived_mail():
    """收信服务与本机时钟不必严丝合缝：容差内的邮件仍要收下，否则永远收不到码。"""
    mail_time = parse_mail_time(NEW["time"])
    since = mail_time + timedelta(seconds=_MAIL_TIME_TOLERANCE_SEC - 30)
    code, _ = extract_github_code_from_emails([NEW], since=since)
    assert code == "112233"


def test_beyond_tolerance_is_rejected():
    mail_time = parse_mail_time(NEW["time"])
    since = mail_time + timedelta(seconds=_MAIL_TIME_TOLERANCE_SEC + 60)
    code, _ = extract_github_code_from_emails([NEW], since=since)
    assert code is None


def test_unparseable_time_degrades_to_no_filter():
    """收信页结构变了导致时间解析不出来时，宁可冒一次取旧码的风险，也好过永远收不到码。"""
    undated = _mail("", "[GitHub] launch code 445566")
    code, _ = extract_github_code_from_emails([undated], since=datetime(2026, 8, 3, 6, 0, 0))
    assert code == "445566"


def test_non_github_mail_never_matches():
    code, _ = extract_github_code_from_emails([NOISE], since=None)
    assert code is None
