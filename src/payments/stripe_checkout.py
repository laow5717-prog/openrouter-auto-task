"""Stripe Checkout 页面操作 —— 支付供应商层，与具体平台无关。

选币种、选卡类支付方式、填手机号、填卡与账单地址、取消保存信息、勾选 AI agent 声明、
提交付款，以及 hCaptcha / 3DS 挑战与失败弹窗的识别与关闭。

抽出来不是"为了分层而分层"：这批函数早就被 opencode 的充值与订阅两条流程共享了
（subscribe.py 一次性从 billing.py import 了其中 14 个），这里只是把既成事实显式化。
任何用 Stripe Checkout 收款的平台都能直接复用。

**这批代码是一个个坑换来的**——3DS 宽限期、顶层遮罩计数、否定词优先的拒付判定，
每条都对应一次线上事故。改动前先确认你知道它在防什么。
"""

import random
import re
import time

from src.browser.monitor import step as _step

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


# Stripe **客户端表单校验**的提示——与拒付有本质区别：卡根本没提交给银行。
#
# 触发原因只有两类，两类都不该判废这张卡：
#   a) 我们没把字段填完整（输入被 DOM 重排吞字符，见 _type_and_verify）；
#   b) 该 Stripe 账户没启用这个卡种，于是位数对不上——实测 14 位 Diners 在
#      infron 的 Payment Element 上就报 "Your card number is incomplete."
#
# 必须**先于** _DECLINE_HINTS 判定：后者含裸词 "incorrect" 与 "card number is"，
# 会把这两句一并吞掉判成拒付。实跑中两张好 Diners 就是这么被误废的，而判废不可逆。
_INPUT_INVALID_HINTS = [
    "card number is incomplete", "card number is incorrect", "card number is invalid",
    "security code is incomplete", "security code is invalid",
    "expiration date is incomplete", "expiration date is invalid",
    "postal code is incomplete", "postal code is invalid",
    "zip code is incomplete",
]

_DECLINE_HINTS = [
    "declined", "was declined", "insufficient", "incorrect",
    "card number is", "security code is", "expired", "not be processed",
    "do not honor", "card was not accepted",
    # 实机（2026-07-23 真实卡 1010）终态：主 frame 红字 "We are unable to authenticate
    # your payment method. Please choose a different payment method and try again."
    # ——这是「卡无法验证」的明确失败并提示换卡，归 failed（换下一张卡），不是等待人工。
    "unable to authenticate", "authenticate your payment",
    "choose a different payment",
    # 提交后 Stripe 通用报错「There was an error processing your request.」——支付未成，
    # 归 failed 自动换下一张卡（不空等超时）。
    "error processing your request", "processing your request",
]

# hCaptcha 图像挑战真正出现时，挑战帧内会渲染的提示文案（题面 / 题格）。
# 只作二次确认用，主判据是帧 URL 的 #frame=challenge——见 _captcha_challenge_present。
#
# ⚠️ 绝不能把 "i am human" 放进来：那是常驻 checkbox 帧的固定标签（见下）。


_CAPTCHA_TEXT_HINTS = [
    "select each image", "click each image", "select all images",
    "verify you are human", "are you a human",
]

# hCaptcha 把自己拆成两种 iframe，URL 片段区分（与 services/hcaptcha_click_solver.py 一致）：
#   #frame=checkbox   —— 常驻帧，Stripe 结账页**永远**存在，哪怕根本不需要验证。
#                        它的 body 文本就是固定的 "I am human"。
#   #frame=challenge  —— 真正的图像挑战帧，只在需要人点选时才出现。


_CAPTCHA_CHALLENGE_FRAME_MARK = "#frame=challenge"


# Stripe 的两种集成形态，frame URL 特征不同：
#   hosted Checkout —— 整页或 iframe 的 checkout.stripe.com（opencode 用这种）
#   Payment Element —— 页面内嵌 js.stripe.com/v3/elements-inner-payment-*（infron 用这种）
# 两者的字段命名也不同，见 fill_card_and_address / fill_payment_element_card。
CHECKOUT_MARK = "checkout.stripe.com"
ELEMENT_MARK = "elements-inner-payment"


