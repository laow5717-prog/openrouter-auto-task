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
from src.services.adspower import AdsPowerError
from src.browser import github_signup as gh
from src.services.email import create_temp_email, wait_for_github_launch_code
from src.services.hotmail_inbox import wait_for_github_launch_code_ruoanzhu

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


def _collect_and_fill_code(session, fetch_code, result):
    """已在邮箱验证页时的收尾：侦察验证页 DOM → 收 launch code → 回填 → 判定完成。

    就地更新 result 的 outcome/reason/ok。fetch_code 为零参可调用，返回验证码或 None——
    mail.tm 路径闭包了 (token, since_ts)，hotmail 路径闭包了 ruoanzhu 收信链接；收尾逻辑
    对邮箱源无感。收两次码（注册确认 + 新设备验证）都走同一 fetch_code。
    """
    # 1) 侦察验证页真实 DOM（该页选择器为推断，dump 一次便于收敛；即便回填失败也留下真实 DOM）
    dom = gh.dump_verification_dom(session)
    print(f"验证页 DOM 侦察: {json.dumps(dom, ensure_ascii=False)[:800]}")

    # 2) 收 GitHub 验证码邮件
    print("=== 收 GitHub 验证码邮件 ===")
    code = fetch_code()
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
        code2 = fetch_code()
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


def _finish_semi_auto(session, fetch_code, result):
    """验证码出现后的半自动收尾：先等人工手动过 Arkose，过码后走通用收码回填。"""
    if not gh.wait_for_captcha_cleared(session, timeout=300):
        result["outcome"] = "captcha_timeout"
        result["reason"] = "等待人工过验证码超时（5 分钟内未通过）"
        return
    _collect_and_fill_code(session, fetch_code, result)


