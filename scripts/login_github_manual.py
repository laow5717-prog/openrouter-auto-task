#!/usr/bin/env python3
"""用数据库里保存的 GitHub 账号，在固定 profile 浏览器里自动登录。

流程：
  1) 从 accounts 表按 email 读取 login_password
  2) 打开固定 profile(默认 manual) 的有头浏览器，导航到 github.com/login
  3) 自动填用户名/密码并提交
  4) 若触发新设备邮箱验证 / 2FA，浏览器保持打开，请手动完成
  5) 登录态持久化在 data/profiles/<profile>，之后 create_driver(profile_id=<profile>) 可复用

用法:
    python3 scripts/login_github_manual.py --email abcie2024@gmail.com
    python3 scripts/login_github_manual.py --email abcie2024@gmail.com --profile manual
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver  # noqa: E402
from src.browser import github_signup as gh  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.models.account import AccountModel  # noqa: E402

DEFAULT_PROFILE = "manual"
LOGIN_URL = "https://github.com/login"


def _read_credentials(email):
    db = Database()
    try:
        row = db.fetchone(
            "SELECT email, login_password FROM accounts WHERE email=?", (email,)
        )
    finally:
        db.close()
    if not row:
        return None, None
    row = dict(row)
    return row.get("email"), row.get("login_password")


def _keep_alive(session):
    print("\n🔓 浏览器保持打开。若出现二次验证/2FA 请手动完成；完成后关闭窗口或 Ctrl+C。")
    try:
        while True:
            time.sleep(2)
            try:
                _ = session.current_url
            except Exception:
                print("🚪 检测到浏览器窗口已关闭，登录态已保存。")
                break
    except KeyboardInterrupt:
        print("\n🛑 收到 Ctrl+C，正在关闭浏览器（登录态已保存）...")


def main():
    parser = argparse.ArgumentParser(description="用数据库账号自动登录 GitHub（固定 profile）")
    parser.add_argument("--email", required=True, help="accounts 表中的 GitHub 登录邮箱")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"持久化 profile 名，默认 {DEFAULT_PROFILE}")
    args = parser.parse_args()

    email, password = _read_credentials(args.email)
    if not email:
        print(f"❌ 数据库中未找到账号: {args.email}")
        sys.exit(1)
    if not password:
        print(f"❌ 账号 {email} 未保存 login_password")
        sys.exit(1)
    print(f"✅ 已从数据库读取凭据: {email}")

    session = create_driver(headless=False, profile_id=args.profile)
    print(f"   profile 目录: {session._user_data_dir}")

    try:
        session.get(LOGIN_URL)
        time.sleep(2)

        # 已登录检测：若已在登录态，github.com/login 会直接跳走
        cur = session.current_url.lower()
        if "/login" not in cur and "github.com" in cur:
            print(f"✅ 该 profile 似乎已是登录态，当前页: {session.current_url}")
            _mark_status(email, "logged_in")
            _keep_alive(session)
            return

        login = gh.login_after_signup(session, email, password)
        print(f"\n登录结果: {login}")

        if login.get("suspended"):
            print("⚠️ 账号登录后被 GitHub 挂起（/suspended）。")
            _mark_status(email, "suspended")
        elif login.get("ok"):
            print("✅ 登录成功，登录态已写入 profile。")
            _mark_status(email, "logged_in")
        elif login.get("needs_device_verification"):
            print("📧 触发新设备验证/2FA，请在浏览器中手动输入验证码后完成登录。")
            _mark_status(email, "need_device_verification")
        else:
            print(f"ℹ️ 登录状态未确认：{login.get('detail')}，请在浏览器中人工确认。")

        _keep_alive(session)
    finally:
        close_driver(session)


def _mark_status(email, status):
    db = Database()
    try:
        AccountModel(db).update_identity_status(email, status)
    except Exception as e:
        print(f"  ⚠️ 更新账号状态失败(忽略): {str(e)[:80]}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
