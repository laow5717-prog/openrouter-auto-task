"""GitHub 注册页（https://github.com/signup）页面操作层。

职责边界：只封装 GitHub signup 页的选择器与分步填表/终态判定，供
`services/github_signup_service.py` 编排调用。仅复用 driver.py 的通用 helper
（create_driver / _safe_goto / _wait_visible 等），不含任何 Cloudflare / opencode 语义，
与 driver.py 中标注 LEGACY 的方法群完全隔离。

选择器依据：task research/github-signup-dom.md（2026-07-22 实跑侦察确认）。
GitHub signup 为**单页全字段**表单（email/password/username/country/checkbox 一开始即可见），
非逐字段揭示；Create account 按钮初始 disabled，三字段校验通过后才 enabled。
"""
import re
import time
from urllib.parse import urlparse

from src.browser.driver import _safe_goto, _wait_visible

SIGNUP_URL = "https://github.com/signup?source=form-home-signup&user_email="

# —— 权威选择器（见 research/github-signup-dom.md）——
SEL_EMAIL = "#email"
SEL_EMAIL_ERR = "#email-err"
SEL_PASSWORD = "#password"
SEL_USERNAME = "#login"
SEL_USERNAME_ERR = "#login-err"          # 用户名错误容器（与 email-err 同构）
SEL_COUNTRY_BTN = "#country-dropdown-panel-button"
SEL_COPILOT_OPTIN = 'input[name="user_signup[copilot_opt_in]"]'
SEL_SUBMIT = 'button[type="submit"]:has-text("Create account")'
SEL_CAPTCHA_BOX = "#captcha-container-nux"

# 阶段常量（返回给编排层判断推进到哪一步）
STAGE_LOADED = "loaded"
STAGE_EMAIL_FILLED = "email_filled"
STAGE_FORM_FILLED = "form_filled"
STAGE_SUBMITTED = "submitted"

# 终态常量
TERM_CAPTCHA = "reached_captcha"
TERM_VERIFY_EMAIL = "verify_email"   # 未出验证码，GitHub 直接进「输入 launch code」邮箱验证页
TERM_REJECTED = "rejected_by_github"
TERM_UNKNOWN = "unknown"

# 注册页打不开时的**具体原因**。三种都是 GitHub 侧的临时状况，与账号本身无关，
# 编排层据此把账号留在可重试状态，而不是判成「注册失败」。
BLOCK_RESTRICTED = "access_restricted"      # 反滥用拦截页（按出口 IP）
BLOCK_UNAVAILABLE = "github_unavailable"    # GitHub 503「独角兽」页
BLOCK_BLANK = "blank_page"                  # 页面全空，什么都没渲染出来
BLOCK_NONE = ""                             # 页面正常，只是没等到邮箱框

# 拦截页正文里的 IP，形如 "Automated (bot) activity on your network (IP 1.2.3.4)"。
# 抓出来是为了让日志能指认是**哪个代理**被 GitHub 标记了——没有它，
# 面对一批失败账号根本不知道该换哪条代理。
_RE_BLOCK_IP = re.compile(r'\(IP\s+([0-9a-fA-F.:]+)\)')


def _human_type(page, selector, text):
    """逐字符输入，规避一次性 fill 粘贴的机器特征。先聚焦点击再敲字符。"""
    loc = page.locator(selector)
    loc.click(timeout=15000)
    loc.press_sequentially(text, delay=90)


def _field_error(page, selector):
    """读取校验错误容器的可见文本；无可见错误返回 ''。"""
    try:
        loc = page.locator(selector)
        if loc.count() == 0:
            return ""
        if not loc.first.is_visible():
            return ""
        return (loc.first.inner_text() or "").strip()
    except Exception:
        return ""


