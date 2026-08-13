#!/usr/bin/env python3
"""把已有 AdsPower 环境重新绑定到「代理管理」里的当前代理。

## 为什么需要它

代理管理里的条目被删掉重建后（换供应商、换套餐、批量重导），proxy_id 会整批变号。
AdsPower 不会把老环境一起删，而是把它们的代理**悄悄退回 no_proxy** —— 也就是直连
本机 IP。反关联就这样静默失效了：浏览器照常启动、账号照常登录，日志里一个字都没有，
只有出口 IP 变成了本机。2026-08-11 的现场就是 5 个环境全退成了 no_proxy。

重绑而不是删环境重建，是因为环境里的 GitHub 授权态很贵：删了要完整重登，还要过一次
「新设备」邮箱验证码。何况撞上创建次数日限额时，根本建不出新环境。

## 用法

    python3 scripts/rebind_adspower_proxies.py --dry-run   # 只看要改什么
    python3 scripts/rebind_adspower_proxies.py             # 只修直连/失效的
    python3 scripts/rebind_adspower_proxies.py --all       # 全部换一遍（换批代理后用这个）

环境必须处于已停止状态：运行中的环境改代理不会作用到当前会话，反而会让「日志说换了、
实际还在用旧出口」——脚本检测到运行中的环境会跳过并报出来。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import cfg                                     # noqa: E402
from src.models.database import Database                       # noqa: E402
from src.models.adspower_profile import AdsPowerProfileModel    # noqa: E402
from src.services.adspower import AdsPowerClient, AdsPowerError  # noqa: E402


def _proxy_state(client):
    """AdsPower 侧每个环境当前的代理形态：profile_id -> (soft, host:port)。

    用 v1/user/list 而不是本地映射：本地记的 proxy_id 在代理换批后就是个失效的数字，
    真相只在 AdsPower 那边。注意它回显的是**展开后的配置**（proxy_soft/host/port），
    不回显 proxy_id，所以判据只能是「有没有代理」，不能是「绑的是哪个 id」。
    """
    out = {}
    for p in client.list_all_profiles():
        conf = p.get("user_proxy_config") or {}
        soft = (conf.get("proxy_soft") or "").strip()
        host = conf.get("proxy_host") or ""
        port = conf.get("proxy_port") or ""
        out[p.get("user_id")] = (soft, f"{host}:{port}" if host else "")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改任何东西")
    ap.add_argument("--all", action="store_true",
                    help="所有环境都换新代理（默认只修直连或代理已失效的）")
    ap.add_argument("--db", default=None, help="数据库路径，默认取配置")
    args = ap.parse_args()

    db = Database(args.db) if args.db else Database()
    profiles = AdsPowerProfileModel(db)
    client = AdsPowerClient(cfg.adspower.base_url, cfg.adspower.api_key)

    mappings = profiles.get_all()
    if not mappings:
        print("本地没有任何环境映射，无事可做")
        return 0

    try:
        state = _proxy_state(client)
        pool = client.list_all_proxies()
    except AdsPowerError as e:
        print(f"读取 AdsPower 失败: {e}")
        return 1

    # 空闲代理 = 没有任何环境绑着它。一环境一代理是反关联的底线，绝不复用。
    free = [str(p["proxy_id"]) for p in pool
            if str(p.get("profile_count") or "0") == "0"]
    print(f"代理池 {len(pool)} 个，其中空闲 {len(free)} 个")

    plan = []
    for row in mappings:
        email, pid = row["email"], row["profile_id"]
        if pid not in state:
            print(f"  跳过 {email}: 环境 {pid} 在 AdsPower 侧已不存在")
            continue
        soft, endpoint = state[pid]
        direct = soft in ("", "no_proxy")
        if direct or args.all:
            plan.append((email, pid, "直连" if direct else f"当前 {endpoint}"))
        else:
            print(f"  跳过 {email}: 已有代理 {endpoint}（要强制换用 --all）")

    if not plan:
        print("\n没有需要重绑的环境")
        return 0
    if len(plan) > len(free):
        print(f"\n空闲代理不够：需要 {len(plan)} 个，只有 {len(free)} 个。"
              f"请先在 AdsPower「代理管理」里补充代理")
        return 1

    print(f"\n计划重绑 {len(plan)} 个环境：")
    for (email, pid, why), proxy_id in zip(plan, free):
        print(f"  {email:<32} {pid:<10} {why:<16} -> proxy_id={proxy_id}")

    if args.dry_run:
        print("\n--dry-run，未做任何修改")
        return 0

    print()
    done = failed = 0
    for (email, pid, _why), proxy_id in zip(plan, free):
        # 运行中的环境改了也不会作用到当前会话，只会制造「以为换了」的假象
        try:
            active, _ws = client.profile_active(pid)
        except AdsPowerError as e:
            print(f"  !! {email}: 查询运行状态失败 {e}")
            failed += 1
            continue
        if active:
            print(f"  !! {email}: 环境正在运行，跳过（请先关掉浏览器再跑一次）")
            failed += 1
            continue

        try:
            client.update_profile(pid, {"proxyid": proxy_id})
        except AdsPowerError as e:
            print(f"  !! {email}: 绑定失败 {e}")
            failed += 1
            continue

        # 本地映射同步更新：它是下次「挑空闲代理」时判断占用的依据之一，
        # 留着旧号会让人以为这个环境还绑在一个早就不存在的代理上。
        profiles.upsert(email, pid, row_no(profiles, email), proxy_id)
        print(f"  {email:<32} -> proxy_id={proxy_id} ✓")
        done += 1

    print(f"\n完成 {done} 个，失败/跳过 {failed} 个")
    if done:
        print("提示：出口 IP 变了，这些账号下次登录 GitHub 可能会触发一次新设备验证")
    return 0 if not failed else 1


def row_no(profiles, email):
    """取回已有的 profile_no —— upsert 是整行覆盖，不带上会把它清空。"""
    row = profiles.get_by_email(email) or {}
    return row.get("profile_no") or ""


if __name__ == "__main__":
    sys.exit(main())
