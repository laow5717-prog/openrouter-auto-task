#!/usr/bin/env python3
"""把历史上已成功绑定的卡，在卡池里回填为 bound 状态。

背景：一卡一账号此前只由 card_bindings 实时派生，卡池自身不留痕。新增
card_pool.status='bound' 后，只有此后新绑的卡会被标记，存量记录仍是空状态——
不影响选卡正确性（get_successfully_bound_card_numbers 那层过滤仍在），
但卡池界面看不出这些卡已被用掉。本脚本补齐这段历史。

刻意做成独立脚本而非数据库迁移：迁移失败会导致服务起不来，为一次补数据操作
冒这个险不值得。默认只预演不写入。

用法：
    python3 backfill_bound_cards.py            # 预演，只打印将要改动的内容
    python3 backfill_bound_cards.py --apply    # 实际写入
    python3 backfill_bound_cards.py --db PATH  # 指定数据库（默认用项目 data/ 下的库）
"""

import sys

from src.models.database import Database
from src.models.card_binding import CardBindingModel
from src.utils import CARD_STATUS_BOUND, CARD_STATUS_PAID, CARD_STATUS_UNUSABLE


def main():
    apply = '--apply' in sys.argv

    db_path = None
    if '--db' in sys.argv:
        db_path = sys.argv[sys.argv.index('--db') + 1]

    db = Database(db_path)
    print(f"数据库: {db.db_path}")

    bound_numbers = CardBindingModel(db).get_successfully_bound_card_numbers()
    print(f"card_bindings 中成功绑定的卡号: {len(bound_numbers)} 个")
    if not bound_numbers:
        print("无需回填")
        return

    # 只回填空状态的记录。以下一律保留，信息量都比「被绑过」更大：
    #   invalid/expired —— 卡不可用及其归因
    #   paid            —— 该卡真实付款成功过，是可用性的最强证据
    #   bound           —— 已回填过，无需重写
    # 与 CardPoolModel.mark_bound_by_number 的保留规则保持一致。
    keep = tuple(CARD_STATUS_UNUSABLE) + (CARD_STATUS_PAID, CARD_STATUS_BOUND)
    placeholders = ','.join('?' * len(bound_numbers))
    keep_ph = ','.join('?' * len(keep))
    rows = db.fetchall(
        f"""SELECT id, card_number, COALESCE(status,'') AS status, group_id
            FROM card_pool
            WHERE card_number IN ({placeholders})
              AND COALESCE(status,'') NOT IN ({keep_ph})""",
        list(bound_numbers) + list(keep),
    )

    kept = db.fetchall(
        f"""SELECT card_number, COALESCE(status,'') AS status FROM card_pool
            WHERE card_number IN ({placeholders})
              AND COALESCE(status,'') IN ({keep_ph})""",
        list(bound_numbers) + list(keep),
    )
    if kept:
        by_status = {}
        for r in kept:
            by_status.setdefault(r['status'], 0)
            by_status[r['status']] += 1
        detail = '、'.join(f"{k} {v} 张" for k, v in sorted(by_status.items()))
        print(f"保留原状态不改写: {detail}")

    if not rows:
        print("卡池中没有需要回填的记录（可能已回填过，或这些卡不在卡池里）")
        return

    print(f"\n将标记为 bound 的卡池记录: {len(rows)} 条")
    for r in rows[:20]:
        num = r['card_number']
        masked = f"{num[:4]}****{num[-4:]}" if len(num) >= 8 else num
        print(f"  id={r['id']} 分组={r['group_id']} 卡号={masked} 当前状态={r['status']!r}")
    if len(rows) > 20:
        print(f"  ... 另有 {len(rows) - 20} 条")

    # 顺带报告一致性：已绑但不在卡池的卡（多为卡池中被删除，或外部绑定）
    in_pool = {r['card_number'] for r in db.fetchall(
        f"SELECT card_number FROM card_pool WHERE card_number IN ({placeholders})",
        list(bound_numbers))}
    missing = bound_numbers - in_pool
    if missing:
        print(f"\n提示: {len(missing)} 张已绑卡不在卡池中（已删除或从未入池），跳过")

    if not apply:
        print("\n[预演] 未写入。确认无误后加 --apply 执行")
        return

    ids = [r['id'] for r in rows]
    id_ph = ','.join('?' * len(ids))
    db.execute(
        f"UPDATE card_pool SET status=? WHERE id IN ({id_ph})",
        [CARD_STATUS_BOUND] + ids,
    )
    print(f"\n已回填 {len(ids)} 条记录为 bound")


if __name__ == '__main__':
    main()