def classify_block(session):
    """邮箱框没出现时，判断到底卡在什么页面上。返回 (BLOCK_*, 说明文本)。

    存在的理由是「同一个现象、三种完全不同的成因」：反滥用拦截、GitHub 503、
    以及页面确实变了。此前这三种一律报「邮箱输入框未出现，页面结构可能已变」，
    那句话把人直接引向选择器排查，而实测 40 张截图里 17 张是前两种——
    去改选择器一辈子也修不好，因为选择器根本没问题。

    判据取页面正文关键字而不是 DOM 结构：这三张都是 GitHub 的静态错误页，
    没有稳定的 id/class 可依赖，正文措辞反而是最不容易变的部分。
    """
    try:
        body = (session.page.inner_text('body') or '').strip()
    except Exception:
        body = ''
    low = body.lower()

    if 'access is temporarily restricted' in low or 'unusual activity' in low:
        m = _RE_BLOCK_IP.search(body)
        ip = m.group(1) if m else ''
        detail = f"GitHub 反滥用拦截（出口 IP {ip} 被标记为自动化流量）" if ip \
            else "GitHub 反滥用拦截（未能从页面解析出被标记的 IP）"
        return BLOCK_RESTRICTED, detail

    if 'no server is currently available' in low:
        return BLOCK_UNAVAILABLE, "GitHub 服务暂不可用（503 独角兽页）"

    if not body:
        return BLOCK_BLANK, "页面完全空白，未渲染出任何内容"

    return BLOCK_NONE, f"页面已加载但邮箱框未出现，正文开头: {body[:120]}"


def open_signup(session):
    """导航到 signup 页并等待邮箱框可见。

    返回 (ok, block, detail)：ok 为 True 时 block 为 BLOCK_NONE。
    失败时 block 指明是 GitHub 拦截 / 服务不可用 / 空白页 / 还是页面真的变了——
    编排层要靠它区分「账号有问题」和「这次运气不好」。
    """
    print("打开 GitHub 注册页...")
    _safe_goto(session, SIGNUP_URL)
    session.capture_frame()
    if _wait_visible(session.page.locator(SEL_EMAIL), timeout=30000):
        return True, BLOCK_NONE, ""
    block, detail = classify_block(session)
    print(f"  ❌ {detail}")
    return False, block, detail


def fill_signup_form(session, email, password, username):
    """按 GitHub 单页表单顺序填写 email / password / username。

    每填一个关键字段后停顿，让页面异步校验（邮箱查重、用户名查重、Country 自动填充）。
    email 填完立即检查是否被拒，被拒则短路返回，不再填后续字段。

    返回 dict：
      {"stage": <STAGE_*>, "rejected": bool, "reject_reason": str, "country": str}
    """
    page = session.page
    result = {"stage": STAGE_LOADED, "rejected": False, "reject_reason": "", "country": ""}

    # 1) Email
    print(f"填写邮箱: {email}")
    _human_type(page, SEL_EMAIL, email)
    page.locator(SEL_EMAIL).blur()
    time.sleep(3)  # 等异步查重
    err = _field_error(page, SEL_EMAIL_ERR)
    if err:
        result["rejected"] = True
        result["reject_reason"] = f"邮箱被拒: {err}"
        print(f"  ❌ {result['reject_reason']}")
        return result
    result["stage"] = STAGE_EMAIL_FILLED
    print("  ✅ 邮箱通过初步校验")

    # 2) Password
    print("填写密码...")
    _human_type(page, SEL_PASSWORD, password)
    page.locator(SEL_PASSWORD).blur()
    time.sleep(1)

    # 3) Username
    print(f"填写用户名: {username}")
    _human_type(page, SEL_USERNAME, username)
    page.locator(SEL_USERNAME).blur()
    time.sleep(3)  # 等用户名查重
    uerr = _field_error(page, SEL_USERNAME_ERR)
    if uerr:
        result["rejected"] = True
        result["reject_reason"] = f"用户名被拒: {uerr}"
        print(f"  ❌ {result['reject_reason']}")
        return result

    # 4) Country —— 通常已按 IP 自动填充；仅记录，不强制干预
    try:
        result["country"] = (page.locator(SEL_COUNTRY_BTN).inner_text() or "").strip()
        print(f"  Country/Region: {result['country'] or '(未自动填充)'}")
    except Exception:
        pass

    result["stage"] = STAGE_FORM_FILLED
    print("  ✅ 表单字段填写完成")
    return result


