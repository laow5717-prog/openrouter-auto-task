"""实验：用原始 CDP 前置注入 hcaptcha hook 到 OOPIF 子目标（绕过 Patchright 禁用的
add_init_script）。跑到点 Subscribe（造出 hcaptcha 帧+触发 execute），**不解题、不扣款**，
检查 hcaptcha 帧是否真被 hook 到、execute 是否被拦（ec>0）。

试三种前置原语，看哪种把 hook 送进 b.stripecdn/HCaptchaInvisible OOPIF：
  A. page 主会话 Page.addScriptToEvaluateOnNewDocument（覆盖主帧+同进程子帧）
  B. page 主会话 Target.setAutoAttach(flatten,waitForDebuggerOnStart)，记录 attachedToTarget
  C. page.on("frameattached") → new_cdp_session(frame) → addScriptToEvaluateOnNewDocument（抢 about:blank→src 之间的窗口）

用法: python3 scripts/probe_cdp_inject.py --email carold030@hotmail.com --group 1
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.captcha import _HCAPTCHA_HOOK_JS
from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe)
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent, _captcha_challenge_present)
from src.models.database import Database
from src.models.card_pool import CardPoolModel

_attach_log = []
_frame_sessions = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    args = ap.parse_args()

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]

    session = create_driver(headless=False, profile_id=args.email)
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            sys.exit(1)

        # ---- 到结账页前不碰 CDP（避免过早泄漏）。先进 checkout 再装 CDP 前置注入 ----
        ok, d = start_subscribe_go(session, lg["wid"])
        print("start_subscribe_go:", ok, d)
        print("currency:", select_usd_subscribe(session))
        print("select_card:", select_card_method(session, None))
        print("fill:", fill_card_and_address(session, card, None))
        fill_phone_if_present(session, card, None)
        uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)

        cdp = session._cdp()  # page 主会话

        # 策略 A：主会话前置注入
        try:
            rA = cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": _HCAPTCHA_HOOK_JS})
            print("[A] page.addScriptToEvaluateOnNewDocument ->", rA)
        except Exception as e:
            print("[A] 失败:", str(e)[:120])

        # 策略 B：auto-attach，记录子目标
        def on_attached(params):
            ti = params.get("targetInfo", {})
            _attach_log.append({"sid": params.get("sessionId"), "type": ti.get("type"),
                                "url": (ti.get("url") or "")[:70],
                                "waiting": params.get("waitingForDebugger")})
        try:
            cdp.on("Target.attachedToTarget", on_attached)
            rB = cdp.send("Target.setAutoAttach",
                          {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
            print("[B] setAutoAttach ->", rB)
        except Exception as e:
            print("[B] 失败:", str(e)[:120])

        # 策略 C：新帧一出现就建 per-frame CDP 会话前置注入
        def on_frameattached(frame):
            try:
                fs = session.context.new_cdp_session(frame)
                fs.send("Page.addScriptToEvaluateOnNewDocument", {"source": _HCAPTCHA_HOOK_JS})
                _frame_sessions.append(fs)  # 持有引用防 GC
                print(f"[C] frameattached 注入 ok: {(frame.url or 'about:blank')[:60]}")
            except Exception as e:
                print(f"[C] frameattached 注入失败 {(frame.url or '?')[:40]}: {str(e)[:80]}")
        session.page.on("frameattached", on_frameattached)

        print("\n== CDP 前置注入已装，点 Subscribe（触发 hcaptcha，不解题不扣款）==")
        print("click_subscribe:", click_subscribe(session))

        # 等 hCaptcha 帧出现
        for _ in range(20):
            if _captcha_challenge_present(session) is not None:
                break
            time.sleep(1)
        time.sleep(4)

        print("\n===== attachedToTarget 日志 =====")
        for a in _attach_log:
            print("  ", json.dumps(a, ensure_ascii=False))

        # 检查 hcaptcha 帧的 hook 状态
        print("\n===== hcaptcha 帧 hook 落地检查 =====")
        probe = r"""() => { var h=window.__hcapHook; return {
            installed: !!window.__hcapHookInstalled,
            diag: h ? {sf:h.setterFired, rc:h.renderCalls, ec:h.executeCalls,
                       gr:h.getResponseCalls, cbs:(h.callbacks||[]).length,
                       rs:(h.resolvers||[]).length} : null,
            hasHcaptcha: typeof window.hcaptcha !== 'undefined' }; }"""
        for i, fr in enumerate(session.page.frames):
            url = (fr.url or "")
            if not ("hcaptcha" in url.lower() or "HCaptchaInvisible" in url or "stripecdn" in url):
                continue
            try:
                info = fr.evaluate(probe)
            except Exception as e:
                info = {"error": str(e)[:60]}
            mark = "HOOK✓" if info.get("installed") else "hook✗"
            print(f"  [{i}] {mark} {url[:75]}")
            print(f"       {json.dumps(info, ensure_ascii=False)}")
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
