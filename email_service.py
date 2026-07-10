"""
邮箱服务模块
使用 mail.tm 免费 API 提供临时邮箱功能
API 文档: https://docs.mail.tm
"""

import random
import string
import time

from config import (
    EMAIL_WAIT_TIMEOUT,
    EMAIL_POLL_INTERVAL,
    HTTP_TIMEOUT,
)
from utils import http_session, extract_verification_link, extract_verification_code

MAIL_TM_API = "https://api.mail.tm"


def _get_available_domain():
    """获取 mail.tm 可用的邮箱域名"""
    try:
        response = http_session.get(
            f"{MAIL_TM_API}/domains",
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code == 200:
            data = response.json()
            domains = data.get('hydra:member', data) if isinstance(data, dict) else data
            if domains and len(domains) > 0:
                domain = domains[0].get('domain', '')
                if domain:
                    print(f"  ✅ 获取到可用域名: {domain}")
                    return domain
        print(f"  ❌ 获取域名失败: HTTP {response.status_code}")
    except Exception as e:
        print(f"  ❌ 获取域名失败: {e}")
    return None


def create_temp_email():
    """
    创建临时邮箱（使用 mail.tm）
    返回: (邮箱地址, token) 或 (None, None)
    """
    print("📧 正在创建临时邮箱 (mail.tm)...")

    # 1. 获取可用域名
    domain = _get_available_domain()
    if not domain:
        return None, None

    # 2. 生成随机邮箱地址和密码
    prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    address = f"{prefix}@{domain}"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

    headers = {"Content-Type": "application/json"}

    # 3. 创建账号
    try:
        response = http_session.post(
            f"{MAIL_TM_API}/accounts",
            headers=headers,
            json={"address": address, "password": password},
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code not in (200, 201):
            print(f"  ❌ 创建账号失败: HTTP {response.status_code} - {response.text[:200]}")
            return None, None

        print(f"  ✅ 账号创建成功: {address}")

    except Exception as e:
        print(f"  ❌ 创建账号失败: {e}")
        return None, None

    # 4. 获取 Token
    try:
        response = http_session.post(
            f"{MAIL_TM_API}/token",
            headers=headers,
            json={"address": address, "password": password},
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            token = response.json().get('token', '')
            if token:
                print(f"✅ 邮箱创建成功: {address}")
                return address, token
            else:
                print("  ❌ 响应中未包含 token")
        else:
            print(f"  ❌ 获取 token 失败: HTTP {response.status_code}")

    except Exception as e:
        print(f"  ❌ 获取 token 失败: {e}")

    return None, None


def fetch_emails(token: str):
    """获取邮件列表"""
    headers = {
        "Authorization": f"Bearer {token}",
    }

    try:
        response = http_session.get(
            f"{MAIL_TM_API}/messages",
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            data = response.json()
            messages = data.get('hydra:member', data) if isinstance(data, dict) else data
            return messages if isinstance(messages, list) else []
        else:
            print(f"  获取邮件错误: HTTP {response.status_code}")

    except Exception as e:
        print(f"  获取邮件错误: {e}")

    return None


def get_email_detail(token: str, message_id: str):
    """获取邮件详情"""
    headers = {
        "Authorization": f"Bearer {token}",
    }

    try:
        response = http_session.get(
            f"{MAIL_TM_API}/messages/{message_id}",
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 200:
            return response.json()

    except Exception as e:
        print(f"  获取邮件详情错误: {e}")

    return None


def _to_str(value):
    """
    将 mail.tm 返回的字段安全转换为字符串
    有些字段可能是 list、dict 或 None
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # 将列表中的元素拼接为字符串
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # 可能是 {"value": "...", ...} 结构
                parts.append(str(item.get('value', item.get('html', str(item)))))
            else:
                parts.append(str(item))
        return '\n'.join(parts)
    if isinstance(value, dict):
        return str(value)
    return str(value)


def wait_for_verification_email(token: str, timeout: int = None):
    """
    等待 Cloudflare 验证邮件并提取验证链接
    返回: 验证链接字符串，未找到返回 None
    """
    if timeout is None:
        timeout = EMAIL_WAIT_TIMEOUT

    print(f"⏳ 正在等待验证邮件（最长 {timeout} 秒）...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        messages = fetch_emails(token)

        if messages and len(messages) > 0:
            for msg in messages:
                sender = str(msg.get('from', {}).get('address', '')).lower()
                subject = _to_str(msg.get('subject', ''))

                # 判断是否为 Cloudflare 验证邮件
                if 'cloudflare' in sender or 'cloudflare' in subject.lower():
                    print(f"\n📧 收到 Cloudflare 验证邮件!")
                    print(f"   主题: {subject}")

                    # 获取邮件详情以拿到完整内容
                    message_id = msg.get('id', '')
                    if message_id:
                        detail = get_email_detail(token, message_id)
                        if detail:
                            # 安全转换所有字段为字符串
                            html_content = _to_str(detail.get('html'))
                            text_content = _to_str(detail.get('text'))
                            intro = _to_str(detail.get('intro'))

                            # DEBUG: 打印字段类型帮助排查
                            print(f"   DEBUG html 类型: {type(detail.get('html')).__name__}, 长度: {len(html_content)}")
                            print(f"   DEBUG text 类型: {type(detail.get('text')).__name__}, 长度: {len(text_content)}")

                            # 按优先级尝试提取验证链接
                            for content in [html_content, text_content, intro, subject]:
                                if content:
                                    link = extract_verification_link(content)
                                    if link:
                                        return link

                            # 备用：尝试提取验证码
                            for content in [html_content, text_content, intro, subject]:
                                if content:
                                    code = extract_verification_code(content)
                                    if code:
                                        return code

                            # 如果都没提取到，打印内容片段帮助排查
                            print(f"   ⚠️ 未能从邮件中提取验证信息")
                            if text_content:
                                print(f"   邮件文本预览: {text_content[:300]}")

                    # 如果没有 id，尝试从摘要提取
                    intro = _to_str(msg.get('intro', ''))
                    if intro:
                        link = extract_verification_link(intro)
                        if link:
                            return link

        elapsed = int(time.time() - start_time)
        print(f"  等待中... ({elapsed}秒)", end='\r')
        time.sleep(EMAIL_POLL_INTERVAL)

    print("\n⏰ 等待验证邮件超时")
    return None