def submit(session, wait_button_timeout=15000):
    """等待 Create account 按钮可点后点击提交。

    返回 True 表示已点击提交；False 表示按钮始终 disabled/不可点（通常意味某字段未通过校验）。
    """
    page = session.page
    btn = page.locator(SEL_SUBMIT)
    if not _wait_visible(btn, timeout=wait_button_timeout):
        print("  ❌ 未找到 Create account 按钮")
        return False

    # 按钮初始 disabled，轮询等它 enabled（三字段校验通过后由页面启用）
    deadline = time.time() + wait_button_timeout / 1000
    while time.time() < deadline:
        try:
            if btn.first.is_enabled():
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print("  ❌ Create account 按钮始终不可点击（字段校验未全部通过）")
        return False

    print("点击 Create account 提交...")
    try:
        btn.first.click(timeout=15000)
        session.capture_frame()
        return True
    except Exception as e:
        print(f"  ❌ 点击提交失败: {str(e)[:120]}")
        return False


def _on_verify_email_page(session):
    """判定当前是否已进入「输入邮箱 launch code」验证页。

    信号（任一命中）：URL 含 verify/verification/launch/account_verification；
    或页面出现 launch code 输入框（_CODE_INPUT_CANDIDATES 任一可见）；
    或正文含「launch code / verification code / verify your email / enter the code」等提示。
    """
    page = session.page
    try:
        url = (session.current_url or "").lower()
    except Exception:
        url = ""
    if any(k in url for k in ("account_verification", "/verify", "verification", "launch_code")):
        return True

    # 输入框信号
    for sel in _CODE_INPUT_CANDIDATES:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue

    # 正文提示信号
    try:
        hit = page.evaluate(
            r"""() => {
              const t = (document.body.innerText || '').toLowerCase();
              return /launch code|verification code|verify your email|enter the code|we sent|check your email/.test(t);
            }""")
        if hit:
            return True
    except Exception:
        pass
    return False


