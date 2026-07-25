#!/usr/bin/env python3
"""探测复充（Add Balance → 填金额 → Add）后跳转的页面结构。不点最终 Pay，不扣款。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.browser.driver import create_driver, close_driver  # noqa

WID = "wrk_01KY4YQ25J6MV0W0VTE51WAJBX"


def main():
    s = create_driver(headless=False, profile_id="manual")
    try:
        p = s.page
        s.get(f"https://opencode.ai/workspace/{WID}/billing")
        time.sleep(3)
        print("点 Add Balance...", flush=True)
        p.get_by_role("button", name="Add Balance").first.click(timeout=6000)
        time.sleep(1.5)
        try:
            amt = p.locator("input[type='number']").first
            amt.click(timeout=3000); amt.fill(""); amt.press_sequentially("20", delay=80)
        except Exception as e:
            print("填金额失败:", str(e)[:60], flush=True)
        print("点 Add...", flush=True)
        p.get_by_role("button", name="Add", exact=True).first.click(timeout=6000)
        time.sleep(8)
        print("URL:", s.current_url, flush=True)
        print("FRAMES:", flush=True)
        for f in p.frames:
            try:
                ins = f.evaluate("()=>[...document.querySelectorAll('input,button')].map(e=>({t:e.tagName,id:e.id||e.name||'',txt:(e.innerText||e.value||'').slice(0,25),type:e.type||''})).filter(x=>x.id||x.txt)")
            except Exception:
                ins = None
            if ins:
                print(f"  [{f.url[:70]}]", flush=True)
                for i in ins[:25]:
                    print("     ", i, flush=True)
        # 页面文本样本（看是否有 5573 已存卡选项 / 选美元）
        try:
            body = p.inner_text("body", timeout=4000)
            print("BODY:", " ".join(body.split())[:500], flush=True)
        except Exception:
            pass
        s.capture_frame()
        with open("data/screenshots/reload_probe.png", "wb") as fh:
            fh.write(s.get_screenshot_as_png())
        print("SHOT: data/screenshots/reload_probe.png", flush=True)
    finally:
        close_driver(s)


if __name__ == "__main__":
    main()
