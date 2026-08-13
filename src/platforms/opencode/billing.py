"""opencode.ai zen 控制台充值浏览器流程（Stripe Checkout 自动填卡支付）。

被 services/registration.recharge_account 调用。所有选择器均来自实机验证
（见 scripts/opencode_zen.py 与 memory/opencode-zen-billing.md）。

流程：确保 opencode 登录 → 打开 workspace billing → Enable billing → 跳 Stripe Checkout
→ 选美金币种 → 选 Card → 填卡 + 账单地址 → 取消「保存到 Link」→ 点 Pay → 判定结果。

关键事实：选中 Card 后，卡字段是 checkout.stripe.com 主文档里的普通 input（非跨域
js.stripe.com iframe），可直接用 page 定位填充。
"""
import re
import time
from datetime import datetime

from src.browser.monitor import step as _step
from src.services import captcha as captcha_solver
from src.payments.stripe_checkout import (
    _stripe_frame, _wait_stripe_frame, pick_currency_usd, select_card_method,
    fill_phone_if_present, fill_card_and_address, uncheck_save_info,
    check_ai_agent_consent, _form_ready_state, click_pay,
    _captcha_challenge_present, _captcha_frames_debug,
    _threeds_challenge_present, _count_top_layer_overlays, _threeds_failure_modal,
    _close_threeds_modal, _threeds_challenge_lightbox, _close_challenge_lightbox,
    _DECLINE_HINTS, _THREEDS_CHALLENGE_GRACE_SEC, decline_line,
)

WORKSPACE_RE = re.compile(r'/workspace/(wrk_[A-Za-z0-9]+)')

# 找美金币种块中心坐标（Stripe 币种区默认按 IP 显示 CN¥，需点美金块切换）
def _extract_wid(url):
    m = WORKSPACE_RE.search(url or '')
    return m.group(1) if m else None


def _auto_verify_device(session, monitor, verify_link, timeout=180, since=None):
    """GitHub 新设备邮箱验证（/sessions/verified-device）自动过码。

    登录一个此前没在这台机器/这个指纹环境登录过的账号时，GitHub 会拦在这一页要 8 位
    邮箱验证码。换 AdsPower 后每个账号都是全新环境，所以**这一步几乎必然触发**，
    不能再依赖人工。

    verify_link 是该账号的若安收信链接（accounts.email_verify_link）。没有就没法收码，
    返回 False 由调用方回退到等人工。

    收码与回填复用注册流程那套（wait_for_github_launch_code_ruoanzhu + submit_email_code）——
    同一种 8 位码、同样的分格输入框。

    since：**必须由调用方在提交登录表单之前取**，用来把注册时那封旧 GitHub 码邮件挡在外面。
    这些 hotmail 是长期真实邮箱，注册收过的码一直躺在收件箱里（实测 cunninghamh22 的
    收件箱里就同时有旧的 "[GitHub] Please verify your device"）；不过滤就会拿旧码去填新
    表单，现象是「明明收到了验证码却验证不通过」，而且每轮稳定复现。
    """
    if not verify_link:
        return False
    from src.services.hotmail_inbox import wait_for_github_launch_code_ruoanzhu
    from src.browser import github_signup as gh

    _step(monitor, session, "GitHub 要求新设备邮箱验证，自动收码中…")
    code = wait_for_github_launch_code_ruoanzhu(verify_link, timeout=timeout, since=since)
    if not code:
        _step(monitor, session, f"{timeout}s 内未收到 GitHub 验证码邮件")
        return False

    _step(monitor, session, f"收到验证码 {code}，回填中…")
    if not gh.submit_email_code(session, code):
        _step(monitor, session, "验证码回填失败（输入框未命中）")
        return False

    # 回填后等 GitHub 离开验证页。提交是异步的，立刻判 URL 会读到还没跳转的旧地址。
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        url = (session.current_url or "").lower()
        if "verified-device" not in url and "two-factor" not in url:
            _step(monitor, session, "GitHub 新设备验证已自动完成")
            return True
    _step(monitor, session, "已回填验证码但 60s 内仍停在验证页")
    return False


