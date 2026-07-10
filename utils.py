"""
通用工具函数模块
"""

import random
import string
import os
import sys
import re
import time
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    PASSWORD_LENGTH,
    PASSWORD_CHARS,
    TXT_FILE,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT,
    USER_AGENT,
)


def create_http_session():
    """创建带重试机制的 HTTP Session"""
    session = requests.Session()
    retry_strategy = Retry(
        total=HTTP_MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# 全局 HTTP Session
http_session = create_http_session()


def get_user_agent():
    """获取 User-Agent 字符串"""
    return USER_AGENT


def generate_random_password(length=None):
    """
    生成随机密码
    确保包含大写字母、小写字母、数字和特殊字符
    """
    if length is None:
        length = PASSWORD_LENGTH

    password = ''.join(random.choice(PASSWORD_CHARS) for _ in range(length))
    # 确保包含各类字符
    password = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits) +
        random.choice("!@#$%") +
        password[4:]
    )
    print(f"✅ 已生成密码: {password}")
    return password


def save_to_txt(email: str, password: str = None, status="已注册", email_password: str = None):
    """
    保存账号信息到 TXT 文件
    格式: 邮箱----CF密码----时间----状态----邮箱密码
    """
    try:
        base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
        file_path = os.path.join(base, TXT_FILE)
        current_date = datetime.now().strftime("%Y%m%d_%H%M%S")

        lines = []
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        found = False
        ep = email_password if email_password else 'N/A'
        new_line = f"{email}----{password if password else 'N/A'}----{current_date}----{status}----{ep}\n"

        for i, line in enumerate(lines):
            if line.startswith(f"{email}----"):
                parts = line.strip().split("----")
                current_pw = parts[1] if len(parts) > 1 else 'N/A'
                current_ep = parts[4] if len(parts) > 4 else 'N/A'
                final_pw = password if password else current_pw
                final_ep = email_password if email_password else current_ep
                lines[i] = f"{email}----{final_pw}----{current_date}----{status}----{final_ep}\n"
                found = True
                break

        if not found:
            lines.append(new_line)

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        print(f"💾 账号状态已更新: {status}")

    except Exception as e:
        print(f"❌ 保存账号信息失败: {e}")


def update_account_status(email: str, new_status: str, password: str = None, email_password: str = None):
    """更新账号状态的快捷函数"""
    save_to_txt(email, password, new_status, email_password=email_password)


def extract_verification_link(content: str):
    """
    从邮件内容中提取 Cloudflare 验证链接
    Cloudflare 通常发送验证链接而非验证码
    """
    if not content:
        return None

    patterns = [
        r'(https?://dash\.cloudflare\.com/[^\s"<>]+verify[^\s"<>]*)',
        r'(https?://[^\s"<>]*cloudflare[^\s"<>]*verify[^\s"<>]*)',
        r'(https?://[^\s"<>]*cloudflare[^\s"<>]*confirm[^\s"<>]*)',
        r'href="(https?://[^\s"<>]*cloudflare[^\s"<>]*)"',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            link = matches[0]
            print(f"  ✅ 提取到验证链接: {link[:80]}...")
            return link

    return None


def extract_verification_code(content: str):
    """从邮件内容中提取 6 位数字验证码（备用方案）"""
    if not content:
        return None

    patterns = [
        r'code is\s*(\d{6})',
        r'verification code[:\s]*(\d{6})',
        r'(\d{6})',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            code = matches[0]
            print(f"  ✅ 提取到验证码: {code}")
            return code

    return None
