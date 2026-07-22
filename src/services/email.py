"""
邮箱服务模块
使用 mail.tm 免费 API 提供临时邮箱功能
"""

import random
import string
import time
from datetime import datetime, timezone

from src.config import cfg
from src.utils import http_session, extract_verification_link, extract_verification_code

MAIL_TM_API = "https://api.mail.tm"


def _get_available_domain():
    try:
        response = http_session.get(
            f"{MAIL_TM_API}/domains",
            timeout=cfg.retry.http_timeout,
        )
        if response.status_code == 200:
            data = response.json()
            domains = data.get('hydra:member', data) if isinstance(data, dict) else data
            if domains and len(domains) > 0:
                domain = domains[0].get('domain', '')
                if domain:
                    print(f"  获取到可用域名: {domain}")
                    return domain
        print(f"  获取域名失败: HTTP {response.status_code}")
    except Exception as e:
        print(f"  获取域名失败: {e}")
    return None


def create_temp_email():
    """
    创建邮箱（使用 mail.tm）
    返回: (邮箱地址, 邮箱密码, token) 或 (None, None, None)
    """
    print("正在创建临时邮箱 (mail.tm)...")

    domain = _get_available_domain()
    if not domain:
        return None, None, None

    prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    address = f"{prefix}@{domain}"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    headers = {"Content-Type": "application/json"}

    try:
        response = http_session.post(
            f"{MAIL_TM_API}/accounts",
            headers=headers,
            json={"address": address, "password": password},
            timeout=cfg.retry.http_timeout,
        )

        if response.status_code not in (200, 201):
            print(f"  创建账户失败: HTTP {response.status_code} - {response.text[:200]}")
            return None, None, None

        print(f"  账户已创建: {address}")

    except Exception as e:
        print(f"  创建账户失败: {e}")
        return None, None, None

    try:
        response = http_session.post(
            f"{MAIL_TM_API}/token",
            headers=headers,
            json={"address": address, "password": password},
            timeout=cfg.retry.http_timeout,
        )

        if response.status_code == 200:
            token = response.json().get('token', '')
            if token:
                print(f"邮箱已创建: {address}")
                return address, password, token
            else:
                print("  响应中无 token")
        else:
            print(f"  获取 token 失败: HTTP {response.status_code}")

    except Exception as e:
        print(f"  获取 token 失败: {e}")

    return None, None, None


def get_mail_token(address: str, password: str):
    """用已存库的邮箱密码换取 mail.tm 访问 token。

    用于老账号（注册时的 token 未落库）重新读取收件箱。token 不缓存、不落库，
    每次现换。

    返回:
        str | None: 成功返回 token，失败返回 None（不抛异常）
    """
    if not address or not password:
        print("  缺少邮箱地址或密码，无法获取 mail.tm token")
        return None

    try:
        response = http_session.post(
            f"{MAIL_TM_API}/token",
            headers={"Content-Type": "application/json"},
            json={"address": address, "password": password},
            timeout=cfg.retry.http_timeout,
        )

        if response.status_code == 200:
            token = response.json().get('token', '')
            if token:
                return token
            print("  mail.tm 响应中无 token")
        else:
            print(f"  获取 mail.tm token 失败: HTTP {response.status_code}")

    except Exception as e:
        print(f"  获取 mail.tm token 异常: {e}")

    return None


