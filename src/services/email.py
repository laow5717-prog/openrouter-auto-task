"""
邮箱服务模块
使用 mail.tm 免费 API 提供临时邮箱功能
"""

import random
import string
import time

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

                if 'cloudflare' in sender or 'cloudflare' in subject.lower():
                    print(f"\n收到 Cloudflare 验证邮件！")
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
