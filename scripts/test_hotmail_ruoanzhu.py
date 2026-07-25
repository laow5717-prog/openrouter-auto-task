"""阶段一收码链路验证脚本（不注册、不落库、不解 Arkose）。

验证链路：读 hotmail.xlsx → 拉 ruoanzhu 收信页 → 解析邮件 → 尝试提 GitHub 验证码。

用法:
    python3 scripts/test_hotmail_ruoanzhu.py                 # 解析 xlsx + 拉第 1 条邮箱的收信页
    python3 scripts/test_hotmail_ruoanzhu.py --index 2       # 指定第几条（1 起）
    python3 scripts/test_hotmail_ruoanzhu.py --all           # 遍历所有邮箱各拉一次收信页
    python3 scripts/test_hotmail_ruoanzhu.py --xlsx path.xlsx
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.hotmail_inbox import (
    read_hotmail_accounts,
    fetch_ruoanzhu_emails,
    extract_github_code_from_emails,
)

_DEFAULT_XLSX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hotmail.xlsx")


def _mask(pw):
    if not pw:
        return ""
    return pw[:2] + "*" * max(0, len(pw) - 4) + pw[-2:] if len(pw) > 4 else "***"


def _probe_one(acc):
    print(f"\n{'='*60}")
    print(f"邮箱: {acc.email}   密码: {_mask(acc.password)}")
    print(f"收信链接: {acc.link[:80]}{'...' if len(acc.link) > 80 else ''}")
    if not acc.link:
        print("  ⚠️ 该行无收信链接，跳过拉信")
        return
    emails = fetch_ruoanzhu_emails(acc.link)
    print(f"  解析到 {len(emails)} 封邮件:")
    for i, em in enumerate(emails):
        body = em.get("body", "")
        print(f"    [{i}] {em.get('time','')}  «{em.get('subject','')[:50]}»")
        print(f"        {body[:100]}{'...' if len(body) > 100 else ''}")
    code, matched = extract_github_code_from_emails(emails)
    if code:
        print(f"  ✅ 提取到 GitHub 验证码: {code}（主题: {matched.get('subject','')[:50]}）")
    else:
        print("  ℹ️ 未找到 GitHub 验证码邮件（收件箱内可能暂无 GitHub 邮件——注册触发发信后才会有）")


def main():
    parser = argparse.ArgumentParser(description="hotmail + ruoanzhu 收码链路验证")
    parser.add_argument("--xlsx", default=_DEFAULT_XLSX, help="hotmail.xlsx 路径")
    parser.add_argument("--index", type=int, default=1, help="拉第几条邮箱的收信页（1 起）")
    parser.add_argument("--all", action="store_true", help="遍历所有邮箱各拉一次")
    args = parser.parse_args()

    print(f"读取 {args.xlsx} ...")
    accounts = read_hotmail_accounts(args.xlsx)
    print(f"解析到 {len(accounts)} 个 hotmail 账号:")
    for i, acc in enumerate(accounts, 1):
        print(f"  {i:2d}. {acc.email}   pw={_mask(acc.password)}   link={'有' if acc.link else '无'}")

    if not accounts:
        print("❌ 未解析出任何账号，检查 xlsx 格式")
        sys.exit(1)

    if args.all:
        for acc in accounts:
            _probe_one(acc)
    else:
        idx = max(1, min(args.index, len(accounts)))
        _probe_one(accounts[idx - 1])

    print(f"\n{'='*60}\n链路验证结束。")


if __name__ == "__main__":
    main()
