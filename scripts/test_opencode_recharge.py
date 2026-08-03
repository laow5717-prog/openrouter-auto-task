#!/usr/bin/env python3
"""集成验证 opencode 充值编排（browser/opencode_billing）。

默认用 profile=manual（已登录）+ 卡池真实卡跑完整复充流程（含点 Pay，真实扣款
$21.23），验证整条链路能从登录跑到支付成功判定。加 --test-card 改用 Stripe 测试卡
4242（live 模式被拒、不扣款）只验证链路与失败判定，不花钱。

用法:
    python3 scripts/test_opencode_recharge.py                 # 真实卡真实扣款
    python3 scripts/test_opencode_recharge.py --test-card     # 4242 测试卡，不扣款
    python3 scripts/test_opencode_recharge.py --group 1 --card-index 0
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver  # noqa: E402
from src.platforms.opencode import billing as ob  # noqa: E402
from src.models.database import Database  # noqa: E402
from src.models.card_pool import CardPoolModel  # noqa: E402

TEST_CARD = {
    "number": "4242424242424242",
    "expiry_month": "12", "expiry_year": "2034", "cvc": "123",
    "first_name": "Test", "last_name": "User",
    "country": "US", "address": "1 Test Street", "address2": "",
    "city": "New York", "state": "NY", "zip": "10001",
}


def _pick_pool_card(group_id, index):
    pool = CardPoolModel(Database())
    usable, _ = pool.get_usable_cards_as_list(group_id)
    if not usable:
        return None
    return usable[min(index, len(usable) - 1)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="manual")
    p.add_argument("--email", default="abcie2024@gmail.com")
    p.add_argument("--test-card", action="store_true",
                   help="用 Stripe 测试卡 4242（不扣款）而非卡池真实卡")
    p.add_argument("--group", type=int, default=1, help="卡池分组 id")
    p.add_argument("--card-index", type=int, default=0, help="取分组内第几张可用卡")
    args = p.parse_args()

    if args.test_card:
        card = TEST_CARD
    else:
        card = _pick_pool_card(args.group, args.card_index)
        if not card:
            print(f"分组 {args.group} 无可用卡", flush=True)
            return
    print(f"[card] 末4={str(card['number'])[-4:]} "
          f"{'（测试卡4242）' if args.test_card else '（卡池真实卡，真实扣款）'}", flush=True)

    session = create_driver(headless=False, profile_id=args.profile)
    try:
        wid, detail = ob.ensure_opencode_session(session, None, None, args.email)
        print(f"[session] wid={wid} detail={detail}", flush=True)
        if not wid:
            print("未登录，无法继续（请先手动登录该 profile）", flush=True)
            return
        res = ob.recharge_via_stripe(session, card, wid, monitor=None, should_stop=None)
        print("\n===== RECHARGE RESULT =====", flush=True)
        print("ok      :", res["ok"], flush=True)
        print("outcome :", res["outcome"], flush=True)
        print("err     :", res["err"], flush=True)
        print("last4   :", res["last4"], flush=True)
        print("steps   :", flush=True)
        for s in res["steps"]:
            print("   -", s, flush=True)
        # 截图最终状态
        shot = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "screenshots", "recharge_final.png")
        try:
            session.capture_frame()
            with open(shot, "wb") as f:
                f.write(session.get_screenshot_as_png())
            print("shot    :", shot, flush=True)
        except Exception as e:
            print("shot-err:", str(e)[:80], flush=True)
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
