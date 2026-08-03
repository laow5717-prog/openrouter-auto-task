#!/usr/bin/env python3
"""抓取有余额账号的 opencode API key，落库 accounts.apikey。

每个账号有独立持久化 profile（data/profiles/<email>/），复用已登录态：
    /auth  自动跳到 /workspace/<wid> → 提取 wid
    /workspace/<wid>/keys  页面显示打码 key，但 outerHTML 含完整 sk- 明文
    正则抓 sk-[A-Za-z0-9_-]{20,} → update_apikey

登录态失效则停在 /auth（无 wid），记失败并继续下一个，不中断整批。
有头浏览器 + 同 profile 不可并发，故串行。

用法：
    python scripts/fetch_apikeys.py                       # 默认 credits_balance>0 的账号
    python scripts/fetch_apikeys.py --email a@b.com       # 只跑单个账号
    python scripts/fetch_apikeys.py --all                 # 所有账号
    python scripts/fetch_apikeys.py --dry                 # 只列出目标账号，不开浏览器
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import Database
from src.models.account import AccountModel
from src.models.platform_account import PlatformAccountModel
from src.browser.driver import create_driver, close_driver

WID_RE = re.compile(r"wrk_[A-Za-z0-9]+")
KEY_JS = r"""
() => {
  const m = document.documentElement.outerHTML.match(/sk-[A-Za-z0-9_\-]{20,}/);
  return m ? m[0] : null;
}
"""


def mask(key):
    return f"{key[:8]}...{key[-4:]}" if key and len(key) > 14 else "***"


def wait_workspace(session, timeout=25):
    """轮询等 /auth 跳转到 /workspace/<wid>，返回 wid 或 None。"""
    start = time.time()
    while time.time() - start < timeout:
        m = WID_RE.search(session.current_url or "")
        if m:
            return m.group(0)
        time.sleep(1)
    return None


def fetch_one(email):
    """单账号：返回 (ok: bool, detail: str, key: str|None)。"""
    session = create_driver(headless=False, profile_id=email)
    try:
        session.get("https://opencode.ai/auth")
        wid = wait_workspace(session)
        if not wid:
            return False, f"登录态失效/未跳转 workspace（停在 {str(session.current_url)[:60]}）", None
        session.get(f"https://opencode.ai/workspace/{wid}/keys")
        time.sleep(3)
        key = session.page.evaluate(KEY_JS)
        if key:
            return True, f"wid={wid}", key
        return False, f"/keys 未抓到 sk-（wid={wid}）", None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}", None
    finally:
        close_driver(session)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=None, help="只跑单个账号")
    ap.add_argument("--all", action="store_true", help="所有账号（默认只跑 credits_balance>0）")
    ap.add_argument("--dry", action="store_true", help="只列出目标账号，不开浏览器")
    ap.add_argument("--db", default=None, help="数据库路径（默认用内置路径）")
    ap.add_argument("--platform", default="opencode", help="目标平台 slug")
    args = ap.parse_args()

    db = Database(args.db)
    account = AccountModel(db)
    platform_account = PlatformAccountModel(db)

    if args.email:
        targets = [args.email]
    elif args.all:
        rows = db.fetchall("SELECT email FROM accounts ORDER BY id")
        targets = [dict(r)["email"] for r in rows]
    else:
        # 余额已搬到 platform_accounts，按平台取
        rows = db.fetchall(
            "SELECT email FROM platform_accounts WHERE platform=? AND credits_balance>0 "
            "ORDER BY id", (args.platform,))
        targets = [dict(r)["email"] for r in rows]

    print(f"目标账号 {len(targets)} 个: {', '.join(targets)}")
    if args.dry:
        return

    ok_list, fail_list = [], []
    for i, email in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {email} ...", flush=True)
        ok, detail, key = fetch_one(email)
        if ok:
            platform_account.update_apikey(args.platform, email, key)
            ok_list.append(email)
            print(f"  ✓ 落库 apikey={mask(key)}  ({detail})", flush=True)
        else:
            fail_list.append((email, detail))
            print(f"  ✗ {detail}", flush=True)

    print(f"\n{'=' * 50}")
    print(f"成功 {len(ok_list)} / 失败 {len(fail_list)}")
    for email, detail in fail_list:
        print(f"  ✗ {email}: {detail}")


if __name__ == "__main__":
    main()
