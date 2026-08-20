#!/usr/bin/env python3
"""把 export_card_pool.py 导出的迁移包合并进目标机器的库。

用法（在迁移包目录里执行）：
    python3 import_card_pool.py --db /path/to/data/openrouter_auto.db
    python3 import_card_pool.py --db ... --dry-run     # 只报告，不落库

只依赖标准库，目标机器不装依赖也能跑。

合并而非覆盖：
  - 卡组按 name 匹配，group_id 自动重映射（两台机器的自增 id 对不上）
  - 卡片按 (card_number, group_id) 去重，已存在的不动
  - 状态表按主键 upsert，失败计数取较大值、冷却时间取较晚、终态不被空值覆盖
重复执行安全。
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

TABLES = [
    "card_groups",
    "card_pool",
    "valid_cards",
    "card_payment_state",
    "card_platform_state",
]


def load_staging(sql_path):
    """把 cards.sql 灌进一个临时库，后面所有比对都对着它做。

    自己解析 SQL 不如让 sqlite 解析——它本来就是 sqlite 生成的。
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cardpool-staging-")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    with open(sql_path, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn, path


def table_exists(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def ensure_tables(dst, staging):
    """目标库缺表就照搬源结构建出来（含索引）。"""
    created = []
    for t in TABLES:
        if table_exists(dst, t):
            continue
        row = staging.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if not row:
            continue
        dst.execute(row[0])
        for (isql,) in staging.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL",
            (t,),
        ):
            try:
                dst.execute(isql)
            except sqlite3.OperationalError:
                pass
        created.append(t)
    return created


def cols_of(conn, t):
    return [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]


def shared_cols(dst, staging, t, drop_pk=("id",)):
    """两边列的交集，按目标库顺序。自增主键排除掉，让目标库自己发号。"""
    d = cols_of(dst, t)
    s = set(cols_of(staging, t))
    return [c for c in d if c in s and c not in drop_pk]


