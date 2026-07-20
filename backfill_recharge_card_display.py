#!/usr/bin/env python3
"""把 recharge_logs.card_display 里的脱敏串还原成完整卡号。

背景：充值记账时页面上只能读到卡号后四位，app.py 的 _match_full_card 负责还原成
完整卡号。它此前只在「该账号已绑定的卡」里找，找不到就写入 '•••• 1234' 脱敏串。
后果有两个：
  1. 充值记录界面显示的是脱敏卡号；
  2. 更要命的是所有按完整卡号等值匹配的判定（success_count_since / last_success_at，
     即 24h 内成功 2 次的选卡冷却）对这些记录永远返回 0，静默失效。

写入侧已改为「账号绑定卡 → 全局卡池按末四位反查 → 裸后四位」三级回退，本脚本
补齐存量数据。反查不到时退而去掉 '•••• ' 前缀，至少让末四位口径的统计能命中。

刻意做成独立脚本而非数据库迁移：迁移失败会导致服务起不来，为一次补数据操作
冒这个险不值得。默认只预演不写入。

用法：
    python3 backfill_recharge_card_display.py            # 预演，只打印将要改动的内容
    python3 backfill_recharge_card_display.py --apply    # 实际写入
    python3 backfill_recharge_card_display.py --db PATH  # 指定数据库（默认用项目 data/ 下的库）
"""

import sys

from src.models.database import Database
from src.models.card_pool import CardPoolModel

MASK_PREFIX = '•••• '


def main():
    apply = '--apply' in sys.argv

    db_path = None
    if '--db' in sys.argv:
        db_path = sys.argv[sys.argv.index('--db') + 1]

    db = Database(db_path)
    print(f"数据库: {db.db_path}")

    rows = db.fetchall(
        "SELECT DISTINCT card_display FROM recharge_logs WHERE card_display LIKE ?",
        (f'{MASK_PREFIX}%',),
    )
    masked = [r['card_display'] for r in rows]
    print(f"脱敏的 card_display 取值: {len(masked)} 个")
    if not masked:
        print("无需回填")
        return

    pool = CardPoolModel(db)
    restored = 0
    stripped = 0
    for display in masked:
        last4 = display[len(MASK_PREFIX):].strip()
        full = pool.find_number_by_last4(last4)
        # 反查不到就只去掉脱敏前缀：完整卡号无从得知，但裸后四位不会再污染
        # 按 card_display 等值匹配的判定，末四位口径的统计也能正常命中。
        new_value = full or last4

        cnt = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM recharge_logs WHERE card_display=?",
            (display,),
        )['cnt']
        tag = '卡池反查到完整卡号' if full else '卡池无匹配，仅去除脱敏前缀'
        print(f"  {display} → {new_value}  ({cnt} 条, {tag})")

        if apply:
            db.execute(
                "UPDATE recharge_logs SET card_display=? WHERE card_display=?",
                (new_value, display),
            )
        if full:
            restored += cnt
        else:
            stripped += cnt

    print(f"\n还原为完整卡号: {restored} 条；仅去除前缀: {stripped} 条")
    if not apply:
        print("以上为预演结果，加 --apply 才会写入")


if __name__ == '__main__':
    main()