def detect_terminal_state(session, timeout=40):
    """提交后判定终态：验证码 / 直接进邮箱验证页 / 被拒 / 未知。

    轮询直到命中或超时：
      - Arkose 验证码容器内出现可见 iframe → TERM_CAPTCHA
      - 进入「输入 launch code」验证页 → TERM_VERIFY_EMAIL（未出验证码，可全自动收码回填）
      - #email-err / #login-err 出现可见文本 → TERM_REJECTED
    返回 dict：{"terminal": <TERM_*>, "detail": str}
    """
    page = session.page
    print(f"等待终态（最长 {timeout}s）：验证码 / 邮箱验证页 / 拒绝 ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 验证码：captcha 容器内出现可见 iframe（Arkose challenge 已加载）
        try:
            frame = page.locator(f"{SEL_CAPTCHA_BOX} iframe")
            if frame.count() > 0 and frame.first.is_visible():
                return {"terminal": TERM_CAPTCHA,
                        "detail": "Arkose 验证码已加载（captcha-container-nux 内出现可见 iframe）"}
        except Exception:
            pass

        # 直接进邮箱验证页（未出验证码）
        if _on_verify_email_page(session):
            return {"terminal": TERM_VERIFY_EMAIL,
                    "detail": f"已进入邮箱验证页（输入 launch code），当前 URL: {session.current_url}"}

        # 拒绝：字段错误
        for sel in (SEL_EMAIL_ERR, SEL_USERNAME_ERR):
            err = _field_error(page, sel)
            if err:
                return {"terminal": TERM_REJECTED, "detail": err}

        time.sleep(1)

    return {"terminal": TERM_UNKNOWN,
            "detail": f"{timeout}s 内未检测到验证码/验证页/拒绝，当前 URL: {session.current_url}"}


# —————————————————————————————————————————————————————————————
# 半自动收尾（semi_auto=True 路径）
#
# 以下 4 个函数处理「人工手动过 Arkose 验证码之后」的流程。GitHub 过码后会展示
# 「输入邮箱 launch code」页面，本项目从未真实到达过该页，其选择器均为**推断**，
# 需要真实到达一次后用 dump_verification_dom() 的产出收敛。
# —————————————————————————————————————————————————————————————

def wait_for_captcha_cleared(session, timeout=300):
    """等待人工手动过掉 Arkose 验证码。

    判定「已过码」的信号（任一命中即返回 True）：
      - captcha 容器（SEL_CAPTCHA_BOX）内不再有可见 iframe（挑战被解掉/收起）；
      - 页面 URL 已离开 signup 页（跳到验证/欢迎页）。
    轮询到超时仍未过码返回 False。

    参数 timeout 为秒。有头模式下由人在场点选验证码。
    """
    page = session.page
    start_url = session.current_url
    print(f"⏳ 等待人工过 Arkose 验证码（最长 {timeout}s）...")
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        # 信号 1：captcha 容器内无可见 iframe
        captcha_gone = False
        try:
            frame = page.locator(f"{SEL_CAPTCHA_BOX} iframe")
            captcha_gone = (frame.count() == 0) or (not frame.first.is_visible())
        except Exception:
            captcha_gone = False

        # 信号 2：已离开 signup 页
        try:
            cur = session.current_url
        except Exception:
            cur = start_url
        left_signup = ("/signup" not in cur) and (cur != start_url)

        if left_signup or captcha_gone:
            print(f"  ✅ 判定已过码（left_signup={left_signup}, captcha_gone={captcha_gone}），当前 URL: {cur}")
            return True

        now = time.time()
        if now - last_log >= 15:
            print(f"  等待中...（剩 {int(deadline - now)}s）", end="\r")
            last_log = now
        time.sleep(2)

    print("\n  ❌ 等待过码超时")
    return False


_VERIF_DUMP_JS = r"""
() => {
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis).map(e => ({
    id: e.id || null,
    name: e.getAttribute('name'),
    type: e.getAttribute('type'),
    autocomplete: e.getAttribute('autocomplete'),
    inputmode: e.getAttribute('inputmode'),
    maxlength: e.getAttribute('maxlength'),
    placeholder: e.getAttribute('placeholder'),
  }));
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit]'))
    .filter(vis).map(b => (b.innerText || b.value || '').trim()).filter(Boolean).slice(0, 20);
  const bodyText = (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 600);
  return { url: location.href, title: document.title, inputs, buttons, bodyText };
}
"""


def dump_verification_dom(session):
    """过码后 dump「输入 launch code」验证页的候选 DOM，供后续收敛选择器。

    如实采集当前 url/title、所有可见 input 的关键属性、可见 button 文本、页面正文片段。
    不做任何断言。返回可 json 序列化的 dict；采集失败返回带 error 的 dict。
    """
    try:
        data = session.page.evaluate(_VERIF_DUMP_JS)
        return data
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}",
                "url": getattr(session, "current_url", None)}


# 验证页 launch code 输入框候选选择器（优先级从高到低）。
# 首项为 2026-07-22 真实到达 github.com/account_verifications 侦察确认：
# 8 个 <input id="launch-code-N" name="launch_code[]" type="number" maxlength="1">（分格 OTP）。
# 其余为跨版本兜底启发式。
_CODE_INPUT_CANDIDATES = [
    'input[name="launch_code[]"]',
    'input[id^="launch-code-"]',
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="launch" i]',
    'input[id*="launch" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[name*="verification" i]',
    'input[inputmode="numeric"]',
]

def _find_verify_submit_button(page):
    """定位验证页的提交按钮，**避开** "Continue with Google/Apple" 社交登录按钮。

    优先级：
      1) launch_code 输入所在 <form> 内的 submit 按钮（最可靠，社交按钮在别的 form）；
      2) 文本严格等于 Continue/Verify/Submit 的按钮（社交按钮文本是 "Continue with X"，不匹配）。
    找不到返回 None。
    """
    # 1) OTP 输入所在 form 内的 submit
    try:
        code_input = page.locator('input[name="launch_code[]"], input[id^="launch-code-"]').first
        if code_input.count() > 0:
            form = code_input.locator("xpath=ancestor::form[1]")
            btn = form.locator('button[type="submit"], input[type="submit"]')
            if btn.count() > 0:
                return btn.first
    except Exception:
        pass
    # 2) 文本严格匹配（排除社交登录的 "Continue with Google" 等）
    for word in ("Continue", "Verify", "Submit"):
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{word}\s*$", re.I))
            if btn.count() > 0:
                return btn.first
        except Exception:
            continue
    return None


