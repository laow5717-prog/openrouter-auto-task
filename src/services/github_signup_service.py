"""GitHub 注册编排层。

串起：建 mail.tm 临时邮箱 → 起 Patchright 浏览器 → 填 GitHub 注册表单 → 提交 →
判定终态（验证码/拒绝/未知）→ 截图 → 关闭浏览器。

两种模式：
- 默认（semi_auto=False）：只推进到「Arkose 验证码出现」为止（不解验证码）。
- 半自动（semi_auto=True）：跑到验证码后暂停等人工手动过码，通过后自动收 mail.tm 验证邮件、
  回填 launch code、判定注册是否完成。

返回结构化结果，outcome 多态可区分各终局。
"""
import os
import re
import time
import json
import random
import string
from datetime import datetime, timezone

from src.browser.driver import create_driver, close_driver
from src.browser import github_signup as gh
from src.services.email import create_temp_email, wait_for_github_launch_code

_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "screenshots",
)


def _gen_username():
    """生成合法 GitHub 用户名：小写字母开头 + 字母数字，无连字符，长度 ~12。

    规则（见 research/github-signup-dom.md）：仅字母数字或单连字符，不以连字符开头/结尾，≤39。
    这里保守只用字母数字，规避连字符边界问题。
    """
    first = random.choice(string.ascii_lowercase)
    rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=11))
    name = first + rest
    assert re.fullmatch(r"[a-z0-9]{1,39}", name), name
    return name


def _gen_password():
    """生成满足 GitHub 强度的密码：≥15 字符，含大小写、数字、符号。"""
    pools = [
        random.choices(string.ascii_uppercase, k=4),
        random.choices(string.ascii_lowercase, k=6),
        random.choices(string.digits, k=4),
        random.choices("!@#$%^&*-_", k=3),
    ]
    chars = [c for pool in pools for c in pool]
    random.shuffle(chars)
    return "".join(chars)  # 17 字符


def _screenshot(session, email):
    os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", email or "unknown")
    path = os.path.join(_SCREENSHOT_DIR, f"gh_signup_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.png")
    try:
        session.page.screenshot(path=path)
        return path
    except Exception as e:
        print(f"  ⚠️ 截图失败: {str(e)[:120]}")
        return None


