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

WORKSPACE_RE = re.compile(r'/workspace/(wrk_[A-Za-z0-9]+)')

# 找美金币种块中心坐标（Stripe 币种区默认按 IP 显示 CN¥，需点美金块切换）
_FIND_USD_JS = r"""
() => {
  const cands = [];
  document.querySelectorAll('button, [role=button], label, div, a').forEach(el => {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 24) return;
    if (!(/\$\s?\d/.test(t) && !/[¥￥]/.test(t) && !/CN/i.test(t))) return;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10) return;
    cands.push({ text: t, x: r.left + r.width/2, y: r.top + r.height/2, area: r.width*r.height });
  });
  if (!cands.length) return null;
  cands.sort((a,b) => a.area - b.area);
  return cands[0];
}
"""

# 判定支付结果时扫描 Stripe 主结账页文本的「明确拒付」信号。
# 只保留强拒付句式——实机（scripts/probe_pay_result.py）确认拒付时主 frame 稳定出现
# "Your card was declined..." / "insufficient funds" 等。刻意不含 "try again" / "error"
# / "failed" / "unable to" 这类宽泛词：点 Pay 后会同时弹 hCaptcha（文案含 "Please try
# again"），宽泛词会把「需人机验证」误判成「拒付」进而错误标卡无效。
_DECLINE_HINTS = [
    "declined", "was declined", "insufficient", "incorrect",
    "card number is", "security code is", "expired", "not be processed",
    "do not honor", "card was not accepted",
    # 实机（2026-07-23 真实卡 1010）终态：主 frame 红字 "We are unable to authenticate
    # your payment method. Please choose a different payment method and try again."
    # ——这是「卡无法验证」的明确失败并提示换卡，归 failed（换下一张卡），不是等待人工。
    "unable to authenticate", "authenticate your payment",
    "choose a different payment",
]

# hCaptcha 人机验证「图像挑战」：仅当 Stripe 真的弹出需人工点选图片的挑战时才归
# needs_captcha（账号级风控、需人工、换卡无用）。判据刻意只认图像挑战的特征文案——
# 不能用裸 "verify" / "try again"：hCaptcha 壳/加载失败态常带这些词（实机 newassets.
# hcaptcha.com frame 文案就是 "Please try again ⚠️ Verify"），会把上面那种「卡无法验证」
# 的普通失败误判成需人工验证码从而错误停手。
_CAPTCHA_TEXT_HINTS = [
    "select each image", "click each image", "select all images",
    "verify you are human", "are you a human", "i am human",
]


def _step(monitor, session, msg):
    """刷新 UI 截图 + 记录步骤；monitor 在停止请求时会抛 InterruptedError。"""
    if monitor:
        monitor(session, msg)
    else:
        session.capture_frame()


def _extract_wid(url):
    m = WORKSPACE_RE.search(url or '')
    return m.group(1) if m else None


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


def ensure_opencode_session(session, monitor, login_password, email):
    """确保浏览器处于 opencode 登录态，返回 (workspace_id, detail)。

    未登录时尝试用 GitHub 账号密码自动登录（best-effort）。遇 GitHub 新设备验证
    等人工环节则返回 (None, 原因)，由上层提示先手动登录一次建立 profile 登录态。
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
        login = gh.login_after_signup(session, email, login_password)
        if login.get("suspended"):
            return None, "GitHub 账号被反滥用挂起（/suspended）"
        if login.get("needs_device_verification"):
            # 用户要求：需邮箱/设备验证时不关浏览器，等待人工在浏览器里完成
            _step(monitor, session, "GitHub 需邮箱/设备验证，请在浏览器手动输入验证码，等待中…")
            if not _wait_github_verified(session, monitor, timeout=600):
                return None, "GitHub 邮箱/设备验证 10 分钟内未完成"
        elif not login.get("ok"):
            return None, f"GitHub 登录未确认：{login.get('detail')}"

    # 回到 opencode 建立会话
    session.get("https://opencode.ai/auth")
    time.sleep(3)
    _step(monitor, session, "GitHub 登录后建立 opencode 会话")
    wid = _extract_wid(session.current_url)
    if wid:
        return wid, "GitHub 登录成功并建立 opencode 会话"
    return None, "GitHub 已登录但未能建立 opencode 会话"


_BAL_RE = re.compile(r'\$([0-9]+(?:\.[0-9]+)?)\s*Current Balance')


def _read_balance(session):
    """读 billing 页 Current Balance 数字（美元）。读不到返回 None。"""
    try:
        body = session.page.inner_text("body", timeout=4000) or ""
    except Exception:
        return None
    m = _BAL_RE.search(body)
    return float(m.group(1)) if m else None


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


def _stripe_frame(session, retries=6):
    """返回 Stripe Checkout 所在 frame。

    首充：Enable Billing 整页跳转 checkout.stripe.com → 主文档即 Stripe → main_frame。
    复充：Add Balance 在 opencode 页面内嵌入 checkout.stripe.com iframe → 该子 frame。

    复充的嵌入 iframe 在渲染/重排时可能瞬时从 page.frames 中消失，若此时直接回退
    main_frame（opencode 页），后续 locator 会落到错误的文档、静默命中不到（曾导致
    Pay 按钮 count=0 的偶发假失败）。故先短暂重试等待 checkout 帧出现，重试耗尽才回退。
    """
    page = session.page
    for _ in range(max(1, retries)):
        if "checkout.stripe.com" in (page.url or ""):
            return page.main_frame
        for fr in page.frames:
            if "checkout.stripe.com" in (fr.url or ""):
                return fr
        time.sleep(0.5)
    return page.main_frame


def _wait_stripe_frame(session, timeout=60):
    """等待 Stripe Checkout frame 出现（整页 or iframe）。返回 frame 或 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        page = session.page
        if "checkout.stripe.com" in (page.url or ""):
            time.sleep(2)
            return page.main_frame
        for fr in page.frames:
            if "checkout.stripe.com" in (fr.url or ""):
                time.sleep(2)
                return fr
        session.capture_frame()
        time.sleep(2)
    return None


