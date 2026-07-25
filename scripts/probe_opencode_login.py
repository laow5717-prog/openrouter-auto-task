"""实机探测：用已注册的 hotmail profile 走 opencode GitHub OAuth 登录 + 跳 workspace。

carold030 的持久 profile 里 GitHub 已登录。这里探 opencode.ai/auth 的授权流程：
点 "Continue with GitHub" → GitHub OAuth 授权页（可能需点 Authorize）→ 回落 opencode →
再导航到目标 workspace /go 页。全程 dump（URL/标题/按钮/链接）+ 截图，供收敛正式实现。

用法:
    python3 scripts/probe_opencode_login.py --email carold030@hotmail.com --keep
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver

TARGET = "https://opencode.ai/workspace/wrk_01KXQBHDNVBKX30TK740YF5D5F/go"

DUMP_JS = r"""
() => {
  const txt = el => (el.innerText || el.textContent || '').trim().replace(/\s+/g,' ').slice(0,60);
  const buttons = [];
  document.querySelectorAll("button,[role=button],input[type=submit]").forEach(b=>{
    const t = txt(b); if (t) buttons.push(t);
  });
  const links = [];
  document.querySelectorAll("a").forEach(a=>{
    const t = txt(a); if (t) links.push(t + ' -> ' + (a.getAttribute('href')||''));
  });
  return {
    url: location.href,
    title: document.title,
    buttons: [...new Set(buttons)].slice(0,30),
    links: [...new Set(links)].slice(0,30),
    bodySample: (document.body ? document.body.innerText : '').replace(/\s+/g,' ').slice(0,500),
  };
}
"""


def dump(session, tag):
    time.sleep(2)
    try:
        info = session.page.evaluate(DUMP_JS)
    except Exception as e:
        info = {"error": str(e)[:200], "url": session.current_url}
    print(f"\n===== DUMP [{tag}] =====", flush=True)
    print("URL   :", info.get("url"), flush=True)
    print("TITLE :", info.get("title"), flush=True)
    print("BUTTONS:", info.get("buttons"), flush=True)
    print("LINKS :", info.get("links"), flush=True)
    print("BODY  :", info.get("bodySample", "")[:400], flush=True)
    if info.get("error"):
        print("ERROR :", info["error"], flush=True)
    print("=========================\n", flush=True)
    return info


def _try_click_text(session, texts):
    """在 button/a 里按可见文本模糊点击第一个命中。返回命中的文本或 None。"""
    page = session.page
    for t in texts:
        try:
            loc = page.get_by_role("button", name=t)
            if loc.count():
                loc.first.click(timeout=5000)
                return f"button:{t}"
        except Exception:
            pass
        try:
            loc = page.locator(f"a:has-text('{t}'), button:has-text('{t}')")
            if loc.count():
                loc.first.click(timeout=5000)
                return f"text:{t}"
        except Exception:
            pass
    return None


def _cur_url(session):
    """安全读 current_url，页面处于导航/关闭态时返回空串而非抛异常。"""
    try:
        return session.current_url or ""
    except Exception:
        return ""


def _shot(session, tag):
    try:
        session.capture_frame()
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "screenshots", f"oclogin_{tag}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(session.get_screenshot_as_png())
        print(f"  📸 {path}", flush=True)
    except Exception as e:
        print(f"  截图失败: {str(e)[:80]}", flush=True)


def _wait_url_change(session, from_substr, timeout=45):
    """轮询等待 URL 离开 from_substr。每 2s guard 读一次，返回最终 URL。"""
    start = time.time()
    last = _cur_url(session)
    while time.time() - start < timeout:
        u = _cur_url(session)
        if u and from_substr not in u:
            return u
        if u != last:
            print(f"    url -> {u[:100]}", flush=True)
            last = u
        time.sleep(2)
    return _cur_url(session)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--email", default="carold030@hotmail.com")
    p.add_argument("--target", default=TARGET)
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()

    session = create_driver(headless=False, profile_id=args.email)
    try:
        print(f"profile: {args.email}", flush=True)

        # 1) 直接打目标 /go（邀请链接未登录会自行跳 auth.opencode.ai/authorize）
        print(f"\n[1] 直接导航目标: {args.target}", flush=True)
        session.get(args.target)
        info = dump(session, "target_first")
        url = info.get("url") or ""

        # 2) 已在 auth.opencode.ai 登录页：点 Continue with GitHub（是 <a>，不是 button）
        if "auth.opencode.ai" in url:
            print("[2] 在 opencode 登录页，点 Continue with GitHub 链接", flush=True)
            try:
                session.page.get_by_role("link", name="Continue with GitHub").first.click(timeout=8000)
                print("  已点击 link:Continue with GitHub", flush=True)
            except Exception as e:
                print(f"  点击失败，尝试直接导航 /github/authorize: {str(e)[:80]}", flush=True)
                try:
                    session.page.goto("https://auth.opencode.ai/github/authorize")
                except Exception:
                    pass
            # 点击后进入 GitHub OAuth，轮询等待离开 auth.opencode.ai
            url = _wait_url_change(session, "auth.opencode.ai", timeout=45)
            print(f"  跳转后 URL: {url[:120]}", flush=True)
            _shot(session, "after_github_click")

        # 3) 若停在 GitHub OAuth 授权页，点 Authorize（新号首次授权才有）
        if "github.com" in _cur_url(session):
            print("[3] 在 GitHub 域，检查是否需点 Authorize", flush=True)
            dump(session, "github_oauth")
            try:
                btn = session.page.get_by_role("button", name="Authorize")
                if btn.count():
                    btn.first.click(timeout=8000)
                    print("  已点 Authorize", flush=True)
                    _wait_url_change(session, "github.com", timeout=45)
            except Exception as e:
                print(f"  Authorize 处理: {str(e)[:80]}", flush=True)
            _shot(session, "after_authorize")

        # 4) 等回落 opencode，dump 终态
        print("\n[4] 等待回落 opencode 并 dump 终态", flush=True)
        for _ in range(20):
            if "opencode.ai" in _cur_url(session) and "auth.opencode" not in _cur_url(session):
                break
            time.sleep(2)
        dump(session, "after_login_landing")
        _shot(session, "after_login_landing")
        print(f"\n  登录后落地 URL: {_cur_url(session)}", flush=True)

        # 5) 决定性测试：已登录 session 活着时，再导航目标 workspace /go，看能否进入
        print(f"\n[5] 已登录态再次导航目标: {args.target}", flush=True)
        try:
            session.get(args.target)
        except Exception as e:
            print(f"  导航异常: {str(e)[:100]}", flush=True)
        time.sleep(3)
        dump(session, "target_final")
        _shot(session, "target_final")
        final = _cur_url(session)
        print(f"\n>>> 终态 URL: {final}", flush=True)
        print(f">>> 是否到目标 workspace: {'wrk_01KXQBHDNVBKX30TK740YF5D5F' in final}", flush=True)
        print(f">>> 是否被弹回登录: {'auth.opencode' in final}", flush=True)

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