def _collect_and_fill_code(session, token, since_ts, result):
    """已在邮箱验证页时的收尾：侦察验证页 DOM → 收 launch code → 回填 → 判定完成。

    就地更新 result 的 outcome/reason/ok。since_ts 为收邮件的时间下界（取在提交前，
    保证不漏掉提交后到达的验证邮件；mail.tm 为本次新建收件箱，无历史 GitHub 邮件干扰）。
    """
    # 1) 侦察验证页真实 DOM（该页选择器为推断，dump 一次便于收敛；即便回填失败也留下真实 DOM）
    dom = gh.dump_verification_dom(session)
    print(f"验证页 DOM 侦察: {json.dumps(dom, ensure_ascii=False)[:800]}")

    # 2) 收 GitHub 验证码邮件
    print("=== 收 GitHub 验证码邮件 ===")
    code = wait_for_github_launch_code(token, since_ts)
    if not code:
        result["outcome"] = "no_verification_email"
        result["reason"] = "未收到 GitHub 验证码邮件（可能 GitHub 未向临时邮箱发信）"
        return

    # 3) 回填验证码
    if not gh.submit_email_code(session, code):
        result["outcome"] = "verification_failed"
        result["reason"] = f"收到验证码 {code} 但回填失败（验证页输入框选择器未命中，见上方 DOM dump）"
        return

    # 4) 判定：邮箱验证通过后 GitHub 建号并跳登录页（绿条「account created successfully」）
    created = gh.detect_account_created(session)
    if not created["created"]:
        # 未见「已创建」也未跳登录：可能直接进了 onboarding（也是成功）
        done = gh.detect_signup_complete(session)
        if done["complete"]:
            result["ok"] = True
            result["outcome"] = "signup_complete"
            result["reason"] = f"注册完成，已进入 {done['url']}"
            print(f"  ✅ {result['reason']}")
        else:
            result["outcome"] = "verification_failed"
            result["reason"] = f"已填验证码 {code} 但未确认账号创建，停在 {done['url']}"
            print(f"  ⚠️ {result['reason']}")
        return

    # 账号已创建——核心交付达成。用新凭据自动登录到底（best-effort）。
    result["ok"] = True
    result["outcome"] = "signup_complete"
    result["reason"] = f"账号已创建成功（邮箱已验证），停在登录页 {created['url']}"
    print(f"  ✅ {result['reason']}")

    login = gh.login_after_signup(session, result["username"], result["github_password"])
    if login.get("suspended"):
        # 账号建成即被反滥用挂起——外部限制，非脚本缺陷。判为失败态但区分于异常。
        result["ok"] = False
        result["outcome"] = "account_suspended"
        result["reason"] = ("账号已创建但登录后立即被 GitHub 反滥用挂起（/suspended）。"
                            "根因是外部风控：mail.tm 临时邮箱域名 + 自动化指纹被识别，非脚本缺陷。")
        print(f"  ⚠️ {result['reason']}")
    elif login["ok"]:
        result["reason"] = f"账号已创建并成功登录，已进入 {login['url']}"
        print(f"  ✅ {result['reason']}")
    elif login["needs_device_verification"]:
        # 新设备二次验证：仍是邮箱 8 位码，复用同一收码回填闭环
        print("  登录触发新设备邮箱验证，再收一次码回填...")
        code2 = wait_for_github_launch_code(token, since_ts)
        if code2 and gh.submit_email_code(session, code2):
            done = gh.detect_signup_complete(session)
            if done["complete"]:
                result["reason"] = f"账号已创建，过设备验证后登录成功，已进入 {done['url']}"
                print(f"  ✅ {result['reason']}")
            else:
                result["reason"] = f"账号已创建；设备验证码已填但未确认登录，停在 {done['url']}（账号本身可用）"
                print(f"  ⚠️ {result['reason']}")
        else:
            result["reason"] = f"账号已创建；新设备验证收码/回填未完成（账号本身可用，可手动登录）"
            print(f"  ⚠️ {result['reason']}")
    else:
        result["reason"] = f"账号已创建（核心成功）；自动登录未确认：{login['detail']}（可用凭据手动登录）"
        print(f"  ⚠️ {result['reason']}")


def _finish_semi_auto(session, token, since_ts, result):
    """验证码出现后的半自动收尾：先等人工手动过 Arkose，过码后走通用收码回填。"""
    if not gh.wait_for_captcha_cleared(session, timeout=300):
        result["outcome"] = "captcha_timeout"
        result["reason"] = "等待人工过验证码超时（5 分钟内未通过）"
        return
    _collect_and_fill_code(session, token, since_ts, result)


