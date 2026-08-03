"""诊断：全新 opencode 账号登录后拿不到 workspace，dump 卡住页面看缺哪步 onboarding。
用法: python3 scripts/probe_opencode_onboarding.py --email leilao40@hotmail.com [--keep]
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go

_DUMP = r"""() => {
  const txt = el => (el.innerText||el.textContent||'').trim().replace(/\s+/g,' ').slice(0,60);
  const links = [...document.querySelectorAll('a,[role=link]')].map(txt).filter(Boolean);
  const btns  = [...document.querySelectorAll('button,[role=button],input[type=submit]')].map(txt).filter(Boolean);
  const heads = [...document.querySelectorAll('h1,h2,h3')].map(txt).filter(Boolean);
  const inputs= [...document.querySelectorAll('input,textarea,select')].map(e=>({name:e.name,type:e.type,ph:e.placeholder}));
  return {url: location.href, title: document.title,
          heads:[...new Set(heads)].slice(0,15), links:[...new Set(links)].slice(0,30),
          btns:[...new Set(btns)].slice(0,30), inputs: inputs.slice(0,20),
          body:(document.body?document.body.innerText:'').replace(/\s+/g,' ').slice(0,1500)};
}"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    s = create_driver(headless=False, profile_id=args.email)
    try:
        r = login_and_open_own_go(s)
        print("\n登录结果:", json.dumps(r, ensure_ascii=False))
        # 无论成败都 dump 一次当前页
        for _ in range(3):
            try:
                info = s.page.evaluate(_DUMP)
                break
            except Exception:
                time.sleep(2); info = {"err": "evaluate 失败"}
        print("\n===== 当前页 dump =====")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        # 也 dump 所有 frame 的 url
        print("\nframes:", [ (f.url or '')[:80] for f in s.page.frames ])
        if args.keep:
            print("\nkeep：浏览器保活，你可手动点 onboarding，Ctrl-C 结束", flush=True)
            try:
                while True: time.sleep(5)
            except KeyboardInterrupt: pass
    finally:
        if not args.keep:
            close_driver(s)

if __name__ == "__main__":
    main()
