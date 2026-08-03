"""诊断：跑到 Stripe 订阅 hCaptcha 弹出，dump frame 树 + 逐帧 hcaptcha 内部结构。

目的：定位 2captcha token 该注入到哪个 frame（h-captcha-response textarea / window.hcaptcha /
callback 到底在 js.stripe.com/hcaptcha-inner 帧还是别处），为改 _inject_hcaptcha_token 提供依据。

hCaptcha 会挡住扣款，本诊断到 hCaptcha 出现即停，不点解题、不扣款。--keep 保活供人工查看。
用法: python3 scripts/probe_hcaptcha_frames.py --email carold030@hotmail.com --keep
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe)
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent, _captcha_challenge_present)
from src.services import captcha as captcha_solver
from src.models.database import Database
from src.models.card_pool import CardPoolModel

# 逐帧探测 hcaptcha 内部结构 + hook 安装状态（决定 add_init_script 是否覆盖到本帧）
_FRAME_PROBE_JS = r"""
() => {
  const q = s => Array.from(document.querySelectorAll(s));
  const tas = q('textarea[name*="captcha-response"], textarea[id*="captcha-response"]')
      .map(t => ({name:t.name, id:t.id, len:(t.value||'').length}));
  const widgets = q('[data-hcaptcha-widget-id]').map(e => e.getAttribute('data-hcaptcha-widget-id'));
  const cbs = q('[data-callback]').map(e => e.getAttribute('data-callback'));
  const hcIframes = q('iframe').map(f=>f.src).filter(s=>/hcaptcha/i.test(s));
  let hasHcaptcha=false, ids=[], hcKeys=[];
  try { hasHcaptcha = typeof window.hcaptcha !== 'undefined';
        if (window.hcaptcha) { hcKeys = Object.keys(window.hcaptcha).slice(0,15);
          if (window.hcaptcha.getAllIds) ids = window.hcaptcha.getAllIds(); } } catch(e){}
  // hook 是否装进本帧：__hcapHookInstalled / __hcapHook 及其诊断
  let hookInstalled=false, hookDiag=null;
  try {
    hookInstalled = !!window.__hcapHookInstalled;
    var h = window.__hcapHook;
    if (h) hookDiag = {sf:h.setterFired, rc:h.renderCalls, ec:h.executeCalls,
                       gr:h.getResponseCalls, cbs:(h.callbacks||[]).length,
                       rs:(h.resolvers||[]).length, wids:(h.widgetIds||[]).length};
  } catch(e){}
  return {url: location.href.slice(0,90), textareas: tas, widgetIds: widgets,
          callbacks: cbs, hcaptchaObj: hasHcaptcha, hcaptchaKeys: hcKeys, hcaptchaIds: ids,
          hcaptchaIframes: hcIframes, hookInstalled: hookInstalled, hookDiag: hookDiag};
}
"""


def dump_frames(session, tag):
    print(f"\n===== FRAME 树 [{tag}] =====", flush=True)
    for i, fr in enumerate(session.page.frames):
        url = (fr.url or "")[:90]
        try:
            info = fr.evaluate(_FRAME_PROBE_JS)
        except Exception as e:
            info = {"error": str(e)[:80]}
        # 全帧都打 hook 覆盖标记（决定 add_init_script 是否进到子帧）
        hk = "HOOK✓" if info.get("hookInstalled") else "hook✗"
        rel = ("captcha" in url.lower() or "stripe" in url.lower()
               or info.get("textareas") or info.get("hcaptchaObj") or info.get("hcaptchaIframes"))
        if rel:
            print(f"[{i}] {hk} {url}", flush=True)
            print(f"     {json.dumps(info, ensure_ascii=False)[:500]}", flush=True)
        else:
            print(f"[{i}] {hk} {url}", flush=True)
    print("========================\n", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]
    print(f"用卡 ****{str(card.get('number',''))[-4:]} 触发 hCaptcha（不解题、不扣款）")

    session = create_driver(headless=False, profile_id=args.email)
    # 与真实脚本一致：导航前装 hook，探测其是否覆盖到各子帧
    captcha_solver.install_hcaptcha_hook(session)
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            sys.exit(1)
        wid = lg["wid"]
        ok, d = start_subscribe_go(session, wid)
        print("start_subscribe_go:", ok, d)
        print("currency:", select_usd_subscribe(session))
        print("select_card:", select_card_method(session, None))
        print("fill:", fill_card_and_address(session, card, None))
        fill_phone_if_present(session, card, None)
        uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)
        print("click_subscribe:", click_subscribe(session))

        # 等 hCaptcha 出现
        print("等待 hCaptcha 出现...")
        for _ in range(20):
            if _captcha_challenge_present(session) is not None:
                print("  hCaptcha 已出现")
                break
            time.sleep(2)
        time.sleep(3)
        dump_frames(session, "hcaptcha_present")

        if args.keep:
            print("keep：浏览器保活，Ctrl-C 结束", flush=True)
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
