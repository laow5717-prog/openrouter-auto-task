"""诊断 Stripe invisible enterprise hCaptcha 弹出的挑战帧真实 DOM 结构（为点击求解器定 selector）。
点 Subscribe 后持续轮询 dump hcaptcha 帧的完整 URL + 元素清单，看图片挑战何时/是否出现。
用法: python3 scripts/probe_click_dom.py --email carold030@hotmail.com --group 1 --keep
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.browser.driver import create_driver, close_driver
from src.browser.opencode_login import login_and_open_own_go
from src.browser.opencode_subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe, _captcha_challenge_present)
from src.browser.opencode_billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent)
from src.models.database import Database
from src.models.card_pool import CardPoolModel

_PROBE = r"""() => {
  const cls = {};
  document.querySelectorAll('*').forEach(e => {
    (e.className && typeof e.className==='string' ? e.className.split(/\s+/) : []).forEach(c=>{
      if (/task|challenge|prompt|image|grid|tile|canvas|submit|example|crumb|refresh/i.test(c)) cls[c]=(cls[c]||0)+1;
    });
  });
  return {
    url: location.href,
    bodyLen: (document.body?document.body.innerText:'').length,
    bodyHead: (document.body?document.body.innerText:'').replace(/\s+/g,' ').slice(0,120),
    promptText: (document.querySelector('.prompt-text')?.textContent||'').trim().slice(0,60),
    taskGridImgs: document.querySelectorAll('.task-grid .image').length,
    canvases: document.querySelectorAll('canvas').length,
    submitBtn: !!document.querySelector('.button-submit'),
    relevantClasses: cls
  };
}"""

def dump(session, tag):
    print(f"\n===== [{tag}] =====", flush=True)
    for i, fr in enumerate(session.page.frames):
        url = (fr.url or "")
        if "hcaptcha" not in url.lower() and "#frame=" not in url:
            continue
        try:
            info = fr.evaluate(_PROBE)
        except Exception as e:
            info = {"err": str(e)[:60], "url": url}
        print(f"[{i}] {json.dumps(info, ensure_ascii=False)[:600]}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable: print("无可用卡"); sys.exit(1)
    card = usable[0]
    session = create_driver(headless=False, profile_id=args.email)
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"))
        if not lg.get("ok"): sys.exit(1)
        start_subscribe_go(session, lg["wid"]); select_usd_subscribe(session)
        select_card_method(session, None); fill_card_and_address(session, card, None)
        fill_phone_if_present(session, card, None); uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)
        print("click_subscribe:", click_subscribe(session))
        # 每 5s dump 一次，共 40s，观察挑战何时出现
        for k in range(8):
            time.sleep(5)
            dump(session, f"t+{(k+1)*5}s  captcha_present={_captcha_challenge_present(session) is not None}")
        if args.keep:
            print("\nkeep：Ctrl-C 结束", flush=True)
            try:
                while True: time.sleep(3600)
            except KeyboardInterrupt: pass
    finally:
        if not args.keep: close_driver(session)

if __name__ == "__main__":
    main()