def submit_email_code(session, code):
    """把 8 位 launch code 填进验证页并提交。

    选择器未知，按 _CODE_INPUT_CANDIDATES 优先级尝试：
      - 命中「单个」输入框 → 逐字符敲入整串（多数 OTP 组件靠 input 事件自动分格）；
      - 命中「多个」同类框（分格 OTP，每格一位）→ 逐格填一位。
    填完若有可点的提交按钮则点击；无按钮时填满即视为已提交（自动提交型）。

    命中并完成填充返回 True；一个候选都没命中返回 False。
    选择器为推断，真实 DOM 待 dump_verification_dom() 收敛。
    """
    page = session.page
    code = str(code).strip()

    matched_sel = None
    matched_count = 0
    for sel in _CODE_INPUT_CANDIDATES:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
        except Exception:
            continue
        if cnt <= 0:
            continue
        try:
            if not loc.first.is_visible():
                continue
        except Exception:
            continue
        matched_sel = sel
        matched_count = cnt
        break

    if matched_sel is None:
        print("  ❌ 验证页未命中任何 launch code 输入框候选（选择器待真实 DOM 收敛）")
        return False

    print(f"  命中验证码输入框: {matched_sel}（count={matched_count}）")
    try:
        loc = page.locator(matched_sel)
        if matched_count >= len(code):
            # 分格 OTP：聚焦第一格后逐字符敲整串——GitHub 会自动逐格进焦，
            # 这样按原生 keydown/input 事件推进，受控组件才会识别到「已满」并启用 Continue。
            # （逐格 fill 只设 value 不派发完整事件，Continue 会保持 disabled。）
            first = loc.first
            first.click(timeout=10000)
            first.press_sequentially(code, delay=110)
        else:
            # 单个输入框：逐字符敲整串
            _human_type(page, matched_sel, code)
    except Exception as e:
        print(f"  ❌ 填入验证码失败: {str(e)[:160]}")
        return False

    # 提交：轮询等 OTP 表单的提交按钮 enabled 后点击（GitHub 填满 8 位才启用 Continue）。
    # 用 form 内定位，避免误点 "Continue with Google" 社交登录按钮跳到 Google OAuth。
    deadline = time.time() + 12
    clicked = False
    while time.time() < deadline and not clicked:
        btn = _find_verify_submit_button(page)
        if btn is not None:
            try:
                if btn.is_visible() and btn.is_enabled():
                    print("  点击 OTP 表单提交按钮（form 内 Continue）")
                    btn.click(timeout=10000)
                    session.capture_frame()
                    clicked = True
                    break
            except Exception:
                pass
        time.sleep(0.5)

    if not clicked:
        # 兜底：在验证码输入框上按 Enter（很多 OTP 页回车即提交），不用键盘全局 Enter 以免焦点跑偏
        print("  未等到可点提交按钮，尝试在输入框按 Enter 提交")
        try:
            page.locator(matched_sel).last.press("Enter")
        except Exception:
            pass

    session.capture_frame()
    return True


def detect_account_created(session, timeout=15):
    """判定 launch code 提交后账号是否已创建。

    GitHub 邮箱验证通过后会**创建账号并跳到登录页**，顶部绿条
    "Your account was created successfully! Please sign in to continue."。
    信号（任一命中）：正文含该提示；或 host=github.com 且 path 以 /login 开头。
    返回 {"created": bool, "url": str}。
    """
    page = session.page
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = session.current_url
        except Exception:
            url = ""
        try:
            host = (urlparse(url).hostname or "").lower()
            path = (urlparse(url).path or "").lower()
        except Exception:
            host, path = "", ""

        banner = False
        try:
            banner = bool(page.evaluate(
                r"""() => /your account was created successfully/i.test(document.body.innerText || '')"""))
        except Exception:
            banner = False

        if banner or (host.endswith("github.com") and path.startswith("/login")):
            return {"created": True, "url": url}
        time.sleep(1)

    try:
        url = session.current_url
    except Exception:
        url = ""
    return {"created": False, "url": url}