def _stripe_frame(session, retries=6, mark=CHECKOUT_MARK):
    """返回 Stripe 表单所在 frame。

    首充：Enable Billing 整页跳转 checkout.stripe.com → 主文档即 Stripe → main_frame。
    复充：Add Balance 在 opencode 页面内嵌入 checkout.stripe.com iframe → 该子 frame。

    复充的嵌入 iframe 在渲染/重排时可能瞬时从 page.frames 中消失，若此时直接回退
    main_frame（opencode 页），后续 locator 会落到错误的文档、静默命中不到（曾导致
    Pay 按钮 count=0 的偶发假失败）。故先短暂重试等待目标帧出现，重试耗尽才回退。

    mark 是 frame URL 的匹配标记，默认 hosted Checkout。用 Payment Element 的平台
    （infron）传 ELEMENT_MARK。做成参数而不是在函数里按站点分支——分支会随平台数量
    线性膨胀，而这里真正变化的只是一个字符串。
    """
    page = session.page
    for _ in range(max(1, retries)):
        if mark in (page.url or ""):
            return page.main_frame
        for fr in page.frames:
            if mark in (fr.url or ""):
                return fr
        time.sleep(0.5)
    return page.main_frame


def _wait_stripe_frame(session, timeout=60, mark=CHECKOUT_MARK):
    """等待 Stripe 表单 frame 出现（整页 or iframe）。返回 frame 或 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        page = session.page
        if mark in (page.url or ""):
            time.sleep(2)
            return page.main_frame
        for fr in page.frames:
            if mark in (fr.url or ""):
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


# Payment Element 的字段 ID 与 hosted Checkout 不同。两套都列出来逐个试，命中即用。
#
# 为什么是多候选而不是写死一套：infron 的填卡表单没能实探到（hCaptcha 未解时
# Payment Element 根本不渲染，见任务 08-04 的 Stage 1）。Stripe 官方对 Payment
# Element 用 Field-*Input 命名、对 Checkout 用裸名，两套都覆盖比赌一套稳；真跑时
# 哪套命中会记进 steps，据此再收敛。
_CARD_FIELD_CANDIDATES = {
    # 顺序按实测命中率排：infron 的 Payment Element 用**裸 name 属性**
    # （实测 cvc 命中 input[name='cvc']），所以那一组放最前。
    "number": ("input[name='number']", "#Field-numberInput", "#cardNumber",
               "input[name='cardnumber']"),
    "expiry": ("input[name='expiry']", "#Field-expiryInput", "#cardExpiry",
               "input[name='exp-date']"),
    "cvc": ("input[name='cvc']", "#Field-cvcInput", "#cardCvc"),
    # 持卡人姓名：Payment Element 未必渲染这个字段（取决于集成方的配置），
    # 所以它不进 ok 判据。
    "name": ("input[name='name']", "#Field-nameInput", "#billingName"),
}
# 地址字段同样把**裸 name** 放最前——Payment Element 用的就是这套命名
# （实测 number/expiry/cvc 均命中裸 name）。此前只给了 Checkout 那套 ID，
# 结果邮编等字段全部填不进去。
_ADDR_FIELD_CANDIDATES = {
    "country": ("select[name='country']", "#Field-countryInput", "#billingCountry"),
    "line1": ("input[name='addressLine1']", "#Field-addressLine1Input", "#billingAddressLine1"),
    "line2": ("input[name='addressLine2']", "#Field-addressLine2Input", "#billingAddressLine2"),
    "city": ("input[name='locality']", "#Field-localityInput", "#billingLocality"),
    "zip": ("input[name='postalCode']", "#Field-postalCodeInput", "#billingPostalCode"),
    "state": ("select[name='administrativeArea']", "#Field-administrativeAreaInput",
              "#billingAdministrativeArea"),
}


def _digits(text):
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _type_and_verify(loc, value, delay=90, timeout=4000, attempts=3):
    """逐字符输入并**回读校验**，短了就清空重来。全部尝试后仍不完整返回 False。

    校验不是多余的谨慎，是必需的：Stripe 的字段边输入边重排 DOM（做卡号分组、
    卡种识别、自动跳到下一格），元素在输入途中被 detach 时**剩余字符会静默丢掉**——
    没有异常、没有报错，只是卡号少了几位。表现出来是「付款莫名失败」，而日志里
    这一步是成功的，极难定位。实测就撞上过：卡号没填完就去填下一个字段。

    用 press_sequentially 而不是废弃的 type()：前者发真实按键事件，Stripe 的
    受控输入才认。
    """
    want = str(value)
    for _ in range(max(1, attempts)):
        loc.click(timeout=timeout)
        try:
            loc.fill("", timeout=timeout)       # 先清空，避免重试时叠加
        except Exception:
            pass
        loc.press_sequentially(want, delay=delay)
        time.sleep(0.3)
        try:
            got = loc.input_value(timeout=timeout)
        except Exception:
            return True     # 读不回来（受控组件可能不暴露 value），按成功处理
        # Stripe 会把卡号格式化成分组（"4111 1111 1111 1111"），只比数字
        if _digits(got) == _digits(want) or got.strip() == want.strip():
            return True
    return False


def _type_first(page, selectors, value, delay=90, timeout=4000):
    """按顺序试多个选择器，第一个能**完整填入**的算成功。返回命中的选择器或 None。

    注意「填进去了」与「填完整了」不是一回事——见 _type_and_verify。
    """
    if value in (None, ""):
        return None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            if _type_and_verify(loc, value, delay=delay, timeout=timeout):
                return sel
        except Exception:
            continue
    return None


def _select_first(page, selectors, value):
    if not value:
        return None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            loc.select_option(value, timeout=4000)
            return sel
        except Exception:
            continue
    return None


def select_card_tab(session, monitor, mark=ELEMENT_MARK):
    """在 Payment Element 里切到 Card 支付方式。

    **必须显式点**：实测 infron 的 Element 默认选中的是 **Alipay** 而不是 Card，
    不点就根本不会出现卡号字段。这与 hosted Checkout 的 accordion 结构不同，
    所以不能复用 select_card_method。
    """
    fr = _stripe_frame(session, mark=mark)
    for sel in ("[role=tab]:has-text('Card')", "button:has-text('Card')", "text=Card"):
        try:
            loc = fr.locator(sel).first
            if not loc.count():
                continue
            loc.click(timeout=4000)
            _step(monitor, session, "已切到 Card 支付方式")
            time.sleep(2)
            return True
        except Exception:
            continue
    _step(monitor, session, "未找到 Card 支付方式 tab")
    return False


def fill_payment_element_card(session, card, monitor, mark=ELEMENT_MARK):
    """在 Stripe Payment Element 里填卡与账单地址。返回 (ok, detail)。

    与 fill_card_and_address（hosted Checkout 版）并列而不是合并：两者的字段命名、
    地址展开方式、frame 定位都不同，硬合成一个函数会变成一堆 if。

    ok 的判据只看**卡三要素**（号/有效期/CVC）是否填上——地址字段缺失通常不阻止提交，
    而卡填不进去必然失败。
    """
    fr = _stripe_frame(session, mark=mark)
    exp_yy = str(card.get("expiry_year", ""))[-2:]
    exp = f"{str(card.get('expiry_month','')).zfill(2)}{exp_yy}"
    name = f"{card.get('first_name','')} {card.get('last_name','')}".strip()

    hits, misses = [], []
    for key, value in (("number", card.get("number")),
                       ("expiry", exp),
                       ("cvc", card.get("cvc")),
                       ("name", name)):
        sel = _type_first(fr, _CARD_FIELD_CANDIDATES[key], value)
        (hits if sel else misses).append(f"{key}={sel or 'miss'}")
    _step(monitor, session, f"填卡字段：{', '.join(hits + misses)}")

    # 国家要先选，地址子字段可能随之变化
    _select_first(fr, _ADDR_FIELD_CANDIDATES["country"], card.get("country") or "US")
    time.sleep(1)
    # US 默认给的是地址自动完成搜索框，要展开成手动输入
    for sel in ("text=Enter address manually", "button:has-text('Enter address manually')"):
        try:
            loc = fr.locator(sel).first
            if loc.count():
                loc.click(timeout=3000)
                time.sleep(1)
                break
        except Exception:
            continue
    addr_hits, addr_misses = [], []
    for key, value in (("line1", card.get("address")),
                       ("city", card.get("city")),
                       ("zip", card.get("zip")),
                       ("line2", card.get("address2"))):
        if not value:
            continue
        sel = _type_first(fr, _ADDR_FIELD_CANDIDATES[key], value, delay=60)
        (addr_hits if sel else addr_misses).append(key)
    _select_first(fr, _ADDR_FIELD_CANDIDATES["state"], card.get("state"))
    # 地址字段的命中情况也要报出来。此前只报卡字段，于是「邮编没填进去」这种事
    # 完全看不见——付款失败了还以为是卡的问题。
    _step(monitor, session,
          f"账单地址：填上 {addr_hits or '无'}" +
          (f"，未命中 {addr_misses}" if addr_misses else ""))

    filled = {h.split("=")[0] for h in hits}
    ok = {"number", "expiry", "cvc"} <= filled
    detail = "" if ok else f"卡字段未填全：{', '.join(misses)}"
    return ok, detail


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
    """检测是否出现「需人工完成」的 hCaptcha 图像挑战。返回 frame 或 None。

    判据是**挑战帧真的渲染出了题目**：URL 带 #frame=challenge，且帧内有题面
    （.prompt-text）或题格（.task-grid）。两个条件缺一不可——挑战帧会被提前创建成空壳，
    只看 URL 会在题目出现前就误判。

    2026-08-03 事故：旧实现是「任意 hcaptcha.com 帧的 body 文本命中关键词」，而关键词里
    有 "i am human" —— 那正是常驻 checkbox 帧的固定标签，于是**页面上根本没有验证码时也
    必然命中**。后果不是多打一行日志：每张卡白烧 3 次付费解题（约 90 秒），3 次后返回
    needs_captcha，而 registration.py 对 needs_captcha 的处理是「账号级风控，换卡无用，
    立即停手」——整个账号的充值当场终止，真实的付款结果（成功/拒付）被彻底掩盖。
    实机佐证：注入诊断里 getResponse 调用数 gr=0，说明 Stripe 从未索取过验证码答案。
    """
    try:
        for fr in session.page.frames:
            if _CAPTCHA_CHALLENGE_FRAME_MARK not in (fr.url or ""):
                continue
            try:
                if fr.query_selector(".prompt-text") or fr.query_selector(".task-grid"):
                    return fr
            except Exception:
                continue
    except Exception:
        pass
    return None


def _captcha_frames_debug(session):
    """列出当前所有 hCaptcha 帧的 URL 片段，供「为什么没判到挑战」时排查。

    存在的理由：判据收紧后，若哪天真挑战因结构变化不再命中，现象是「静默不解题」，
    从日志里看不出任何异常。这个函数让那种情况留下痕迹。
    """
    marks = []
    try:
        for fr in session.page.frames:
            u = fr.url or ""
            if "hcaptcha" not in u.lower():
                continue
            frag = u.split("#", 1)[1][:60] if "#" in u else "(无 fragment)"
            marks.append(frag)
    except Exception:
        pass
    return marks


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


# 3DS 交互挑战 Lightbox 出现后的宽限秒数：先等它自动完成/消失（部分发卡行自动放行，
# 余额到账走 success）。超过仍在则视为需持卡人参与的真挑战——自动化场景无人可验，
# 判失败、关弹窗、换下一张卡。


_THREEDS_CHALLENGE_GRACE_SEC = 30


def _threeds_challenge_lightbox(session):
    """Stripe 3DS2 交互挑战弹窗：.LightboxModal 内嵌 iframe#challengeFrame /
    name=stripe-challenge-frame（实机 2026-07-28，capitalone ACS）。返回承载弹窗的
    frame；无则 None。该弹窗要求持卡人在发卡行侧完成验证，干等不会自行成功。"""
    sel = (".LightboxModal iframe#challengeFrame, "
           ".LightboxModal iframe[name='stripe-challenge-frame'], "
           ".LightboxModal .ThreeDS2-challenge")
    try:
        for fr in session.page.frames:
            try:
                if fr.locator(sel).count():
                    return fr
            except Exception:
                continue
    except Exception:
        pass
    return None


def _close_challenge_lightbox(fr, session, monitor):
    """点挑战弹窗的 Cancel（.LightboxModalClose）关闭之，让下一张卡干净重试。尽力而为。"""
    try:
        btn = fr.locator(".LightboxModal .LightboxModalClose")
        if btn.count():
            btn.first.click(timeout=2000)
            _step(monitor, session, "已关闭 3DS 挑战弹窗（Cancel），换下一张")
            return True
    except Exception:
        pass
    return False

