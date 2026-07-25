#!/usr/bin/env python3
"""启动固定 profile 的有头浏览器，供手动登录后复用登录态。

用途：反自动化站点（GitHub / opencode.ai OAuth）无法全自动登录时，
先用本脚本打开固定浏览器手动登录一次，登录态持久化在
data/profiles/<profile> 目录；之后测试代码用
create_driver(profile_id=<profile>) 复用同一份用户数据，无需再登录。

用法:
    python3 scripts/open_manual_browser.py                 # 默认 profile=manual, 打开 opencode.ai
    python3 scripts/open_manual_browser.py --url https://github.com/login
    python3 scripts/open_manual_browser.py --profile mytest

浏览器窗口保持打开，手动关闭窗口或 Ctrl+C 结束脚本。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver  # noqa: E402

DEFAULT_PROFILE = "manual"
DEFAULT_URL = "https://opencode.ai"


def main():
    parser = argparse.ArgumentParser(description="启动固定 profile 的有头浏览器（手动登录用）")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"持久化 profile 名，默认 {DEFAULT_PROFILE}；测试用 create_driver(profile_id=同名) 复用")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"打开的初始页面，默认 {DEFAULT_URL}")
    args = parser.parse_args()

    session = create_driver(headless=False, profile_id=args.profile)
    profile_dir = session._user_data_dir
    try:
        session.get(args.url)
    except Exception as e:
        print(f"⚠️ 初始页面打开失败(浏览器仍保持): {str(e)[:120]}")

    print(f"\n🔓 浏览器已就绪，请在窗口中手动完成登录。")
    print(f"   profile 目录: {profile_dir}")
    print(f"   测试复用方式: create_driver(profile_id='{args.profile}')")
    print("   登录完成后直接关闭浏览器窗口（或 Ctrl+C）即可，登录态已落盘。\n")

    try:
        while True:
            time.sleep(2)
            try:
                _ = session.current_url  # 浏览器被手动关闭后此处抛异常 → 退出
            except Exception:
                print("🚪 检测到浏览器窗口已关闭，登录态已保存。")
                break
    except KeyboardInterrupt:
        print("\n🛑 收到 Ctrl+C，正在关闭浏览器（登录态已保存）...")
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
