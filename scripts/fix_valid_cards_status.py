"""一次性数据修复：让 valid_cards 成员（有效卡）恢复为有效——不再残留 invalid/expired。

背景：曾支付成功过的有效卡若在订阅流程再次被拒，历史代码会无条件 mark_invalid，
把它误标为 invalid，导致其落入无效桶、不再被选。修复后代码已加不变式
（card_pool.mark_invalid_by_number 跳过 valid_cards 成员），此脚本清理存量脏数据：

  UPDATE card_pool SET status='' WHERE card_number IN (valid_cards)
                                   AND COALESCE(status,'') IN ('invalid','expired')

清为空状态而非某具体值：valid_cards 可能来自 bind 或 payment，「有效」由 is_valid
派生渲染，无需具体 status。幂等：可重复运行。

用法：先 `--dry-run`（默认）核对，再 `--apply` 落库。执行前建议备份 data/*.db。
"""
import sqlite3
import sys
import glob

STALE = ('invalid', 'expired')


def _bucket_counts(conn, group_id):
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN COALESCE(status,'') IN ('invalid','expired') THEN 1 ELSE 0 END) invalid, "
        "  SUM(CASE WHEN COALESCE(status,'') NOT IN ('invalid','expired') "
        "           AND card_number IN (SELECT card_number FROM valid_cards) THEN 1 ELSE 0 END) valid, "
        "  COUNT(*) total "
        "FROM card_pool WHERE group_id=?",
        (group_id,),
    ).fetchone()
    return dict(row)


def main(apply):
    dbs = sorted(glob.glob("data/*.db"))
    if not dbs:
        print("未找到 data/*.db")
        return 1
    db = dbs[0]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    print(f"DB: {db}\n")

    stale = conn.execute(
        "SELECT COUNT(*) c FROM card_pool "
        "WHERE card_number IN (SELECT card_number FROM valid_cards) "
        "AND COALESCE(status,'') IN ('invalid','expired')"
    ).fetchone()['c']
    print(f"待修复（valid_cards 成员却标 invalid/expired）: {stale} 张\n")

    # 「已验证卡」分组修复前桶计数（若存在）
    grp = conn.execute("SELECT id,name FROM card_groups WHERE name='已验证卡'").fetchone()
    if grp:
        print(f"分组「已验证卡」(id={grp['id']}) 修复前: {_bucket_counts(conn, grp['id'])}")

    if not apply:
        print("\n[dry-run] 未改动。确认无误后加 --apply 执行。")
        return 0

    cur = conn.execute(
        "UPDATE card_pool SET status='' "
        "WHERE card_number IN (SELECT card_number FROM valid_cards) "
        "AND COALESCE(status,'') IN ('invalid','expired')"
    )
    conn.commit()
    print(f"\n[apply] 已修复 {cur.rowcount} 行。")
    if grp:
        print(f"分组「已验证卡」(id={grp['id']}) 修复后: {_bucket_counts(conn, grp['id'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main('--apply' in sys.argv) or 0)
