#!/usr/bin/env python3
"""AdsPower 接入的端到端探针：建/复用环境 → 接管 → 验出口 IP 与 Stripe 可达 → 关闭。

不碰任何账号业务，只验证「浏览器怎么起来」这一层，因此可以随时对着任意 email 跑。

用法:
    python3 scripts/probe_adspower.py --email probe@example.com
    python3 scripts/probe_adspower.py --email probe@example.com --keep   # 跑完不删环境
    python3 scripts/probe_adspower.py --cleanup                          # 删掉本项目建的所有环境
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import cfg                                    # noqa: E402
from src.models.database import Database                      # noqa: E402
from src.models.adspower_profile import AdsPowerProfileModel   # noqa: E402
from src.services.adspower import AdsPowerClient               # noqa: E402
from src.browser.adspower_driver import (                      # noqa: E402
    AdsPowerProfilePool, create_driver_adspower,
)
from src.browser.driver import close_driver                    # noqa: E402

REMARK_MARK = "openrouter-auto-task"


def _client():
    key = cfg.adspower.api_key or os.environ.get("ADSPOWER_API_KEY", "")
    if not key:
        raise SystemExit("未配置 AdsPower API Key（config.yaml 的 adspower.api_key "
                         "或环境变量 ADSPOWER_API_KEY）")
    return AdsPowerClient(cfg.adspower.base_url, key)


def cmd_cleanup(client):
    rows = client.list_all_profiles()
    mine = [r for r in rows if REMARK_MARK in (r.get("remark") or "")]
    print(f"共 {len(rows)} 个环境，其中本项目创建的 {len(mine)} 个")
    if not mine:
        return
    ids = [r["profile_id"] for r in mine]
    # 运行中的环境删不掉，先逐个关闭（AdsPower 的 stop 是异步的，留一点时间）
    for pid in ids:
        try:
            client.stop_profile(pid)
        except Exception:
            pass
    time.sleep(2)
    client.delete_profiles(ids)
    db = Database()
    AdsPowerProfileModel(db).delete_by_emails(
        [(r.get("remark") or "").split("/")[-1].strip() for r in mine])
    print(f"已删除 {len(mine)} 个环境并清理本地映射")


def cmd_probe(client, email, keep):
    db = Database()
    pool = AdsPowerProfilePool(client, AdsPowerProfileModel(db),
                               group_id=cfg.adspower.group_id,
                               reclaim_batch=cfg.adspower.reclaim_batch,
                               log=print)
    session = create_driver_adspower(email, pool, client)
    try:
        session.get("https://api.ipify.org?format=json")
        ip_text = session.page.inner_text("body")[:120]
        print(f"出口 IP: {ip_text}")

        session.get("https://checkout.stripe.com/")
        print(f"Stripe 可达: {session.page.url[:80]}")

        # hCaptcha token 注入的前提是 add_init_script 能前置生效——这是付款链路最脆弱的
        # 一环（见 memory: stripe-hcaptcha-blocker），CDP 接管后必须实测确认而非推断。
        session.context.add_init_script("window.__probe_init_script__ = 'ok';")
        session.get("https://example.com/")
        injected = session.page.evaluate("() => window.__probe_init_script__ || 'MISSING'")
        print(f"add_init_script 前置注入: {injected}")
    finally:
        close_driver(session)
        print("已关闭环境")

    # AdsPower 的 stop 是异步的，状态大约 1 秒后才翻成 Inactive，立刻查会误判成没关掉。
    row = pool.profiles.get_by_email(email)
    active = True
    if row:
        for _ in range(8):
            time.sleep(1)
            active, _ws = client.profile_active(row["profile_id"])
            if not active:
                break
    print(f"关闭后环境状态 Active={active}（应为 False）")

    if not keep:
        pool.release(email)
        print("已删除探针环境")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="probe@example.com")
    ap.add_argument("--keep", action="store_true", help="跑完保留环境（用于验证复用）")
    ap.add_argument("--cleanup", action="store_true", help="删除本项目创建的所有环境")
    args = ap.parse_args()

    client = _client()
    if args.cleanup:
        cmd_cleanup(client)
        return
    cmd_probe(client, args.email, args.keep)


if __name__ == "__main__":
    main()
