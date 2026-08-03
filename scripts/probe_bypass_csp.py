"""验证假设：bypass_csp=True 后，Patchright 的内联 add_init_script hook 能在 Stripe/hCaptcha
OOPIF 帧生效（不再被 CSP 拦），且拦到 execute。跑到点 Subscribe，**不解题、不扣款**。

判据：hcaptcha 帧 installed=true（且理想情况下 diag.ec>0）→ 假设成立，可接入订阅流程。

用法: python3 scripts/probe_bypass_csp.py --email carold030@hotmail.com --group 1
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import captcha as captcha_solver
from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe)
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent, _captcha_challenge_present)
from src.models.database import Database
from src.models.card_pool import CardPoolModel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    args = ap.parse_args()

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list('opencode', args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]

    session = create_driver(headless=False, profile_id=args.email, bypass_csp=True)
    # 导航前装 hook（Patchright 会把它内联进每个 document 响应；bypass_csp 让它不被 CSP 拦）
    captcha_solver.install_hcaptcha_hook(session)
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            print("⚠️ bypass_csp 下登录失败？", lg)
            sys.exit(1)

        ok, d = start_subscribe_go(session, lg["wid"])
        print("start_subscribe_go:", ok, d)
        print("currency:", select_usd_subscribe(session))
        print("select_card:", select_card_method(session, None))
        print("fill:", fill_card_and_address(session, card, None))
        fill_phone_if_present(session, card, None)
        uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)

        print("\n== 点 Subscribe（触发 hcaptcha，不解题不扣款）==")
        print("click_subscribe:", click_subscribe(session))

        for _ in range(20):
            if _captcha_challenge_present(session) is not None:
                break
            time.sleep(1)
        time.sleep(4)

        print("\n===== hcaptcha 帧 hook 落地检查 =====")
        probe = r"""() => { var h=window.__hcapHook; return {
            installed: !!window.__hcapHookInstalled,
            diag: h ? {sf:h.setterFired, rc:h.renderCalls, ec:h.executeCalls,
                       gr:h.getResponseCalls, cbs:(h.callbacks||[]).length,
                       rs:(h.resolvers||[]).length, wids:(h.widgetIds||[]).length} : null,
            hasHcaptcha: typeof window.hcaptcha !== 'undefined' }; }"""
        hooked_any = False
        for i, fr in enumerate(session.page.frames):
            url = (fr.url or "")
            if not ("hcaptcha" in url.lower() or "HCaptchaInvisible" in url or "stripecdn" in url
                    or "checkout.stripe" in url):
                continue
            try:
                info = fr.evaluate(probe)
            except Exception as e:
                info = {"error": str(e)[:60]}
            mark = "HOOK✓" if info.get("installed") else "hook✗"
            if info.get("installed"):
                hooked_any = True
            print(f"  [{i}] {mark} {url[:75]}")
            print(f"       {json.dumps(info, ensure_ascii=False)}")
        print("\n=> bypass_csp 内联 hook", "成功（至少一帧 HOOK✓）" if hooked_any else "失败（全 hook✗）")
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
