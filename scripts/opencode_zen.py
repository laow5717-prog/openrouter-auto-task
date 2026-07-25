#!/usr/bin/env python3
"""opencode.ai/zen 控制台操作脚本（固定 profile=manual，复用 GitHub 登录态）。

分步执行，每步 dump 页面结构 + 截图，便于观察后决定下一步。

action:
  explore  导航到 opencode.ai/zen，dump 页面(URL/标题/按钮/链接/关键字)并截图
  goto     导航到 --url 指定页面并 dump
  click    导航到 --url(可选) 后点击匹配 --text 的按钮/链接，再 dump

用法:
  PYTHONUNBUFFERED=1 python3 scripts/opencode_zen.py --action explore
  PYTHONUNBUFFERED=1 python3 scripts/opencode_zen.py --action click --text "Enable billing"
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver  # noqa: E402

PROFILE = "manual"
ZEN_URL = "https://opencode.ai/zen"
SHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "screenshots")

DUMP_JS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = (el) => (el.innerText || el.textContent || el.value || '').trim().replace(/\s+/g,' ').slice(0,80);
  const out = { url: location.href, title: document.title, buttons: [], links: [], inputs: [], bodyHint: '' };
  document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]').forEach(b => {
    if (vis(b)) { const t = txt(b); if (t) out.buttons.push(t); }
  });
  document.querySelectorAll('a[href]').forEach(a => {
    if (vis(a)) { const t = txt(a); if (t) out.links.push(t + ' -> ' + a.getAttribute('href')); }
  });
  document.querySelectorAll('input, select').forEach(i => {
    if (vis(i)) out.inputs.push((i.tagName.toLowerCase()) + '[' + (i.type||i.name||i.id||'') + ']');
  });
  const body = (document.body.innerText || '').replace(/\s+/g,' ');
  ['sign in','log in','login','github','billing','enable','currency','USD','payment','subscribe','credit','balance']
    .forEach(k => { if (body.toLowerCase().includes(k.toLowerCase())) out.bodyHint += k + '|'; });
  out.bodySample = body.slice(0, 600);
  return out;
}
"""


def dump(session, tag):
    time.sleep(2)
    try:
        info = session.page.evaluate(DUMP_JS)
    except Exception as e:
        info = {"error": str(e)[:200], "url": session.current_url}
    print(f"\n===== DUMP [{tag}] =====", flush=True)
    print("URL   :", info.get("url"), flush=True)
    print("TITLE :", info.get("title"), flush=True)
    print("HINTS :", info.get("bodyHint"), flush=True)
    print("BUTTONS:", flush=True)
    for b in info.get("buttons", [])[:30]:
        print("   -", b, flush=True)
    print("LINKS:", flush=True)
    for l in info.get("links", [])[:40]:
        print("   -", l, flush=True)
    print("INPUTS:", info.get("inputs", []), flush=True)
    print("BODY  :", info.get("bodySample", "")[:600], flush=True)
    if info.get("error"):
        print("ERROR :", info["error"], flush=True)
    # 截图
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, f"zen_{tag}.png")
    try:
        session.capture_frame()
        with open(path, "wb") as f:
            f.write(session.get_screenshot_as_png())
        print("SHOT  :", path, flush=True)
    except Exception as e:
        print("SHOT-ERR:", str(e)[:120], flush=True)
    print("========================\n", flush=True)
    return info


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--action", default="explore", choices=["explore", "goto", "click", "enable_usd", "probe_card", "fill_card"])
    p.add_argument("--url", default=ZEN_URL)
    p.add_argument("--text", default="")
    p.add_argument("--keep", action="store_true", help="操作后保活浏览器不关闭")
    args = p.parse_args()

    session = create_driver(headless=False, profile_id=PROFILE)
    print(f"profile dir: {session._user_data_dir}", flush=True)
    try:
        target = args.url
        print(f"导航: {target}", flush=True)
        session.get(target)
        time.sleep(3)
        dump(session, "landing")

        if args.action == "fill_card":
            print("点击 Enable billing...", flush=True)
            print(" ", _click_by_text(session, "Enable billing"), flush=True)
            for _ in range(12):
                time.sleep(2)
                if "checkout.stripe.com" in session.current_url:
                    break
            time.sleep(4)
            print("选择美金币种:", _pick_currency_usd(session), flush=True)
            time.sleep(2)
            print("选中 Card:", _click_card_radio(session), flush=True)
            time.sleep(5)
            print("填入测试卡数据（不点 Pay，不扣款）...", flush=True)
            for r in _fill_card(session, TEST_CARD):
                print("   ", r, flush=True)
            time.sleep(2)
            dump(session, "card_filled")
            _keep_alive(session)
            return

        if args.action == "probe_card":
            print("点击 Enable billing...", flush=True)
            print(" ", _click_by_text(session, "Enable billing"), flush=True)
            for _ in range(12):
                time.sleep(2)
                if "checkout.stripe.com" in session.current_url:
                    break
            time.sleep(4)
            print("选择美金币种:", _pick_currency_usd(session), flush=True)
            time.sleep(2)
            print("选中 Card:", _click_card_radio(session), flush=True)
            time.sleep(5)  # 等 Stripe Elements 卡字段 iframe 注入
            dump(session, "card_form")
            _dump_frames(session)
            _keep_alive(session)
            return

        if args.action == "enable_usd":
            # billing 页 → 点 Enable billing → 等 Stripe 加载 → 选美金币种 → 截图 → 保活
            print("点击 Enable billing...", flush=True)
            print(" ", _click_by_text(session, "Enable billing"), flush=True)
            print("等待 Stripe Checkout 加载...", flush=True)
            for _ in range(12):
                time.sleep(2)
                if "checkout.stripe.com" in session.current_url:
                    break
            time.sleep(4)  # 等币种区渲染
            dump(session, "stripe_before")
            print("选择美金币种...", flush=True)
            print(" ", _pick_currency_usd(session), flush=True)
            time.sleep(3)
            dump(session, "stripe_usd")
            _keep_alive(session)
            return

        if args.action == "click" and args.text:
            print(f"尝试点击含文本: {args.text!r}", flush=True)
            clicked = _click_by_text(session, args.text)
            print("点击结果:", clicked, flush=True)
            time.sleep(3)
            dump(session, "after_click")

        if args.keep:
            _keep_alive(session)
    finally:
        if not args.keep:
            close_driver(session)