def signup_one(headless=False, semi_auto=False):
    """执行一次 GitHub 注册。

    semi_auto=False：只跑到 Arkose 验证码出现为止（outcome=reached_captcha）。
    semi_auto=True：跑到验证码后**暂停等人工手动过码**，通过后自动收 mail.tm 验证邮件、
                    回填 launch code、判定注册是否完成。需有头模式且有人在场过码。

    返回 dict：
      ok:             bool     半自动模式下 = 注册完成；默认模式下 = 推进到验证码
      email:          str|None mail.tm 邮箱地址
      email_password: str|None mail.tm 登录密码（供后续收注册验证邮件）
      github_password:str|None 提交给 GitHub 的密码
      username:       str|None
      outcome:        "signup_complete" | "reached_captcha" | "reached_verify_email"
                      | "account_suspended" | "rejected_by_github" | "captcha_timeout"
                      | "no_verification_email" | "verification_failed" | "error"
      reason:         str      人类可读说明
      screenshot:     str|None 终态截图路径
      final_url:      str|None
    """
    result = {
        "ok": False, "email": None, "email_password": None, "github_password": None,
        "username": None, "outcome": "error", "reason": "", "screenshot": None,
        "final_url": None,
    }

    # 1) 临时邮箱
    print("=== 步骤 1/4：创建 mail.tm 临时邮箱 ===")
    address, mail_pw, token = create_temp_email()
    if not address:
        result["reason"] = "创建 mail.tm 邮箱失败"
        print(f"  ❌ {result['reason']}")
        return result
    username = _gen_username()
    github_pw = _gen_password()
    result.update(email=address, email_password=mail_pw,
                  github_password=github_pw, username=username)
    print(f"  邮箱: {address}\n  用户名: {username}\n  GitHub 密码: {github_pw}")

    # 收验证邮件的时间下界：取在提交之前，保证不漏掉提交后到达的验证邮件。
    # mail.tm 为本次新建收件箱，无历史 GitHub 邮件，故不会误取旧码。
    since_ts = datetime.now(timezone.utc)

    session = None
    try:
        # 2) 起浏览器 + 打开注册页
        print("=== 步骤 2/4：打开 GitHub 注册页 ===")
        session = create_driver(headless=headless)
        if not gh.open_signup(session):
            result["reason"] = "GitHub 注册页加载失败（邮箱框未出现）"
            result["screenshot"] = _screenshot(session, address)
            result["final_url"] = session.current_url
            return result

        # 3) 填表
        print("=== 步骤 3/4：填写注册表单 ===")
        form = gh.fill_signup_form(session, address, github_pw, username)
        if form["rejected"]:
            result["outcome"] = "rejected_by_github"
            result["reason"] = form["reject_reason"]
            result["screenshot"] = _screenshot(session, address)
            result["final_url"] = session.current_url
            return result
        if form["stage"] != gh.STAGE_FORM_FILLED:
            result["reason"] = f"表单未填写完成，停在阶段: {form['stage']}"
            result["screenshot"] = _screenshot(session, address)
            result["final_url"] = session.current_url
            return result

        # 4) 提交 + 判终态
        print("=== 步骤 4/4：提交并等待终态 ===")
        if not gh.submit(session):
            result["reason"] = "Create account 按钮不可点击或点击失败"
            result["screenshot"] = _screenshot(session, address)
            result["final_url"] = session.current_url
            return result

        term = gh.detect_terminal_state(session)
        result["final_url"] = session.current_url
        result["screenshot"] = _screenshot(session, address)

        if term["terminal"] == gh.TERM_CAPTCHA:
            if not semi_auto:
                result["ok"] = True
                result["outcome"] = "reached_captcha"
                result["reason"] = term["detail"]
                print(f"  ✅ 已推进到验证码：{term['detail']}")
                return result
            # —— 半自动收尾：人工过码 → 收邮件 → 回填 → 判定完成 ——
            _finish_semi_auto(session, token, since_ts, result)
            result["final_url"] = session.current_url
            result["screenshot"] = _screenshot(session, address)
        elif term["terminal"] == gh.TERM_VERIFY_EMAIL:
            # 未出验证码，直接进邮箱验证页——无需人工，直接自动收码回填。
            if not semi_auto:
                result["ok"] = True
                result["outcome"] = "reached_verify_email"
                result["reason"] = term["detail"] + "（默认模式不自动收码；加 --semi-auto 可自动回填）"
                print(f"  ✅ 已进入邮箱验证页：{term['detail']}")
                return result
            print("  未出验证码，直接进入邮箱验证页，自动收码回填...")
            _collect_and_fill_code(session, token, since_ts, result)
            result["final_url"] = session.current_url
            result["screenshot"] = _screenshot(session, address)
        elif term["terminal"] == gh.TERM_REJECTED:
            result["outcome"] = "rejected_by_github"
            result["reason"] = f"提交后被拒: {term['detail']}"
            print(f"  ❌ {result['reason']}")
        else:
            result["outcome"] = "error"
            result["reason"] = term["detail"]
            print(f"  ⚠️ 未知终态：{term['detail']}")

        return result

    except Exception as e:
        result["outcome"] = "error"
        result["reason"] = f"脚本异常: {type(e).__name__}: {str(e)[:200]}"
        print(f"  ❌ {result['reason']}")
        if session is not None:
            result["screenshot"] = _screenshot(session, address)
            try:
                result["final_url"] = session.current_url
            except Exception:
                pass
        return result
    finally:
        if session is not None:
            close_driver(session)
