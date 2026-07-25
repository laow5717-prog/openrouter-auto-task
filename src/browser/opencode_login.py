"""opencode.ai GitHub-OAuth 自动登录，并进入该账号自己的 /go 页。

用于「GitHub 注册成功后，在同一浏览器 session 里续上 opencode 登录」。前提：当前 profile
的 github.com 已登录（signup_one 成功后即处于此态）。

实机验证的 OAuth 链路（见 07-25-hotmail-github-signup task / probe_opencode_login.py）：
  访问受保护页 → 跳 auth.opencode.ai/authorize → 点「Continue with GitHub」(<a role=link>)
  → github.com/login/oauth/authorize 授权页点「Authorize」(新号首次授权才有)
  → 回落 opencode.ai/workspace/{own_wid}

关键事实：
- opencode 会话不跨浏览器重启持久，但 GitHub 授权持久 → 重登时授权页无感自动跳过。
- 登录后落地的是该账号「自己自动创建」的 workspace；别人的 workspace 非成员访问会被弹回登录页。
"""
import re
import time

WORKSPACE_RE = re.compile(r'opencode\.ai/workspace/(wrk_[A-Za-z0-9]+)')
_AUTH_HOST = "auth.opencode.ai"


def _cur_url(session):
    try:
        return session.current_url or ""
    except Exception:
        return ""


def _extract_wid(url):
    m = WORKSPACE_RE.search(url or "")
    return m.group(1) if m else None


def _wait_until(session, pred, timeout=45, poll=1.5):
    """轮询直到 pred(url) 为真或超时；返回最终 url。guard 读，导航中不抛异常。"""
    start = time.time()
    while time.time() - start < timeout:
        u = _cur_url(session)
        if pred(u):
            return u
        time.sleep(poll)
    return _cur_url(session)


def _step(monitor, session, msg):
    print(f"  [opencode] {msg}", flush=True)
    try:
        if monitor:
            monitor(session, msg)
        else:
            session.capture_frame()
    except Exception:
        pass


def _click_continue_github(session):
    """点 opencode 登录页的「Continue with GitHub」链接。成功返回 True。"""
    page = session.page
    try:
        loc = page.get_by_role("link", name="Continue with GitHub")
        if loc.count():
            loc.first.click(timeout=8000)
            return True
    except Exception:
        pass
    # 兜底：直接导航到该链接目标
    try:
        page.goto("https://auth.opencode.ai/github/authorize", wait_until="domcontentloaded", timeout=15000)
        return True
    except Exception:
        return False


def _click_authorize_if_present(session):
    """GitHub OAuth 授权页若有「Authorize」按钮则点（新号首次）。返回是否点了。

    GitHub 的 Authorize 按钮有防点击劫持延迟（初始 disabled 数秒），Playwright 的 click
    会等到可交互，timeout 给足。
    """
    if "github.com" not in _cur_url(session):
        return False
    page = session.page
    for name in ("Authorize", "Authorize OpenCode Console"):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count():
                btn.first.click(timeout=12000)
                return True
        except Exception:
            continue
    return False


def login_and_open_own_go(session, monitor=None, timeout=150):
    """登录 opencode 并进入该账号自己的 /go 页。

    返回 dict:
      ok:      bool     是否成功进入 /go
      wid:     str|None 该账号自己的 workspace id
      go_url:  str|None
      detail:  str
    """
    result = {"ok": False, "wid": None, "go_url": None, "detail": ""}
    deadline = time.time() + timeout

    # 1) 触发 opencode 鉴权：访问受保护入口
    _step(monitor, session, "打开 opencode，检查登录态")
    try:
        session.get("https://opencode.ai/auth")
    except Exception as e:
        result["detail"] = f"打开 opencode 失败: {str(e)[:120]}"
        return result
    time.sleep(3)
    url = _cur_url(session)

    # 2) 若被弹到 auth.opencode.ai → 走 GitHub OAuth
    if _AUTH_HOST in url:
        _step(monitor, session, "未登录，点 Continue with GitHub")
        if not _click_continue_github(session):
            result["detail"] = "未能点到 Continue with GitHub"
            return result
        # 等待离开 opencode 登录页（进 github 授权页 or 直接回落 opencode）
        url = _wait_until(session, lambda u: u and _AUTH_HOST not in u,
                          timeout=max(10, int(deadline - time.time())))
        # 3) GitHub 授权页（新号首次）点 Authorize
        if "github.com" in _cur_url(session):
            _step(monitor, session, "GitHub 授权页，点 Authorize")
            _click_authorize_if_present(session)
            url = _wait_until(session, lambda u: u and "github.com" not in u,
                              timeout=max(10, int(deadline - time.time())))

    # 4) 等回落到 opencode workspace，取自己的 wid
    url = _wait_until(
        session,
        lambda u: _extract_wid(u) is not None,
        timeout=max(10, int(deadline - time.time())),
    )
    wid = _extract_wid(url)
    # 全新账号首次授权后，opencode 自动创建的 workspace 有**瞬态 provision 延迟**——
    # 授权成功却一时落不到 /workspace/{wid}。实测重试即好（见 07-25 task 第五轮：leilao40）。
    # 故兜底改为重试轮询：反复访问 /auth 让其 provision 完成后重定向到默认 workspace。
    if not wid:
        _step(monitor, session, "授权已过但未落到 workspace，等待新号 workspace provision（重试中）…")
        for _ in range(8):
            if time.time() > deadline:
                break
            try:
                session.get("https://opencode.ai/auth")
            except Exception:
                pass
            u = _wait_until(session, lambda u: _extract_wid(u) is not None,
                            timeout=6, poll=1.5)
            wid = _extract_wid(u)
            if wid:
                break
    if not wid:
        result["detail"] = f"登录后未能取到自己的 workspace id，停在 {_cur_url(session)[:120]}"
        return result

    result["wid"] = wid
    _step(monitor, session, f"已登录，自己的 workspace = {wid}")

    # 5) 进入自己的 /go 页
    go_url = f"https://opencode.ai/workspace/{wid}/go"
    try:
        session.get(go_url)
        time.sleep(2)
    except Exception as e:
        result["detail"] = f"导航 /go 失败: {str(e)[:120]}"
        result["go_url"] = go_url
        return result

    final = _cur_url(session)
    result["go_url"] = go_url
    # 成功判据：最终仍在自己的 workspace 域内（未被弹回登录页）
    if _extract_wid(final) == wid and _AUTH_HOST not in final:
        result["ok"] = True
        result["detail"] = f"已登录并进入自己的 /go 页: {final}"
    else:
        result["detail"] = f"已登录但 /go 落地异常: {final[:120]}"
    _step(monitor, session, result["detail"])
    return result


# --- 独立测试入口：对已注册账号的 profile 直接跑登录+进 /go ---------------------
if __name__ == "__main__":
    import argparse
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.browser.driver import create_driver, close_driver

    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="用作 profile_id 的已注册账号邮箱")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    sess = create_driver(headless=False, profile_id=args.email)
    try:
        r = login_and_open_own_go(sess)
        print("\n结果:", r, flush=True)
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
