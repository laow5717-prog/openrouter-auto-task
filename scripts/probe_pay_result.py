#!/usr/bin/env python3
"""E2E 探测：跑完整充值流程到点 Pay，然后密集 dump 点 Pay 之后的页面/弹窗结构。

目的：看清 Pay 之后真实出现的是什么（拒付内联报错 / 3DS 弹窗 / 跳回 billing 成功），
以便校准 opencode_billing.detect_payment_result 的判定。

默认用 Stripe 测试卡 4242（opencode 是 live 模式，会被拒、不扣款）观察失败路径。
加 --real 用卡池真实卡（真实扣款！）观察成功路径。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver  # noqa: E402
from src.platforms.opencode import billing as ob  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.models.card_pool import CardPoolModel  # noqa: E402

WID = "wrk_01KY4YQ25J6MV0W0VTE51WAJBX"
EMAIL = "abcie2024@gmail.com"
SHOT_DIR = "data/screenshots"

TEST_CARD = {
    "number": "4242424242424242",
    "expiry_month": "12", "expiry_year": "2034", "cvc": "123",
    "first_name": "Test", "last_name": "User",
    "country": "US", "address": "1 Test Street", "address2": "",
    "city": "New York", "state": "NY", "zip": "10001",
}


def _pick_pool_card(group_id=1, index=0):
    pool = CardPoolModel(Database())
    usable, _ = pool.get_usable_cards_as_list('opencode', group_id)
    return usable[index] if usable else None


def _shot(s, name):
    try:
        s.capture_frame()
        path = os.path.join(SHOT_DIR, name)
        with open(path, "wb") as f:
            f.write(s.get_screenshot_as_png())
        print(f"  SHOT {path}", flush=True)
    except Exception as e:
        print(f"  shot-err {str(e)[:60]}", flush=True)


def _dump_frames(s, tag):
    """dump 当前所有 frame 的 url + 关键文本 + 交互元素，观察 Pay 后结构。"""
    print(f"\n===== DUMP [{tag}] url={s.current_url} =====", flush=True)
    page = s.page
    for fr in page.frames:
        url = (fr.url or "")[:90]
        try:
            body = fr.evaluate("()=>document.body?document.body.innerText:''") or ""
            body = " ".join(body.split())[:400]
        except Exception:
            body = "<no-body>"
        try:
            els = fr.evaluate(
                "()=>[...document.querySelectorAll('button,input,[role=button],iframe')]"
                ".map(e=>({tag:e.tagName,id:e.id||e.name||'',"
                "txt:(e.innerText||e.value||e.placeholder||'').slice(0,30),"
                "testid:e.getAttribute('data-testid')||''}))"
                ".filter(x=>x.id||x.txt||x.testid).slice(0,20)"
            )
        except Exception:
            els = []
        if not (url and (body or els)):
            continue
        print(f"  FRAME {url}", flush=True)
        if body:
            print(f"    BODY: {body}", flush=True)
        for e in els:
            print(f"    EL {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用卡池真实卡（真实扣款！）")
    ap.add_argument("--group", type=int, default=1)
    ap.add_argument("--card-index", type=int, default=0)
    args = ap.parse_args()

    if args.real:
        card = _pick_pool_card(args.group, args.card_index)
        if not card:
            print("卡池无可用卡", flush=True)
            return
        print(f"[card] 真实卡 末4={str(card['number'])[-4:]}（真实扣款）", flush=True)
    else:
        card = TEST_CARD
        print("[card] 测试卡 4242（live 会被拒、不扣款）", flush=True)

    s = create_driver(headless=False, profile_id="manual")
    try:
        wid, detail = ob.ensure_opencode_session(s, None, None, EMAIL)
        print(f"[session] wid={wid} detail={detail}", flush=True)
        if not wid:
            return

        mode, bal_before = ob.start_recharge(s, wid, 20, None)
        print(f"[start] mode={mode} balance_before={bal_before}", flush=True)
        if mode is None:
            _dump_frames(s, "no-entry")
            return

        print("[cur]", ob.pick_currency_usd(s, None), flush=True)
        if not ob.select_card_method(s, None):
            print("select_card 失败", flush=True)
            _dump_frames(s, "select-card-fail")
            return
        ok, fdetail = ob.fill_card_and_address(s, card, None)
        print(f"[fill] ok={ok} detail={fdetail}", flush=True)
        ob.fill_phone_if_present(s, card, None)
        ob.uncheck_save_info(s, None)
        ob.check_ai_agent_consent(s, None)
        print("[form_ready]", ob._form_ready_state(s), flush=True)
        _shot(s, "pay_result_00_before_pay.png")

        pay_ok, pay_detail = ob.click_pay(s, None)
        print(f"[pay] ok={pay_ok} detail={pay_detail}", flush=True)

        # Pay 之后密集观察：每 3s 一次，共 ~60s
        for i in range(1, 21):
            time.sleep(3)
            _dump_frames(s, f"t+{i*3}s")
            _shot(s, f"pay_result_{i:02d}.png")
            # 也跑一遍现有判定逻辑看它此刻会怎么判
            bal = ob._read_balance(s)
            print(f"  [balance-now] {bal} (before={bal_before})", flush=True)
    finally:
        close_driver(s)


if __name__ == "__main__":
    main()
