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
    """GitHub OAuth/App 授权页若有「Authorize」按钮则点（新号首次）。返回是否点了。

    GitHub 的 Authorize 按钮有**防点击劫持延迟**（初始 disabled 数秒，JS 定时器到点才启用）；
    且文案随应用名变化（"Authorize opencode" / "Authorize and install" 等），是 <button
    type=submit name=authorize value=1> 或 <input type=submit>。故**轮询等其可点再点**，
    并按文案 + 选择器双重兜底，避免停在授权页不动。
    """
    if "github.com" not in _cur_url(session):
        return False
    page = session.page
    names = ["Authorize", "Authorize and install", "Continue", "Install", "授权"]
    selectors = [
        'button[name="authorize"][value="1"]',
        'form[action*="/authorize"] button[type="submit"]',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    start = time.time()
    while time.time() - start < 25:
        if "github.com" not in _cur_url(session):
            return True            # 已离开授权页（可能已被别的点击推进）
        # 1) 自然可点时点击（防点击劫持延迟自然解除后 disabled 消失）
        for nm in names:
            try:
                btn = page.get_by_role("button", name=nm)
                if btn.count() and btn.first.is_enabled():
                    btn.first.click(timeout=5000)
                    print(f"  [opencode] 已点授权按钮：{nm}", flush=True)
                    return True
            except Exception:
                pass
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_enabled():
                    loc.first.click(timeout=5000)
                    print(f"  [opencode] 已点授权按钮（selector {sel}）", flush=True)
                    return True
            except Exception:
                pass
        # 2) 按钮一直 disabled（GitHub 防点击劫持 JS 定时器在自动化/后台标签下常不解除）
        #    → 等 6s 后强制移除 disabled 属性再 JS 点击
        if time.time() - start > 6:
            try:
                ok = page.evaluate(r"""() => {
                    const b = document.querySelector(
                        'button[name="authorize"][value="1"], button.js-oauth-authorize-btn, '
                        + 'form[action*="/authorize"] button[type=submit]');
                    if (!b) return false;
                    b.removeAttribute('disabled'); b.disabled = false;
                    b.click();
                    return true;
                }""")
                if ok:
                    print("  [opencode] 强制点授权按钮（移除 disabled）", flush=True)
                    time.sleep(2)
                    if "github.com" not in _cur_url(session):
                        return True
            except Exception:
                pass
        time.sleep(1.5)
    print("  [opencode] 授权按钮 25s 内未能点击（可能被 flag / 无按钮）", flush=True)
    return False


def _account_flagged(session):
    """检测「This account is flagged … cannot authorize a third party application」。
    新注册账号常被 GitHub 反滥用 flag。文案在 github 授权页/dashboard 出现——**不限定域名**匹配
    （授权后页面可能已跳离 github），命中即快速返回，供上层立刻标记跳过、不空等。"""
    try:
        body = (session.page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        return False
    return "account is flagged" in body or ("flagged" in body and "authorize" in body)


def login_and_open_own_go(session, monitor=None, timeout=240):
    """登录 opencode 并进入该账号自己的 /go 页。

    返回 dict:
      ok:      bool     是否成功进入 /go
      wid:     str|None 该账号自己的 workspace id
      go_url:  str|None
      flagged: bool     GitHub 账号被 flag，无法授权第三方应用（新号常见）
      detail:  str
    """
    result = {"ok": False, "wid": None, "go_url": None, "flagged": False, "detail": ""}
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
            # 新注册账号常被 GitHub flag，授权页直接报「account is flagged, cannot authorize」
            if _account_flagged(session):
                result["flagged"] = True
                result["detail"] = "GitHub 账号被 flagged，无法授权第三方应用（opencode OAuth）"
                _step(monitor, session, result["detail"])
                return result
            _click_authorize_if_present(session)
            # 点 Authorize 后最可能立刻出现 flag——先短等再查，命中即刻返回，绝不进后面的长等待
            time.sleep(2)
            if _account_flagged(session):
                result["flagged"] = True
                result["detail"] = "GitHub 账号被 flagged，无法授权第三方应用（opencode OAuth）"
                _step(monitor, session, result["detail"])
                return result
            url = _wait_until(session, lambda u: u and "github.com" not in u,
                              timeout=max(8, int(deadline - time.time())))

    # 4) 等回落到 opencode workspace，取自己的 wid（首等缩短，provision 延迟交给下面重试循环）
    url = _wait_until(session, lambda u: _extract_wid(u) is not None,
                      timeout=min(15, max(6, int(deadline - time.time()))))
    wid = _extract_wid(url)
    # 全新账号首次授权后 workspace 有瞬态 provision 延迟（重试即好，见 leilao40）；
    # 但被 flag 的新号永远拿不到 wid——**每轮先查 flag，命中立刻标记返回，不空等 provision**。
    if not wid:
        _step(monitor, session, "未落到 workspace，检查是否被 flag / 等待 provision（重试中）…")
        for _ in range(15):
            if time.time() > deadline:
                break
            # 每轮先查当前 url——provision 可能刚完成、wid 已出现（避免误判失败）
            wid = _extract_wid(_cur_url(session))
            if wid:
                break
            if _account_flagged(session):
                result["flagged"] = True
                result["detail"] = "GitHub 账号被 flagged，无法授权第三方应用（opencode OAuth）"
                _step(monitor, session, result["detail"])
                return result
            try:
                session.get("https://opencode.ai/auth")
            except Exception:
                pass
            u = _wait_until(session, lambda u: _extract_wid(u) is not None,
                            timeout=6, poll=1.5)
            wid = _extract_wid(u)
            if wid:
                break
    # 末次兜底：再从当前 url 取一次 wid（provision 可能在最后一刻才完成）
    if not wid:
        wid = _extract_wid(_cur_url(session))
    if not wid:
        # 仍无 wid：查 flag（多半被 flag），否则如实报未取到 workspace
        if _account_flagged(session):
            result["flagged"] = True
            result["detail"] = "GitHub 账号被 flagged，无法授权第三方应用（opencode OAuth）"
            return result
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
