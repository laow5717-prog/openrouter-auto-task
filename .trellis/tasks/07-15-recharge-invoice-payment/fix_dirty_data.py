"""一次性脏数据订正（S10 / D9 / R9）——仅运行一次，不接入流水线。

背景：2026-07-15 18:28 每日任务实跑因 Top-up 假成功 bug，把三个余额为 $0 的账号
误记为「充值成功」，并把其中两张拒付卡误插入 valid_cards。本脚本：
  - recharge_logs id 70/71/72：success → failed（注明订正原因）
  - valid_cards id 8(0217)/9(7772)：删除（4673 的 id=3 为 07-14 旧记录，保留不动）

用法：先 --dry-run 核对，再 --apply 落库。执行前请确保已备份 data/*.db。
"""
import sqlite3
import sys
import glob

LOG_IDS = (70, 71, 72)
VALID_CARD_IDS = (8, 9)
REASON = "订正：Top-up 拒付/未到账，余额$0（2026-07-15误记）"


def main(apply):
    db = sorted(glob.glob("data/*.db"))[0]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    print(f"DB: {db}\n")

    print("== 待改 recharge_logs ==")
    for r in conn.execute(
        f"SELECT id,email,substr(card_display,-4) c4,amount,status,error FROM recharge_logs WHERE id IN {LOG_IDS}"
    ):
        print(dict(r))

    print("\n== 待删 valid_cards ==")
    for r in conn.execute(
        f"SELECT id,substr(card_number,-4) c4,source_type,source_email,validated_at FROM valid_cards WHERE id IN {VALID_CARD_IDS}"
    ):
        print(dict(r))

    if not apply:
        print("\n[dry-run] 未改动。确认无误后加 --apply 执行。")
        return

    with conn:
        cur = conn.execute(
            f"UPDATE recharge_logs SET status='failed', error=? WHERE id IN {LOG_IDS} AND status='success'",
            (REASON,),
        )
        upd = cur.rowcount
        cur = conn.execute(f"DELETE FROM valid_cards WHERE id IN {VALID_CARD_IDS}")
        deleted = cur.rowcount

    print(f"\n[applied] recharge_logs 更新 {upd} 行；valid_cards 删除 {deleted} 行。")

    print("\n== 订正后核对 ==")
    for r in conn.execute(
        f"SELECT id,email,status,error FROM recharge_logs WHERE id IN {LOG_IDS}"
    ):
        print(dict(r))
    remain = conn.execute(
        f"SELECT COUNT(*) c FROM valid_cards WHERE id IN {VALID_CARD_IDS}"
    ).fetchone()["c"]
    id3 = conn.execute("SELECT COUNT(*) c FROM valid_cards WHERE id=3").fetchone()["c"]
    print(f"valid_cards id 8/9 残留 {remain} 行（应为 0）；id 3 存在 {id3} 行（应为 1）")
    conn.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
