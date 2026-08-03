"""一次性真实订阅：登录已注册账号 → /go → Subscribe to Go → 用卡池真卡真实付款。

⚠️ 会真实扣款（首月 $5）。用于打通全链 + 标定「订阅成功」信号。逐张试卡、成功即止、逐卡记账
（镜像 services.registration.recharge_account 的卡消耗规则）。

用法:
    python3 scripts/run_subscribe_once.py --email carold030@hotmail.com --group 1 --max 5
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, create_driver_vanilla, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import subscribe_via_stripe
from src.services import captcha as captcha_solver
from src.config import cfg
from src.models.database import Database
from src.models.card_pool import CardPoolModel
from src.models.recharge_log import RechargeLogModel
from src.models.valid_card import ValidCardModel
from src.models.account import AccountModel
from src.models.platform_account import PlatformAccountModel

_DUMP_JS = r"""
() => {
  const txt = el => (el.innerText||el.textContent||'').trim().replace(/\s+/g,' ').slice(0,80);
  const c=[]; document.querySelectorAll("button,[role=button],a").forEach(b=>{const t=txt(b); if(t)c.push((b.tagName||'').toLowerCase()+':'+t);});
  return {url:location.href, title:document.title, controls:[...new Set(c)].slice(0,40),
          body:(document.body?document.body.innerText:'').replace(/\s+/g,' ').slice(0,1200)};
}
"""


def _dump_go(session, wid, tag):
    """付款后抓取 /go 页状态，用于标定订阅成功信号。"""
    try:
        session.get(f"https://opencode.ai/workspace/{wid}/go")
        time.sleep(3)
        info = session.page.evaluate(_DUMP_JS)
    except Exception as e:
        info = {"error": str(e)[:200]}
    print(f"\n===== 付款后 /go 状态 [{tag}] =====", flush=True)
    print("URL     :", info.get("url"), flush=True)
    print("CONTROLS:", info.get("controls"), flush=True)
    print("BODY    :", (info.get("body") or "")[:900], flush=True)
    if info.get("error"):
        print("ERROR   :", info["error"], flush=True)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--group", type=int, default=1, help="支付卡池分组 id")
    ap.add_argument("--platform", default="opencode", help="目标平台 slug")
    ap.add_argument("--max", type=int, default=5, help="最多尝试卡数（防风控）")
    ap.add_argument("--captcha-key", default=os.environ.get("TWOCAPTCHA_API_KEY", "") or cfg.captcha.api_key,
                    help="2captcha API key（默认取环境变量 TWOCAPTCHA_API_KEY 或 config）")
    ap.add_argument("--server", default=os.environ.get("CAPTCHA_SERVER", "2captcha.com"),
                    help="求解服务域名：2captcha.com（默认）或 api.multibot.cloud")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--patchright", action="store_true",
                    help="用 Patchright 主栈（默认用原生 Playwright——hCaptcha token 注入只在原生栈生效）")
    args = ap.parse_args()

    # 初始化 hCaptcha 求解器（付款时 Stripe 弹 hCaptcha，自动解）；server 可切 Multibot
    if args.captcha_key:
        captcha_solver.init_solver(args.captcha_key, server=args.server)
        print(f"  求解器已启用({args.server}): {'可用' if captcha_solver.is_available() else '不可用(未装库?)'}")
    else:
        print("  ⚠️ 未提供 captcha key，遇 hCaptcha 需人工")

    db = Database()
    card_pool = CardPoolModel(db)
    recharge_log = RechargeLogModel(db)
    valid_card = ValidCardModel(db)
    account = AccountModel(db)
    platform_account = PlatformAccountModel(db)

    usable, _ = card_pool.get_usable_cards_as_list(args.group)
    if not usable:
        print(f"❌ 分组 {args.group} 无可用卡")
        sys.exit(1)
    print(f"分组 {args.group} 可用卡 {len(usable)} 张，最多尝试 {args.max} 张")

    # 默认原生 Playwright：Patchright 阉割了 add_init_script，token 注入不进 hcaptcha OOPIF。
    # 原生栈作主调试器能前置注入（见 driver.create_driver_vanilla / task 第六轮）。
    if args.patchright:
        session = create_driver(headless=False, profile_id=args.email)
    else:
        session = create_driver_vanilla(profile_id=args.email)
    # 在任何导航前装 hCaptcha callback 劫持（invisible hCaptcha 交付所需；原生栈下真正生效）
    if captcha_solver.is_available():
        captcha_solver.install_hcaptcha_hook(session)
    final = {"ok": False, "outcome": None, "last4": None}
    try:
        lg = login_and_open_own_go(session)
        print("登录:", json.dumps(lg, ensure_ascii=False))
        if not lg.get("ok"):
            print("❌ 登录失败，终止")
            sys.exit(1)
        wid = lg["wid"]

        for i, card in enumerate(usable[:args.max], 1):
            num = card.get("number", "")
            last4 = str(num)[-4:]
            print(f"\n{'#'*56}\n# 第 {i} 张卡 ****{last4} 真实订阅付款\n{'#'*56}")
            log_id = recharge_log.create(args.email, num, amount=5)
            res = subscribe_via_stripe(session, card, wid, dry=False)
            print("订阅结果:", json.dumps(res, ensure_ascii=False))
            outcome = res.get("outcome")
            final = {"ok": res.get("ok"), "outcome": outcome, "last4": last4}

            if outcome == "success":
                card_pool.mark_status_by_number(num, "paid")
                valid_card.record(card, source_type="payment", source_email=args.email)
                platform_account.update_status(args.platform, args.email, "subscribed")
                recharge_log.mark_success(log_id, api_response={"result": res})
                print(f"  ✅ 订阅成功（卡 ****{last4} 标 paid，账号标 subscribed）")
                _dump_go(session, wid, "success")
                break
            elif outcome == "needs_captcha":
                recharge_log.mark_failed(log_id, error="hCaptcha 需人工", api_response={"result": res})
                print("  ⚠️ 遇 hCaptcha 人机验证，停止（账号级风控，需人工）")
                break
            elif outcome == "failed":
                # 明确拒付：从未成功过的卡判无效
                card_pool.mark_invalid_by_number(num)
                recharge_log.mark_failed(log_id, error=res.get("err", ""), api_response={"result": res})
                print(f"  ❌ 卡 ****{last4} 拒付，标 invalid，换下一张")
            else:  # error / unknown
                recharge_log.mark_failed(log_id, error=res.get("err", "") or outcome, api_response={"result": res})
                print(f"  ⚠️ outcome={outcome}（{res.get('err','')}），不消耗此卡，换下一张")

        print(f"\n{'='*56}\n最终: {json.dumps(final, ensure_ascii=False)}")

        if args.keep:
            print("keep：Ctrl-C 结束", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        if not args.keep:
            close_driver(session)


if __name__ == "__main__":
    main()