# GitHub 登录页选择器
SEL_LOGIN_FIELD = "#login_field"          # 用户名或邮箱
SEL_LOGIN_PASSWORD = "#password"
SEL_LOGIN_SUBMIT = 'input[name="commit"], button[type="submit"]'


def login_after_signup(session, login_id, password):
    """在「账号已创建，请登录」页用用户名/密码登录（best-effort）。

    登录后可能：直接进已登录区 / 触发新设备邮箱二次验证（github.com/sessions/verified-device，
    仍是 8 位邮箱码）/ 回到登录页报错。
    返回 dict：{"ok": bool, "needs_device_verification": bool, "url": str, "detail": str}
    """
    page = session.page
    out = {"ok": False, "needs_device_verification": False, "suspended": False,
           "url": "", "detail": ""}

    if not _wait_visible(page.locator(SEL_LOGIN_FIELD), timeout=15000):
        out["detail"] = "登录页用户名输入框未出现"
        out["url"] = session.current_url
        return out

    print(f"  用新凭据登录: {login_id}")
    _human_type(page, SEL_LOGIN_FIELD, login_id)
    _human_type(page, SEL_LOGIN_PASSWORD, password)
    try:
        page.locator(SEL_LOGIN_SUBMIT).first.click(timeout=10000)
        session.capture_frame()
    except Exception as e:
        out["detail"] = f"点击 Sign in 失败: {str(e)[:120]}"
        out["url"] = session.current_url
        return out

    # 等页面转移
    deadline = time.time() + 20
    while time.time() < deadline:
        url = session.current_url
        low = url.lower()
        if "/suspended" in low:
            out["suspended"] = True
            out["url"] = url
            out["detail"] = "账号登录后被 GitHub 反滥用立即挂起（/suspended）"
            return out
        if "verified-device" in low or "two-factor" in low or "device" in low:
            out["needs_device_verification"] = True
            out["url"] = url
            out["detail"] = "登录触发新设备邮箱验证"
            return out
        done = detect_signup_complete(session, timeout=1)
        if done["complete"]:
            out["ok"] = True
            out["url"] = done["url"]
            out["detail"] = "登录成功，已进入已登录区域"
            return out
        time.sleep(1)

    out["url"] = session.current_url
    out["detail"] = f"登录后 20s 内未确认状态，停在 {out['url']}"
    return out


def detect_signup_complete(session, timeout=25):
    """判定是否已完成注册进入已登录/onboarding 区域。

    间接信号（任一命中即视为完成）：
      - URL 已离开注册验证流程（github.com 主区，且不含 signup/verif/session/login 片段）；
      - 存在已登录标记：meta[name="user-login"] 有值、dashboard 区域、头像/侧栏菜单。
    仍停在 account_verifications（含 "verif" 片段）一律判未完成，避免把「码没提交成功」误报为完成。
    轮询到 timeout。返回 {"complete": bool, "url": str}。
    """
    page = session.page
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = session.current_url
        except Exception:
            url = ""

        # 按真实 host 判断，不能用子串——OAuth 回跳 URL 的 query 里常编码有 github.com，
        # 子串匹配会把 accounts.google.com 误判成 GitHub 页。
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            host, path = "", ""
        url_ok = (host.endswith("github.com")
                  and "signup" not in path
                  and "verif" not in path       # 覆盖 verify / account_verifications
                  and "/session" not in path
                  and "/login" not in path
                  and "/suspended" not in path)  # 被反滥用挂起，不算成功

        nav_ok = False
        try:
            nav_ok = bool(page.evaluate(
                r"""() => {
                  const m = document.querySelector('meta[name="user-login"]');
                  if (m && (m.getAttribute('content') || '').trim()) return true;
                  if (document.querySelector('[aria-label*="dashboard" i]')) return true;
                  if (document.querySelector('[data-target*="deferred-side-panel"]')) return true;
                  if (document.querySelector('img.avatar, .Header-link .avatar')) return true;
                  return false;
                }"""))
        except Exception:
            nav_ok = False

        if url_ok or nav_ok:
            return {"complete": True, "url": url}
        time.sleep(1)

    try:
        url = session.current_url
    except Exception:
        url = ""
    return {"complete": False, "url": url}
