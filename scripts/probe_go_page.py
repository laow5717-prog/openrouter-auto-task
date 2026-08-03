"""探测 opencode workspace /go 页：登录后 dump /go 页的按钮/链接/正文，找「订阅/付款」入口。

用法: python3 scripts/probe_go_page.py --email carold030@hotmail.com
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go

DUMP_JS = r"""
() => {
  const txt = el => (el.innerText || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,80);
  const buttons = [];
  document.querySelectorAll("button,[role=button],input[type=submit],a").forEach(b=>{
    const t = txt(b); if (t) buttons.push((b.tagName||'').toLowerCase()+':'+t);
  });
  return {
    url: location.href, title: document.title,
    controls: [...new Set(buttons)].slice(0,60),
    body: (document.body ? document.body.innerText : '').replace(/\s+/g,' ').slice(0,1500),
  };
}
"""


def _dump(sess, tag):
    try:
        info = sess.page.evaluate(DUMP_JS)
    except Exception as e:
        info = {"error": str(e)[:200], "url": sess.current_url}
    print(f"\n===== DUMP [{tag}] =====", flush=True)
    print("URL  :", info.get("url"), flush=True)
    print("TITLE:", info.get("title"), flush=True)
    print("CONTROLS (tag:text):", flush=True)
    for c in info.get("controls", []):
        print("   -", c, flush=True)
    print("\nBODY :", info.get("body", "")[:1200], flush=True)
    if info.get("error"):
        print("ERROR:", info["error"], flush=True)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--subscribe", action="store_true",
                    help="点 Subscribe to Go 并 dump 结果页（不填卡不付款）")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    sess = create_driver(headless=False, profile_id=args.email)
    try:
        r = login_and_open_own_go(sess)
        print("\n登录结果:", r, flush=True)
        time.sleep(2)
        _dump(sess, "go_page")

        if args.subscribe:
            print("\n>>> 点击 Subscribe to Go（不填卡、不点 Pay）...", flush=True)
            try:
                sess.page.get_by_role("button", name="Subscribe to Go").first.click(timeout=8000)
            except Exception as e:
                print("  点击失败:", str(e)[:120], flush=True)
            # 等跳转/弹出 Stripe
            for _ in range(20):
                u = ""
                try:
                    u = sess.current_url or ""
                except Exception:
                    pass
                if "checkout.stripe.com" in u or "stripe" in u:
                    break
                time.sleep(1.5)
            time.sleep(3)
            print("  跳转后 URL:", (sess.current_url if True else ""), flush=True)
            # 列出所有 frame，判断 Stripe 是整页还是 iframe
            try:
                for fr in sess.page.frames:
                    print("   frame:", (fr.url or "")[:100], flush=True)
            except Exception as e:
                print("   frame 列举失败:", str(e)[:100], flush=True)
            _dump(sess, "after_subscribe")
            try:
                path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "screenshots", "after_subscribe.png")
                sess.capture_frame()
                with open(path, "wb") as f:
                    f.write(sess.get_screenshot_as_png())
                print("  SHOT:", path, flush=True)
            except Exception:
                pass
        # 截图
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "screenshots", "go_page.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sess.capture_frame()
            with open(path, "wb") as f:
                f.write(sess.get_screenshot_as_png())
            print("SHOT :", path, flush=True)
        except Exception as e:
            print("SHOT-ERR:", str(e)[:100], flush=True)

        if args.keep:
            print("keep：Ctrl-C 结束", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        if not args.keep:
            close_driver(sess)


if __name__ == "__main__":
    main()