FIND_USD_JS = r"""
() => {
  // 在 Stripe Checkout 币种选择区找「美金」块，返回其中心坐标。
  // 币种块文本形如 "🇺🇸 $21.23" / "US$21.23"，排除含 ¥ 或 CN 的人民币块。
  const cands = [];
  document.querySelectorAll('button, [role=button], label, div, a').forEach(el => {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 24) return;
    const isUsd = /\$\s?\d/.test(t) && !/[¥￥]/.test(t) && !/CN/i.test(t);
    if (!isUsd) return;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10) return;
    cands.push({ text: t, x: r.left + r.width/2, y: r.top + r.height/2, area: r.width*r.height });
  });
  if (!cands.length) return null;
  // 取面积最小的叶子块（避免点到包含它的大容器）
  cands.sort((a,b) => a.area - b.area);
  return cands[0];
}
"""


FRAME_INPUTS_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('input, select').forEach(i => {
    const r = i.getBoundingClientRect();
    out.push({
      tag: i.tagName.toLowerCase(),
      name: i.name || '', id: i.id || '', type: i.type || '',
      placeholder: i.placeholder || '', autocomplete: i.getAttribute('autocomplete') || '',
      aria: i.getAttribute('aria-label') || '',
      visible: r.width > 0 && r.height > 0,
    });
  });
  return out;
}
"""


# Stripe 官方测试卡（Luhn 合法；cs_live 提交会被拒，仅用于验证「字段能否被程序填入」，不点 Pay）
TEST_CARD = {
    "number": "4242424242424242", "exp": "1234", "cvc": "123",
    "name": "Test User", "country": "US", "zip": "10001",
}


def _fill_card(session, c):
    """把卡数据逐字符填入 Stripe Checkout 主 frame 的卡字段（反检测：模拟人工输入）。

    c: {number, exp(MMYY), cvc, name, country(ISO2), zip}
    返回每个字段的填写结果列表。不提交、不点 Pay。
    """
    page = session.page

    def type_into(sel, val):
        el = page.locator(sel).first
        el.click(timeout=3000)
        el.press_sequentially(str(val), delay=90)

    results = []
    # 先选国家（部分字段如邮编依赖国家）
    try:
        page.locator("#billingCountry").first.select_option(c["country"])
        results.append(f"country={c['country']} OK")
    except Exception as e:
        results.append(f"country ERR {str(e)[:50]}")
    for sel, val, key in [
        ("#cardNumber", c["number"], "cardNumber"),
        ("#cardExpiry", c["exp"], "cardExpiry"),
        ("#cardCvc", c["cvc"], "cardCvc"),
        ("#billingName", c["name"], "billingName"),
        ("#billingPostalCode", c["zip"], "billingPostalCode"),
    ]:
        try:
            type_into(sel, val)
            results.append(f"{key} OK")
        except Exception as e:
            results.append(f"{key} ERR {str(e)[:50]}")
    return results


def _click_card_radio(session):
    """选中 Stripe Checkout 的 Card 支付方式（accordion radio）。"""
    page = session.page
    sels = [
        "label[for='payment-method-accordion-item-title-card']",
        "#payment-method-accordion-item-title-card",
        "[data-testid='card-accordion-item-button']",
        "text=Card",
    ]
    last = ""
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                last = f"{sel}: 0 match"
                continue
            loc.click(timeout=4000, force=True)
            return f"clicked {sel!r}"
        except Exception as e:
            last = f"{sel}: {str(e)[:60]}"
    return f"fail ({last})"


def _dump_frames(session):
    """遍历所有 frame，列出卡输入相关的 input 字段（判断 Stripe 是否用 iframe）。"""
    print("\n===== FRAMES / INPUTS =====", flush=True)
    for fr in session.page.frames:
        try:
            inputs = fr.evaluate(FRAME_INPUTS_JS)
        except Exception as e:
            print(f"[frame] {fr.url[:80]}  evaluate-err: {str(e)[:60]}", flush=True)
            continue
        if not inputs:
            continue
        print(f"[frame] {fr.url[:100]}", flush=True)
        for i in inputs:
            print(f"    input name={i['name']!r} id={i['id']!r} type={i['type']!r} "
                  f"ph={i['placeholder']!r} ac={i['autocomplete']!r} aria={i['aria']!r} vis={i['visible']}", flush=True)
    print("===========================\n", flush=True)


def _pick_currency_usd(session):
    """在 Stripe Checkout 币种区点选美金块（坐标真实点击，兼容 React）。"""
    page = session.page
    try:
        target = page.evaluate(FIND_USD_JS)
    except Exception as e:
        return f"evaluate 失败: {str(e)[:100]}"
    if not target:
        return "未找到美金币种块"
    try:
        page.mouse.click(float(target["x"]), float(target["y"]))
        return f"已点击美金币种: {target['text']!r} @({int(target['x'])},{int(target['y'])})"
    except Exception as e:
        return f"点击失败: {str(e)[:100]} (target={target.get('text')!r})"


def _click_by_text(session, text):
    page = session.page
    tl = text.lower()
    # 优先精确/包含匹配可点击元素
    for sel in ["button", "[role=button]", "a", "input[type=submit]"]:
        loc = page.locator(sel)
        n = loc.count()
        for i in range(min(n, 60)):
            el = loc.nth(i)
            try:
                t = (el.inner_text(timeout=500) or "").strip().lower()
            except Exception:
                t = ""
            if tl in t and t:
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=5000)
                    return f"clicked <{sel}> text={t!r}"
                except Exception as e:
                    return f"found but click failed: {str(e)[:100]}"
    return "no matching element"


def _keep_alive(session):
    print("\n🔓 浏览器保活中，每 5s 截一张实时图，Ctrl+C 结束。", flush=True)
    os.makedirs(SHOT_DIR, exist_ok=True)
    n = 0
    try:
        while True:
            time.sleep(5)
            try:
                url = session.current_url
            except Exception:
                break
            n += 1
            path = os.path.join(SHOT_DIR, f"zen_live_{n % 6}.png")
            try:
                session.capture_frame()
                with open(path, "wb") as f:
                    f.write(session.get_screenshot_as_png())
                print(f"[live {n}] {url}  -> {os.path.basename(path)}", flush=True)
            except Exception as e:
                print(f"[live {n}] shot-err: {str(e)[:80]}", flush=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
