"""
通用工具函数模块
"""

import random
import string
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import cfg


def create_http_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=cfg.retry.http_max_retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


http_session = create_http_session()


def get_user_agent():
    return cfg.browser.user_agent


def generate_random_password(length=None):
    if length is None:
        length = cfg.password.length

    chars = cfg.password.charset
    password = ''.join(random.choice(chars) for _ in range(length))
    password = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits) +
        random.choice("!@#$%") +
        password[4:]
    )
    print(f"Generated password: {password}")
    return password


def extract_verification_link(content: str):
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
            print(f"  Found verification link: {link[:80]}...")
            return link

    return None


def extract_verification_code(content: str):
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
            print(f"  Found verification code: {code}")
            return code

    return None
