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
- auth.opencode.ai 是 SST OpenAuth。它的 state 存在 cookie 里且是一次性的，回调时对不上就
  甩出「The browser was in an unknown state」错误页（见 _auth_broken / _recover_auth）。
"""
import re
import time

WORKSPACE_RE = re.compile(r'opencode\.ai/workspace/(wrk_[A-Za-z0-9]+)')
_AUTH_HOST = "auth.opencode.ai"
_AUTH_ENTRY = "https://opencode.ai/auth"

# OpenAuth 错误页最多恢复几次。第 1 次重开 authorize（治陈旧 state），第 2 次带清 cookie
# （治 cookie 损坏/串台）。两次都不行说明是服务端或代理层问题，继续重来只会把单账号耗时
# 线性放大——充值是并发轮转的，账号失败一次下一轮还会被重试，不需要在单次调用里死磕。
_MAX_AUTH_RECOVER = 2

# 只清这两个域的 cookie，绝不全清（见 _clear_opencode_cookies）
_OPENCODE_COOKIE_DOMAINS = ("opencode.ai", "auth.opencode.ai")

_FLAGGED_DETAIL = "GitHub 账号被 flagged，无法授权第三方应用（opencode OAuth）"


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


def _budget(deadline, cap, floor=8):
    """取「剩余预算」与 cap 的较小值，至少 floor 秒。

    **不能把剩余预算整块交给一次等待**：停在 OpenAuth 错误页时 URL 根本不会变，一次
    `_wait_until(离开 auth host)` 就能把 240 秒总预算吃光，后面的恢复重试连跑的机会都没有。
    OAuth 重定向链正常是秒级的，cap 只是给走代理时留余量。
    """
    return max(floor, min(cap, int(deadline - time.time())))


# 单次跳转等待的上限（秒）。见 _budget 的说明。
_HOP_WAIT_CAP = 45


def _step(monitor, session, msg):
    print(f"  [opencode] {msg}", flush=True)
    try:
        if monitor:
            monitor(session, msg)
        else:
            session.capture_frame()
    except Exception:
        pass


def _try_click_continue(session):
    """在当前页面上找一次「Continue with GitHub」并点击。找不到/点不到返回 False。"""
    try:
        loc = session.page.get_by_role("link", name="Continue with GitHub")
        if loc.count():
            loc.first.click(timeout=8000)
            return True
    except Exception:
        pass
    return False


def _click_continue_github(session):
    """点 opencode 登录页的「Continue with GitHub」链接。成功返回 True。

    找不到链接时**重载 opencode.ai/auth 拿一张新鲜的 authorize 页再找一次**，绝不直接
    goto auth.opencode.ai/github/authorize。后者绕过 opencode 侧的 /authorize，OpenAuth
    没机会种 state cookie，GitHub 回调回来必然撞上「The browser was in an unknown state」
    错误页——那是确定性的构造错误，不是概率问题（2026-08-08 现场，见 _auth_broken）。
    重载顺带刷新了 state，对「页面放了几分钟已陈旧」的情形也是对症的。
    """
    if _try_click_continue(session):
        return True
    try:
        session.get(_AUTH_ENTRY)
    except Exception:
        return False
    time.sleep(2)
    return _try_click_continue(session)


def _auth_broken(session):
    """检测 opencode 认证服务（auth.opencode.ai，SST OpenAuth）的「浏览器状态未知」错误页。

    原文：「The browser was in an unknown state. This could be because certain cookies
    expired or the browser was switched in the middle of an authentication flow.」
    含义是 OAuth 回调回来时，OpenAuth 找不到自己在 /authorize 阶段种下的 state cookie。

    与 _account_flagged 同理**不限定域名**——这一页可能落在 auth.opencode.ai，也可能出现在
    回跳链路的中间态。"unknown state" 这个短语不会出现在正常的 opencode / GitHub 页面上，
    误报风险可以接受。第二条判据是防文案微调的冗余分支。

    不检测它的代价（2026-08-08 现场）：下面只读 URL 的 _wait_until 会在这一页上空转，
    provision 重试 15 轮全在同一个坏状态里打转，白耗上百秒后报「未能取到 workspace id」。
    """
    try:
        body = (session.page.inner_text("body", timeout=1500) or "").lower()
    except Exception:
        return False
    if "unknown state" in body:
        return True
    return "cookies expired" in body and "authentication flow" in body


def _clear_opencode_cookies(session):
    """只清 opencode 相关域的 cookie。清成功过至少一个域返回 True。

    **绝不能调无参 clear_cookies()**：那会连 github.com 的登录 cookie 一起抹掉，逼出一次
    完整重登 + 一次新设备邮箱验证（实测数分钟 + 一封验证码，见 billing._auto_verify_device）。
    同理见 .trellis/spec/backend/browser-profile-guidelines.md「Cookies 不可删」那一条——
    这里做的是按域收窄的版本，不是全清。
    """
    try:
        ctx = session.page.context
    except Exception:
        return False
    ok = False
    for domain in _OPENCODE_COOKIE_DOMAINS:
        try:
            ctx.clear_cookies(domain=domain)
            ok = True
        except Exception:
            pass
    return ok


def _recover_auth(session, monitor, attempt):
    """从 OpenAuth 错误页恢复：重新从 opencode.ai/auth 起一个全新的 authorize 流程。

    attempt >= 2 时先清 opencode 域 cookie——第 1 次错多半只是 state 陈旧（GitHub 登录 +
    新设备验证要跑好几分钟，期间最初那张 authorize 页早就放凉了），重开即好；再错说明
    cookie 本身损坏或串台，得先清掉。
    """
    if attempt >= 2:
        cleared = _clear_opencode_cookies(session)
        _step(monitor, session,
              f"命中 OpenAuth 错误页，第 {attempt} 次恢复（清 opencode cookie={cleared}，重开 authorize）")
    else:
        _step(monitor, session, f"命中 OpenAuth 错误页，第 {attempt} 次恢复（重开 authorize）")
    try:
        session.get(_AUTH_ENTRY)
    except Exception:
        pass
    time.sleep(3)


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


def login_and_open_own_go(session, monitor=None, timeout=240, open_go=True):
    """登录 opencode（并按需进入该账号自己的 /go 页）。

    open_go=False 时拿到 wid 即返回，不再导航 /go —— 充值走 zen 的 billing 页，
    /go 对它毫无用处，白跑一趟约 34 秒。

    返回 dict:
      ok:      bool     open_go=True 时表示成功进入 /go；open_go=False 时表示已取到 wid
      wid:     str|None 该账号自己的 workspace id
      go_url:  str|None open_go=False 时为 None
      flagged: bool     GitHub 账号被 flag，无法授权第三方应用（新号常见）
      detail:  str
    """
    result = {"ok": False, "wid": None, "go_url": None, "flagged": False, "detail": ""}
    deadline = time.time() + timeout
    recover_n = 0          # 已执行的 OpenAuth 错误页恢复次数，上限 _MAX_AUTH_RECOVER

    def _mark_flagged():
        result["flagged"] = True
        result["detail"] = _FLAGGED_DETAIL
        _step(monitor, session, result["detail"])
        return result

    def _oauth_leg():
        """从 auth.opencode.ai 登录页走完一趟 GitHub OAuth。返回 (推进成功, 被 flag)。

        抽成闭包是因为**恢复后要原样重走这一趟**——OpenAuth 的 state 是一次性的，恢复只是
        重新种了 state，授权链本身得再走一遍。
        """
        if not _click_continue_github(session):
            return False, False
        # 等待离开 opencode 登录页（进 github 授权页 or 直接回落 opencode）
        _wait_until(session, lambda u: u and _AUTH_HOST not in u,
                    timeout=_budget(deadline, _HOP_WAIT_CAP))
        # GitHub 授权页（新号首次）点 Authorize
        if "github.com" in _cur_url(session):
            _step(monitor, session, "GitHub 授权页，点 Authorize")
            # 新注册账号常被 GitHub flag，授权页直接报「account is flagged, cannot authorize」
            if _account_flagged(session):
                return False, True
            _click_authorize_if_present(session)
            # 点 Authorize 后最可能立刻出现 flag——先短等再查，命中即刻返回，绝不进后面的长等待
            time.sleep(2)
            if _account_flagged(session):
                return False, True
            _wait_until(session, lambda u: u and "github.com" not in u,
                        timeout=_budget(deadline, _HOP_WAIT_CAP))
        return True, False

    # 1) 触发 opencode 鉴权：访问受保护入口
    _step(monitor, session, "打开 opencode，检查登录态")
    try:
        session.get(_AUTH_ENTRY)
    except Exception as e:
        result["detail"] = f"打开 opencode 失败: {str(e)[:120]}"
        return result
    time.sleep(3)
    url = _cur_url(session)

    # 2) 若被弹到 auth.opencode.ai → 走 GitHub OAuth
    if _AUTH_HOST in url:
        _step(monitor, session, "未登录，点 Continue with GitHub")
        ok, flagged = _oauth_leg()
        if flagged:
            return _mark_flagged()
        if not ok:
            result["detail"] = "未能点到 Continue with GitHub"
            return result

    # 3) OAuth 回调可能落在 OpenAuth 的「unknown state」错误页（state cookie 丢失/陈旧）。
    #    必须在这里恢复：下面取 wid 的等待只读 URL，停在错误页时会一路空转到超时。
    while (recover_n < _MAX_AUTH_RECOVER and time.time() < deadline
           and _auth_broken(session)):
        recover_n += 1
        _recover_auth(session, monitor, recover_n)
        if _AUTH_HOST not in _cur_url(session):
            continue        # 恢复后直接回落（会话还在），交给下面取 wid
        ok, flagged = _oauth_leg()
        if flagged:
            return _mark_flagged()
        if not ok:
            break

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
                return _mark_flagged()
            # 停在 OpenAuth 错误页时，重访 /auth 只会再撞回同一页——要走恢复（第 2 次带清
            # cookie）。恢复配额用尽后继续按普通 provision 重试跑完剩余轮次，末尾据实报错。
            if _auth_broken(session) and recover_n < _MAX_AUTH_RECOVER:
                recover_n += 1
                _recover_auth(session, monitor, recover_n)
            else:
                try:
                    session.get(_AUTH_ENTRY)
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
            return _mark_flagged()
        # 停在 OpenAuth 错误页要如实说，别混进「未取到 workspace id」——那是两种完全不同的
        # 故障（一个是认证 state 问题，一个是 provision 慢），混报会让下一次排查从头开始。
        if _auth_broken(session):
            result["detail"] = (f"opencode 认证 state 失效（OpenAuth unknown state），"
                                f"已恢复 {recover_n} 次未果，停在 {_cur_url(session)[:120]}")
            _step(monitor, session, result["detail"])
            return result
        result["detail"] = f"登录后未能取到自己的 workspace id，停在 {_cur_url(session)[:120]}"
        return result

    result["wid"] = wid
    _step(monitor, session, f"已登录，自己的 workspace = {wid}")

    # 订阅流程需要 /go 页（Subscribe to Go 按钮在那儿）；充值走 zen，只要 wid 就能直接去
    # /workspace/<wid>/billing。实机计量：多这一跳每个账号白等约 34 秒，且 /go 是重页面，
    # 走代理时更慢。故由调用方决定要不要进。
    if not open_go:
        result["ok"] = True
        result["detail"] = f"已登录，workspace = {wid}（按调用方要求跳过 /go）"
        return result

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
