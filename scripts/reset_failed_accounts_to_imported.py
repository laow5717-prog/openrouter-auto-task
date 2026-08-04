"""把注册失败的账号退回 'imported'，让下一次每日任务重新为它们注册 GitHub。

## 与 fix_failed_accounts_status.py 的区别（**别用错**）

那个脚本把 failed 改成 **registered**，处理的是一批「其实注册成功了、只是被误标失败」
的历史账号——它宣称账号已经有 GitHub 了。

本脚本改成 **imported**，处理的是「真的没注册成功、想让它重来一次」。
两者不能互换：`run_daily_pipeline._registerable_imported()` 只认 `imported`，
改成 registered 的账号永远不会进补号流程；而它们又没有 GitHub 密码，
登录充值也会失败——等于既不注册也不能用，静静地卡在列表里。

## 重置后能被领走的前提

`_registerable_imported()` 要求两件事，本脚本会逐个核对并跳过不满足的：

  1. identity_status == 'imported'
  2. `_hotmail_for_account` 取得到收码数据 —— accounts.email_verify_link 非空，
     或该邮箱出现在 hotmail.xlsx 里。两者都没有的话，重置了也领不走
     （注册流程拿不到验证码），只会让列表多几行看着能用其实不能用的账号。

## 用法

    python3 scripts/reset_failed_accounts_to_imported.py           # 预览，不改动
    python3 scripts/reset_failed_accounts_to_imported.py --apply   # 落库

幂等：无 failed 时改 0 行。执行前建议备份 data/*.db。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import Database

# 视为「注册没成」的身份状态。三者都由 _register_one_account 写入：
#   failed    注册流程未走完
#   pending   碰上 Arkose 人机验证，主动跳过
# suspended 刻意不在其中——那是「注册出来就被 GitHub 挂起」，同一个邮箱重注册大概率
# 还是同样下场，退回 imported 只会让它每轮都白跑一次。要重试请显式加 --include-suspended。
RESETTABLE = ('failed', 'pending')


def _counts(db):
    rows = db.fetchall(
        "SELECT COALESCE(identity_status,'') AS s, COUNT(*) AS c "
        "FROM accounts GROUP BY 1 ORDER BY c DESC"
    )
    return {dict(r)['s']: dict(r)['c'] for r in rows}


def _hotmail_emails():
    """hotmail.xlsx 里的邮箱集合。文件不存在时返回空集（不是错误——多数账号
    的收码链接在 DB 里，xlsx 只是第二来源）。"""
    try:
        from src.services.hotmail_inbox import read_hotmail_accounts
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return {a.email for a in read_hotmail_accounts(os.path.join(base, 'hotmail.xlsx'))}
    except Exception:
        return set()


def main(apply, statuses):
    db = Database()
    print(f"DB: {db.db_path}\n")
    print(f"当前状态分布: {_counts(db)}")

    ph = ','.join('?' * len(statuses))
    rows = [dict(r) for r in db.fetchall(
        f"SELECT id, email, identity_status, email_verify_link "
        f"FROM accounts WHERE COALESCE(identity_status,'') IN ({ph}) ORDER BY id",
        statuses,
    )]
    print(f"命中 {statuses} 的账号: {len(rows)} 个")
    if not rows:
        print("无需处理。")
        return

    xlsx = _hotmail_emails()
    ready, skipped = [], []
    for r in rows:
        has_link = bool((r.get('email_verify_link') or '').strip())
        (ready if (has_link or r['email'] in xlsx) else skipped).append(r)

    print(f"  可重置（有收码数据）: {len(ready)}")
    print(f"  跳过（无收码链接且不在 hotmail.xlsx，重置了也领不走）: {len(skipped)}")
    for r in skipped[:10]:
        print(f"     - {r['email']}")
    if len(skipped) > 10:
        print(f"     ... 另有 {len(skipped) - 10} 个")

    if not apply:
        print("\n[演练] 未写入。加 --apply 实际执行。")
        return
    if not ready:
        print("\n没有可重置的账号。")
        return

    with db.transaction() as conn:
        for r in ready:
            conn.execute(
                "UPDATE accounts SET identity_status='imported', "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (r['id'],),
            )
    print(f"\n已将 {len(ready)} 个账号重置为 imported")
    print(f"改后状态分布: {_counts(db)}")
    db.close()


if __name__ == '__main__':
    args = sys.argv[1:]
    st = list(RESETTABLE)
    if '--include-suspended' in args:
        st.append('suspended')
    main('--apply' in args, st)