def signup_one(headless=False, semi_auto=False, keep_open=False, account=None,
               post_provision=None, auto_skip_captcha=False, proxy=None,
               browser_factory=None):
    """执行一次 GitHub 注册。

    本模块是**身份供给层**：产出的是一个通用 GitHub 账号，任何走 GitHub OAuth 的平台
    都能复用同一个身份。它不知道有哪些平台存在——要在注册完顺手预热某平台的会话，
    由调用方把那个平台的 adapter 传进 post_provision。

    semi_auto=False：只跑到 Arkose 验证码出现为止（outcome=reached_captcha）。
    semi_auto=True：跑到验证码后**暂停等人工手动过码**，通过后自动收验证邮件、
                    回填 launch code、判定注册是否完成。需有头模式且有人在场过码。
    auto_skip_captcha=True：**全自动**——不弹 Arkose 时自动收码完成注册（signup_complete）；
                    弹 Arkose 时立即返回 reached_captcha 跳过、绝不等人工。用于批量注册。
    keep_open=True：流程跑完后**不关浏览器**，进程挂住（Ctrl-C 结束），供人肉眼查看最终页面。
    account：可选 HotmailAccount（含 email/password/link）。
             - None（默认）：走原 mail.tm 临时邮箱路径，收码走 mail.tm API。
             - 提供时：用该 hotmail 邮箱注册，收码走 ruoanzhu 收信链接，浏览器用
               以 email 命名的持久 profile（固定指纹环境，降挂起风险）。
    post_provision：可选 PlatformAdapter。注册成功（signup_complete）后在同一浏览器
             session 里顺手建立该平台的会话（省一次冷启动），结果写入
             result['post_provision']。传 None 则只注册 GitHub。
    browser_factory：可选 callable(email) -> BrowserSession，用于替换默认的本地
             Chrome 启动方式（AdsPower 指纹浏览器接入即走这里）。为 None 时行为不变。
             proxy 参数在有 factory 时被忽略——代理由 factory 那一侧绑定。

    返回 dict：
      ok:             bool     半自动模式下 = 注册完成；默认模式下 = 推进到验证码
      email:          str|None 注册邮箱（mail.tm 临时箱或 hotmail 地址）
      email_password: str|None 邮箱登录密码（mail.tm 密码或 hotmail 密码）
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
        "final_url": None, "post_provision": None,
    }

    use_hotmail = account is not None
    # 1) 邮箱：hotmail 用现成账号，否则建 mail.tm 临时箱
    if use_hotmail:
        print("=== 步骤 1/4：使用 hotmail 邮箱（ruoanzhu 收码）===")
        address, mail_pw = account.email, account.password
        token = None
        if not account.link:
            result["reason"] = f"hotmail 账号 {address} 缺少 ruoanzhu 收信链接，无法收码"
            print(f"  ❌ {result['reason']}")
            return result
    else:
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
    # hotmail 是长期真实邮箱，收件箱里可能留着上一次注册/设备验证的旧 GitHub 码。
    # 若安收信页的时间是本地时区的 naive 字符串，故另取一个本地时刻作下界。
    since_local = datetime.now()

    # 收码闭包：两条路径都收敛到零参可调用，收尾逻辑对邮箱源无感。
    if use_hotmail:
        def fetch_code():
            return wait_for_github_launch_code_ruoanzhu(account.link, since=since_local)
    else:
        def fetch_code():
            return wait_for_github_launch_code(token, since_ts)

    session = None
    try:
        # 2) 起浏览器 + 打开注册页（hotmail 用以 email 命名的持久 profile）
        print("=== 步骤 2/4：打开 GitHub 注册页 ===")
        if browser_factory is not None:
            session = browser_factory(address)
        else:
            session = create_driver(headless=headless,
                                    profile_id=(address if use_hotmail else None),
                                    proxy=proxy)
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
            # auto_skip_captcha：弹 Arkose 即返回 reached_captcha 跳过，绝不等人工
            # （全自动批量注册用；不弹 Arkose 的账号仍会走 VERIFY_EMAIL 分支自动完成）。
            if not semi_auto or auto_skip_captcha:
                result["ok"] = True
                result["outcome"] = "reached_captcha"
                result["reason"] = term["detail"]
                print(f"  ✅ 已推进到验证码：{term['detail']}")
                return result
            # —— 半自动收尾：人工过码 → 收邮件 → 回填 → 判定完成 ——
            _finish_semi_auto(session, fetch_code, result)
            result["final_url"] = session.current_url
            result["screenshot"] = _screenshot(session, address)
        elif term["terminal"] == gh.TERM_VERIFY_EMAIL:
            # 未出验证码，直接进邮箱验证页——无需人工，直接自动收码回填。
            # semi_auto 或 auto_skip_captcha 都走自动收码完成；纯默认模式才停在此。
            if not semi_auto and not auto_skip_captcha:
                result["ok"] = True
                result["outcome"] = "reached_verify_email"
                result["reason"] = term["detail"] + "（默认模式不自动收码；加 --semi-auto 可自动回填）"
                print(f"  ✅ 已进入邮箱验证页：{term['detail']}")
                return result
            print("  未出验证码，直接进入邮箱验证页，自动收码回填...")
            _collect_and_fill_code(session, fetch_code, result)
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

        # 注册成功后可选：同一 session 顺手把目标平台的会话也建好（省一次冷启动）。
        # 平台由调用方注入——身份供给层不该知道有哪些平台存在。
        if post_provision is not None and result["ok"] and result["outcome"] == "signup_complete":
            print(f"=== 续接：建立 {post_provision.display_name} 会话 ===")
            try:
                from src.platforms.base import Credentials
                sess = post_provision.ensure_session(
                    session,
                    Credentials(email=result["email"],
                                login_password=result["github_password"]),
                )
                result["post_provision"] = {"ok": sess.ok, "detail": sess.detail,
                                            "tenant_id": sess.tenant_id}
                if sess.ok:
                    result["final_url"] = session.current_url
                    print(f"  ✅ {post_provision.slug}：{sess.detail}")
                else:
                    print(f"  ⚠️ {post_provision.slug} 会话未建立：{sess.detail}")
            except Exception as e:
                detail = f"{post_provision.slug} 会话异常: {type(e).__name__}: {str(e)[:150]}"
                result["post_provision"] = {"ok": False, "detail": detail}
                print(f"  ⚠️ {detail}")

        return result

    except AdsPowerError:
        # 浏览器根本没起来（AdsPower 配额满 / 客户端没开）——这不是「这个账号注册失败」，
        # 而是基础设施不可用。若在此转成 outcome='error'，上层会把账号标 failed，
        # 于是它退出 imported 池、再也不会被重试，一次配额耗尽就永久废掉一批好账号
        # （2026-08-03 实测：30 个刚导入的账号因此被误标）。原样抛出，由上层区分处理。
        raise

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
            if keep_open:
                print("\n" + "=" * 50)
                print("🔎 keep-open：浏览器保持打开，供你查看最终页面")
                print(f"  outcome:   {result.get('outcome')}")
                print(f"  reason:    {result.get('reason')}")
                try:
                    print(f"  final_url: {session.current_url}")
                except Exception:
                    print(f"  final_url: {result.get('final_url')}")
                print(f"  email:     {result.get('email')}  /  pw: {result.get('email_password')}")
                print(f"  username:  {result.get('username')}  /  gh_pw: {result.get('github_password')}")
                print("  看完后按 Ctrl-C 结束进程即可关闭浏览器")
                print("=" * 50)
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    print("\n收到中断，关闭浏览器...")
            close_driver(session)
