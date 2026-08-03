"""一次性数据修复：把被误标 'failed'（实际可用）的账号批量改回 'registered'。

背景：accounts.identity_status='failed' 仅由 GitHub 注册失败分支写入
（src/web/app.py _subscribe_one_account）；充值流程从不写它。这些账号目前实际都可用，
账号列表显示"失败"是错误的。本脚本把它们统一改回 'registered'，使其在列表正常显示、
并被充值/订阅轮转正常纳入。

幂等：可重复运行（无 failed 时改 0 行）。
用法：
  python3 scripts/fix_failed_accounts_status.py          # dry-run 预览，不改动
  python3 scripts/fix_failed_accounts_status.py --apply  # 落库
执行前建议备份 data/*.db。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import Database
from src.models.account import AccountModel


def _status_counts(db):
    rows = db.fetchall(
        "SELECT COALESCE(identity_status,'') AS status, COUNT(*) AS c "
        "FROM accounts GROUP BY identity_status ORDER BY c DESC"
    )
    return {dict(r)['status']: dict(r)['c'] for r in rows}


def main(apply):
    db = Database()
    print(f"DB: {db.db_path}\n")

    before = _status_counts(db)
    failed = before.get('failed', 0)
    print(f"当前状态分布: {before}")
    print(f"待修复 identity_status='failed': {failed} 个\n")

    if failed == 0:
        print("无 failed 账号，无需修复。")
        return 0

    if not apply:
        print("[dry-run] 未改动。确认无误后加 --apply 执行。")
        return 0

    acct = AccountModel(db)
    n = acct.reset_failed_to_registered()
    after = _status_counts(db)
    print(f"[apply] 已把 {n} 个 failed 账号改回 registered。")
    print(f"修复后状态分布: {after}")
    return 0


if __name__ == '__main__':
    sys.exit(main('--apply' in sys.argv) or 0)