def merge_groups(dst, staging, dry):
    """返回 源 group_id -> 目标 group_id 的映射。"""
    mapping = {}
    existing = {
        name: gid for gid, name in dst.execute("SELECT id, name FROM card_groups")
    }
    cols = shared_cols(dst, staging, "card_groups")
    reused = created = 0
    for row in staging.execute("SELECT id, name FROM card_groups ORDER BY id"):
        src_id, name = row
        if name in existing:
            mapping[src_id] = existing[name]
            reused += 1
            continue
        if dry:
            # 干跑时没有真实 id，用负数占位，后面只用来计数
            mapping[src_id] = -src_id
            created += 1
            continue
        vals = staging.execute(
            f"SELECT {', '.join(cols)} FROM card_groups WHERE id=?", (src_id,)
        ).fetchone()
        cur = dst.execute(
            f"INSERT INTO card_groups ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
        mapping[src_id] = cur.lastrowid
        existing[name] = cur.lastrowid
        created += 1
    return mapping, reused, created


def merge_pool(dst, staging, mapping, dry):
    cols = shared_cols(dst, staging, "card_pool")
    have = {
        (n, g) for n, g in dst.execute("SELECT card_number, group_id FROM card_pool")
    }
    gi = cols.index("group_id")
    ni = cols.index("card_number")
    ins = skip = 0
    rows = []
    for r in staging.execute(f"SELECT {', '.join(cols)} FROM card_pool"):
        r = list(r)
        r[gi] = mapping.get(r[gi], r[gi])
        if (r[ni], r[gi]) in have:
            skip += 1
            continue
        have.add((r[ni], r[gi]))
        rows.append(r)
        ins += 1
    if rows and not dry:
        dst.executemany(
            f"INSERT OR IGNORE INTO card_pool ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            rows,
        )
    return ins, skip


def merge_valid(dst, staging, mapping, dry):
    cols = shared_cols(dst, staging, "valid_cards")
    key = ("card_number", "source_type", "platform")
    if not all(k in cols for k in key):
        return 0, 0
    have = {
        t for t in dst.execute(f"SELECT {', '.join(key)} FROM valid_cards")
    }
    idx = [cols.index(k) for k in key]
    sgi = cols.index("source_group_id") if "source_group_id" in cols else None
    ins = skip = 0
    rows = []
    for r in staging.execute(f"SELECT {', '.join(cols)} FROM valid_cards"):
        r = list(r)
        if sgi is not None and r[sgi] is not None:
            r[sgi] = mapping.get(r[sgi], r[sgi])
        k = tuple(r[i] for i in idx)
        if k in have:
            skip += 1
            continue
        have.add(k)
        rows.append(r)
        ins += 1
    if rows and not dry:
        dst.executemany(
            f"INSERT OR IGNORE INTO valid_cards ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            rows,
        )
    return ins, skip


def merge_payment_state(dst, staging, dry):
    """fail_streak 取较大、tds_until 取较晚——两边都是"这张卡有多脏"的证据，取严的那个。"""
    cols = shared_cols(dst, staging, "card_payment_state", drop_pk=())
    have = {
        (n, p): dict(zip(cols, row))
        for row in dst.execute(f"SELECT {', '.join(cols)} FROM card_payment_state")
        for n, p in [(row[cols.index("card_number")], row[cols.index("platform")])]
    }
    ins = upd = same = 0
    for row in staging.execute(f"SELECT {', '.join(cols)} FROM card_payment_state"):
        src = dict(zip(cols, row))
        k = (src["card_number"], src["platform"])
        cur = have.get(k)
        if cur is None:
            ins += 1
            if not dry:
                dst.execute(
                    f"INSERT OR IGNORE INTO card_payment_state ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [src[c] for c in cols],
                )
            continue
        merged = dict(cur)
        if "fail_streak" in cols:
            merged["fail_streak"] = max(cur.get("fail_streak") or 0,
                                        src.get("fail_streak") or 0)
        for c in ("tds_until", "last_fail_at", "updated_at"):
            if c in cols and (src.get(c) or "") > (cur.get(c) or ""):
                merged[c] = src[c]
                if c == "tds_until" and src.get("tds_reason"):
                    merged["tds_reason"] = src["tds_reason"]
        if merged == cur:
            same += 1
            continue
        upd += 1
        if not dry:
            setc = [c for c in cols if c not in ("card_number", "platform")]
            dst.execute(
                f"UPDATE card_payment_state SET {', '.join(c + '=?' for c in setc)} "
                "WHERE card_number=? AND platform=?",
                [merged[c] for c in setc] + list(k),
            )
    return ins, upd, same


def merge_platform_state(dst, staging, dry):
    """invalid / paid 是终态，不能被空 status 盖掉。"""
    cols = shared_cols(dst, staging, "card_platform_state", drop_pk=())
    have = {}
    for row in dst.execute(f"SELECT {', '.join(cols)} FROM card_platform_state"):
        d = dict(zip(cols, row))
        have[(d["card_number"], d["platform"])] = d
    ins = upd = same = 0
    for row in staging.execute(f"SELECT {', '.join(cols)} FROM card_platform_state"):
        src = dict(zip(cols, row))
        k = (src["card_number"], src["platform"])
        cur = have.get(k)
        if cur is None:
            ins += 1
            if not dry:
                dst.execute(
                    f"INSERT OR IGNORE INTO card_platform_state ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [src[c] for c in cols],
                )
            continue
        if (src.get("status") or "") and not (cur.get("status") or ""):
            upd += 1
            if not dry:
                dst.execute(
                    "UPDATE card_platform_state SET status=?, updated_at=? "
                    "WHERE card_number=? AND platform=?",
                    [src["status"], src.get("updated_at"), *k],
                )
        else:
            same += 1
    return ins, upd, same


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="把卡池迁移包合并进目标库")
    ap.add_argument("--db", required=True, help="目标库路径，如 data/openrouter_auto.db")
    ap.add_argument("--sql", default=os.path.join(here, "cards.sql"),
                    help="迁移包里的 cards.sql，默认取本脚本同目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--no-backup", action="store_true", help="跳过导入前备份")
    args = ap.parse_args()

    if not os.path.exists(args.sql):
        sys.exit(f"找不到 cards.sql：{args.sql}")
    if not os.path.exists(args.db):
        sys.exit(f"目标库不存在：{args.db}（先跑一次目标项目让它建库）")

    staging, staging_path = load_staging(args.sql)
    try:
        if not args.dry_run and not args.no_backup:
            bak = f"{args.db}.bak-preimport-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy2(args.db, bak)
            print(f"已备份目标库 -> {bak}")

        dst = sqlite3.connect(args.db)
        dst.execute("PRAGMA foreign_keys=OFF")
        try:
            created = ensure_tables(dst, staging)
            if created:
                print(f"目标库缺表，已建：{', '.join(created)}")

            mapping, reused, new_g = merge_groups(dst, staging, args.dry_run)
            print(f"card_groups          复用 {reused}，新建 {new_g}")

            ins, skip = merge_pool(dst, staging, mapping, args.dry_run)
            print(f"card_pool            新增 {ins:,}，已存在跳过 {skip:,}")

            ins, skip = merge_valid(dst, staging, mapping, args.dry_run)
            print(f"valid_cards          新增 {ins:,}，已存在跳过 {skip:,}")

            i, u, s = merge_payment_state(dst, staging, args.dry_run)
            print(f"card_payment_state   新增 {i:,}，更新 {u:,}，无变化 {s:,}")

            i, u, s = merge_platform_state(dst, staging, args.dry_run)
            print(f"card_platform_state  新增 {i:,}，更新 {u:,}，无变化 {s:,}")

            if args.dry_run:
                dst.rollback()
                print("\n[dry-run] 未写入任何数据")
            else:
                dst.commit()
                print("\n导入完成")
        finally:
            dst.close()
    finally:
        staging.close()
        os.path.exists(staging_path) and os.remove(staging_path)


if __name__ == "__main__":
    main()
