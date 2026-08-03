"""决定性实验：用**原生 Playwright**（非 Patchright）驱动，验证 context.add_init_script 能否
把 hcaptcha hook **前置注入到跨域 OOPIF 帧**（Patchright 阉割了这能力，原生作主调试器能暂停 OOPIF）。
跑到点 Subscribe（造出 hcaptcha 帧+触发 execute），**不解题不扣款**，看 hook 是否落地并拦到 execute。

判据：hcaptcha 帧 installed=true 且 diag.ec>0 → 原生 Playwright 前置注入成立 → token 注入路打通。

复用现有 BrowserSession 包装 + 登录/订阅流程函数（原生与 patchright API 同构）。
用法: python3 scripts/probe_vanilla_inject.py --email leilao40@hotmail.com --group 1
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from src.services.captcha import _HCAPTCHA_HOOK_JS
from src.browser.driver import (
    BrowserSession, _kill_chrome_for_profile, BROWSER_LANG, BROWSER_ACCEPT_LANG,
    BROWSER_ACCEPT_LANG_HEADER, DEFAULT_TIMEOUT_MS, NAV_TIMEOUT_MS)
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe)
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent, _captcha_challenge_present)
from src.models.database import Database
from src.models.card_pool import CardPoolModel


def create_vanilla_session(profile_id):
    """原生 Playwright 版持久 context + BrowserSession（对照 patchright create_driver）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    safe = re.sub(r'[^\w@.\-]', '_', profile_id)
    user_data_dir = os.path.join(root, 'data', 'profiles', safe)
    _kill_chrome_for_profile(user_data_dir, f'vanilla {safe}')
    for n in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        p = os.path.join(user_data_dir, n)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",
        headless=False,
        no_viewport=True,
        args=["--no-first-run", "--no-default-browser-check",
              f"--lang={BROWSER_LANG}", f"--accept-lang={BROWSER_ACCEPT_LANG}",
              "--window-size=1440,900"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    ctx.set_default_timeout(DEFAULT_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    try:
        ctx.set_extra_http_headers({"Accept-Language": BROWSER_ACCEPT_LANG_HEADER})
    except Exception:
        pass
    s = BrowserSession(pw, ctx, page, temp_profile=None, download_dir=None,
                       user_data_dir=user_data_dir)
    page.on("response", s._on_response)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="leilao40@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    args = ap.parse_args()

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]

    session = create_vanilla_session(args.email)
    # 原生 Playwright：导航前 add_init_script，应能前置注入所有帧（含 OOPIF）
    session.context.add_init_script(_HCAPTCHA_HOOK_JS)
    print("✅ 原生 Playwright add_init_script 已装")
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            print("⚠️ 原生 Playwright 下登录失败:", lg.get("detail"))
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
        hooked_any, ec_any = False, False
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
            if (info.get("diag") or {}).get("ec"):
                ec_any = True
            print(f"  [{i}] {mark} {url[:75]}")
            print(f"       {json.dumps(info, ensure_ascii=False)}")
        print(f"\n=> 原生 Playwright 前置注入: hook 落地={'✓' if hooked_any else '✗'}  execute 拦截={'✓' if ec_any else '✗'}")
    finally:
        session.quit()


if __name__ == "__main__":
    main()
