"""opencode「Subscribe to Go」订阅付款浏览器流程。

与 opencode_billing 的 recharge（billing 页 Add Balance/Enable Billing 充 credits）并列的
**新增**付款路径：从 workspace /go 页点「Subscribe to Go」订阅 OpenCode Go（首月 $5，后 $10/月）。

实机（2026-07-25）确认点 Subscribe to Go → 整页跳 checkout.stripe.com（live），结账页结构与
首充一致（币种 CN¥/USD 切换、Card/Alipay、AI agent 声明勾选），仅提交按钮文案为「Subscribe」。
故填卡全链复用 opencode_billing，本模块只新增：入口 start_subscribe_go、提交 click_subscribe、
订阅成功判定 detect_subscribe_result。

⚠️ 点 Subscribe = 真实扣款（cs_live）。带 dry=True 时填好卡停在提交前，不点 Subscribe。
"""
import re
import time

from src.browser.opencode_billing import (
    _stripe_frame, _step,
    pick_currency_usd, select_card_method, fill_card_and_address,
    fill_phone_if_present, uncheck_save_info, check_ai_agent_consent,
    _captcha_challenge_present, _threeds_challenge_present,
    _threeds_failure_modal, _close_threeds_modal, _count_top_layer_overlays,
    _DECLINE_HINTS,
)
from src.browser.opencode_login import login_and_open_own_go, _extract_wid
from src.services import captcha as captcha_solver


