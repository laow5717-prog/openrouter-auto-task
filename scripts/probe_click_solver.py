"""验证 hCaptcha 点击式求解器对真实 Stripe invisible enterprise 挑战是否有效。
用 **Patchright**（非 vanilla），登录 → /go → Subscribe → 点提交触发 hCaptcha →
调 HCaptchaClickSolver 点击求解。**会真实扣款**（若过码+卡有效）。

关键验证：Stripe 弹出的挑战帧是否 #frame=challenge/.task-grid 结构、Multibot 能否识别、
点击能否解掉让 Stripe 放行。

用法: MULTIBOT_API_KEY=xxx python3 scripts/probe_click_solver.py --email carold030@hotmail.com --group 1
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.hcaptcha_click_solver import HCaptchaClickSolver
from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import (
    start_subscribe_go, select_usd_subscribe, click_subscribe,
    detect_subscribe_result, _captcha_challenge_present)
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent)
from src.models.database import Database
from src.models.card_pool import CardPoolModel


def _dump_challenge_frames(session, tag):
    print(f"\n===== 挑战帧结构 [{tag}] =====", flush=True)
    for i, fr in enumerate(session.page.frames):
        url = (fr.url or "")
        if not ("hcaptcha" in url.lower() or "#frame=" in url):
            continue
        try:
            info = fr.evaluate(r"""() => ({
                url: location.href.slice(0,80),
                hasPrompt: !!document.querySelector('.prompt-text'),
                promptText: (document.querySelector('.prompt-text')?.textContent||'').trim().slice(0,50),
                grid: document.querySelectorAll('.task-grid .image').length,
                canvas: document.querySelectorAll('canvas').length,
                submit: !!document.querySelector('.button-submit'),
                checkbox: !!document.querySelector('#checkbox')
            })""")
        except Exception as e:
            info = {"err": str(e)[:50]}
        print(f"  [{i}] {json.dumps(info, ensure_ascii=False)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    ap.add_argument("--key", default=os.environ.get("MULTIBOT_API_KEY", ""))
    ap.add_argument("--dry", action="store_true", help="只验证求解，不真正点提交扣款前停")
    args = ap.parse_args()
    if not args.key:
        print("缺 MULTIBOT_API_KEY"); sys.exit(1)

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]

    session = create_driver(headless=False, profile_id=args.email)   # Patchright
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            sys.exit(1)
        ok, d = start_subscribe_go(session, lg["wid"])
        print("start_subscribe_go:", ok, d)
        print("currency:", select_usd_subscribe(session))
        print("select_card:", select_card_method(session, None))
        print("fill:", fill_card_and_address(session, card, None))
        fill_phone_if_present(session, card, None)
        uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)
        print("click_subscribe:", click_subscribe(session))

        # 等 hCaptcha 挑战出现
        print("等待 hCaptcha 挑战出现...")
        for _ in range(20):
            if _captcha_challenge_present(session) is not None:
                break
            time.sleep(1.5)
        time.sleep(2)
        _dump_challenge_frames(session, "求解前")

        # 点击式求解
        print("\n>>> 启动点击式求解器")
        t0 = time.time()
        solver = HCaptchaClickSolver(session.page, args.key, attempt=8)
        ok = solver.solve()
        print(f">>> 求解返回: {ok}（耗时 {time.time()-t0:.0f}s）")
        _dump_challenge_frames(session, "求解后")

        # 看订阅结果（求解成功则应放行到扣款/成功）
        print("\n>>> 判定订阅结果...")
        res = detect_subscribe_result(session, lg["wid"], timeout=60)
        print("订阅结果:", json.dumps(res, ensure_ascii=False))
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
