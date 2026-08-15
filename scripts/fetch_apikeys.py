#!/usr/bin/env python3
"""补抓账号的平台 API key，落库 platform_accounts.apikey。

用途是**补漏**：正常情况下 key 由充值流程顺手抓走（registration._grab_apikey），
这个脚本处理那些没抓成的——2026-08-16 就有两个刚充成功的账号 apikey 列是空的。

浏览器怎么起（两条路，自动选）：
  1. **AdsPower 环境**（默认，前提是设置里启用了 AdsPower 且该账号有环境映射）。
     现在的账号跑在指纹浏览器里，登录态在 AdsPower 环境的 Cookie 里，不在本地
     profile —— 这一条不走 AdsPower 的话，脚本对它们**一律抓不到**。
  2. **本地持久化 profile**（data/profiles/<email>/）。老账号与没有环境映射的账号
     走这条，行为与改造前一致。
  `--local` 可强制全部走本地。

AdsPower 模式下**只接管已有映射的环境**：没有映射就意味着没有登录态，为它新建一个
环境只会白占一格配额（上限 12），还抓不到东西——那种账号自动回落本地 profile。

wid 优先取 platform_accounts.tenant_id（充值流程写好的），取不到才开 /auth 等跳转。

抓取本身调 adapter.fetch_apikey，不在这里抄一份正则：判据只有一处才不会漂移
（infron 那种「只有脱敏串、拿不到明文」的平台也由适配器如实返回 None）。

⚠️ 别在任务跑着的时候用：它会占 AdsPower 环境配额，和 worker 抢。

用法：
    python scripts/fetch_apikeys.py                  # 缺 key 且有余额的账号
    python scripts/fetch_apikeys.py --email a@b.com  # 只跑单个账号
    python scripts/fetch_apikeys.py --all            # 所有账号（仍跳过已有 key 的）
    python scripts/fetch_apikeys.py --refetch        # 连已有 key 的也重抓
    python scripts/fetch_apikeys.py --local          # 强制走本地 profile
    python scripts/fetch_apikeys.py --dry            # 只列目标，不开浏览器
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import Database
from src.models.platform_account import PlatformAccountModel
from src.models.settings import SettingsModel
from src.models.adspower_profile import AdsPowerProfileModel
from src.browser.driver import create_driver, close_driver
from src.config import cfg
import src.platforms as platforms

WID_RE = re.compile(r"wrk_[A-Za-z0-9]+")


def mask(key):
    return f"{key[:8]}...{key[-4:]}" if key and len(key) > 14 else "***"


def wait_workspace(session, timeout=25):
    """轮询等 /auth 跳转到 /workspace/<wid>，返回 wid 或 None。"""
    start = time.time()
    while time.time() - start < timeout:
        m = WID_RE.search(session.current_url or "")
        if m:
            return m.group(0)
        time.sleep(1)
    return None


def make_adspower(db, models_settings):
    """按 UI/配置里的 AdsPower 设置建客户端与环境池；未启用返回 (None, None)。"""
    s = models_settings.adspower_effective(cfg.adspower)
    if not s.get('enabled'):
        return None, None
    from src.services.adspower import AdsPowerClient
    from src.browser.adspower_driver import AdsPowerProfilePool
    client = AdsPowerClient(s['base_url'], s['api_key'])
    pool = AdsPowerProfilePool(client, AdsPowerProfileModel(db),
                               group_id=cfg.adspower.group_id, log=print)
    return client, pool


def open_session(email, client, pool, has_profile):
    """开一个登录态可用的会话。返回 (session, 用了哪条路)。"""
    if client is not None and has_profile:
        from src.browser.adspower_driver import create_driver_adspower
        return create_driver_adspower(email, pool, client), "AdsPower"
    return create_driver(headless=False, profile_id=email), "本地 profile"


def fetch_one(email, adapter, wid_hint, client, pool, has_profile):
    """单账号：返回 (ok, detail, key)。"""
    try:
        session, how = open_session(email, client, pool, has_profile)
    except Exception as e:
        return False, f"浏览器起不来: {type(e).__name__}: {str(e)[:100]}", None
    try:
        wid = wid_hint
        if not wid:
            session.get("https://opencode.ai/auth")
            wid = wait_workspace(session)
        if not wid:
            return False, (f"[{how}] 登录态失效/未跳转 workspace"
                           f"（停在 {str(session.current_url)[:60]}）"), None
        key = adapter.fetch_apikey(session, wid)
        if key:
            return True, f"[{how}] wid={wid}", key
        return False, f"[{how}] keys 页没抓到 sk-（wid={wid}）", None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}", None
    finally:
        close_driver(session)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=None, help="只跑单个账号")
    ap.add_argument("--all", action="store_true", help="所有账号（默认只跑有余额的）")
    ap.add_argument("--refetch", action="store_true", help="连已有 key 的账号也重抓")
    ap.add_argument("--local", action="store_true", help="强制走本地 profile，不用 AdsPower")
    ap.add_argument("--dry", action="store_true", help="只列出目标账号，不开浏览器")
    ap.add_argument("--db", default=None, help="数据库路径（默认用内置路径）")
    ap.add_argument("--platform", default="opencode", help="目标平台 slug")
    args = ap.parse_args()

    db = Database(args.db)
    platform_account = PlatformAccountModel(db)
    adapter = platforms.get(args.platform)

    rows = db.fetchall(
        "SELECT email, tenant_id, COALESCE(apikey,'') AS apikey, "
        "       COALESCE(credits_balance,0) AS bal "
        "FROM platform_accounts WHERE platform=? ORDER BY id", (args.platform,))
    info = {r["email"]: dict(r) for r in rows}

    if args.email:
        targets = [args.email]
    elif args.all:
        targets = list(info)
    else:
        targets = [e for e, r in info.items() if r["bal"] > 0]

    if not args.refetch:
        skipped = [e for e in targets if (info.get(e) or {}).get("apikey")]
        targets = [e for e in targets if not (info.get(e) or {}).get("apikey")]
        if skipped:
            print(f"跳过 {len(skipped)} 个已有 key 的账号（--refetch 可强制重抓）")

    print(f"目标账号 {len(targets)} 个: {', '.join(targets) or '（无）'}")
    if args.dry or not targets:
        return

    client, pool = (None, None) if args.local else make_adspower(db, SettingsModel(db))
    have_profile = ({r["email"] for r in AdsPowerProfileModel(db).get_all()}
                    if client is not None else set())
    if client is not None:
        print(f"AdsPower 已启用，其中 {len(set(targets) & have_profile)} 个账号有现成环境；"
              f"其余走本地 profile")

    ok_list, fail_list = [], []
    for i, email in enumerate(targets, 1):
        print(f"\n[{i}/{len(targets)}] {email} ...", flush=True)
        ok, detail, key = fetch_one(email, adapter,
                                    (info.get(email) or {}).get("tenant_id"),
                                    client, pool, email in have_profile)
        if ok:
            platform_account.update_apikey(args.platform, email, key)
            ok_list.append(email)
            print(f"  ✓ 落库 apikey={mask(key)}  ({detail})", flush=True)
        else:
            fail_list.append((email, detail))
            print(f"  ✗ {detail}", flush=True)

    print(f"\n{'=' * 50}")
    print(f"成功 {len(ok_list)} / 失败 {len(fail_list)}")
    for email, detail in fail_list:
        print(f"  ✗ {email}: {detail}")


if __name__ == "__main__":
    main()