def start_subscribe_go(session, wid, monitor=None, timeout=60):
    """进 workspace /go 页，点「Subscribe to Go」，等 Stripe Checkout 整页出现。

    返回 (ok: bool, detail: str)。ok=True 表示已进入 checkout.stripe.com 结账页。
    """
    go_url = f"https://opencode.ai/workspace/{wid}/go"
    try:
        session.get(go_url)
        time.sleep(2)
    except Exception as e:
        return False, f"打开 /go 失败: {str(e)[:120]}"
    _step(monitor, session, "打开 /go 页，点 Subscribe to Go")

    clicked = False
    for name in ("Subscribe to Go", "Subscribe To Go"):
        try:
            loc = session.page.get_by_role("button", name=name)
            if loc.count():
                loc.first.click(timeout=8000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        return False, "未找到 Subscribe to Go 按钮"

    # 等 checkout.stripe.com 整页出现（后端建 checkout session 再 302，给足时间）
    deadline = time.time() + timeout
    while time.time() < deadline:
        u = ""
        try:
            u = session.current_url or ""
        except Exception:
            pass
        if "checkout.stripe.com" in u:
            _step(monitor, session, "已进入 Stripe 订阅结账页")
            time.sleep(2)
            return True, "已进入 Stripe Checkout"
        try:
            session.capture_frame()
        except Exception:
            pass
        time.sleep(2)
    return False, f"{timeout}s 内未跳转 Stripe Checkout"


def select_usd_subscribe(session, monitor=None):
    """订阅结账页切美元币种。返回描述字符串。

    订阅页币种切换是明确标注「USD」的按钮（区别于充值页的「$金额」块），现有
    pick_currency_usd 的 $金额匹配认不出。这里多策略跨 frame 找并点 USD；都不中再回退。
    """
    time.sleep(1.5)  # 等币种控件渲染
    page = session.page
    # 候选 frame：Stripe 所在 frame + 主 frame + 所有含 checkout 的 frame
    frames = []
    try:
        frames.append(_stripe_frame(session))
    except Exception:
        pass
    try:
        frames.append(page.main_frame)
        for fr in page.frames:
            if "checkout.stripe.com" in (fr.url or "") and fr not in frames:
                frames.append(fr)
    except Exception:
        pass

    for fr in frames:
        for strat_name, make in (
            ("role~USD", lambda f: f.get_by_role("button", name=re.compile(r"^\s*USD\s*$", re.I))),
            ("btn:has-text", lambda f: f.locator("button:has-text('USD')")),
            ("text=USD", lambda f: f.get_by_text(re.compile(r"^\s*USD\s*$", re.I))),
        ):
            try:
                loc = make(fr)
                if loc.count():
                    loc.first.click(timeout=5000)
                    time.sleep(1)
                    _step(monitor, session, f"已切换币种 USD（{strat_name}）")
                    return f"USD({strat_name})"
            except Exception:
                continue

    # 失败：dump 候选按钮文本便于收敛
    try:
        fr = _stripe_frame(session)
        texts = fr.eval_on_selector_all(
            "button,[role=button]",
            "els => els.map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,30)")
        print(f"  [subscribe] USD 按钮未命中，结账页按钮: {texts}", flush=True)
    except Exception:
        pass
    return pick_currency_usd(session, monitor)


def _checkout_frames(session):
    """返回所有 checkout.stripe.com 帧（整页时含 main_frame）。"""
    page = session.page
    frames = []
    try:
        if "checkout.stripe.com" in (page.url or ""):
            frames.append(page.main_frame)
        for f in page.frames:
            if "checkout.stripe.com" in (f.url or "") and f not in frames:
                frames.append(f)
    except Exception:
        pass
    return frames


def click_subscribe(session, monitor=None, timeout=25):
    """点结账页的订阅提交按钮（frame-aware，带等待）。返回 (bool, detail)。

    实机（2026-07-25）确认提交按钮为 Stripe Checkout 的
    `<button type="submit" class="SubmitButton SubmitButton--complete">`（文本 "Subscribe\\nProcessing"，
    含换行，故 name=^Subscribe 匹配不到）。结账页渐进渲染、SubmitButton 需表单完成才 enabled，
    故轮询等待「存在且可点」再点；超时则跨帧 dump 诊断。
    """
    deadline = time.time() + timeout
    last_diag = ""
    while time.time() < deadline:
        for fr in _checkout_frames(session):
            for sel in ("button.SubmitButton[type='submit']", "button[type='submit']"):
                try:
                    loc = fr.locator(sel).first
                    if loc.count() == 0:
                        continue
                    try:
                        loc.wait_for(state="visible", timeout=2000)
                    except Exception:
                        pass
                    if not loc.is_enabled():
                        last_diag = f"{sel} 存在但 disabled"
                        continue
                    try:
                        loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    loc.click(timeout=5000)
                    _step(monitor, session, f"已点击订阅提交（{sel}）")
                    return True, f"clicked {sel}"
                except Exception as e:
                    last_diag = f"{sel} 点击异常 {str(e)[:40]}"
                    continue
        time.sleep(1.5)

    # 失败诊断：跨帧 dump 按钮
    try:
        diag = []
        for fr in _checkout_frames(session):
            bs = fr.eval_on_selector_all(
                "button",
                "els=>els.map(e=>({t:(e.innerText||'').trim().slice(0,24),"
                "ty:e.getAttribute('type'),dis:e.disabled,cls:(e.className||'').slice(0,40)}))")
            diag.append({"url": (fr.url or "")[:50], "buttons": bs})
        print(f"  [click_subscribe] 失败诊断 last='{last_diag}' frames={diag}", flush=True)
    except Exception as e:
        print(f"  [click_subscribe] 诊断 dump 失败: {str(e)[:80]}", flush=True)
    return False, f"未找到可点订阅提交按钮({last_diag})"


def detect_subscribe_result(session, wid, monitor=None, timeout=200):
    """点 Subscribe 后判定订阅结果。返回 dict{outcome, detail}。

    outcome: "success" | "failed" | "needs_captcha" | "unknown"

    与 recharge 的差异：订阅**不增加余额**，故成功判据不能用余额增长，改为：
      离开 checkout.stripe.com 且回落 opencode（订阅完成 Stripe 会跳回 return_url /go）。
    拒付/3DS/hCaptcha 判据复用 opencode_billing 的文本/弹窗信号。

    ⚠️ 成功 DOM/URL 的精确信号需一次真实付费标定（见 task implement.md 批次 B），当前成功判据
    为「离开 checkout 回到 opencode」的保守启发式，标 success 时 detail 注明待确认。
    """
    deadline = time.time() + timeout
    saw_captcha = False
    saw_3ds = False
    overlay_baseline = 0
    captcha_tries = 0

    def _stripe_fr():
        for fr in session.page.frames:
            if "checkout.stripe.com" in (fr.url or ""):
                return fr
        return None

    def _cur():
        try:
            return session.current_url or ""
        except Exception:
            return ""

    def _decline_snippet():
        """逐帧扫描可见文本找拒付/认证失败文案——错误可能渲染在主 checkout 帧或其支付子帧，
        只读主帧会漏判导致空等到超时。命中返回文案片段，否则 None。

        ⚠️ 排除 hCaptcha 自身报错的误命中：hCaptcha 帧常出现「sitekey for this hcaptcha is
        incorrect」等文案，含 "incorrect" 会被 _DECLINE_HINTS 误判成卡拒付、错误地把好卡标废。
        故命中片段若提到 hcaptcha/sitekey 则跳过——那是人机验证问题、不是卡的问题。"""
        for fr in session.page.frames:
            try:
                body = (fr.inner_text("body", timeout=1200) or "").lower()
            except Exception:
                continue
            for h in _DECLINE_HINTS:
                idx = body.find(h)
                if idx >= 0:
                    seg = body[max(0, idx - 45):idx + 80]
                    if "hcaptcha" in seg or "sitekey" in seg or "site key" in seg:
                        continue   # hCaptcha 报错，非卡拒付，跳过
                    return body[max(0, idx - 30):idx + 60].strip()
        return None

    while time.time() < deadline:
        cur = _cur()
        stripe_fr = _stripe_fr()

        # 成功启发式：已离开 checkout 且回到 opencode workspace（订阅完成跳回 return_url）
        if "checkout.stripe.com" not in cur and stripe_fr is None and _extract_wid(cur):
            _step(monitor, session, "已离开 Stripe 结账页并回落 opencode，判定订阅成功（待真实付费标定确认）")
            return {"outcome": "success",
                    "detail": f"离开 checkout 回落 {cur[:100]}（成功信号待标定）"}

        # 拒付/认证失败文案：逐帧扫描（错误可能在子帧），命中即刻 failed → 换下一张卡，绝不空等
        snip = _decline_snippet()
        if snip:
            return {"outcome": "failed", "detail": f"拒付/认证失败: {snip[:120]}"}

        if stripe_fr is not None:
            # 3DS 结果弹窗「Payment failed」→ 明确失败
            fmfr, fmsg = _threeds_failure_modal(session)
            if fmfr is not None:
                _close_threeds_modal(fmfr, session, monitor)
                return {"outcome": "failed", "detail": f"3DS 认证失败: {fmsg[:150]}"}
            # hCaptcha 人机验证：2captcha 自动解，最多 3 次；3 次仍未过即提前返回 needs_captcha，
            # 不空等满 timeout（避免一直停在支付页）。
            if _captcha_challenge_present(session) is not None:
                if not saw_captcha:
                    saw_captcha = True
                    _step(monitor, session, "检测到 hCaptcha 人机验证")
                if captcha_solver.is_available() and captcha_tries < 3:
                    captcha_tries += 1
                    _step(monitor, session, f"用 2captcha 自动解 hCaptcha（第 {captcha_tries} 次）…")
                    try:
                        if captcha_solver.solve_hcaptcha(session):
                            _step(monitor, session, "hCaptcha token 已注入，等待订阅结果…")
                            time.sleep(4)
                    except Exception as e:
                        print(f"  2captcha 解题异常: {str(e)[:120]}", flush=True)
                elif captcha_solver.is_available() and captcha_tries >= 3:
                    # 解了 3 次仍卡在 hCaptcha（token 被拒/账号级风控）——提前收手换下一张卡
                    if _decline_snippet() is None:
                        _step(monitor, session, "hCaptcha 解 3 次仍未过，提前收手（换下一张卡）")
                        return {"outcome": "needs_captcha",
                                "detail": "hCaptcha 解 3 次仍未通过（token 被拒/账号级风控）"}
                elif not captcha_solver.is_available():
                    _step(monitor, session, "未配置 2captcha，请在浏览器手动点 Verify…")
            # 3DS 交互挑战：等待加载，不立即失败
            if _threeds_challenge_present(session):
                if not saw_3ds:
                    saw_3ds = True
                    overlay_baseline = max(_count_top_layer_overlays(session), 1)
                    _step(monitor, session, "检测到 3DS 交互挑战，等待其加载完成…")
                elif _count_top_layer_overlays(session) > overlay_baseline:
                    return {"outcome": "failed", "detail": "3DS 出现新弹窗，认证失败"}

        time.sleep(3)

    # 超时收尾：末次确认是否已回落 opencode
    cur = _cur()
    if "checkout.stripe.com" not in cur and _stripe_fr() is None and _extract_wid(cur):
        return {"outcome": "success", "detail": f"超时前回落 {cur[:100]}（成功信号待标定）"}
    if saw_captcha:
        return {"outcome": "needs_captcha",
                "detail": f"hCaptcha 未在 {timeout}s 内完成（账号级风控，需人工）"}
    if saw_3ds:
        fmfr, fmsg = _threeds_failure_modal(session)
        if fmfr is not None:
            _close_threeds_modal(fmfr, session, monitor)
            return {"outcome": "failed", "detail": f"3DS 认证失败: {fmsg[:150]}"}
        return {"outcome": "failed", "detail": f"3DS 交互挑战 {timeout}s 内未完成"}
    return {"outcome": "unknown", "detail": f"{timeout}s 内未确认订阅结果，仍在结账页"}


def subscribe_via_stripe(session, card, wid, monitor=None, should_stop=None, dry=False):
    """完整订阅付款编排。返回 dict{ok, outcome, err, last4, steps}。

    镜像 opencode_billing.recharge_via_stripe，入口换 start_subscribe_go、提交换 click_subscribe、
    判定换 detect_subscribe_result，中间选币种/选卡/填卡/电话/取消保存/AI 声明全复用。

    dry=True：填好卡后**停在提交前，不点 Subscribe、不扣款**，返回 outcome="dry_ready"。
    outcome：success / failed / needs_captcha / unknown / error / dry_ready。
    ok 仅在 success 时 True。
    """
    steps = []
    last4 = str(card.get("number", ""))[-4:]

    def stop_check():
        if should_stop and should_stop():
            raise InterruptedError("用户请求停止")

    stop_check()
    ok, detail = start_subscribe_go(session, wid, monitor)
    if not ok:
        return {"ok": False, "outcome": "error",
                "err": f"未进入订阅结账页: {detail}", "last4": last4, "steps": steps}
    steps.append("start:subscribe_go")

    stop_check()
    steps.append("currency:" + select_usd_subscribe(session, monitor))

    stop_check()
    if not select_card_method(session, monitor):
        return {"ok": False, "outcome": "error",
                "err": "选中 Card 支付方式失败", "last4": last4, "steps": steps}
    steps.append("select_card")

    stop_check()
    fill_ok, fill_detail = fill_card_and_address(session, card, monitor)
    steps.append(f"fill:{fill_detail or 'ok'}")
    if not fill_ok:
        return {"ok": False, "outcome": "error",
                "err": f"填卡失败: {fill_detail}", "last4": last4, "steps": steps}

    fill_phone_if_present(session, card, monitor)
    uncheck_save_info(session, monitor)
    check_ai_agent_consent(session, monitor)
    steps.append("phone+uncheck+consent")

    if dry:
        _step(monitor, session, "dry：已填好卡，停在提交前（不点 Subscribe、不扣款）")
        # 诊断：dump 结账页所有按钮（文本/type/disabled），用于定位真实提交按钮
        try:
            fr = _stripe_frame(session)
            btns = fr.eval_on_selector_all(
                "button",
                "els => els.map(e => ({t:(e.innerText||'').trim().slice(0,40), "
                "type:e.getAttribute('type'), cls:(e.className||'').slice(0,40), "
                "disabled:e.disabled}))")
            print(f"  [subscribe:dry] 结账页 button 列表: {btns}", flush=True)
        except Exception as e:
            print(f"  [subscribe:dry] dump 按钮失败: {str(e)[:100]}", flush=True)
        steps.append("dry_stop_before_submit")
        return {"ok": False, "outcome": "dry_ready",
                "err": "", "last4": last4, "steps": steps}

    stop_check()
    sub_ok, sub_detail = click_subscribe(session, monitor)
    steps.append(f"click_subscribe:{sub_detail}")
    if not sub_ok:
        return {"ok": False, "outcome": "error",
                "err": f"点击 Subscribe 失败: {sub_detail}", "last4": last4, "steps": steps}

    result = detect_subscribe_result(session, wid, monitor)
    steps.append(f"result:{result['outcome']}")
    return {
        "ok": result["outcome"] == "success",
        "outcome": result["outcome"],
        "err": "" if result["outcome"] == "success" else result["detail"],
        "last4": last4,
        "steps": steps,
    }


# --- 独立测试入口：登录 → /go → Subscribe → 填测试卡 → 停在提交前（--dry，不扣款）-------
if __name__ == "__main__":
    import argparse
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.browser.driver import create_driver, close_driver

    # 明确的测试卡（Stripe 官方测试号；仅 --dry 填充演示，不会真提交）。键名对齐 card_pool
    # / fill_card_and_address 实际读取字段（expiry_month/expiry_year/first_name/... ）。
    _TEST_CARD = {"number": "4242424242424242", "expiry_month": "12", "expiry_year": "2034",
                  "cvc": "123", "first_name": "Test", "last_name": "User",
                  "country": "US", "address": "1 Test St", "city": "New York",
                  "state": "NY", "zip": "10001"}

    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="已注册账号邮箱（用作 profile_id）")
    ap.add_argument("--dry", action="store_true", help="填好卡停在提交前，不点 Subscribe、不扣款")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if not args.dry:
        print("⚠️ 未加 --dry：将真实点 Subscribe 扣款。请确认后再运行。", flush=True)

    sess = create_driver(headless=False, profile_id=args.email)
    try:
        lg = login_and_open_own_go(sess)
        print("登录:", lg, flush=True)
        if not lg.get("ok"):
            print("登录失败，终止", flush=True)
        else:
            r = subscribe_via_stripe(sess, _TEST_CARD, lg["wid"], dry=args.dry)
            print("\n订阅结果:", r, flush=True)
        if args.keep:
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        if not args.keep:
            close_driver(sess)