def _parse_created_at(value):
    """解析 mail.tm 的 createdAt（形如 2026-07-19T00:03:48+00:00）。

    返回 aware datetime；无法解析时返回 None——调用方必须把 None 当作
    「不可信，跳过该邮件」，绝不能当作新邮件放行，否则旧验证码会漏进来。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def wait_for_login_code(token: str, since_ts, timeout: int = None):
    """等待并提取 opencode 登录二次验证码（two-factor）。

    与 wait_for_verification_email 的区别，二者不可互换：
      1. 只认验证码，不认验证链接；
      2. 按 createdAt 过滤，只接受 since_ts 之后到达的邮件。

    第 2 点是关键：账号收件箱通常已积压多封历史 "Your opencode login token"
    邮件，不做时间过滤会立刻返回一个早已过期的码。

    参数:
        token: mail.tm 访问 token
        since_ts: aware datetime，只接受严格晚于它的邮件。
                  调用方须在「点击登录按钮之前」取值，否则可能错过邮件。
        timeout: 秒，默认取 cfg.email.wait_timeout
    返回:
        str | None: 验证码；超时未收到新邮件返回 None
    """
    if timeout is None:
        timeout = cfg.email.wait_timeout

    print(f"等待登录验证码 (最长 {timeout} 秒，只认 {since_ts} 之后的邮件)...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        messages = fetch_emails(token) or []

        for msg in messages:
            created = _parse_created_at(msg.get('createdAt'))
            if created is None or created <= since_ts:
                continue  # 旧邮件或时间不可信，一律跳过

            sender = str(msg.get('from', {}).get('address', '')).lower()
            subject = _to_str(msg.get('subject', ''))
            # TODO(opencode): 目标站点为 https://opencode.ai。过滤词 'opencode' 为占位，
            # 接入时按 opencode.ai 实际验证邮件的发件人域名/主题核对调整。
            if 'opencode' not in sender and 'opencode' not in subject.lower():
                continue

            # 登录码通常直接在主题里（Your opencode login token: 1234567）
            code = extract_verification_code(subject)
            if not code:
                detail = get_email_detail(token, msg.get('id', '')) or {}
                for content in (_to_str(detail.get('text')),
                                _to_str(detail.get('html')),
                                _to_str(detail.get('intro'))):
                    if content:
                        code = extract_verification_code(content)
                        if code:
                            break

            if code:
                print(f"\n收到登录验证码: {code} (邮件时间 {created})")
                return code

        elapsed = int(time.time() - start_time)
        print(f"  等待中... ({elapsed}秒)", end='\r')
        time.sleep(cfg.email.poll_interval)

    print("\n等待登录验证码超时")
    return None


def wait_for_github_launch_code(token: str, since_ts, timeout: int = None):
    """等待并提取 GitHub 注册验证邮件里的 launch code（8 位数字）。

    与 wait_for_login_code 同构（按 createdAt 过滤 + 只提码不认链接），区别只在
    发件人/主题过滤词换成 GitHub。GitHub 注册确认邮件发件人为 noreply@github.com，
    主题形如 "Your GitHub launch code" 或 "[GitHub] Please verify your device"，
    正文/主题含 8 位数字码。

    参数:
        token: mail.tm 访问 token
        since_ts: aware datetime，只接受严格晚于它的邮件（调用方须在「点提交/触发发信之前」取值）
        timeout: 秒，默认取 cfg.email.wait_timeout
    返回:
        str | None: 验证码；超时未收到返回 None
    """
    if timeout is None:
        timeout = cfg.email.wait_timeout

    print(f"等待 GitHub 验证码邮件 (最长 {timeout} 秒，只认 {since_ts} 之后的邮件)...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        messages = fetch_emails(token) or []

        for msg in messages:
            created = _parse_created_at(msg.get('createdAt'))
            if created is None or created <= since_ts:
                continue  # 旧邮件或时间不可信，一律跳过

            sender = str(msg.get('from', {}).get('address', '')).lower()
            subject = _to_str(msg.get('subject', ''))
            if 'github' not in sender and 'github' not in subject.lower():
                continue

            # launch code 常直接出现在主题里（Your GitHub launch code: 12345678）
            code = extract_verification_code(subject)
            if not code:
                detail = get_email_detail(token, msg.get('id', '')) or {}
                for content in (_to_str(detail.get('text')),
                                _to_str(detail.get('html')),
                                _to_str(detail.get('intro'))):
                    if content:
                        code = extract_verification_code(content)
                        if code:
                            break

            if code:
                print(f"\n收到 GitHub 验证码: {code} (邮件时间 {created})")
                return code

        elapsed = int(time.time() - start_time)
        print(f"  等待中... ({elapsed}秒)", end='\r')
        time.sleep(cfg.email.poll_interval)

    print("\n等待 GitHub 验证码邮件超时")
    return None


def fetch_emails(token: str):
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = http_session.get(
            f"{MAIL_TM_API}/messages",
            headers=headers,
            timeout=cfg.retry.http_timeout,
        )

        if response.status_code == 200:
            data = response.json()
            messages = data.get('hydra:member', data) if isinstance(data, dict) else data
            return messages if isinstance(messages, list) else []
        else:
            print(f"  获取邮件失败: HTTP {response.status_code}")

    except Exception as e:
        print(f"  获取邮件失败: {e}")

    return None


def get_email_detail(token: str, message_id: str):
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = http_session.get(
            f"{MAIL_TM_API}/messages/{message_id}",
            headers=headers,
            timeout=cfg.retry.http_timeout,
        )

        if response.status_code == 200:
            return response.json()

    except Exception as e:
        print(f"  获取邮件详情失败: {e}")

    return None


def _to_str(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get('value', item.get('html', str(item)))))
            else:
                parts.append(str(item))
        return '\n'.join(parts)
    if isinstance(value, dict):
        return str(value)
    return str(value)


def wait_for_verification_email(token: str, timeout: int = None):
    if timeout is None:
        timeout = cfg.email.wait_timeout

    print(f"等待验证邮件 (最长 {timeout} 秒)...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        messages = fetch_emails(token)

        if messages and len(messages) > 0:
            for msg in messages:
                sender = str(msg.get('from', {}).get('address', '')).lower()
                subject = _to_str(msg.get('subject', ''))

                if 'opencode' in sender or 'opencode' in subject.lower():
                    print(f"\n收到 opencode 验证邮件！")
                    print(f"   主题: {subject}")

                    message_id = msg.get('id', '')
                    if message_id:
                        detail = get_email_detail(token, message_id)
                        if detail:
                            html_content = _to_str(detail.get('html'))
                            text_content = _to_str(detail.get('text'))
                            intro = _to_str(detail.get('intro'))

                            for content in [html_content, text_content, intro, subject]:
                                if content:
                                    link = extract_verification_link(content)
                                    if link:
                                        return link

                            for content in [html_content, text_content, intro, subject]:
                                if content:
                                    code = extract_verification_code(content)
                                    if code:
                                        return code

                            print(f"   无法从邮件中提取验证信息")
                            if text_content:
                                print(f"   文本预览: {text_content[:300]}")

                    intro = _to_str(msg.get('intro', ''))
                    if intro:
                        link = extract_verification_link(intro)
                        if link:
                            return link

        elapsed = int(time.time() - start_time)
        print(f"  等待中... ({elapsed}秒)", end='\r')
        time.sleep(cfg.email.poll_interval)

    print("\n等待验证邮件超时")
    return None