def pick_currency_usd(session, monitor):
    """点选美金币种（frame-aware）。找不到不算致命（默认币种仍可支付）。"""
    fr = _stripe_frame(session)
    try:
        # 美元币种按钮文本形如 "$21.23"（含 $ 数字）；CN¥ 按钮不含 $
        btn = fr.get_by_role("button", name=re.compile(r"\$\s?\d")).first
        if btn.count():
            btn.click(timeout=5000)
            _step(monitor, session, "选择美金币种")
            time.sleep(2)
            return "已选美金"
    except Exception as e:
        return f"选美金失败: {str(e)[:60]}"
    return "未找到美金按钮（可能已是美元）"


def select_card_method(session, monitor):
    """选中 Card 支付方式（accordion radio，无 inner_text 需按 id 点）。返回 bool。"""
    page = _stripe_frame(session)
    for sel in [
        "#payment-method-accordion-item-title-card",
        "label[for='payment-method-accordion-item-title-card']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=5000, force=True)
                _step(monitor, session, "选中 Card 支付方式")
                time.sleep(4)  # 等卡字段渲染
                return True
        except Exception:
            continue
    return False


# 部分常见有效美国区号（NANP，首位 2-9），按卡号确定性挑一个，避免所有卡共用同一
# 电话号触发风控。
_US_AREA_CODES = [
    "212", "213", "312", "305", "404", "415", "512", "617", "702", "713",
    "202", "206", "303", "469", "480", "602", "646", "718", "801", "917",
]


def _gen_us_phone(card):
    """按卡号确定性生成一个格式合法的美国电话号（10 位）。

    opencode 复充的 Stripe Checkout 开了 phone_number_collection：#phoneNumber 为必填
    （aria-required=true），不填则 SubmitButton 停留在 --incomplete，Pay 会被拦。卡池无
    电话字段，故据卡号哈希生成——同一张卡每次生成一致（便于重试/排查），不同卡各异。
    交换局码(exchange)首位取 2-9，避免 N11/服务号段。返回纯数字串（Stripe 自动格式化）。
    """
    digits = re.sub(r"\D", "", str(card.get("number", "")) or "0")
    h = 0
    for ch in digits:
        h = (h * 31 + int(ch)) % 1_000_000_007
    area = _US_AREA_CODES[h % len(_US_AREA_CODES)]
    exch = str(2 + (h // 100) % 8) + f"{(h // 1000) % 100:02d}"   # 200-999
    subs = f"{(h // 10) % 10000:04d}"
    return area + exch + subs


def fill_phone_if_present(session, card, monitor):
    """填 Stripe Checkout 的必填电话字段（存在且为空时）。返回 bool（是否已填/已有值）。

    电话是复充页表单完成(SubmitButton--complete)的最后一块拼图，缺它 Pay 无法提交。
    """
    page = _stripe_frame(session)
    try:
        el = page.locator("#phoneNumber").first
        if not el.count():
            return False
        if (el.input_value(timeout=2000) or "").strip():
            return True  # 已有值（如浏览器自动填充），不覆盖
        el.click(timeout=3000)
        el.press_sequentially(_gen_us_phone(card), delay=70)
        _step(monitor, session, "已填电话号")
        return True
    except Exception:
        return False


def _type(page, sel, val, delay=90, timeout=5000):
    el = page.locator(sel).first
    el.click(timeout=timeout)
    el.press_sequentially(str(val), delay=delay)


def _select(page, sel, val):
    """select 元素按 value 再按 label 尝试选择。"""
    loc = page.locator(sel).first
    try:
        loc.select_option(value=str(val))
        return True
    except Exception:
        pass
    try:
        loc.select_option(label=str(val))
        return True
    except Exception:
        return False


def fill_card_and_address(session, card, monitor):
    """填卡号/有效期/CVC/持卡人 + 完整账单地址。返回 (ok, detail)。

    card: card_pool dict（number/expiry_month/expiry_year/cvc/first_name/last_name/
          country/address/address2/city/state/zip）。
    """
    page = _stripe_frame(session)
    exp_yy = str(card.get("expiry_year", ""))[-2:]
    exp = f"{str(card.get('expiry_month','')).zfill(2)}{exp_yy}"
    name = f"{card.get('first_name','')} {card.get('last_name','')}".strip()
    country = card.get("country") or "US"

    errs = []
    # 卡信息
    try:
        _type(page, "#cardNumber", card["number"])
    except Exception as e:
        errs.append(f"cardNumber:{str(e)[:40]}")
    try:
        _type(page, "#cardExpiry", exp)
    except Exception as e:
        errs.append(f"cardExpiry:{str(e)[:40]}")
    try:
        _type(page, "#cardCvc", card["cvc"])
    except Exception as e:
        errs.append(f"cardCvc:{str(e)[:40]}")
    try:
        _type(page, "#billingName", name)
    except Exception as e:
        errs.append(f"billingName:{str(e)[:40]}")
    _step(monitor, session, "已填卡信息，填账单地址")

    # 国家
    if not _select(page, "#billingCountry", country):
        errs.append("billingCountry 选择失败")
    time.sleep(1)

    # 展开手动地址输入（US 默认是地址自动完成搜索框）
    for sel in ["text=Enter address manually", "button:has-text('Enter address manually')"]:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=3000)
                time.sleep(1)
                break
        except Exception:
            continue

    # 账单地址明细
    addr_fields = [
        ("#billingAddressLine1", card.get("address", "")),
        ("#billingLocality", card.get("city", "")),
        ("#billingPostalCode", card.get("zip", "")),
    ]
    for sel, val in addr_fields:
        if not val:
            continue
        try:
            _type(page, sel, val, delay=60)
        except Exception as e:
            errs.append(f"{sel}:{str(e)[:30]}")
    # 州（select）
    state = card.get("state", "")
    if state:
        _select(page, "#billingAdministrativeArea", state)
    if card.get("address2"):
        try:
            _type(page, "#billingAddressLine2", card["address2"], delay=60)
        except Exception:
            pass

    _step(monitor, session, "账单地址填写完成")
    ok = not any(k in " ".join(errs) for k in ["cardNumber", "cardExpiry", "cardCvc"])
    return ok, ("; ".join(errs) if errs else "")


def uncheck_save_info(session, monitor):
    """取消勾选「Save my information with Link / 设置为我的个人信息」。"""
    page = _stripe_frame(session)
    # name=enableStripePass 是「保存到 Link 加速结账」的复选框
    for sel in ["#enableStripePass", "input[name='enableStripePass']"]:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_checked():
                loc.uncheck(timeout=3000, force=True)
                _step(monitor, session, "已取消「保存个人信息」勾选")
                return True
        except Exception:
            continue
    return False


def check_ai_agent_consent(session, monitor):
    """勾选「I am an AI agent acting on behalf of someone else」声明（复充页出现，可能为
    支付前置条件）。best-effort：存在且未勾选则勾上。"""
    page = _stripe_frame(session)
    try:
        cb = page.get_by_label(re.compile("AI agent", re.I)).first
        if cb.count() and not cb.is_checked():
            cb.check(timeout=3000, force=True)
            _step(monitor, session, "已勾选 AI agent 声明")
            return True
    except Exception:
        pass
    return False


_FIND_PAY_JS = r"""
() => {
  const cands = [];
  document.querySelectorAll("button, [role=button], input[type=submit], [type=submit]").forEach(el => {
    const t = (el.innerText || el.value || '').trim();
    if (!/\bpay\b/i.test(t)) return;
    if (/apple\s*pay/i.test(t) || /powered/i.test(t) || /link/i.test(t)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) return;
    cands.push({ text: t.slice(0,40), testid: el.getAttribute('data-testid') || '',
                 type: el.getAttribute('type') || '', tag: el.tagName,
                 x: r.left + r.width/2, y: r.top + r.height/2, area: r.width*r.height,
                 top: r.top });
  });
  if (!cands.length) return null;
  // 提交按钮通常是页面靠下的宽按钮：优先面积最大者
  cands.sort((a,b) => b.area - a.area);
  return cands[0];
}
"""


def _form_ready_state(session):
    """读 Stripe 提交按钮完成态：complete(可提交) / incomplete(仍缺必填) / unknown。

    仅作诊断记录进 steps，不阻断（个别版本无该 class 时仍尝试提交）。
    """
    fr = _stripe_frame(session)
    try:
        cls = fr.evaluate(
            "()=>{const e=document.querySelector"
            "(\"[data-testid='hosted-payment-submit-button']\");return e?e.className:'';}"
        ) or ""
    except Exception:
        return "unknown"
    if "SubmitButton--incomplete" in cls:
        return "incomplete"
    if "SubmitButton--complete" in cls:
        return "complete"
    return "unknown"


def click_pay(session, monitor):
    """点击 Pay 提交支付（frame-aware，用 locator 避免 iframe 坐标转换）。返回 (bool, detail)。"""
    fr = _stripe_frame(session)
    for sel in ["[data-testid='hosted-payment-submit-button']",
                "button[type='submit']:has-text('Pay')"]:
        try:
            loc = fr.locator(sel).last
            if loc.count():
                loc.click(timeout=6000)
                _step(monitor, session, "已点击 Pay")
                return True, f"clicked {sel}"
        except Exception:
            continue
    # 回退：role=button 且文本以 Pay 开头（排除 Apple Pay / Link）
    try:
        btn = fr.get_by_role("button", name=re.compile(r"^Pay", re.I)).last
        if btn.count():
            btn.click(timeout=6000)
            _step(monitor, session, "已点击 Pay（role）")
            return True, "clicked role=Pay"
    except Exception as e:
        return False, f"Pay 点击失败: {str(e)[:60]}"
    return False, "未找到 Pay 按钮"


def _captcha_challenge_present(session):
    """检测是否出现「需人工完成」的 hCaptcha 挑战。返回 frame 或 None。

    仅当 hcaptcha frame 存在且其可见文本含挑战文案（Verify / Please try again 等）时才算，
    以区分需交互的挑战与 Stripe 后台常驻、无需交互的 invisible hCaptcha。
    """
    try:
        for fr in session.page.frames:
            if "hcaptcha.com" not in (fr.url or "").lower():
                continue
            try:
                txt = (fr.inner_text("body", timeout=800) or "").lower()
            except Exception:
                continue
            if any(h in txt for h in _CAPTCHA_TEXT_HINTS):
                return fr
    except Exception:
        pass
    return None


def _threeds_challenge_present(session):
    """当前是否存在 3DS 交互挑战框（Stripe three-ds-2-challenge / 发卡行 ACS）。

    只认交互挑战框；无感 3DS（frictionless）只有 fingerprint/method 框，不含下列标记，
    不会命中——它会自动完成并到账，走余额判定为成功。"""
    try:
        for fr in session.page.frames:
            u = (fr.url or "").lower()
            if "three-ds-2-challenge" in u or "/acs" in u or "acs." in u:
                return True
    except Exception:
        pass
    return False


def _count_top_layer_overlays(session):
    """统计各 frame 内 [data-react-aria-top-layer] 遮罩层数量（3DS 挑战弹窗即挂在其中）。

    用于捕捉「3DS 期间冒出新弹窗」这一认证失败信号：基线之上数量增加即视为新弹窗。
    跨源子 frame 查询失败时静默跳过，尽力而为。"""
    total = 0
    try:
        for fr in session.page.frames:
            try:
                total += fr.locator("[data-react-aria-top-layer]").count()
            except Exception:
                pass
    except Exception:
        pass
    return total


def _threeds_failure_modal(session):
    """3DS2 挑战结束后若发卡行拒绝，Stripe 会在挑战 iframe 内渲染一个结果弹窗
    （.LightboxModal / .ThreeDS2CardholderInfo，headline『Payment failed』+ 银行提示语 + OK）。
    这是**确定性支付失败**信号。返回 (frame, message)；无则 (None, '')。"""
    try:
        for fr in session.page.frames:
            try:
                head = fr.locator(".ThreeDS2CardholderInfo-headline")
                if head.count() == 0:
                    continue
                title = (head.first.inner_text(timeout=1000) or "").strip()
                low = title.lower()
                if any(k in low for k in ("payment failed", "failed", "declined",
                                          "not authenticated", "unable")):
                    msg = title
                    try:
                        body = fr.locator(".ThreeDS2CardholderInfo-content").first.inner_text(timeout=1000)
                        if body:
                            msg = f"{title}：{' '.join(body.split())}"
                    except Exception:
                        pass
                    return fr, msg
            except Exception:
                continue
    except Exception:
        pass
    return None, ""


def _close_threeds_modal(fr, session, monitor):
    """关闭 3DS2 结果弹窗（点 OK / Cancel），让下一张卡能干净地重新发起支付。尽力而为。"""
    for sel in (".ThreeDS2CardholderInfo-button--primary",
                ".ThreeDS2CardholderInfo-button", ".LightboxModalClose"):
        try:
            btn = fr.locator(sel)
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                _step(monitor, session, "已关闭 3DS「Payment failed」弹窗，换下一张")
                return True
        except Exception:
            continue
    return False


def detect_payment_result(session, wid, balance_before, monitor, timeout=120):
    """点 Pay / Add 后判定支付结果。返回 dict{outcome, detail}。

    outcome: "success" | "failed" | "needs_captcha" | "unknown"

    权威判据是 opencode 账户余额是否增加（钱真正到账的唯一凭证），每轮优先判。

    3DS 交互挑战的处理（关键）：检测到挑战弹窗后**不立即判失败**，而是等它加载/完成——
    很多情况下弹窗加载完会自动消失、支付其实成功（余额增长 → success）。仅当出现下列
    「认证失败」信号才判 failed：
      - 挑战期间冒出**新弹窗**（top-layer 遮罩层数量超过初见时的基线）；
      - Stripe 出现明确拒付/认证失败文案（如 unable to authenticate，内容变了）；
      - 直到超时挑战弹窗**仍未自动消失**（未解决）。
    failed 由上层按「是否曾成功」判定消耗（好卡→24h 冷却，坏卡→invalid）。

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
            return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}"}

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
                return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}"}
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
                if any(h in body for h in _DECLINE_HINTS):
                    snippet = ""
                    for h in _DECLINE_HINTS:
                        idx = body.find(h)
                        if idx >= 0:
                            snippet = body[max(0, idx-30):idx+60].strip()
                            break
                    return {"outcome": "failed", "detail": f"拒付/认证失败: {snippet[:120]}"}
            except Exception:
                pass
            # hCaptcha 人机验证挑战：点 Pay 后 Stripe 风控可能弹出，需人工点 Verify 才放行。
            if _captcha_challenge_present(session) is not None and not saw_captcha:
                saw_captcha = True
                _step(monitor, session, "检测到 hCaptcha 人机验证，请在浏览器点 Verify 完成…")
            # 3DS 交互挑战：等待其加载/完成，不立即失败。
            if _threeds_challenge_present(session):
                if not saw_3ds:
                    saw_3ds = True
                    # 基线下限取 1：挑战本身就在一个 top-layer 遮罩里；若初见时遮罩尚未渲染
                    # （计数 0），不把随后正常渲染出的这一个误判为「新弹窗」。
                    overlay_baseline = max(_count_top_layer_overlays(session), 1)
                    _step(monitor, session, "检测到 3DS 交互挑战，等待其加载完成…")
                else:
                    # 挑战期间遮罩层数超过基线（冒出新弹窗）→ 认证失败
                    if _count_top_layer_overlays(session) > overlay_baseline:
                        _step(monitor, session, "3DS 期间出现新弹窗，判定认证失败，换下一张")
                        return {"outcome": "failed", "detail": "3DS 出现新弹窗，认证失败"}

        time.sleep(3)

    # 超时收尾：先看余额（可能刚好在最后一刻到账）
    grew = _balance_grew()
    if grew is not None:
        _step(monitor, session, f"支付成功（余额 ${balance_before}→${grew}）")
        return {"outcome": "success", "detail": f"余额 ${balance_before}→${grew}"}
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
    return {"outcome": "unknown", "detail": f"{timeout}s 内余额未增加，未确认成功"}


def recharge_via_stripe(session, card, wid, amount=20, monitor=None, should_stop=None):
    """完整充值编排。返回 dict{ok, outcome, err, last4, mode, steps}。

    自动区分两种路径（由 billing 页当前入口决定）：
      - mode="first"：首充，走 Stripe Checkout 填 card 参数的卡 + 点 Pay
      - mode="reload"：复充，start_recharge 已用账号已存卡点了 Add，直接判定余额
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
        "steps": steps,
    }
