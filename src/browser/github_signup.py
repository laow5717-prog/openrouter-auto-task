"""GitHub 注册页（https://github.com/signup）页面操作层。

职责边界：只封装 GitHub signup 页的选择器与分步填表/终态判定，供
`services/github_signup_service.py` 编排调用。仅复用 driver.py 的通用 helper
（create_driver / _safe_goto / _wait_visible 等），不含任何 Cloudflare / opencode 语义，
与 driver.py 中标注 LEGACY 的方法群完全隔离。

选择器依据：task research/github-signup-dom.md（2026-07-22 实跑侦察确认）。
GitHub signup 为**单页全字段**表单（email/password/username/country/checkbox 一开始即可见），
非逐字段揭示；Create account 按钮初始 disabled，三字段校验通过后才 enabled。
"""
import time

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
TERM_REJECTED = "rejected_by_github"
TERM_UNKNOWN = "unknown"


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


def open_signup(session):
    """导航到 signup 页并等待邮箱框可见。返回 True/False。"""
    print("打开 GitHub 注册页...")
    _safe_goto(session, SIGNUP_URL)
    session.capture_frame()
    ok = _wait_visible(session.page.locator(SEL_EMAIL), timeout=30000)
    if not ok:
        print("  ❌ 邮箱输入框未出现，页面结构可能已变")
    return ok


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


def detect_terminal_state(session, timeout=40):
    """提交后判定终态：出现验证码 / 被拒 / 未知。

    轮询直到命中或超时：
      - Arkose 验证码容器内出现可见 iframe → TERM_CAPTCHA
      - #email-err / #login-err 出现可见文本 → TERM_REJECTED
    返回 dict：{"terminal": <TERM_*>, "detail": str}
    """
    page = session.page
    print(f"等待终态（最长 {timeout}s）：验证码 / 拒绝 ...")
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

        # 拒绝：字段错误
        for sel in (SEL_EMAIL_ERR, SEL_USERNAME_ERR):
            err = _field_error(page, sel)
            if err:
                return {"terminal": TERM_REJECTED, "detail": err}

        time.sleep(1)

    return {"terminal": TERM_UNKNOWN,
            "detail": f"{timeout}s 内未检测到验证码或明确拒绝，当前 URL: {session.current_url}"}


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


# 验证页 launch code 输入框候选选择器（优先级从高到低；均为推断，待真实 DOM 收敛）
_CODE_INPUT_CANDIDATES = [
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

# 提交按钮候选（很多 OTP 页填满自动提交，无按钮时填完即视为已提交）
_CODE_SUBMIT_CANDIDATES = [
    'button[type="submit"]',
    'button:has-text("Verify")',
    'button:has-text("Continue")',
    'button:has-text("Submit")',
]


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
            # 分格 OTP：每格填一位
            for i, ch in enumerate(code):
                box = loc.nth(i)
                box.click(timeout=10000)
                box.press_sequentially(ch, delay=90)
        else:
            # 单个输入框：逐字符敲整串
            _human_type(page, matched_sel, code)
    except Exception as e:
        print(f"  ❌ 填入验证码失败: {str(e)[:160]}")
        return False

    # 尝试点提交（无按钮/点不动则视为自动提交型，填满即已提交）
    for sel in _CODE_SUBMIT_CANDIDATES:
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
                print(f"  点击提交按钮: {sel}")
                btn.first.click(timeout=10000)
                session.capture_frame()
                break
        except Exception:
            continue
    else:
        print("  （未见可点提交按钮，按自动提交型处理）")

    session.capture_frame()
    return True


def detect_signup_complete(session, timeout=20):
    """判定是否已完成注册进入已登录区域。

    间接信号（任一命中即视为完成）：
      - URL 跳到 github.com 主区，且不再含 /signup 或 verify/session 路径；
      - 存在已登录导航标记：meta[name="user-login"] 有值、dashboard 区域、头像/侧栏菜单。
    轮询到 timeout。返回 {"complete": bool, "url": str}。
    """
    page = session.page
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = session.current_url
        except Exception:
            url = ""

        url_ok = ("github.com" in url
                  and "/signup" not in url
                  and "verify" not in url
                  and "/session" not in url)

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