def _wait_github_verified(session, monitor, timeout=600):
    """等待人工在浏览器完成 GitHub 邮箱/设备验证。期间不关浏览器、不返回。

    人工输入邮箱验证码后 GitHub 会离开 verified-device / two-factor / sessions 验证页
    回到已登录区域，此时视为完成。超时返回 False。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        url = (session.current_url or "").lower()
        if "github.com" in url and not any(
            k in url for k in ["verified-device", "two-factor", "/sessions/", "/login"]
        ):
            _step(monitor, session, "GitHub 验证已完成")
            return True
        # 若人工直接跳到了 opencode，也视为完成
        if "opencode.ai" in url:
            return True
        try:
            session.capture_frame()
        except Exception:
            pass
    return False


def ensure_opencode_session(session, monitor, login_password, email, verify_link=None):
    """确保浏览器处于 opencode 登录态，返回 (workspace_id, detail)。

    未登录时尝试用 GitHub 账号密码自动登录（best-effort）。

    verify_link：该账号的若安收信链接。GitHub 新设备验证会用它自动收码回填；
    不传（或收码失败）才回退到等人工 10 分钟。换 AdsPower 后每个账号都是全新指纹环境，
    新设备验证几乎必然触发，所以这个参数实际上是必需的——缺了它整条流水线会退化成
    「每个账号都停下来等人」。
    """
    session.get("https://opencode.ai/auth")
    time.sleep(3)
    _step(monitor, session, "检查 opencode 登录态")
    wid = _extract_wid(session.current_url)
    if wid:
        return wid, "已登录（复用 profile 登录态）"

    # 未登录：尝试登录 GitHub（opencode 走 GitHub OAuth）
    _step(monitor, session, "opencode 未登录，尝试登录 GitHub")
    from src.browser import github_signup as gh
    session.get("https://github.com/login")
    time.sleep(2)
    if "/login" in (session.current_url or "").lower():
        if not login_password:
            return None, "该账号未保存登录密码，且 profile 未登录，请先手动登录一次"
        # 收码的时间下界必须在**提交登录表单之前**取：GitHub 是在提交那一刻发信的，
        # 取晚了会把刚到的新邮件也判成旧邮件而永远收不到码。
        since = datetime.now()
        login = gh.login_after_signup(session, email, login_password)
        if login.get("suspended"):
            return None, "GitHub 账号被反滥用挂起（/suspended）"
        if login.get("needs_device_verification"):
            # 先自动收码回填；有收信链接时这条几乎总能走通，无需人工。
            if not _auto_verify_device(session, monitor, verify_link, since=since):
                # 回退：不关浏览器，等人工在浏览器里输码（无收信链接的老账号才会走到这）
                _step(monitor, session, "自动过码未成功，请在浏览器手动输入验证码，等待中…")
                if not _wait_github_verified(session, monitor, timeout=600):
                    return None, "GitHub 邮箱/设备验证未完成（自动收码失败且 10 分钟内无人工处理）"
        elif not login.get("ok"):
            return None, f"GitHub 登录未确认：{login.get('detail')}"

    # 回到 opencode 建立会话：走完整 OAuth 链（Continue with GitHub → Authorize →
    # flagged 检测 → provision 重试，见 opencode_login）。此前只重访 /auth 裸等 3 秒取
    # wid——凡 profile 里 opencode 会话 cookie 已过期（GitHub 能重登）的账号必然失败。
    from src.platforms.opencode import login as ol
    _step(monitor, session, "GitHub 登录后建立 opencode 会话（OAuth 链）")
    # open_go=False：充值走 zen 的 /workspace/<wid>/billing，/go 页是订阅流程才要的入口，
    # 这里进它纯属白跑（实机每账号约 34 秒）。
    res = ol.login_and_open_own_go(session, monitor, open_go=False)
    wid = res.get("wid")
    if wid:
        return wid, "GitHub 登录成功并建立 opencode 会话"
    if res.get("flagged"):
        return None, "GitHub 账号被 flagged，无法授权 opencode OAuth"
    return None, f"GitHub 已登录但未能建立 opencode 会话：{(res.get('detail') or '')[:120]}"


_BAL_RE = re.compile(r'\$([0-9]+(?:\.[0-9]+)?)\s*Current Balance')


def _read_balance(session):
    """读 billing 页 Current Balance 数字（美元）。读不到返回 None。"""
    try:
        body = session.page.inner_text("body", timeout=4000) or ""
    except Exception:
        return None
    m = _BAL_RE.search(body)
    return float(m.group(1)) if m else None


def read_current_balance(session, wid, monitor=None):
    """导航到 billing 页读当前 AI Credits 余额（美元），读不到返回 None。

    供充值前「余额 ≥ 阈值即跳过并归档」预检使用：登录拿到 wid 后、进入试卡循环前调用一次，
    以**实时余额**为准（DB 余额会随 credits 消耗过时，不可作归档依据）。
    """
    billing_url = f"https://opencode.ai/workspace/{wid}/billing"
    try:
        session.get(billing_url)
        time.sleep(3)
    except Exception:
        return None
    _step(monitor, session, "读取当前余额")
    return _read_balance(session)


_APIKEY_RE_JS = r"""
() => {
  const m = document.documentElement.outerHTML.match(/sk-[A-Za-z0-9_\-]{20,}/);
  return m ? m[0] : null;
}
"""


def fetch_apikey(session, wid, monitor=None):
    """导航到 /keys 页抓 API key 明文（sk-…），抓不到返回 None。

    页面展示的是打码 key，但 outerHTML 里含完整明文（与 scripts/fetch_apikeys.py
    同一判据）。要求会话已登录；充值/归档后顺手调用可免去事后单独开浏览器补抓。
    """
    try:
        session.get(f"https://opencode.ai/workspace/{wid}/keys")
        time.sleep(3)
        key = session.page.evaluate(_APIKEY_RE_JS)
    except Exception:
        return None
    if key:
        _step(monitor, session, "已抓到 API key")
    return key or None


def start_recharge(session, wid, amount, monitor):
    """打开 billing 页并发起充值。返回 (mode, balance_before)。

    mode:
      "first"  首次充值：点 Enable Billing → 跳 Stripe Checkout（需填新卡）
      "reload" 复充：点 Add Balance → 填金额 → 点 Add（用账号已存卡直接扣款，不再填卡）
      None     两个入口都没找到（返回 balance_before 供上层记录）

    复充优先：已启用计费的账号 billing 页显示 "Add Balance"（+ 已存卡），此时用已存卡
    复充，忽略传入的 card；未启用计费才显示 "Enable Billing"，走 Stripe 填新卡。
    """
    billing_url = f"https://opencode.ai/workspace/{wid}/billing"
    session.get(billing_url)
    time.sleep(3)
    _step(monitor, session, "打开 billing 页")
    page = session.page
    balance_before = _read_balance(session)

    # 复充优先：Add Balance → 填金额 → Add → 页面内嵌入 Stripe Checkout iframe（同首充填卡）
    try:
        add_bal = page.get_by_role("button", name="Add Balance").first
        if add_bal.count():
            add_bal.click(timeout=6000)
            time.sleep(1.5)
            try:
                amt = page.locator("input[type='number']").first
                amt.click(timeout=3000)
                amt.fill("")
                amt.press_sequentially(str(int(amount)), delay=80)
            except Exception:
                pass
            # 表单提交按钮是 "Add"（精确匹配，避开 "Add Balance"）
            page.get_by_role("button", name="Add", exact=True).first.click(timeout=6000)
            _step(monitor, session, f"复充：Add ${int(amount)}，等待 Stripe 支付页…")
            # 复充的 Stripe 是嵌入 iframe（current_url 仍是 opencode），等它出现
            if _wait_stripe_frame(session, timeout=60) is None:
                return (None, balance_before)
            _step(monitor, session, "已进入 Stripe 支付页（复充）")
            return ("reload", balance_before)
    except Exception:
        pass

    # 首充：Enable Billing → Stripe Checkout
    clicked = False
    for sel in ["button:has-text('Enable Billing')", "button:has-text('Enable billing')"]:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=6000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        return (None, balance_before)
    # 等跳转 Stripe（后端创建 checkout session 再 302，耗时波动，给足 60s）
    for _ in range(30):
        if "checkout.stripe.com" in (session.current_url or ""):
            _step(monitor, session, "已进入 Stripe Checkout")
            time.sleep(3)
            return ("first", balance_before)
        session.capture_frame()
        time.sleep(2)
    return (None, balance_before)


def detect_payment_result(session, wid, balance_before, monitor, timeout=120):
    """点 Pay / Add 后判定支付结果。返回 dict{outcome, detail}。

    outcome: "success" | "failed" | "needs_captcha" | "unknown"

    权威判据是 opencode 账户余额是否增加（钱真正到账的唯一凭证），每轮优先判。

    3DS 交互挑战的处理（关键）：检测到挑战弹窗后**不立即判失败**，而是等它加载/完成——
    很多情况下弹窗加载完会自动消失、支付其实成功（余额增长 → success）。仅当出现下列
    「认证失败」信号才判 failed：
      - 挑战期间冒出**新弹窗**（top-layer 遮罩层数量超过初见时的基线）；
      - Stripe 出现明确拒付/认证失败文案（如 unable to authenticate，内容变了）；
      - 3DS 挑战 Lightbox（iframe#challengeFrame，需持卡人在发卡行侧验证）出现且
        _THREEDS_CHALLENGE_GRACE_SEC 内未自动消失 → 点 Cancel 关弹窗后判 failed；
      - 直到超时挑战弹窗**仍未自动消失**（未解决）。
    failed 由上层统一消耗：无条件进冷却（默认 24h）+ 连续失败计数 +1，
    计数达阈值（默认 3）才判 invalid；成功一次计数清零。好卡的豁免靠
    mark_invalid_by_number 底层那道 valid_cards 守卫，不在这里也不在编排层判。

    其它：hCaptcha → 记录并继续等（人工点 Verify 后余额增长即 success），超时未完成 →
    needs_captcha（账号级风控）；都没有 → unknown。
    复充（reload）路径点 Add 后一直在 billing 页，余额可全程读取。
    """
    page = session.page
    billing_url = f"https://opencode.ai/workspace/{wid}/billing"
    deadline = time.time() + timeout
    saw_captcha = False
    saw_3ds = False
    overlay_baseline = 0
    captcha_tries = 0
    challenge_since = None    # 3DS 挑战 Lightbox 首见时刻（宽限计时）

    def _balance_grew():
        if balance_before is None:
            return None
        bal = _read_balance(session)
        if bal is not None and bal > balance_before + 0.001:
            return bal
        return None

    def _stripe_present():
        # 首充整页(main frame url=checkout) 或复充 iframe，均以「存在 checkout.stripe.com
        # frame」判断是否还在支付中——不能只看 current_url（复充时它一直是 opencode）
        for fr in session.page.frames:
            if "checkout.stripe.com" in (fr.url or ""):
                return fr
        return None

    while time.time() < deadline:
        # 每轮优先判余额到账（3DS 自动完成/无感完成/人工过掉后都会在此判成功）
        grew = _balance_grew()
        if grew is not None:
            _step(monitor, session, f"支付成功（余额 ${balance_before}→${grew}）")
            return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}", "balance_after": grew}

        stripe_fr = _stripe_present()

        if stripe_fr is None:
            # Stripe 已关闭（首充跳回 / 复充 iframe 消失）→ 回 billing 再确认一次余额
            if "/billing" not in (session.current_url or ""):
                try:
                    session.get(billing_url)
                    time.sleep(2)
                except Exception:
                    pass
            grew = _balance_grew()
            if grew is not None:
                _step(monitor, session, f"支付成功（余额 ${balance_before}→${grew}）")
                return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}", "balance_after": grew}
        else:
            # 3DS2 结果弹窗「Payment failed」（发卡行拒绝/需额外验证）→ 确定失败：关闭弹窗后
            # 判 failed、换下一张。独立于挑战框 URL 匹配，是最快最可靠的失败信号，故最先查。
            fmfr, fmsg = _threeds_failure_modal(session)
            if fmfr is not None:
                _close_threeds_modal(fmfr, session, monitor)
                return {"outcome": "failed", "detail": f"3DS 认证失败: {fmsg[:150]}"}
            # 仍在支付：拒付/认证失败文案 → 失败（3DS「内容变了」的失败也走这里，如
            # unable to authenticate your payment method）
            try:
                body = (stripe_fr.inner_text("body", timeout=1500) or "").lower()
                snippet = decline_line(body)
                if snippet:
                    return {"outcome": "failed", "detail": f"拒付/认证失败: {snippet}"}
            except Exception:
                pass
            # 3DS 交互挑战 Lightbox（challengeFrame）：需持卡人在发卡行侧验证，自动化下无人
            # 可点。给 _THREEDS_CHALLENGE_GRACE_SEC 宽限（可能自动放行消失/余额到账判成功），
            # 仍在则视为校验不通过：点 Cancel 关掉弹窗后判 failed 换下一张。
            ch_fr = _threeds_challenge_lightbox(session)
            if ch_fr is not None:
                if not saw_3ds:      # 挑战出现 = captcha 关已过，同样禁止回头解 hCaptcha
                    saw_3ds = True
                    overlay_baseline = max(_count_top_layer_overlays(session), 1)
                if challenge_since is None:
                    challenge_since = time.time()
                    _step(monitor, session, "检测到 3DS 挑战弹窗（challengeFrame），等待其自动完成…")
                elif time.time() - challenge_since > _THREEDS_CHALLENGE_GRACE_SEC:
                    _close_challenge_lightbox(ch_fr, session, monitor)
                    return {"outcome": "failed",
                            "detail": f"3DS 挑战弹窗 {_THREEDS_CHALLENGE_GRACE_SEC}s 内未自动通过"
                                      "（需持卡人验证），已关闭换卡"}
            else:
                challenge_since = None    # 弹窗消失（自动放行）→ 复位，余额判定接手
            # 3DS 优先：3DS 出现 = 人机验证已过（Stripe 先过 captcha 才进发卡行授权），故一旦
            # 见过 3DS 就绝不回头解 hCaptcha——否则常驻 invisible hCaptcha 的 checkbox iframe
            # （含「i am human」文案）会被反复误判。3DS 交互挑战等待加载/完成，不立即失败。
            if _threeds_challenge_present(session):
                if not saw_3ds:
                    saw_3ds = True
                    # 基线下限取 1：挑战本身就在一个 top-layer 遮罩里；若初见时遮罩尚未渲染
                    # （计数 0），不把随后正常渲染出的这一个误判为「新弹窗」。
                    overlay_baseline = max(_count_top_layer_overlays(session), 1)
                    _step(monitor, session, "检测到 3DS 交互挑战，等待其加载完成…")
                # 挑战期间遮罩层数超过基线（冒出新弹窗）→ 认证失败
                elif _count_top_layer_overlays(session) > overlay_baseline:
                    _step(monitor, session, "3DS 期间出现新弹窗，判定认证失败，换下一张")
                    return {"outcome": "failed", "detail": "3DS 出现新弹窗，认证失败"}
            # hCaptcha 人机验证：仅在尚未进入 3DS 阶段时才解。点 Pay 后 Stripe 风控可能弹出，
            # 用 multibot/2captcha 自动解 token（最多 3 次，镜像订阅流程）；3 次仍未过提前返回
            # needs_captcha（不空等，换下一张卡）。solver 不可用时回退旧行为——提示人工点 Verify。
            elif not saw_3ds and _captcha_challenge_present(session) is not None:
                if not saw_captcha:
                    saw_captcha = True
                    _step(monitor, session, "检测到 hCaptcha 人机验证")
                if captcha_solver.is_available() and captcha_tries < 3:
                    captcha_tries += 1
                    _step(monitor, session, f"用 solver 自动解 hCaptcha（第 {captcha_tries} 次）…")
                    try:
                        if captcha_solver.solve_hcaptcha(session):
                            _step(monitor, session, "hCaptcha token 已注入，等待支付结果…")
                            time.sleep(4)
                    except Exception as e:
                        print(f"  hCaptcha 解题异常: {str(e)[:120]}", flush=True)
                elif captcha_solver.is_available() and captcha_tries >= 3:
                    # 解 3 次仍卡在 hCaptcha（token 被拒/账号级风控）——提前收手换下一张卡
                    _step(monitor, session, "hCaptcha 解 3 次仍未过，提前收手（换下一张卡）")
                    return {"outcome": "needs_captcha",
                            "detail": "hCaptcha 解 3 次仍未通过（token 被拒/账号级风控）"}
                elif not captcha_solver.is_available():
                    _step(monitor, session, "未配置 solver，请在浏览器手动点 Verify 完成…")

        time.sleep(3)

    # 超时收尾：先看余额（可能刚好在最后一刻到账）
    grew = _balance_grew()
    if grew is not None:
        _step(monitor, session, f"支付成功（余额 ${balance_before}→${grew}）")
        return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}", "balance_after": grew}
    if saw_captcha:
        return {"outcome": "needs_captcha",
                "detail": f"hCaptcha 人机验证未在 {timeout}s 内完成（账号级风控，需人工）"}
    if saw_3ds:
        # 挑战弹窗直到超时仍未自动消失（未解决）→ 认证失败。若有结果弹窗顺手关掉。
        fmfr, fmsg = _threeds_failure_modal(session)
        if fmfr is not None:
            _close_threeds_modal(fmfr, session, monitor)
            return {"outcome": "failed", "detail": f"3DS 认证失败: {fmsg[:150]}"}
        return {"outcome": "failed",
                "detail": f"3DS 交互挑战 {timeout}s 内未自动完成（弹窗未消失），认证失败"}
    # 判据收紧后「没解题」是常态（页面本就没有验证码）。但万一真挑战因 hCaptcha 结构变化
    # 不再被识别，现象同样是静默不解题——把当时的 hCaptcha 帧结构记一笔，让这种失效可查。
    frames = _captcha_frames_debug(session)
    if frames:
        # 实测（2026-08-03）：Stripe 结账页会**预建**空的 frame=challenge 壳，同时挂
        # frame=checkbox-invisible。所以看到 challenge 帧不代表出现了图像挑战——
        # 判据是帧内有没有渲染出题面，见 _captcha_challenge_present。
        print(f"  [诊断] 超时收尾时的 hCaptcha 帧: {frames}", flush=True)
    return {"outcome": "unknown", "detail": f"{timeout}s 内余额未增加，未确认成功"}


# 首充的固定金额（美元）。**站点定的，我们改不了**：billing 页的 "Enable Billing"
# 只是跳到后端预先建好的 Stripe Checkout，金额在那个 session 里已经写死，页面上没有
# 任何可填金额的地方（见 start_recharge 的首充分支——它压根没用 amount 参数）。
# 只有复充（"Add Balance" → 金额输入框）才认我们传的金额。
#
# 这个常量的用处是让 recharge_via_stripe 能**如实回报实扣金额**，否则上层会拿
# 「想充多少」去记账：2026-08-04 线上出现过账面 $79、实扣 $20 的记录。
FIRST_TOPUP_AMOUNT = 20.0


def recharge_via_stripe(session, card, wid, amount=20, monitor=None, should_stop=None):
    """完整充值编排。返回 dict{ok, outcome, err, last4, mode, amount, steps}。

    自动区分两种路径（由 billing 页当前入口决定）：
      - mode="first"：首充，走 Stripe Checkout 填 card 参数的卡 + 点 Pay。
        **金额固定 FIRST_TOPUP_AMOUNT，传入的 amount 无效**（站点限制，见该常量注释）。
      - mode="reload"：复充，start_recharge 已用账号已存卡点了 Add，直接判定余额。
        金额就是传入的 amount。

    返回的 `amount` 是**实际扣款额**，不是请求额——两者在首充时不同，上层按它记账。
    outcome：success / failed（拒付或 3DS 交互挑战）/ needs_captcha / unknown（同
    detect_payment_result）；另有 error —— 付款前的页面/基础设施故障（未找到入口 / 选卡失败 /
    填卡失败 / 点 Pay 失败），非卡问题，上层据此不消耗该卡、留待重试。
    ok 仅在 success 时为 True。成功判据是余额增加（见 detect_payment_result）。
    """
    steps = []
    last4 = str(card.get("number", ""))[-4:]

    def stop_check():
        if should_stop and should_stop():
            raise InterruptedError("用户请求停止")

    stop_check()
    mode, balance_before = start_recharge(session, wid, amount, monitor)
    if mode is None:
        # 页面/导航故障（非卡问题）→ outcome="error"：上层不判卡无效、不冷却、不消耗，留待重试
        return {"ok": False, "outcome": "error", "mode": None,
                "err": "未找到 Enable Billing / Add Balance 入口，或未跳转 Stripe",
                "last4": last4, "steps": steps}
    steps.append(f"start:{mode}")

    # 首充(整页 Stripe)和复充(iframe Stripe)到达支付页后填卡流程相同——
    # 所有表单操作 frame-aware（_stripe_frame 自动定位到 Stripe 所在 frame）。
    stop_check()
    steps.append("currency:" + pick_currency_usd(session, monitor))

    stop_check()
    if not select_card_method(session, monitor):
        # 页面故障（Stripe 表单未就绪等，非卡问题）→ error，不消耗卡
        return {"ok": False, "outcome": "error", "mode": mode,
                "err": "选中 Card 支付方式失败", "last4": last4, "steps": steps}
    steps.append("select_card")

    stop_check()
    fill_ok, fill_detail = fill_card_and_address(session, card, monitor)
    steps.append(f"fill:{fill_detail or 'ok'}")
    if not fill_ok:
        # 填卡未完成：可能是页面故障也可能卡数据异常，一律按 error 处理不消耗卡（宁可留卡重试，
        # 也不因表单问题误烧卡）。真·坏卡会在提交后由 detect_payment_result 判 failed 再消耗。
        return {"ok": False, "outcome": "error", "mode": mode,
                "err": f"填卡失败: {fill_detail}", "last4": last4, "steps": steps}

    # 填必填电话（复充页 phone_number_collection，缺它表单不完成 Pay 被拦）
    fill_phone_if_present(session, card, monitor)
    uncheck_save_info(session, monitor)  # 用户要求：不勾选「保存我的信息」
    check_ai_agent_consent(session, monitor)  # 复充页可能要求勾选 AI agent 声明
    steps.append("phone+uncheck+consent")

    # 提交前确认表单已完成（SubmitButton 脱离 --incomplete），否则记录以便定位缺项
    steps.append("form:" + _form_ready_state(session))

    stop_check()
    pay_ok, pay_detail = click_pay(session, monitor)
    steps.append(f"click_pay:{pay_detail}")
    if not pay_ok:
        # 点 Pay 未成功（按钮未就绪/页面故障，非卡问题）→ error，不消耗卡
        return {"ok": False, "outcome": "error", "mode": mode,
                "err": f"点击 Pay 失败: {pay_detail}", "last4": last4, "steps": steps}

    result = detect_payment_result(session, wid, balance_before, monitor)
    steps.append(f"result:{result['outcome']}")
    return {
        "ok": result["outcome"] == "success",
        "outcome": result["outcome"],
        "mode": mode,
        "err": "" if result["outcome"] == "success" else result["detail"],
        "last4": last4,
        # 成功时携带充值后余额（美元），供上层落库刷新 accounts.credits_balance；
        # 非成功为 None（detect_payment_result 只有余额增长才判 success，故成功必有值）
        "balance_after": result.get("balance_after"),
        # 实际扣款额。首充由站点定死，与请求额无关——上层照请求额记账就会账实不符。
        "amount": FIRST_TOPUP_AMOUNT if mode == "first" else amount,
        "steps": steps,
    }
