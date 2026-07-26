#!/usr/bin/env python3
"""把 hotmail.xlsx 里的 ruoanzhu 收信链接回填到 accounts.email_verify_link。

只写「账号已存在且链接为空」的行：不新建账号、不覆盖已有链接，可重复执行。
xlsx 有但 accounts 没有的邮箱直接跳过。

用法：
    python scripts/backfill_verify_links.py            # 默认库 + 根目录 hotmail.xlsx
    python scripts/backfill_verify_links.py --db path  # 指定数据库
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from src.models.database import Database
from src.models.account import AccountModel
from src.services.hotmail_inbox import read_hotmail_accounts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="数据库路径（默认用内置路径）")
    ap.add_argument("--xlsx", default=os.path.join(BASE, "hotmail.xlsx"), help="hotmail.xlsx 路径")
    args = ap.parse_args()

    haccs = read_hotmail_accounts(args.xlsx)
    print(f"xlsx 解析到 {len(haccs)} 行")

    db = Database(args.db)
    account = AccountModel(db)

    filled, skipped = 0, []
    for hacc in haccs:
        n = account.backfill_email_verify_link(hacc.email, hacc.link)
        if n:
            filled += n
            print(f"  ✓ {hacc.email}")
        else:
            skipped.append(hacc.email)

    print(f"\n回填 {filled} 个账号认证链接（xlsx {len(haccs)} 行，跳过 {len(skipped)} 个：账号不存在/已有链接/链接为空）")
    for email in skipped:
        print(f"  - {email}")


if __name__ == "__main__":
    main()
